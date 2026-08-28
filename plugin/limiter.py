"""防刷屏闸 —— 纯逻辑,不依赖 AstrBot,可被回放测试单独驱动。

设计要点(逐条都有对应的回放断言):
  · 闸在**调用模型之前**。冷却期内根本不问模型,不产生 API 费用。
  · 弃权不消耗冷却:模型说「不发」时不要调 commit(),下一条消息还能再试。
  · 配额用**滚动 24h 窗口**而非自然日 —— 自然日在零点归零,群里正活跃时会放出一串连发。
  · 状态落盘。否则崩溃重启循环时防刷屏失效,而那正是最需要它的时刻。

不做的事:不追踪、也不回避最近用过哪几张。重复本身不是问题,冷却已经把频率压住了。
"""

import json
import os
import random
from pathlib import Path

WINDOW_SEC = 24 * 3600


class FloodLimiter:
    def __init__(self, cfg, state_path, clock, rng=None):
        self.min_interval = int(cfg["min_interval_sec"])
        self.cap_24h = int(cfg["cap_per_24h"])
        self.rate = float(cfg["rate"])
        # 被 @ 路径的全局下限。与 min_interval 分开是有意的:直接请求该有回应,
        # 但十个人轮流 @ 不能变成每秒一张。缺省 3 秒。
        self.mention_floor = float(cfg.get("mention_floor_sec", 3))

        # ── 自适应节奏(默认关闭) ──────────────────────────────
        # 依据:群聊 agent 研究里的动态限流思路 —— 按「自己的发言占比」
        # 相对「公平份额 1/(人数+1)」来调整积极性:说得比公平份额少就多说点,
        # 多了就转为倾听。静态闸不会因为群里今天特别热闹或特别冷清而变。
        #
        # ⚠️ **默认 false**。理由不是它不好,是**本项目至今零主动发送样本**,
        #    在没观察过静态行为之前开自适应,等于对着想象调参。
        #    等 known-issues 里那条「主动模式从未触发」有了数据再决定。
        ad = cfg.get("adaptive") or {}
        self.adaptive = bool(ad.get("enable", False))
        self.ad_window = int(ad.get("window_sec", 600))     # 观察窗口
        self.ad_boost = float(ad.get("boost", 2.0))         # 低于公平份额时的倍率
        self.ad_suppress = float(ad.get("suppress", 0.3))   # 高于公平份额时的倍率
        self.recent = []                                    # [(时刻, 发送者), ...] 仅内存
        self.clock = clock                      # () -> epoch seconds
        self.rng = rng if rng is not None else random.Random()
        self.state_path = Path(state_path)
        self.sent = []                          # 发送时刻,升序
        self._corrupt = None                    # 状态文件坏了的话,记下原因给调用方报
        self._load()

    # ── 状态持久化 ────────────────────────────────────────────────
    def _load(self):
        """读回落盘状态。**任何解析失败都不得让插件加载失败**,也不得静默归零。

        原来只有 json.loads 在 try 里,而 `[float(t) for t in d.get("sent", [])]`
        在 try 之外、且 except 不含 TypeError/AttributeError。2026-08-28 审计
        用 8 种载荷实测:`{"sent":"abc"}` 抛 ValueError、`[1,2,3]` 抛 AttributeError、
        `{"sent":5}` 抛 TypeError —— 三种都直接打死插件加载,而回溯指向一句
        float 转换,看不出是状态文件坏了。

        另一半更隐蔽:文件被写到一半(掉电、容器被杀)时 json 解析失败,
        原来是静默 `return`,于是 sent=[] —— **24h 配额与冷却一起归零,且日志无痕**。
        而本文件开头写着落盘的理由正是「崩溃重启循环时防刷屏失效,
        那正是最需要它的时刻」。崩溃重启循环恰恰就是把文件写坏的场景。
        """
        raw = None
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except OSError:
            return                              # 无状态文件 = 首次启动,静默是对的
        try:
            d = json.loads(raw)
            self.sent = sorted(float(t) for t in d["sent"])
        except Exception as e:                  # noqa: BLE001 —— 坏文件的形态无法穷举
            # 保守起见当作「已发满」,而不是「没发过」:
            # 拿不准时宁可少说话,也不要在状态未知时放开闸门。
            self.sent = []
            self._corrupt = f"{type(e).__name__}: {str(e)[:80]}"

    def _save(self):
        tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
        # ⚠️ 临时名带 pid:两个进程共用同一个 .tmp 会写出拼接文件,
        # 而 os.replace 保证的是**改名**原子,不是**内容**原子。
        tmp.write_text(json.dumps({"sent": self.sent}), encoding="utf-8")
        os.replace(tmp, self.state_path)        # 原子替换,防写到一半崩溃留下半个文件

    def _prune(self, now):
        cutoff = now - WINDOW_SEC
        self.sent = [t for t in self.sent if t > cutoff]

    # ── 自适应 ────────────────────────────────────────────────────
    def observe(self, sender_id):
        """记一条群消息(谁发的)。自适应关闭时这是个廉价的空转,可无条件调用。"""
        if not self.adaptive:
            return
        now = self.clock()
        self.recent.append((now, str(sender_id)))
        cutoff = now - self.ad_window
        self.recent = [r for r in self.recent if r[0] > cutoff]

    def _effective_rate(self):
        """按「自己的发言占比 vs 公平份额」调整积极性。"""
        if not self.adaptive:
            return self.rate
        now = self.clock()
        cutoff = now - self.ad_window
        humans = {s for t, s in self.recent if t > cutoff}
        n_msgs = sum(1 for t, _ in self.recent if t > cutoff)
        n_mine = sum(1 for t in self.sent if t > cutoff)
        if not humans or (n_msgs + n_mine) == 0:
            return self.rate                       # 没人说话,不主动加码
        fair = 1.0 / (len(humans) + 1)             # 把自己也算作一个参与者
        mine = n_mine / (n_msgs + n_mine)
        factor = self.ad_boost if mine < fair else self.ad_suppress
        return max(0.0, min(1.0, self.rate * factor))

    # ── 闸 ────────────────────────────────────────────────────────
    def gate(self, is_bot_source=False, is_self=False):
        """硬闸 + 骰子。True = 可以去问模型。本方法自身零 API 成本。"""
        if is_bot_source or is_self:            # 防两个 bot 互相触发的回声循环
            return False
        now = self.clock()
        self._prune(now)
        if self.sent and now - self.sent[-1] < self.min_interval:
            return False
        # cap_per_24h <= 0 表示不设上限。
        # 保留这个出口是因为配额的角色已经变了:它不再管节奏(那是模型的判断),
        # 只在模型失灵时兜底。想彻底交给模型的人应该能明确关掉它,
        # 而不是被迫写一个「大到看不出是上限」的数。
        if self.cap_24h > 0 and len(self.sent) >= self.cap_24h:
            return False
        # 硬闸(冷却/配额)永远先过,自适应只影响骰子 ——
        # 保证「自适应算错了也突破不了物理上限」。
        return self.rng.random() < self._effective_rate()

    def allow_mention(self):
        """被 @ 路径的闸。**总量上限与主动路径共用**。

        与 gate() 的区别:不掷骰、不套 min_interval —— 有人直接叫你就该回应。
        但总量必须共管,否则 cap_per_24h 只覆盖少数路径。

        ⚠️ 2026-08-28 两个独立审计者各自查出:此前被 @ 路径**完全不过闸**,
        只有一个进程内的按人冷却(重启即失)。而当日实测被 @ 发送 17 次、
        主动 6 次 —— 配置里写着 cap_per_24h 防的是「失控刷屏把小号打进风控,
        那是不可逆的」,它当时管的是 26% 的流量。
        """
        now = self.clock()
        self._prune(now)
        if self.cap_24h > 0 and len(self.sent) >= self.cap_24h:
            return False
        if self.sent and now - self.sent[-1] < self.mention_floor:
            return False
        return True

    def commit(self):
        """模型确实决定发送之后才调用。弃权时**不要**调。"""
        now = self.clock()
        self._prune(now)
        self.sent.append(now)
        self._save()
