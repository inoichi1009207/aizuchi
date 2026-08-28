"""让视觉模型自己看图,生成 stickers.yaml 描述表。

为什么要有这个脚本:运行时挑图的是哪个模型,描述就该由哪个模型写 ——
换一个模型来描述,等于让 A 写说明书给 B 用,中间多一层翻译损耗。

用法:
    export GLM_API_KEY=...            # 或 DEEPSEEK_API_KEY
    python tools/describe_stickers.py --dir stickers --out stickers/stickers.generated.yaml

**默认写到 .generated.yaml,不覆盖手写的 stickers.yaml** —— 两份并存才能对比。
满意了再自己改名换上去。
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROVIDERS = {
    "glm": {
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-5.3-flash",
        "key_env": "GLM_API_KEY",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-v4-flash-vision-exp",
        "key_env": "DEEPSEEK_API_KEY",
    },
}

PROMPT = """\
这是一个 QQ 群机器人要用的表情包。请你仔细看这张图,然后回答三件事。

你将来要靠自己写的这段描述来决定「什么时候该发这张图」—— 所以写给未来的自己看,
不要写给人看。客观、具体、能区分于其他表情。

只输出 JSON,不要任何其他内容:
{
  "mood": "一到三个字的情绪标签",
  "desc": "客观描述画面:表情、动作、服装、背景。写你看见的,不要写你的解读。",
  "use_when": "什么样的群聊语境下贴这张最合适。可以写多个场景,用顿号分隔。",
  "avoid_when": "什么场合绝对不该用它。没有就写空字符串。"
}"""


def encode(path: Path) -> str:
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif", "webp": "webp"}
    ext = path.suffix.lower().lstrip(".")
    return f"data:image/{mime.get(ext, 'jpeg')};base64," + base64.b64encode(
        path.read_bytes()
    ).decode("ascii")


def ask(cfg: dict, key: str, path: Path) -> dict:
    payload = {
        "model": cfg["model"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": encode(path)}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
        # ⚠️ 这是推理模型:它的思考过程放在 reasoning_content 里,
        # 而那部分**同样吃 max_tokens 预算**。设小了,思考吃光额度,
        # 正文要么为空、要么截断在 JSON 中间。
        # (实撞:max_tokens=500 时 14 张只成 3 张,而通道完全正常。)
        "max_tokens": 3000,
        "temperature": 0.3,
    }
    req = urllib.request.Request(
        cfg["url"],
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    # 不要吞掉 HTTP 层的真实情况:响应体为空时,json.loads 会报
    # 「Expecting value: line 1 column 1」—— 那个错看起来像模型输出格式问题,
    # 实际是传输层。分开报,免得又去修错的那一层。
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            status = r.status
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError(
            f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
        ) from None
    if not body.strip():
        raise RuntimeError(f"HTTP {status} 但响应体为空(长度 0)")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise RuntimeError(f"HTTP {status} 响应非 JSON,前 200 字符: {body[:200]!r}") from None
    if "choices" not in data:
        raise RuntimeError(f"响应无 choices 字段,内容: {str(data)[:200]}")
    raw = data["choices"][0]["message"]["content"].strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # 模型常常在 JSON 前后多说两句。整串解析必碎,
        # 所以退而求其次:从文本里抠出第一个平衡的 {...} 块。
        # (实撞:14 张里 8 张因此失败,而通道本身是好的。)
        start = raw.find("{")
        if start < 0:
            raise
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(raw[start : i + 1])
        raise


def yaml_escape(s: str) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="stickers", help="表情包目录")
    ap.add_argument("--out", default="stickers/stickers.generated.yaml")
    ap.add_argument("--provider", choices=list(PROVIDERS), default="glm")
    args = ap.parse_args()

    cfg = PROVIDERS[args.provider]
    key = os.environ.get(cfg["key_env"])
    if not key:
        print(f"缺环境变量 {cfg['key_env']}", file=sys.stderr)
        return 2

    files = sorted(
        p for p in Path(args.dir).iterdir()
        if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}
    )
    if not files:
        print(f"{args.dir} 下没有图片", file=sys.stderr)
        return 2

    print(f"用 {cfg['model']} 看 {len(files)} 张图…\n")
    rows, failed = [], []
    for i, p in enumerate(files, 1):
        try:
            r = ask(cfg, key, p)
            rows.append((p.name, r))
            print(f"  [{i}/{len(files)}] {p.name} → {r.get('mood', '?')}")
        except Exception as e:                      # 单张失败不中断整批
            failed.append((p.name, str(e)[:80]))
            print(f"  [{i}/{len(files)}] {p.name} → 失败: {str(e)[:60]}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"# 由 {cfg['model']} 看图自动生成。人工校对后再启用。\n")
        f.write("# pool_note 需要人工写 —— 它讲的是「这个池子缺什么」,\n")
        f.write("#   而单张看图的模型看不到全局。\n\n")
        f.write("pool_note: |\n  (待人工填写:本池缺哪类情绪?什么场合应当弃权?)\n\n")
        f.write("stickers:\n")
        for name, r in rows:
            f.write(f'  - file: {name}\n')
            f.write(f'    mood: "{yaml_escape(r.get("mood", ""))}"\n')
            f.write(f'    desc: "{yaml_escape(r.get("desc", ""))}"\n')
            f.write(f'    use_when: "{yaml_escape(r.get("use_when", ""))}"\n')
            if r.get("avoid_when"):
                f.write(f'    avoid_when: "{yaml_escape(r["avoid_when"])}"\n')
            f.write("\n")

    print(f"\n已写出 {out}({len(rows)} 条)")
    if failed:
        print(f"失败 {len(failed)} 张:")
        for n, e in failed:
            print(f"  {n}: {e}")
    print("\n⚠️ 这份是草稿,没覆盖手写的 stickers.yaml。两份并存,自己比对后再决定用哪份。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
