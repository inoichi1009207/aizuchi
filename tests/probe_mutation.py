"""变异探针:证明「只验第一条会被假通过骗过」这句话本身。

做法:注入一个「骰子接错、永远返回 True」的变异体(rng.random() 恒返回 -1,
于是 -1 < rate 恒成立,rate 与种子都不再影响决策),再拿两条断言分别去打它。

预期(这才是本探针要证的):
  · 断言一(rate=1.0 换种子结果不变)  → 变异体**仍然通过** ⇒ 它单独无法发现这个 bug
  · 断言二(rate=0.5 换种子结果改变)  → 变异体**被逮住**   ⇒ 防伪作用在它身上
两条都符合预期,探针退出 0。若断言一意外逮住变异体,说明原判断有误,探针 FAIL。
"""

import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from limiter import FloodLimiter  # noqa: E402

BASE = {"min_interval_sec": 420, "cap_per_24h": 15}


class BrokenRng:
    """变异体:骰子恒通过,种子与 rate 都被旁路。"""

    def __init__(self, seed):
        self.seed = seed

    def random(self):
        return -1.0


class Clock:
    def __init__(self, t):
        self.t = float(t)

    def __call__(self):
        return self.t


def stream(state_file, rate, seed, rng_cls, n=3 * 24 * 120, step=30, start=1767225600):
    clock = Clock(start)
    lim = FloodLimiter(dict(BASE, rate=rate), state_file, clock, rng=rng_cls(seed))
    out = []
    for i in range(n):
        clock.t = start + i * step
        if lim.gate():
            lim.commit()
            out.append(clock.t)
    return out


def assertions(tmp, tag, rng_cls):
    a = stream(tmp / f"{tag}a.json", 1.0, 1, rng_cls)
    b = stream(tmp / f"{tag}b.json", 1.0, 999999, rng_cls)
    c = stream(tmp / f"{tag}c.json", 0.5, 1, rng_cls)
    d = stream(tmp / f"{tag}d.json", 0.5, 999999, rng_cls)
    return (a == b), (c != d)          # 断言一是否通过, 断言二是否通过


def main():
    tmp = Path(tempfile.mkdtemp(prefix="aizuchi-mut-"))
    fails = []
    try:
        ok1, ok2 = assertions(tmp, "real", random.Random)
        print(f"  健康版:  断言一={'通过' if ok1 else '不通过'}  "
              f"断言二={'通过' if ok2 else '不通过'}   (两条都该通过)")
        if not (ok1 and ok2):
            fails.append("健康版本身没全绿")

        m1, m2 = assertions(tmp, "mut", BrokenRng)
        print(f"  变异体:  断言一={'通过' if m1 else '不通过'}  "
              f"断言二={'通过' if m2 else '不通过'}   (一该通过=盲区, 二该不通过=逮住)")
        if not m1:
            fails.append("断言一本应对该 bug 免疫,实际却逮住了 —— 原判断有误")
        if m2:
            fails.append("断言二没能逮住变异体 —— 防伪失效")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    print("ALL PASS —— 断言一单独存在时确有盲区,断言二确实补上了它")
    return 0


if __name__ == "__main__":
    sys.exit(main())
