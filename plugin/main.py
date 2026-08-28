"""aizuchi —— 只发表情包的 AstrBot 插件。

它不说话,也不提供任何聊天内命令 —— 那是唯一会把任意文本送进特权路径的口子。
配置只走磁盘上的 config.yaml。
"""

import json
import os
import random
import sys
import time
from pathlib import Path

import yaml

# AstrBot 以模块方式加载插件,插件目录未必在 sys.path 上 ——
# 不加这一句,同目录的 limiter 可能导入失败,且报错发生在加载期不易定位。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from limiter import FloodLimiter

# 部署后 main.py / config.yaml / stickers 都在同一个插件目录里,
# 所以 ROOT 是**本文件所在目录**,不是它的上一级。
# (曾误写成 parents[1] —— 那是本地仓库布局,部署后会指到 plugins/ 上一层。)
ROOT = Path(__file__).resolve().parent

SYSTEM_PROMPT = """\
你是一个 QQ 群里的表情包机器人。你**只能**发表情包,不能说话。

下面是你手上全部的表情包。你只能从中选一张,或者选择不发。

{pool_note}

可选表情:
{table}

规则:
1. 只输出 JSON,形如 {{"file": "文件名"}} 或 {{"file": null}}。不要输出任何其他内容。
2. 没有合适的就输出 {{"file": null}}。**宁可不发也不要硬凑** —— 贴歪一张比不贴糟得多。
{mode_note}"""

# 被 @ 时:有人点名叫你,必须给回应
MODE_MENTION = """
3. 现在**有人 @ 了你**,是在直接叫你。除非实在没有合适的,否则应当回应。
"""

# 主动模式:没人叫你,你在决定要不要插话 —— 这才是需要判断力的地方
MODE_PROACTIVE = """
3. **现在没有人叫你。** 你是在决定「要不要像个群友一样接一句」。
   你是这个群里的一员,不是需要被批准才能说话的客人。
   闲聊时接话是**正常的**,不必等到「非说不可」才开口。

   下面这些都值得贴一张:
   · 有人说了好笑/惊人/倒霉的事 —— 给个反应;
   · 气氛正热,一张图能接上;
   · 有人在等回应而没人接;
   · 话题轻松,你的一张图能让它更好玩。

   **该闭嘴的是这几种**,遇到就输出 {"file": null}:
   · 正在讨论正事、认真争论、或有人在解决具体问题;
   · 有人在讲真正难过的事,而你手上没有合适的共情图;
   · 刚刚已经贴过,再贴就是刷屏;
   · 单纯想刷存在感,自己也说不出为什么该贴。
"""


