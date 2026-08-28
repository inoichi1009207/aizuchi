"""竞态探针:证明「硬冷却」在并发下会被绕过,以及在途标志确实堵上了它。

背景(2026-08-28 线上实撞):主动路径的形状是
    gate() → await 模型调用(实测 2–50 秒) → commit()
`gate()` 读的是已落账的 sent[-1],**看不见在途的那一笔**。于是模型思考期间
进来的下一条消息照样过闸,两条都发出去 —— 观测到两次主动发送相隔 33 秒,
而 min_interval_sec=60。

回放测试 test_replay.py 逮不到这个:它是同步的,一次只跑一条,
gate 与 commit 之间永远没有别人插进来。**这不是测试写错了,是它的世界里没有并发。**

本探针跑两遍同一场景:
  · 无守卫 → 应当发出 2 次(复现 bug;若只发 1 次,说明本探针没复现出竞态,FAIL)
  · 有守卫 → 应当发出 1 次(修复生效)
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin"))
from limiter import FloodLimiter  # noqa: E402

CFG = {"rate": 1.0, "min_interval_sec": 60, "cap_per_24h": 150}
MODEL_LATENCY = 0.05          # 代表真实的 2–50 秒模型调用


async def scenario(state_file, guarded):
    """两条消息在模型调用期间先后进入主动路径,返回实际发送次数。"""
    now = 1767225600.0
    lim = FloodLimiter(CFG, state_file, lambda: now)
    inflight = {"v": False}
    sends = []

    async def handle(tag):
        if guarded and inflight["v"]:
            return                              # 守卫:已有一次判断在途
        if not lim.gate():
            return
        if guarded:
            inflight["v"] = True
        try:
            await asyncio.sleep(MODEL_LATENCY)  # ← 真实世界里的 await 模型调用
        finally:
            if guarded:
                inflight["v"] = False
        lim.commit()
        sends.append(tag)

    # 第二条比第一条晚一点点进来,但远早于模型返回 —— 线上就是这个形状
    await asyncio.gather(handle("A"), _delayed(handle, "B", MODEL_LATENCY / 4))
    return sends


async def _delayed(fn, tag, d):
    await asyncio.sleep(d)
    return await fn(tag)


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="aizuchi-race-"))
    fails = []
    try:
        bare = await scenario(tmp / "bare.json", guarded=False)
        print(f"  无守卫: 发送 {len(bare)} 次 {bare}   (应为 2 = 成功复现竞态)")
        if len(bare) != 2:
            fails.append(f"无守卫场景只发了 {len(bare)} 次 —— 竞态没复现出来,本探针无效")

        ok = await scenario(tmp / "guarded.json", guarded=True)
        print(f"  有守卫: 发送 {len(ok)} 次 {ok}   (应为 1 = 守卫生效)")
        if len(ok) != 1:
            fails.append(f"有守卫场景发了 {len(ok)} 次 —— 守卫没堵住")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED: " + "; ".join(fails))
        return 1
    print("ALL PASS —— 竞态可复现,且在途标志确实堵住了它")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
