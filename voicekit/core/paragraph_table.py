"""段级表：每段一行，只数结构，不判好坏。

只出 9 列：篇 / 段号 / 汉字 / 句数 / 平均句长 / 逗号串 / 引号 / 极短句 / 段角色。
术语：
- 逗号串 = 段内按全角逗号切出的分句数（逗号数 + 1）。
- 极短句 = 不足 8 个汉字的句子。

**不外判**：本表不参与放行（放行只看 `judge.py` 的 10 例同人判定），也不给
「密度合格 / 不合格」。段中位 199 汉字，短段上算密度是噪声（DD-VOICE-002 §4）。
切段与段角色只有一份实现，直接复用 `voice_review`，不复制规则。

用法（repo 根目录）：
    python scripts/voice/paragraph_table.py --dir 书稿目录 --out tmp/paragraphs.tsv
    python scripts/voice/paragraph_table.py --file 稿.md
    python scripts/voice/paragraph_table.py --dir 书稿目录 --limit 3   # 先看 3 篇
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402
from . import voice_review as vr  # noqa: E402

#: 极短句门槛（汉字数）：低于它就是"碎片句"，与 style_lint 的短句口径无关，只用来数。
VERY_SHORT_CHARS = 8

COLUMNS = ("篇", "段号", "汉字", "句数", "平均句长", "逗号串", "引号", "极短句", "角色")


def _rows(doc_id: str, paragraphs: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for index, para in enumerate(paragraphs, 1):
        sentences = vr._sentences(para)
        cjk = len(vr._CJK_RE.findall(para))
        n_sent = max(1, len(sentences))
        rows.append(
            [
                doc_id,
                str(index),
                str(cjk),
                str(len(sentences)),
                f"{cjk / n_sent:.1f}",
                str(para.count("，") + 1),
                str(para.count("“")),
                str(sum(1 for s in sentences if len(vr._CJK_RE.findall(s)) < VERY_SHORT_CHARS)),
                vr._role(para),
            ]
        )
    return rows


def build(paths: list[Path]) -> list[list[str]]:
    rows: list[list[str]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        rows.extend(_rows(path.stem, vr._paragraphs(text)))
    return rows


def render(rows: list[list[str]]) -> str:
    lines = ["\t".join(COLUMNS)]
    lines.extend("\t".join(row) for row in rows)
    return "\n".join(lines) + "\n"


SUMMARY_COLUMNS = ("篇", "段数", "开头", "结尾", "骨架", "讲理占比", "最长段", "最短段")


def _skeleton(roles: list[str]) -> str:
    """把连续同角色的段并成一段：讲事3→讲理2→讲事4。"""
    runs: list[list] = []
    for role in roles:
        if runs and runs[-1][0] == role:
            runs[-1][1] += 1
        else:
            runs.append([role, 1])
    return "→".join(f"{role}{n}" for role, n in runs)


def chapter_summary(rows: list[list[str]]) -> list[list[str]]:
    """每篇一行：开头/结尾角色、骨架、讲理占比、段长极值。写稿前先看同类骨架。"""
    by_doc: dict[str, list[list[str]]] = {}
    for row in rows:
        by_doc.setdefault(row[0], []).append(row)
    out: list[list[str]] = []
    for doc, group in by_doc.items():
        group.sort(key=lambda r: int(r[1]))
        roles = [r[-1] for r in group]
        chars = [int(r[2]) for r in group]
        moral = roles.count("讲理") / len(roles)
        out.append(
            [
                doc,
                str(len(roles)),
                roles[0],
                roles[-1],
                _skeleton(roles),
                f"{moral:.0%}",
                str(max(chars)),
                str(min(chars)),
            ]
        )
    return out


def render_summary(rows: list[list[str]]) -> str:
    lines = ["\t".join(SUMMARY_COLUMNS)]
    lines.extend("\t".join(row) for row in rows)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="paragraph table (structure only)")
    ap.add_argument("--dir", type=Path, help="整本书稿目录：每篇都出")
    ap.add_argument("--file", type=Path, help="单篇")
    ap.add_argument("--out", type=Path, default=None, help="写到文件（默认 stdout）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 篇，先试水")
    ap.add_argument("--selftest", action="store_true",
                    help="自检：每篇一行、骨架非空、两次结果一致")
    ap.add_argument("--by-chapter", action="store_true",
                    help="改成按篇一行：开头/结尾角色、骨架、讲理占比（写稿前查骨架用）")
    args = ap.parse_args(argv)

    if args.dir:
        if not args.dir.is_dir():
            print(f"[FAIL] dir not found: {args.dir}", file=sys.stderr)
            return 2
        paths = sorted(args.dir.glob("*.md"))
    elif args.file:
        if not args.file.is_file():
            print(f"[FAIL] file not found: {args.file}", file=sys.stderr)
            return 2
        paths = [args.file]
    elif args.selftest:
        paths = sorted(bp.DEFAULT_CORPUS.glob("*.md"))
    else:
        ap.error("need --dir or --file")
    if args.limit > 0:
        paths = paths[: args.limit]
    if not paths:
        print(f"[FAIL] no *.md under {args.dir}", file=sys.stderr)
        return 2

    rows = build(paths)
    if args.selftest:
        first = chapter_summary(rows)
        second = chapter_summary(build(paths))
        problems = []
        if len(first) != len({row[0] for row in rows}):
            problems.append("one row per chapter")
        if any(len(row) != len(SUMMARY_COLUMNS) for row in first):
            problems.append("column count")
        if any(not row[4].strip() for row in first):
            problems.append("empty skeleton")
        if render_summary(first) != render_summary(second):
            problems.append("not reproducible")
        if problems:
            print(f"[FAIL] selftest: {problems}", file=sys.stderr)
            return 1
        print(f"[OK] selftest: {len(first)} 篇骨架，可复算")
        return 0
    if args.by_chapter:
        summary = chapter_summary(rows)
        text = render_summary(summary)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8", newline="\n")
            print(f"[OK] wrote {args.out} — {len(summary)} 篇骨架", file=sys.stdout)
        else:
            sys.stdout.write(text)
        print(f"-- {len(summary)} 篇骨架；只报结构，不参与放行", file=sys.stderr)
        return 0
    text = render(rows)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"[OK] wrote {args.out} — {len(paths)} 篇, {len(rows)} 段", file=sys.stdout)
    else:
        sys.stdout.write(text)
    roles: dict[str, int] = {}
    for row in rows:
        roles[row[-1]] = roles.get(row[-1], 0) + 1
    print(
        f"-- {len(paths)} 篇 / {len(rows)} 段；段角色 {roles}；本表只报结构，不参与放行",
        file=sys.stderr,
    )
    return 0


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把 ≥、中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
