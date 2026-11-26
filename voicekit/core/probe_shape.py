"""探针：他心里有没有「没想明白的事」，跟句子长短、段碎、留门有没有关系？

**结论（2026-11-09，59 篇可用语料，排除 034/037）：「有疑 → 留门」不成立。**
- 句长中位 35.5（松 proxy 判有困惑）vs 31.5，置换检验 p=0.0695，方向跟「有疑→句子更短」相反
  → 句长、段碎不是「他在解决一个问题」的标记，别拿它们当靶子。
- 留门 17/32（53%）vs 4/27（15%），Fisher 单尾 p=0.0022 —— 但这只是**松 proxy** 的结论：
  换成只认「不知道/不明白/想不通」的干净 proxy，**8/24 vs 13/35，p=0.4927（关联消失）**；
  只留「随笔议论」控类型后，松 proxy p=0.0323、干净 proxy p=0.6865。
- 留门那一侧比较稳：把 door 里的语气词（吧呢嘛啊）去掉、只留缓和词，p=0.0199 仍在。
→ 所以真正相关的是「**疑问句多的篇**更常留门」（设问、转述别人的问句都算），
  不是「他自己没想明白」。**别在这条上建门**；关键词 proxy 松紧一换结论就翻，
  这类联动只能当提示（2026-11-09 复查得出）。

用法：py -3.10 scripts/voice/probe_shape.py
"""

from __future__ import annotations

import importlib.util
import math
import pathlib
import random
import re
import statistics
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
from . import rewrite as rw  # noqa: E402

CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
SKIP = {"034", "037"}
#: 干净 proxy：只认「他自己不确定」，不认设问/反问/转述别人问句。
DOUBT = re.compile(r"(?:不知道|不明白|想不通|没想明白|拿不准|说不好|搞不懂|也不确定)")
#: 严 door：去掉语气词（吧呢嘛啊哈），只留缓和/让步词。
SOFT = re.compile(
    r"(?:大概|也许|或许|似乎|好像|算是|怕是|说不定|未必|倒也|还算|也说不准"
    r"|说不准|说不清|谁知道|看来|也可能)"
)


def body_of(path: pathlib.Path) -> str:
    lines = [x.strip() for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]
    return "\n".join(lines[1:]) if lines else ""


def types_of() -> dict[str, str]:
    """catalog.md 的 H2 = 类型，表格首列 = 篇号。"""
    out: dict[str, str] = {}
    kind = ""
    for line in CATALOG.read_text(encoding="utf-8").splitlines():
        head = re.match(r"^## (\S+?)（", line)
        if head:
            kind = head.group(1)
        row = re.match(r"^\| (\d{3}) \|", line)
        if row and kind:
            out[row.group(1)] = kind
    return out


def load_rows() -> list[dict]:
    types = types_of()
    rows = []
    for path in sorted(CORPUS.glob("*.md")):
        doc = path.name.split("__")[0]
        if doc in SKIP:
            continue
        text = body_of(path)
        chars = len("".join(text.split()))
        if chars < 200:
            continue
        sents = [s for s in rw._SENT.findall(text) if s.strip()]
        paras = rw.split_paragraphs(text)
        tail = "".join(sents[-2:])
        rows.append({
            "篇": doc,
            "类型": types.get(doc, "?"),
            "有问题(松)": rw.presence(text)["hits"] > 0,
            "有问题(严)": any("我" in s and DOUBT.search(s) for s in sents),
            "留门(松)": rw.door(text),
            "留门(严)": bool(SOFT.search(tail)),
            "句长中位": statistics.median([len("".join(s.split())) for s in sents]) if sents else 0,
            "段长中位": statistics.median([len("".join(p.split())) for p in paras]) if paras else 0,
            "每千字段数": len(paras) / chars * 1000,
            "说死每千字": len(rw._ASSERT.findall(text)) / chars * 1000,
        })
    return rows


def fisher_one_sided(a: int, b: int, c: int, d: int) -> float:
    n, k, x = a + b + c + d, a + b, a + c
    return sum(
        math.comb(k, i) * math.comb(n - k, x - i) / math.comb(n, x)
        for i in range(a, min(k, x) + 1)
    )


def perm_test(a: list[float], b: list[float], rounds: int = 20000) -> float:
    obs = abs(statistics.median(a) - statistics.median(b))
    pool, hit = a + b, 0
    random.seed(0)
    for _ in range(rounds):
        random.shuffle(pool)
        diff = abs(statistics.median(pool[: len(a)]) - statistics.median(pool[len(a):]))
        if diff >= obs:
            hit += 1
    return hit / rounds


def assoc(rows: list[dict], q_key: str, door_key: str) -> None:
    a = sum(1 for r in rows if r[q_key] and r[door_key])
    b = sum(1 for r in rows if r[q_key] and not r[door_key])
    c = sum(1 for r in rows if not r[q_key] and r[door_key])
    d = sum(1 for r in rows if not r[q_key] and not r[door_key])
    print(f"  {q_key} × {door_key}：留门 {a}/{a + b} vs {c}/{c + d}"
          f"  Fisher p={fisher_one_sided(a, b, c, d):.4f}")


def main() -> int:
    rows = load_rows()
    q_loose = [r for r in rows if r["有问题(松)"]]
    q_tight = [r for r in rows if r["有问题(严)"]]
    print(f"共 {len(rows)} 篇：松 proxy 判有疑 {len(q_loose)}，严 proxy {len(q_tight)}")
    for key in ("句长中位", "段长中位", "每千字段数", "说死每千字"):
        a = [r[key] for r in q_loose]
        b = [r[key] for r in rows if not r["有问题(松)"]]
        print(f"  {key}: {statistics.median(a):.2f} vs {statistics.median(b):.2f}")
    print("  句长中位差置换检验 p="
          f"{perm_test([r['句长中位'] for r in q_loose], [r['句长中位'] for r in rows if not r['有问题(松)']]):.4f}")
    print("\n== 敏感性检查（这就是为什么不能建门）==")
    assoc(rows, "有问题(松)", "留门(松)")
    assoc(rows, "有问题(严)", "留门(松)")
    assoc(rows, "有问题(松)", "留门(严)")
    assoc(rows, "有问题(严)", "留门(严)")
    essay = [r for r in rows if r["类型"] == "随笔议论"]
    print(f"\n== 控类型（只留随笔议论 {len(essay)} 篇）==")
    assoc(essay, "有问题(松)", "留门(松)")
    assoc(essay, "有问题(严)", "留门(松)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
