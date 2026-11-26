"""按篇列骨架，生成写稿前要查的策略卡。

为什么要这一步：风格属性只管「怎么写一句」，不管「先写什么、后写什么」。
作者自己的作法是先讲一件具体的事、再抽一层道理、再回到事上（见 DD-VOICE-002 §14）。

2026-11-08 作者拍板的两条口径：
- 格调不进分桶（按格调分，句长只差 2 字、结尾收法差别也不到能定规则）。
- **不按类型推公式**：实测按类型猜开头角色 42/58（72%），一律猜「讲事」73%；
  结尾 39/58（67%），一律猜「讲理」59% —— 分组赢不过默认值。所以卡里直接列每篇自己的骨架。

数据来源（都可复算）：
- 类型 / 格调 / 标题：`dev/fixes/FIX-371-style-triage/catalog.md`（作者判的标签）
- 段角色与骨架：`paragraph_table.chapter_summary()`，口径与 `--by-chapter` 完全一致

用法（repo 根目录）：
    python scripts/voice/build_strategy_card.py            # 写 voices/panchen-voice/references/strategy-card.md
    python scripts/voice/build_strategy_card.py --check    # 只比，不写；漂移 rc=1
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402
from . import paragraph_table as pt  # noqa: E402

CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
OUT = REPO_ROOT / "voices/panchen-voice/references/strategy-card.md"

#: 「全书最常见骨架」最多列几条：再多就不是卡，是表。
TOP_N = 5
#: 少于 3 篇的类型不参与「预测力」实测：2 篇的众数没有意义。
MIN_COMPARE = 3

TYPE_ORDER = ("随笔议论", "叙事记事", "观察吐槽", "书影音评", "清单问答")
_ROLE_SHORT = {"讲事": "事", "讲理": "理", "喊话": "喊"}


def parse_catalog(path: Path) -> dict[str, dict]:
    """catalog.md → {篇号3位: {"type": ..., "mood": ..., "title": ...}}。"""
    out: dict[str, dict] = {}
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)（\d+\s*篇）", line)
        if heading:
            current = heading.group(1).strip()
            continue
        row = re.match(r"^\|\s*(\d{3})\*?\s*\|", line)
        if not row or not current:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            continue
        out[row.group(1)] = {"type": current, "mood": cells[4], "title": cells[1]}
    return out


def chapter_rows() -> list[list[str]]:
    """段角色表，和校准语料同一个口径：034/037 调参时看过，不进卡片统计。"""
    paths = _paths()
    return pt.chapter_summary(pt.build(paths))


def _paths() -> list[Path]:
    return [p for p in sorted(CORPUS.glob("*.md"))
            if not p.name.startswith(bp.EXCLUDED_DOC_PREFIXES)]


def para_medians() -> dict[str, int]:
    """每篇正文段落的字数中位（判断该用长段还是短段推进）。"""
    per: dict[str, list[int]] = {}
    for row in pt.build(_paths()):
        per.setdefault(row[0], []).append(int(row[2]))
    return {doc: sorted(v)[len(v) // 2] for doc, v in per.items()}


def _mode(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]


def _top(values: list[str], n: int = TOP_N) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


#: 骨架最多列几段：013 是编号问答体，97 段，全列出来一整块没人读。
MAX_SHOWN = 8


def short_skeleton(skeleton: str) -> str:
    """讲事4→讲理3 → 事4→理3；太长的折中间（013 那种分条问答）。"""
    for role, short in _ROLE_SHORT.items():
        skeleton = skeleton.replace(role, short)
    parts = skeleton.split("→")
    if len(parts) <= MAX_SHOWN:
        return skeleton
    return "→".join(parts[: MAX_SHOWN - 1] + ["…"] + parts[-1:])


def build() -> str:
    tags = parse_catalog(CATALOG)
    rows = chapter_rows()
    med = para_medians()

    opens = [r[2] for r in rows]
    closes = [r[3] for r in rows]
    moral = sorted(int(r[5].rstrip("%")) for r in rows)
    lens = sorted(int(r[1]) for r in rows)
    global_sk = [re.sub(r"\d+", "", r[4]) for r in rows]
    global_top = [(sk, n) for sk, n in _top(global_sk) if n >= 2]

    # 类型分组的预测力：和「全书一律猜众数」比，比不过就照实写，不拿它当公式。
    buckets: dict[str, list[list[str]]] = {}
    for row in rows:
        tag = tags.get(row[0][:3])
        if tag:
            buckets.setdefault(tag["type"], []).append(row)
    hit_open = hit_close = compared = 0
    for group in buckets.values():
        if len(group) < MIN_COMPARE:
            continue
        open_mode = _mode([r[2] for r in group])
        close_mode = _mode([r[3] for r in group])
        hit_open += sum(1 for r in group if r[2] == open_mode)
        hit_close += sum(1 for r in group if r[3] == close_mode)
        compared += len(group)
    base_open = opens.count(_mode(opens)) / len(rows)
    base_close = closes.count(_mode(closes)) / len(rows)

    lines = [
        "# 写作策略卡（按篇 → 骨架）",
        "",
        "> 只回答一件事：**先写什么、后写什么、怎么收**。句子怎么写得看 `style-card.md`。",
        f"> 由 `scripts/voice/build_strategy_card.py` 从 {len(rows)} 篇段角色表生成，可复算（`--check`）。",
        "> 段角色只有三种：讲事（写具体的事）/ 讲理（下判断、说理）/ 喊话（对读者说话）。",
        "> **不按类型推公式**（2026-11-08 作者拍板）。实测按类型猜开头角色 "
        f"{hit_open}/{compared}（{hit_open / compared:.0%}），全书一律猜「{_mode(opens)}」是 "
        f"{base_open:.0%}；猜结尾 {hit_close}/{compared}（{hit_close / compared:.0%}），"
        f"一律猜「{_mode(closes)}」是 {base_close:.0%}。分组赢不过默认值，所以下面列的是"
        "**每篇自己的骨架**：写一篇时，找同类型、段数相近的一篇照着走。",
        "> 类型标签是作者判的 61 篇（2026-11-08 两轮：只看三段时改 25 篇，整篇复核又改回 21 篇；"
        "最终与机器标一致 56/61）。格调也是作者判的 59 篇，只当参考，不进分桶。",
        "",
        "## 全书事实（不分类型）",
        "",
        f"- 开头角色：讲事 {opens.count('讲事')}、讲理 {opens.count('讲理')}、"
        f"喊话 {opens.count('喊话')}（共 {len(rows)} 篇）",
        f"- 结尾角色：讲理 {closes.count('讲理')}、讲事 {closes.count('讲事')}、"
        f"喊话 {closes.count('喊话')}",
        f"- 段数：中位 {lens[len(lens) // 2]}（{lens[0]}–{lens[-1]}）；"
        f"讲理占比：中位 {moral[len(moral) // 2]}%",
        "",
        "最常见的骨架（不分类型）：",
    ]
    lines += [f"- `{sk}`（{n} 篇）" for sk, n in global_top]
    lines.append("")

    order = [t for t in TYPE_ORDER if t in buckets]
    order += [t for t in buckets if t not in TYPE_ORDER]
    for typ in order:
        group = sorted(buckets[typ], key=lambda r: r[0])
        lines += [f"## {typ}（{len(group)} 篇）", ""]
        for row in group:
            doc_id = row[0][:3]
            tag = tags.get(doc_id, {})
            lines.append(
                f"- {doc_id} {tag.get('title', '')}｜{tag.get('mood', '')}｜{row[1]} 段"
                f"·段均 {med.get(row[0], 0)} 字｜理 {row[5]}｜{short_skeleton(row[4])}"
            )
        lines.append("")
    lines.append("一行读法：`编号 标题｜格调｜12 段·段均 228 字｜理 23%｜事4→理3→事2→理1→事1`")
    lines.append("= 12 段、每段约 228 字、讲理段占 23%、骨架按「讲事 4 段 → 讲理 3 段 → …」走；")
    lines.append("段数超过 8 段的骨架折中间（`…`），013 那种分条问答体不用照它的节奏。")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="strategy card by piece")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="只比，不写；漂移 rc=1")
    ap.add_argument("--selftest", action="store_true", help="自检标签解析与可复算")
    args = ap.parse_args(argv)
    if args.selftest:
        tags = parse_catalog(CATALOG)
        rows = chapter_rows()
        problems = []
        if len(tags) < 50:
            problems.append(f"catalog parsed {len(tags)} rows")
        if len(rows) != len(list(CORPUS.glob("*.md"))) - len(bp.EXCLUDED_DOC_PREFIXES):
            problems.append(f"chapter rows {len(rows)} 与语料篇数不符")
        if any(not t["type"] or not t["mood"] or not t["title"] for t in tags.values()):
            problems.append("missing type/mood/title")
        if any(not tags.get(r[0][:3]) for r in rows):
            problems.append("篇没标签")
        if build() != build():
            problems.append("not reproducible")
        if problems:
            print(f"[FAIL] selftest: {problems}", file=sys.stderr)
            return 1
        print(f"[OK] selftest: catalog {len(tags)} 篇 / 段角色 {len(rows)} 篇，卡可复算")
        return 0

    text = build()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current == text:
            print(f"[OK] strategy card up to date: {args.out}")
            return 0
        print(f"[FAIL] strategy card drift: {args.out}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"[OK] wrote {args.out} — {len(text.splitlines())} 行")
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
