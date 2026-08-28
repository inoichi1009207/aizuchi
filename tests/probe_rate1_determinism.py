"""探针:验证「rate=1.0 时测的是硬闸本身,不是随机数」这句话。

判法:同一条消息流,只换随机数种子。
  · rate=1.0  → 发送时刻应当**逐一相同**(骰子恒通过 ⇒ 随机数不参与决策)
  · rate=0.5  → 发送时刻应当**因种子而异**(否则说明随机数根本没接进去,测试是假的)
第二条同样重要:只验前者的话,一个「骰子被接错、永远返回 True」的 bug 也会让它通过。
该盲区由 probe_mutation.py 实证。
"""

import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from limiter import FloodLimiter  # noqa: E402

BASE = {"min_interval_sec": 420, "cap_per_24h": 15}


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


def stream(state_file, rate, seed, n=3 * 24 * 120, step=30, start=1767225600):
    clock = Clock(start)
    lim = FloodLimiter(dict(BASE, rate=rate), state_file, clock, rng=random.Random(seed))
    out = []
    for i in range(n):
        clock.t = start + i * step
        if lim.gate():
            lim.commit()
            out.append(clock.t)
    return out


def main():
    tmp = Path(tempfile.mkdtemp(prefix="aizuchi-probe-"))
    fails = []
    try:
        a, b = stream(tmp / "a.json", 1.0, 1), stream(tmp / "b.json", 1.0, 999999)
        ok1 = a == b
        print(f"  {'PASS' if ok1 else 'FAIL'}  rate=1.0 换种子结果不变 "
              f"(各 {len(a)}/{len(b)} 次, 时刻逐一相同={ok1})")
        if not ok1:
            fails.append("rate=1.0 应与种子无关")

        c, d = stream(tmp / "c.json", 0.5, 1), stream(tmp / "d.json", 0.5, 999999)
        ok2 = c != d
        print(f"  {'PASS' if ok2 else 'FAIL'}  rate=0.5 换种子结果改变 "
              f"(各 {len(c)}/{len(d)} 次) —— 证明骰子确实接在决策路径上")
        print("    注:次数可能相同,因绑定约束是 cap_per_24h 不是骰子;比的是时刻序列,不是次数。")
        if not ok2:
            fails.append("rate=0.5 应随种子变化(否则骰子没接上)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