@register("aizuchi", "", "只发表情包的群机器人", "0.1.0")
class BqBot(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        # 配置里写的是相对路径,必须锚到 ROOT ——
        # 直接 Path() 会按 AstrBot 的工作目录解析,不是插件目录。
        table = yaml.safe_load(
            (ROOT / self.cfg["stickers"]["table"]).read_text(encoding="utf-8")
        )
        self.pool_note = table.get("pool_note", "")
        self.stickers = table["stickers"]
        self.by_file = {s["file"]: s for s in self.stickers}
        self.sticker_dir = ROOT / self.cfg["stickers"]["dir"]

        self._proactive_inflight = False   # 见 on_group 主动路径的竞态说明
        self.limiter = FloodLimiter(
            self.cfg["proactive"], ROOT / self.cfg["state_file"], time.time
        )
        # ⚠️ 加载期校验。此前这些只在运行期才炸:
        #   · 表里某行缺 mood/desc/use_when ⇒ _render_table 每条消息都 KeyError
        #   · 表里有行但文件不在 ⇒ 发送时才失败,而配额已经先扣了
        #   · 池子为空 ⇒ 被 @ 的随机兜底抛 IndexError
        # stickers.yaml 里写着「改完必跑双向一致核对」——那是个手工仪式,
        # 而它其实是加载时四行代码的事。手工仪式迟早会漏,加载期校验不会。
        need = ("mood", "desc", "use_when")
        bad = [s.get("file", "?") for s in self.stickers
               if not all(k in s for k in need)]
        if bad:
            raise ValueError(f"[aizuchi] 描述表这些行缺字段{need}: {bad[:5]}")
        missing = [n for n in self.by_file if not (self.sticker_dir / n).exists()]
        if missing:
            raise FileNotFoundError(f"[aizuchi] 表里有、文件不在: {missing[:5]}")
        if not self.by_file:
            raise ValueError("[aizuchi] 表情池为空 —— 检查 stickers.yaml")
        if self.limiter._corrupt:
            # 静默归零过一次,至少要留痕:否则「配额被清空」与「今天还没发过」
            # 在日志上一模一样。
            logger.error(f"[aizuchi] 状态文件损坏已重置,24h 配额从零重算: {self.limiter._corrupt}")

        self.history = {}          # group_id -> 最近若干条消息(文字)
        self.img_history = {}      # group_id -> 最近若干张图片的 URL
        self.user_cooldown = {}    # (group, user) -> 上次被 @ 回复的时刻
        # 群里大量内容是表情包,只读文字等于闭着眼读空气。
        # 但图片吃 token 远多于文字,所以带几张要有上限。
        self.max_images = int(self.cfg.get("vision", {}).get("max_images", 3))
        self.groups = set(str(g) for g in self.cfg.get("groups") or [])
        self.ignore_ids = set(str(i) for i in self.cfg.get("ignore_sender_ids") or [])

    # ── 表情表渲染进 prompt。放在最前面做稳定前缀,吃缓存命中价 ──────────
    @staticmethod
    def _trim(buf, keep):
        """只留最后 keep 条。**不要写 `del buf[:-keep]`** —— `-0 == 0`,
        keep=0 时那句是 `buf[:0]`(空切片),一条都删不掉,缓冲无界增长;
        负值还会反向从头删。2026-08-28 审计查出,三处 context_window
        与一处 max_images 全中。"""
        if keep > 0:
            del buf[:-keep]
        else:
            buf.clear()

    def _render_table(self):
        rows = []
        for s in self.stickers:
            # also 是多义标签,必须渲染进去 —— 一张图不止一个意思,
            # 漏掉它等于替模型提前砍掉选项。
            also = "、".join(s.get("also") or [])
            rows.append(
                f"- {s['file']}\n"
                f"    主读法:{s['mood']}" + (f" / 也可表示:{also}" if also else "") + "\n"
                f"    画面:{s['desc']}\n"
                f"    适用:{s['use_when']}"
                + (f"\n    忌用:{s['avoid_when']}" if s.get("avoid_when") else "")
            )
        return "\n".join(rows)

    def _collect_images(self, event, gid):
        """把群消息里的图片 URL 攒起来。

        AstrBot 的 Image 组件在不同平台上字段不一(url / file),两个都试。
        取不到就跳过 —— 少一张图不影响判断,报错会中断整条回复路径。
        """
        buf = self.img_history.setdefault(gid, [])
        try:
            for comp in event.message_obj.message:
                if type(comp).__name__ != "Image":
                    continue
                u = getattr(comp, "url", None) or getattr(comp, "file", None)
                if u and str(u).startswith(("http://", "https://")):
                    buf.append(str(u))
        except Exception:
            pass
        # ⚠️ `-0 == 0`,`buf[:-0]` 是 `buf[:0]` = 空切片 ⇒ 删不掉任何东西。
        # 而 max_images: 0 是 README 明写支持的「纯文字模式」,照原样写会让
        # img_history 无界增长。负值同理会反向删。
        if self.max_images > 0:
            del buf[: -self.max_images]
        else:
            buf.clear()
        return list(buf)

    async def _ask(self, event, context_lines, image_urls=None, proactive=False):
        """走 AstrBot 自己配好的 provider —— 不自建 HTTP 通道、不读环境变量。

        这样 API key 只存在于 AstrBot 的配置里一处,插件侧完全不碰凭据。
        """
        provider = self.context.get_using_provider(event.unified_msg_origin)
        if provider is None:
            # 关键诊断点:关掉 provider_settings.enable 之后,
            # 插件还能不能拿到 provider —— 这是个推断,必须让日志替它作证。
            logger.error("[aizuchi] get_using_provider 返回 None —— 拿不到模型,只能兜底")
            return None
        logger.info(f"[aizuchi] provider OK: {type(provider).__name__}, 带图 {len(image_urls or [])} 张")
        sys_prompt = SYSTEM_PROMPT.format(
            pool_note=self.pool_note,
            table=self._render_table(),
            mode_note=MODE_PROACTIVE if proactive else MODE_MENTION,
        )
        try:
            kw = {}
            if image_urls:
                kw["image_urls"] = image_urls[: self.max_images]
            resp = await provider.text_chat(
                prompt="群里最近的对话:\n" + "\n".join(context_lines),
                system_prompt=sys_prompt,
                model=self.cfg["llm"].get("model") or None,
                **kw,
            )
            raw = (getattr(resp, "completion_text", None) or str(resp)).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                pick = json.loads(raw).get("file")
            except json.JSONDecodeError:
                # 模型常在 JSON 前后多说两句,整串解析必碎。
                # 退而求其次:抠出第一个平衡的 {...} 块。
                start = raw.find("{")
                depth = 0
                pick = None
                if start >= 0:
                    for i in range(start, len(raw)):
                        if raw[i] == "{":
                            depth += 1
                        elif raw[i] == "}":
                            depth -= 1
                            if depth == 0:
                                pick = json.loads(raw[start : i + 1]).get("file")
                                break
                if pick is None:
                    # 关键:把模型实际返回了什么记下来。
                    # 之前只记异常类型,于是「返回空」和「返回了非 JSON 文本」
                    # 在日志上一模一样,只能靠猜。
                    logger.error(f"[bqbot] 模型返回无法解析,原文前200字: {raw[:200]!r}")
                    return None
        except Exception as e:
            # 原来这里是静默 return None —— 于是「模型没被调到」和「模型说不发」
            # 在外部看起来一模一样,查不出区别。必须留痕。
            logger.error(f"[aizuchi] 调模型失败: {type(e).__name__}: {str(e)[:200]}")
            return None                       # 模型抽风一律当弃权,绝不硬凑
        # ⚠️ 这一句原本在 try **之外**:模型返回 {"file": ["x"]} 这类非标量时,
        # `pick in dict` 抛 TypeError: unhashable type,直接冲出 handler ——
        # 而上一行刚说「模型抽风一律当弃权」。放进 try 才算数。
        try:
            if pick not in self.by_file:
                logger.info(f"[aizuchi] 模型给的文件名不在池中,当弃权: {pick!r}")
                return None
        except TypeError:
            logger.info(f"[aizuchi] 模型返回了非文件名的东西,当弃权: {type(pick).__name__}")
            return None
        return pick

    def _send(self, event, file):
        # 把自己发过的也记进历史 ——
        # NapCat 配置 reportSelfMessage=false,机器人自己的消息不会回流,
        # 于是它的上下文里永远没有「我刚才贴了什么」。后果:可能连贴意思重复的图,
        # 也感知不到「上一张发出去没人理」。这里手动补进去。
        gid = str(event.get_group_id() or "")
        s = self.by_file.get(file, {})
        buf = self.history.setdefault(gid, [])
        buf.append(f"我(机器人): [贴了一张表情:{s.get('mood', '?')}]")
        self._trim(buf, self.cfg["proactive"]["context_window"])
        path = str(self.sticker_dir / file)
        return event.chain_result([Comp.Image.fromFileSystem(path)])

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group(self, event: AstrMessageEvent):
        # 注意 gid 的语义随平台变:qqofficial 下是 group_openid(不透明字符串),
        # aiocqhttp 下是 QQ 群号。本插件只把它当字典键用,不解析其内容。
        gid = str(event.get_group_id() or "")
        uid = str(event.get_sender_id() or "")
        # 入口埋点:没有这一条,「消息根本没进插件」与「进了但后面出错」
        # 在日志上无法区分 —— 而前者的排查方向完全不同。
        logger.info(
            f"[aizuchi] on_group 进入 gid={gid} uid={uid} "
            f"at={getattr(event, 'is_at_or_wake_command', '?')} "
            f"len={len(event.message_str or '')}"
        )
        if not self.groups:
            # fail-closed:漏配群号时一个群都不响应。
            # 早前这里写的是 `if self.groups and ...`,空列表等于「所有群」——
            # 于是刚 clone 下来还没填配置的人,会在他所有群里开始冒泡。
            # 默认值出错的代价是不对称的:少说话只是没用,多说话是在别人群里刷屏。
            logger.warning("[aizuchi] 未配置 groups —— fail-closed,不处理任何群")
            return
        if gid not in self.groups:
            logger.info(f"[aizuchi] 群 {gid} 不在白名单,跳过")
            return

        # 攒上下文(无论回不回都攒)
        buf = self.history.setdefault(gid, [])
        if event.message_str:
            # ⚠️ 上限。条数由 context_window 管,但**单条长度不设限** ⇒
            # 一条超长粘贴会永久驻留在窗口里,之后每一次模型调用都带着它。
            # 昵称同样是陌生人可控的,且没有分隔与转义 —— 截断至少压住体量。
            name = (event.get_sender_name() or "")[:32]
            body = (event.message_str or "")[:500]
            buf.append(f"{name}: {body}")
            self._trim(buf, self.cfg["proactive"]["context_window"])
        # 图片单独攒:群里大量内容是表情包,纯文字读不出气氛
        imgs = self._collect_images(event, gid)
        if not event.message_str and imgs:
            buf.append(f"{event.get_sender_name()}: [发了一张图]")
            self._trim(buf, self.cfg["proactive"]["context_window"])

        # 回声循环防线。OneBot 没有通用的「对方是 bot」标志,
        # 所以只有自己可以机器判定,其余靠 config 里的 ignore_sender_ids 名单。
        is_self = uid == str(event.get_self_id() or "")
        is_bot = is_self or uid in self.ignore_ids
        if is_bot:
            return

        # 喂给自适应节奏:它要知道这段时间里有多少人在说话。
        # ⚠️ 不接这一句,自适应会看到「零参与者」而永远不调节 —— 静默失效。
        # 放在 bot 过滤之后:机器人自己的消息不该计入「群里有多少人在聊」。
        self.limiter.observe(uid)

        # ── 被 @:总是回,但按人冷却,防有人拿它当玩具连点 ──
        if event.is_at_or_wake_command:
            now = time.time()
            last = self.user_cooldown.get((gid, uid), 0)
            cd = self.cfg["mention"]["per_user_cooldown_sec"]
            if now - last < cd:
                # 这条以前是静默 return —— 于是「被冷却拦下」在日志上只表现为
                # 「后面那行没出现」,只能靠缺席反推。缺席不是证据,补上留痕。
                logger.info(
                    f"[aizuchi] 按人冷却拦下: uid={uid} 距上次 {now - last:.1f}s < {cd}s"
                )
                return
            # ⚠️ 不要在 yield 之前调 stop_event() ——
            # 实撞(2026-08-28):那样做**掐掉的是自己的输出**(日志里
            # "Prepare to send" 内容为空),而**拦不住** AstrBot 内置的 LLM 聊天
            # (文字照样发出去了)。纯负作用。
            # 内置聊天是靠 provider_settings.enable=false 关掉的,不是靠 stop_event。
            # ⚠️ 总量闸。2026-08-28 两个独立审计者各自查出:此前这条路径
            # **完全不过 limiter** —— 只有上面那个进程内的按人冷却(重启即失,
            # 且按 uid 分键,十个人轮流 @ 就是每秒一张)。而当日实测被 @ 发送 17 次、
            # 主动 6 次 ⇒ cap_per_24h 那道「防止把小号打进风控」的闸,
            # 当时管的是 26% 的流量。
            # 这里刻意不套 min_interval、不掷骰:有人直接叫你就该回应。
            if not self.limiter.allow_mention():
                logger.info("[aizuchi] 被 @ 但总量闸拦下(24h 配额用尽或全局下限内)")
                return
            self.user_cooldown[(gid, uid)] = now
            pick = await self._ask(event, buf, imgs)
            if pick is None:
                # 池子为空时 random.choice 会抛 IndexError,直接冲出 handler。
                if not self.by_file:
                    logger.error("[aizuchi] 表情池为空,无法兜底 —— 检查 stickers.yaml")
                    return
                pick = random.choice(list(self.by_file))   # 被 @ 了必须有回应
                logger.warning(f"[aizuchi] 模型未给出可用选择,随机兜底: {pick}")
            logger.info(f"[aizuchi] 被 @ 触发,发送: {pick}")
            self.limiter.commit()          # 被 @ 的发送同样计入 24h 总量
            yield self._send(event, pick)
            return

        # ── 主动:硬闸 → 骰子 → 才问模型。闸不过则零 API 成本 ──
        # ⚠️ gate() 与 commit() 之间隔着一次 await(模型调用,实测 2–50 秒)。
        # AstrBot 并发处理事件,所以那段时间里进来的下一条消息,gate() 读到的
        # sent[-1] 仍是上上次的时刻 —— 它看不见在途的那笔,于是照样放行,
        # 60 秒硬冷却被整个绕过(2026-08-28 线上实撞:两次主动发送相隔 33 秒)。
        # 记账提前到 gate 之后会破坏「弃权不消耗冷却」,所以改用在途标志占位。
        # asyncio 单线程:检查与置位之间没有 await,这一对是原子的。
        if self._proactive_inflight:
            logger.info("[aizuchi] 已有一次主动判断在途,跳过(防 gate/commit 竞态)")
            return
        if not self.limiter.gate(is_bot_source=is_bot, is_self=is_self):
            return
        self._proactive_inflight = True
        try:
            pick = await self._ask(event, buf, imgs, proactive=True)
            if pick is None:
                logger.info("[aizuchi] 主动模式:模型判断此刻不宜插话,弃权")
                return                        # 弃权不刷新冷却,下一条还能再试
            logger.info(f"[aizuchi] 主动模式触发,发送: {pick}")
            # ⚠️ commit() 必须在标志释放**之前**。原来它在 finally 之后,
            # 今天安全只因为中间那几行恰好没有 await —— 将来任何人在此加一个
            # await(异步日志、异步存在性检查)就会原样重开 probe_race 那个洞。
            self.limiter.commit()
        finally:
            self._proactive_inflight = False
        yield self._send(event, pick)
