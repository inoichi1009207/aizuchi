"""算一算:对 aizuchi 这个负载形状,选哪个模型在钱上有没有区别。

不是估,是按两家的公开单价做算术。token 数按实际 prompt 结构估算(标注在下面),
结论对这些估算不敏感 —— 差一倍也不改变量级。

单价来源(每百万 token,美元):
  deepseek-v4-flash  官方定价页(离峰/高峰):
      输入未命中 0.22 / 0.44   输入命中 0.007 / 0.014   输出 0.66 / 1.32
  GLM-5.3-Flash      Z.ai 报价(未逐字核实,发布期另有 5 折):
      输入未命中 0.15          输入命中 0.03            输出 0.50
"""

M = 1_000_000

PRICES = {
    "deepseek-v4-flash (离峰)": dict(in_miss=0.22, in_hit=0.007, out=0.66),
    "deepseek-v4-flash (高峰)": dict(in_miss=0.44, in_hit=0.014, out=1.32),
    "GLM-5.3-Flash":           dict(in_miss=0.15, in_hit=0.03,  out=0.50),
    "GLM-5.3-Flash (5折)":     dict(in_miss=0.075, in_hit=0.015, out=0.25),
}

# 每次调用的 token 构成
PREFIX = 450   # 系统提示:pool_note + 10 行表情描述 —— 稳定前缀,可命中缓存
VARIES = 200   # 最近 20 条群消息 —— 每次都不同,必然未命中
OUT = 10       # 只输出 {"file": "xxx.jpg"}

SCENARIOS = {
    "仅被 @ 触发 (20 次/天)": 20 * 30,
    "含主动模式 (50 次/天)": 50 * 30,
}


def cost(p, calls, cache_works):
    hit = PREFIX * calls if cache_works else 0
    miss = (VARIES * calls) + (0 if cache_works else PREFIX * calls)
    out = OUT * calls
    return (hit * p["in_hit"] + miss * p["in_miss"] + out * p["out"]) / M


def main():
    for scen, calls in SCENARIOS.items():
        print(f"\n=== {scen} —— {calls} 次/月 ===")
        for cache_works in (True, False):
            tag = "缓存命中" if cache_works else "缓存全未命中"
            print(f"  [{tag}]")
            rows = [(name, cost(p, calls, cache_works)) for name, p in PRICES.items()]
            for name, c in rows:
                print(f"    {name:<26} ${c:8.4f} / 月")
            lo = min(rows, key=lambda r: r[1])
            hi = max(rows, key=lambda r: r[1])
            print(f"    → 最便宜 {lo[0]} (${lo[1]:.4f});最贵与最便宜相差 ${hi[1]-lo[1]:.4f}/月")

    # 分项拆解:到底哪一项主导总价?(用来检验「缓存项主导」这个说法)
    print("\n=== 分项拆解(仅 @ 场景 600 次/月,缓存命中) ===")
    calls = SCENARIOS["仅被 @ 触发 (20 次/天)"]
    print(f"  {'':<26} {'缓存前缀':>10} {'变动输入':>10} {'输出':>10} {'合计':>10}")
    for name, p in PRICES.items():
        a = PREFIX * calls * p["in_hit"] / M
        b = VARIES * calls * p["in_miss"] / M
        c = OUT * calls * p["out"] / M
        print(f"  {name:<26} {a:10.4f} {b:10.4f} {c:10.4f} {a+b+c:10.4f}")
    print("  → 看哪一列最大,就知道谁主导。若「缓存前缀」列不是最大,")
    print("    则「成本压在缓存项上」的说法不成立。")

    print("\n=== 判据 ===")
    worst = max(cost(p, max(SCENARIOS.values()), False) for p in PRICES.values())
    print(f"  所有组合里最贵的一种:${worst:.4f}/月")
    print(f"  结论:{'价格不构成选型依据(最贵也 < $1/月)' if worst < 1 else '价格需要纳入考量'}")


if __name__ == "__main__":
    main()
