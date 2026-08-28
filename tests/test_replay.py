"""回放测试 —— 在真上群之前,用模拟消息流把防刷屏闸跑一遍。

不依赖 pytest、不依赖 AstrBot,直接 `python tests/test_replay.py`,退出码 0 = 全绿。

刻意把骰子 rate 设为 1.0:骰子恒通过,于是测的是**硬闸本身**,而不是随机数。
(这句话本身由 probe_rate1_determinism.py 与 probe_mutation.py 两个探针背书。)
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from limiter import FloodLimiter  # noqa: E402

# ⚠️ **不要从 limiter 导入 WINDOW_SEC。** 断言的标尺必须由测试自己拥有。
# 2026-08-28 变异测试实证:此前这里写的是 `from limiter import ... WINDOW_SEC`,
# 于是把 limiter 里的 WINDOW_SEC 改成 24 分钟后,三天发送量从 45 涨到 618,
# 而**四个测试文件全部通过** —— 断言的两边一起动,14 倍的刷屏回归可以全绿上线。
# 断言②说的是「24 小时」,那就由测试自己写死 24 小时。
WINDOW_SEC = 24 * 3600

CFG = {"rate": 1.0, "min_interval_sec": 420, "cap_per_24h": 15}

_failures = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


def run_stream(state_file, start, n_msgs, step, bot_every=0):
    """喂一段消息流,返回发送时刻列表。bot_every>0 时每 N 条掺一条 bot 来源消息。"""
    clock = Clock(start)
    lim = FloodLimiter(CFG, state_file, clock)
    sent = []
    for i in range(n_msgs):
        clock.t = start + i * step
        if not lim.gate(is_bot_source=(bot_every > 0 and i % bot_every == 0)):
            continue
        lim.commit()
        sent.append(clock.t)
    return sent


def main():
    tmp = Path(tempfile.mkdtemp(prefix="aizuchi-replay-"))
    try:
        midnight = (1767225600 // 86400) * 86400   # 对齐 UTC 零点,好让「跨零点」测到真日界
        state = tmp / "state.json"

        times = run_stream(state, midnight, n_msgs=3 * 24 * 120, step=30)  # 3 天,每 30 秒一条
        print(f"\n模拟 3 天 / {3*24*120} 条消息,主动发送 {len(times)} 次\n")

        gaps = [b - a for a, b in zip(times, times[1:])]
        check("① 任意两次发送间隔 >= min_interval_sec",
              all(g >= CFG["min_interval_sec"] for g in gaps),
              f"最小间隔 {min(gaps) if gaps else '-'}s")

        worst_win = max(
            (sum(1 for t in times if s <= t < s + WINDOW_SEC) for s in times), default=0)
        check("② 任意 24h 滚动窗口内发送数 <= cap_per_24h",
              worst_win <= CFG["cap_per_24h"], f"最坏窗口 {worst_win} 条")

        bot_sent = run_stream(tmp / "s3.json", midnight, n_msgs=500, step=30, bot_every=1)
        check("③ bot 来源消息触发发送数 == 0", len(bot_sent) == 0, f"实际 {len(bot_sent)} 次")

        st = tmp / "s4.json"
        a = FloodLimiter(CFG, st, Clock(midnight))
        a.commit()
        still = FloodLimiter(CFG, st, Clock(midnight + CFG["min_interval_sec"] - 1))  # 全新实例=重启
        done = FloodLimiter(CFG, st, Clock(midnight + CFG["min_interval_sec"] + 1))
        check("④ 重启后冷却仍生效(且到点后恢复)", still.gate() is False and done.gate() is True)

        def hour_max(ts):
            return max((sum(1 for t in ts if s <= t < s + 3600) for s in ts), default=0)
        allowed = 3600 // CFG["min_interval_sec"] + 1
        near = [t for t in times
                if any(abs(t - (midnight + d * 86400)) < 3600 for d in range(4))]
        check("⑤ 跨零点不出现突发(任一小时窗内不超过间隔所允许的条数)",
              hour_max(near) <= allowed, f"零点前后最密 {hour_max(near)} 条 / 允许 {allowed}")

        # ── 被 @ 路径的总量闸(2026-08-28 新增) ──────────────────
        # 两个独立审计者各自查出:此前被 @ 路径完全不过闸。当日实测
        # 被 @ 17 次 / 主动 6 次 ⇒ cap_per_24h 管的是 26% 的流量。
        st7 = tmp / "s7.json"
        clk = Clock(midnight)
        lim = FloodLimiter(dict(CFG, mention_floor_sec=3), st7, clk)
        check("⑦ 被 @ 闸:全局下限内拒绝、过了就放行",
              lim.allow_mention() is True
              and (lim.commit() or True)
              and (setattr(clk, "t", midnight + 1) or lim.allow_mention() is False)
              and (setattr(clk, "t", midnight + 4) or lim.allow_mention() is True))

        st8 = tmp / "s8.json"
        clk8 = Clock(midnight)
        lim8 = FloodLimiter(dict(CFG, mention_floor_sec=0), st8, clk8)
        for i in range(CFG["cap_per_24h"]):
            clk8.t = midnight + i * 10
            lim8.commit()
        clk8.t = midnight + CFG["cap_per_24h"] * 10
        check("⑧ 被 @ 闸:24h 配额用尽后拒绝(与主动路径共用总量)",
              lim8.allow_mention() is False,
              f"配额已满 {len(lim8.sent)}/{CFG['cap_per_24h']} 仍放行")

        check("⑥ 状态文件落盘且可解析",
              isinstance(json.loads(state.read_text(encoding="utf-8")).get("sent"), list))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if _failures:
        print(f"FAILED: {len(_failures)} 条 —— {', '.join(_failures)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
