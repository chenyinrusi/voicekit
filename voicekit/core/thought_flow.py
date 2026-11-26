"""量「思绪跳转」：作者说写文是在复刻脑子的岔路，这个脚本把这句话变成数。

四个指标（都可复算，不用模型）：
1. 联想触发词密度（/万字）：想起 / 忽然 / 大概 / 那会儿 / 小时候 …
2. 分句数/句：一句话里塞了几个念头
3. 均句长（汉字）
4. 离题段占比：带联想词、且与相邻段共享 2 字词很少的段（话题岔开后没立刻回来）

用法（repo 根目录）：
    python scripts/voice/thought_flow.py --file 稿.md          # 单篇 vs 作者基线
    python scripts/voice/thought_flow.py --baseline            # 只打作者基线
    python scripts/voice/thought_flow.py --selftest            # 自检

只报结构，不判好坏；放行仍只看 `judge.py` 的 10 例同人判定。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from statistics import mean

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402
from . import voice_lib as lib  # noqa: E402
from . import voice_review as vr  # noqa: E402

CORPUS = bp.DEFAULT_CORPUS

#: 联想/回忆触发词：出现越多，说明念头岔开越频繁。
TRIGGERS = (
    "想起", "想到", "忽然", "突然", "好像", "大概", "后来", "那会儿", "那时候",
    "记得", "小时候", "以前", "不知怎的", "顺便", "反倒", "又想起",
)
#: 桥：岔开时必须有一个说得出来的关联（同类感受 / 同一个道理 / 同一个人或物）。
#: 作者原话：「不能现在在冰川攀爬，联想到氧化还原反应。」所以岔开句要带桥词。
BRIDGES = (
    "好像", "就像", "像是", "似的", "一样", "也是", "同样", "之所以",
    "大概是因为", "大概因为", "反倒", "反而是", "如同", "好比", "比起来",
)
#: 离题段判据：与相邻段共享的 2 字词比例低于此值，算"岔开后没拉回来"。
DIGRESSION_SHARE = 0.06
#: 小于这个字数的段不参与离题判断（太短，共享词天然少）。
MIN_PARA_CJK = 40


def _bigrams(text: str) -> set[str]:
    han = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return {han[i : i + 2] for i in range(len(han) - 1)}


def measure(text: str) -> dict:
    raw = lib.measure(text)
    paras = [p for p in vr._paragraphs(text) if lib.cjk_count(p) >= MIN_PARA_CJK]
    cjk = raw["_chars"]
    shares: list[float] = []
    for a, b in zip(paras, paras[1:], strict=False):
        ga, gb = _bigrams(a), _bigrams(b)
        if ga and gb:
            shares.append(len(ga & gb) / len(ga | gb))
    sentences = vr._sentences(text)
    trigger_sents = [s for s in sentences if any(w in s for w in TRIGGERS)]
    bridged = [s for s in trigger_sents if any(b in s for b in BRIDGES)]
    digressive = 0
    for i, para in enumerate(paras):
        if not any(w in para for w in TRIGGERS):
            continue
        near = []
        for j in (i - 1, i + 1):
            if 0 <= j < len(paras):
                ga, gb = _bigrams(para), _bigrams(paras[j])
                if ga and gb:
                    near.append(len(ga & gb) / len(ga | gb))
        if near and max(near) < DIGRESSION_SHARE:
            digressive += 1
    return {
        "cjk": cjk,
        "triggers_per_10k": round(sum(text.count(w) for w in TRIGGERS) / cjk * 10000, 1) if cjk else 0.0,
        "clauses_per_sentence": round(raw["L2-01"]["clauses_per_sentence"], 2),
        "mean_sentence": round(raw["L2-01"]["mean"], 1),
        "adjacent_share": round(mean(shares), 3) if shares else 0.0,
        "digressive_share": round(digressive / len(paras), 2) if paras else 0.0,
        "bridge_share": round(len(bridged) / len(trigger_sents), 2) if trigger_sents else 0.0,
    }


def corpus_baseline() -> dict[str, dict]:
    """作者语料的 P10/P50/P90，用来判断一篇稿子岔得比作者多还是少。"""
    rows: list[dict] = []
    for path in sorted(CORPUS.glob("*.md")):
        if path.name.startswith(bp.EXCLUDED_DOC_PREFIXES):
            continue
        body, _ = lib.clean_platform_noise(path.read_text(encoding="utf-8"))
        if lib.cjk_count(body) < bp.MIN_CJK or body.count("，") == 0:
            continue
        rows.append(measure(body))
    out: dict[str, dict] = {}
    for key in ("triggers_per_10k", "clauses_per_sentence", "mean_sentence", "adjacent_share",
                "digressive_share", "bridge_share"):
        vals = sorted(r[key] for r in rows)
        out[key] = {
            "p10": round(vals[len(vals) // 10], 2),
            "p50": round(vals[len(vals) // 2], 2),
            "p90": round(vals[int(len(vals) * 0.9)], 2),
        }
    out["_docs"] = {"p10": len(rows), "p50": len(rows), "p90": len(rows)}
    return out


def _row(label: str, value: float, band: dict) -> str:
    if value < band["p10"]:
        mark = "低于作者 P10"
    elif value > band["p90"]:
        mark = "高于作者 P90"
    else:
        mark = "在作者区间内"
    return f"  {label:14s} {value:>8}  作者 P10/P50/P90 = {band['p10']}/{band['p50']}/{band['p90']}  {mark}"


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="thought-flow metrics (structure only)")
    ap.add_argument("--file", type=Path, help="待看的稿")
    ap.add_argument("--baseline", action="store_true", help="只打作者基线")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        base = corpus_baseline()
        problems = []
        if base["_docs"]["p50"] < 40:
            problems.append("baseline too small")
        if any(base[k]["p10"] > base[k]["p90"] for k in base if not k.startswith("_")):
            problems.append("band inverted")
        text = "我出门买面包，排队的时候忽然想起小时候的事，后来也就忘了要买什么。\n\n" * 2
        if measure(text) != measure(text):
            problems.append("not reproducible")
        if problems:
            print(f"[FAIL] selftest: {problems}", file=sys.stderr)
            return 1
        print(f"[OK] selftest: 基线 {base['_docs']['p50']} 篇，可复算")
        return 0

    base = corpus_baseline()
    if args.baseline or not args.file:
        print(f"== 作者基线（{base['_docs']['p50']} 篇）")
        for key, label in (
            ("triggers_per_10k", "联想词/万字"),
            ("clauses_per_sentence", "分句/句"),
            ("mean_sentence", "均句长"),
            ("adjacent_share", "相邻段共享词"),
            ("digressive_share", "离题段占比"),
            ("bridge_share", "岔开句带桥"),
        ):
            print(_row(label, base[key]["p50"], base[key]))
        return 0

    if not args.file.is_file():
        print(f"[FAIL] file not found: {args.file}", file=sys.stderr)
        return 2
    # 基线走的是清洗后的正文；单篇也要走同一条，否则两边量的不是同一段文字。
    body, _ = lib.clean_platform_noise(args.file.read_text(encoding="utf-8"))
    stats = measure(body)
    print(f"== {args.file.name}（{stats['cjk']} 汉字）")
    for key, label in (
        ("triggers_per_10k", "联想词/万字"),
        ("clauses_per_sentence", "分句/句"),
        ("mean_sentence", "均句长"),
        ("adjacent_share", "相邻段共享词"),
        ("digressive_share", "离题段占比"),
        ("bridge_share", "岔开句带桥"),
    ):
        print(_row(label, stats[key], base[key]))
    print("-- 只报结构，不判好坏；放行只看 judge.py")
    return 0


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
