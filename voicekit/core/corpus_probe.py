"""语料探针：量出 rubric 锚点里那些数字（每千字密度、篇数占比）。

为什么单独成脚本：
- 锚点写「约 3 个/千字」这种数字，必须能一键复算；散在会话临时脚本里，
  下一个人改锚点时只能靠猜。
- 只做确定性统计，不调模型、不联网。

用法（repo 根目录）：
    python scripts/voice/corpus_probe.py                      # 默认语料，人读表
    python scripts/voice/corpus_probe.py --json                # 机器读
    python scripts/voice/corpus_probe.py --check               # 自带小样本自检，rc 0/1

口径：与 DESIGN §1 一致 —— `build_profile.load_corpus` 接受的篇目、正文里有逗号
（逗号被导出成空格的稿子量出的标点密度是假的）。2026-11-08 实测 59 篇 / 148,663 汉字
（三审书稿语料，034/037 不参与校准）。卡里写的标点「全语料几次」以本脚本 `totals_punct` 为准。
`judge` 建基线抽样另外要求每篇 ≥800 汉字，两处下限不同是有意的：量整体密度用全量，
建基线只挑长稿。
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from . import build_profile as bp  # noqa: E402
from . import voice_lib as lib  # noqa: E402

#: 与 `build_profile.load_corpus` 同一个下限；比 judge 抽样的 800 更宽。
MIN_CJK = bp.MIN_CJK

TIME = ["后来", "那时候", "小时候", "之前", "当时", "如今", "那天", "那年",
        "第二天", "从前", "之后", "直到", "现在", "那年冬天", "过了"]
AGENCY = ["做到", "完成", "搞定", "成功", "决定", "选择", "坚持", "努力", "得到", "自己来"]
RELATE = ["一起", "陪着", "陪", "关系", "感情", "理解", "在乎", "照顾", "聊天", "同事", "朋友"]
ABSTRACT = ["意义", "价值", "本质", "概念", "逻辑", "结构", "存在", "状态", "认知", "层面", "定义"]
INDUCT = ["所以", "于是", "才明白", "想想", "回过头", "慢慢", "原来", "后来才"]
DEDUCT = ["因此", "由此", "这说明", "也就是说", "既然", "必然"]
COMMENT = ["我觉得", "我想", "其实", "大概", "可能", "也许"]
SELF_REFLECT = ["我发现", "我才", "我自己", "回头看", "回过头", "后来才", "其实我", "想想"]
CONCEDE = ["但也有", "不过", "可是", "反过来", "有人说", "也有人", "当然", "未必"]
SENSORY = ["看见", "看到", "听见", "听到", "闻到", "摸", "味道", "声音", "光线", "颜色", "眼睛", "耳朵"]
POLITE = ["您", "敬请", "本文", "笔者", "如上"]

#: 每千字密度指标：名字 → 词表。`你`/`他`/`她` 按单字计数，与 DESIGN §1 一致。
PER_K_WORDS = {
    "time": TIME,
    "agency": AGENCY,
    "relate": RELATE,
    "abstract": ABSTRACT,
    "induct": INDUCT,
    "deduct": DEDUCT,
    "comment": COMMENT,
    "self_reflect": SELF_REFLECT,
    "concede": CONCEDE,
    "sensory": SENSORY,
    "polite": POLITE,
}
#: 标点/符号：直接数字符出现次数，不按词表。
PER_K_CHARS = {"curly": "“", "corner": "「", "straight": '"', "book": "《",
               "dash": "—", "ellipsis": "…"}

#: 卡里引用的「全语料 N 次」绝对次数。按字符计：破折号 `——` 记 2、省略号 `……` 记 2。
TOTAL_PUNCT = {"顿号": "、", "分号": "；", "感叹号": "！", "问号": "？",
               "破折号": "—", "省略号": "…", "逗号": "，", "句号": "。"}


def load_docs(corpus: Path) -> list[dict]:
    """按 judge 的口径读作者语料，返回 [{name, body, cjk, title_line}]。"""
    accepted = {doc["file"] for doc in bp.load_corpus(corpus)[0]}
    docs: list[dict] = []
    for path in sorted(corpus.glob("*.md")):
        if path.name not in accepted:
            continue
        raw = path.read_text(encoding="utf-8")
        body, _ = lib.clean_platform_noise(raw)
        cjk = lib.cjk_count(body)
        if cjk < MIN_CJK or "，" not in body:
            continue
        first = body.strip().splitlines()[0].strip() if body.strip() else ""
        title = lib.export_title(raw)
        docs.append(
            {
                "name": path.name,
                "body": body,
                "cjk": cjk,
                # 正文首行是否就是导出元数据里的标题（简书导出残留）。
                "title_line": int(
                    bool(title) and re.sub(r"\s+", "", first) == re.sub(r"\s+", "", title)
                ),
            }
        )
    return docs


def _count_words(body: str, words: list[str]) -> int:
    return sum(body.count(word) for word in words)


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 3) if values else 0.0


def _paragraphs(body: str) -> list[str]:
    return [p for p in re.split(r"\n+", body) if lib.cjk_count(p) > 30]


def adjacent_para_overlap(body: str) -> float | None:
    """相邻段用字集合的 Jaccard 中位。越大 = 段落越像在说同一件事。"""
    paras = _paragraphs(body)
    if len(paras) < 3:
        return None
    shares = []
    for left, right in zip(paras, paras[1:], strict=False):
        sa, sb = set(left), set(right)
        shares.append(len(sa & sb) / max(1, len(sa | sb)))
    return statistics.median(shares)


def measure_corpus(docs: list[dict]) -> dict:
    """纯函数：给 [+{name, body}]，出密度中位、篇数占比、合计。"""
    per_doc: list[dict] = []
    for doc in docs:
        body = doc["body"]
        cjk = doc.get("cjk") or lib.cjk_count(body)
        k = 1000.0 / cjk
        row: dict = {"name": doc.get("name", ""), "cjk": cjk}
        for key, words in PER_K_WORDS.items():
            row[key] = _count_words(body, words) * k
        for key, char in PER_K_CHARS.items():
            row[key] = body.count(char) * k
        row["you"] = body.count("你") * k
        row["third"] = (body.count("他") + body.count("她")) * k
        agency, relate = _count_words(body, AGENCY), _count_words(body, RELATE)
        row["agency_ratio"] = agency / (agency + relate) if (agency + relate) else None
        induct, deduct = _count_words(body, INDUCT), _count_words(body, DEDUCT)
        row["induct_share"] = induct / (induct + deduct) if (induct + deduct) else None
        paras = _paragraphs(body)
        row["mixed_me_third"] = int(
            any("我" in p and ("他" in p or "她" in p) for p in paras)
        )
        row["title_line"] = int(doc.get("title_line") or 0)
        heads = [p[:6] for p in paras]
        row["repeat_para_head"] = (len(heads) - len(set(heads))) * k
        row["overlap"] = adjacent_para_overlap(body)
        row["bullet_lines"] = sum(
            1 for line in body.splitlines() if re.match(r"^\s*(?:[-*·]|\d+[.、)]|[①②③④⑤])", line)
        )
        per_doc.append(row)

    n = len(per_doc)
    per_k: dict[str, float] = {}
    for key in ("time", "agency", "relate", "abstract", "induct", "deduct", "comment",
                "self_reflect", "concede", "sensory", "polite", "curly", "corner",
                "straight", "book", "dash", "ellipsis", "you", "third",
                "agency_ratio", "induct_share", "repeat_para_head", "overlap"):
        values = [row[key] for row in per_doc if row[key] is not None]
        per_k[key] = _median(values)

    coverage = {
        key: sum(1 for row in per_doc if row[key] > 0)
        for key in ("curly", "corner", "straight", "book", "time", "agency", "relate",
                    "abstract", "comment", "self_reflect", "concede", "sensory", "polite",
                    "dash", "ellipsis")
    }
    coverage.update(
        {
            "mixed_me_third": sum(row["mixed_me_third"] for row in per_doc),
            "title_line": sum(row["title_line"] for row in per_doc),
            "bullet_lines": sum(1 for row in per_doc if row["bullet_lines"] > 0),
        }
    )
    totals = {key: sum(doc["body"].count(char) for doc in docs) for key, char in PER_K_CHARS.items()}
    totals_punct = {
        name: sum(doc["body"].count(char) for doc in docs) for name, char in TOTAL_PUNCT.items()
    }
    p75: dict[str, float] = {}
    for key in ("time", "agency", "relate", "abstract", "comment", "sensory", "overlap"):
        values = sorted(row[key] for row in per_doc if row[key] is not None)
        p75[key] = round(values[len(values) * 3 // 4], 3) if values else 0.0
    return {
        "docs": n,
        "cjk": sum(row["cjk"] for row in per_doc),
        "per_k": per_k,
        "coverage": coverage,
        "totals": totals,
        "totals_punct": totals_punct,
        "p75": p75,
        "missing": [row["name"] for row in per_doc if row["overlap"] is None],
    }


def _fmt(result: dict) -> str:
    lines = [f"篇数 {result['docs']} · 汉字 {result['cjk']}"]
    lines.append("每千字中位：")
    for key, value in result["per_k"].items():
        lines.append(f"  {key:16s} {value:8.3f}")
    lines.append("篇数占比（至少一处）：")
    for key, count in result["coverage"].items():
        lines.append(f"  {key:16s} {count}/{result['docs']}")
    lines.append(f"合计字符：{result['totals']}")
    lines.append("标点绝对次数（卡里的「全语料 N 次」）：")
    lines.append("  " + " ".join(f"{k} {v}" for k, v in result["totals_punct"].items()))
    return "\n".join(lines)


def _check() -> int:
    """自带小样本自检：不读语料，数字对不上就红。"""
    body = (
        "后来，我“去”看《书》。——她说……你也在。那天下午我坐在窗边等一个人。\n"
        "他笑了笑，说这么些年过去，大家都变了，只有这条街还老样子，没什么长进。\n"
        "那天下午我坐在窗边等一个人，他又笑了笑，说这么些年过去也就这样了罢了。"
    )
    result = measure_corpus([{"name": "fixture", "body": body}])
    failed = []
    if result["docs"] != 1:
        failed.append(f"docs={result['docs']}")
    if result["totals"].get("curly") != 1:
        failed.append(f"curly={result['totals'].get('curly')}")
    if result["totals"].get("book") != 1:
        failed.append(f"book={result['totals'].get('book')}")
    if result["totals"].get("dash") != 2:
        failed.append(f"dash={result['totals'].get('dash')}")
    if result["totals_punct"].get("逗号") != 7:
        failed.append(f"逗号={result['totals_punct'].get('逗号')}")
    if result["totals_punct"].get("句号") != 5:
        failed.append(f"句号={result['totals_punct'].get('句号')}")
    if result["totals_punct"].get("省略号") != 2:
        failed.append(f"省略号={result['totals_punct'].get('省略号')}")
    if result["coverage"].get("mixed_me_third") != 1:
        failed.append("mixed_me_third should be 1")
    if result["coverage"].get("title_line") != 0:
        failed.append("title_line should be 0")
    if result["per_k"].get("time", 0) <= 0:
        failed.append("time density should be > 0")
    if failed:
        print("[FAIL] corpus_probe self-check: " + "; ".join(failed))
        return 1
    print("corpus_probe ok: fixture counts match")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="作者语料密度探针（不调模型）")
    parser.add_argument("--corpus", type=Path, default=bp.DEFAULT_CORPUS)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true", help="用内置小样本自检")
    args = parser.parse_args(argv)

    if args.check:
        return _check()
    if not args.corpus.is_dir():
        print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
        return 2
    docs = load_docs(args.corpus)
    if not docs:
        print("[FAIL] no docs passed the filter", file=sys.stderr)
        return 2
    result = measure_corpus(docs)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(_fmt(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
