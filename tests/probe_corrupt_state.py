"""损坏状态探针:验「state.json 坏了既不打死插件、也不静默归零」。

背景(2026-08-28 审计实测):原来 `_load` 只有 `json.loads` 在 try 里,
而 `[float(t) for t in d.get("sent", [])]` 在 try 之外,且 except 只含
(OSError, ValueError)。于是:

  {"sent":"abc"} → ValueError      {"sent":[null]} → TypeError
  [1,2,3]        → AttributeError  {"sent":5}      → TypeError

四种都直接让插件加载失败,而回溯指向一句 float 转换,看不出是状态文件坏了。
另一半更隐蔽:文件被写到一半时静默 `return`,sent=[] —— 24h 配额与冷却一起归零,
**日志无痕**。而落盘的理由正是「崩溃重启循环时防刷屏失效,那正是最需要它的时刻」,
崩溃重启循环恰恰就是把文件写坏的场景。

本探针要求:每种载荷都能构造成功(不抛),且坏文件必须留下 `_corrupt` 痕迹。
"""

import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugin"))
from limiter import FloodLimiter  # noqa: E402

CFG = {"rate": 1.0, "min_interval_sec": 60, "cap_per_24h": 150}

CASES = [
    ("合法",        '{"sent":[1.0]}',  False),   # 第三项 = 是否应当留下 corrupt 痕迹
    ("截断 JSON",   '{"sent":[1.0',    True),
    ("sent 是字符串", '{"sent":"abc"}',  True),
    ("含 null",     '{"sent":[null]}', True),
    ("顶层是数组",   '[1,2,3]',         True),
    ("顶层是标量",   '"hello"',         True),
    ("sent 是数字",  '{"sent": 5}',     True),
    ("空文件",      '',                True),
]


def main():
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aizuchi-corrupt-"))
    fails = []
    try:
        for i, (name, payload, want_corrupt) in enumerate(CASES):
            p = tmp / f"s{i}.json"
            p.write_text(payload, encoding="utf-8")
            try:
                lim = FloodLimiter(CFG, p, lambda: 1000.0)
            except Exception as e:                       # noqa: BLE001
                print(f"  FAIL  {name}: 构造抛 {type(e).__name__} —— 坏文件不该打死插件")
                fails.append(name)
                continue
            got = lim._corrupt is not None
            ok = got == want_corrupt
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: sent={lim.sent} "
                  f"corrupt={'有' if got else '无'}(应{'有' if want_corrupt else '无'})")
            if not ok:
                fails.append(name)

        # 首次启动(文件不存在)应当静默,不算损坏
        lim = FloodLimiter(CFG, tmp / "never.json", lambda: 1000.0)
        ok = lim.sent == [] and lim._corrupt is None
        print(f"  {'PASS' if ok else 'FAIL'}  文件不存在: 静默当首次启动,不报损坏")
        if not ok:
            fails.append("文件不存在")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print(f"FAILED: {len(fails)} 条 —— {', '.join(fails)}")
        return 1
    print("ALL PASS —— 坏文件不打死插件,且一律留痕")
    return 0


if __name__ == "__main__":
    sys.exit(main())
