"""给全书 61 篇出一份紧凑摘要，供人工贴「类型 / 格调」标签。

只读本地：用 voice_review 的切段/切句口径算段落功能、人称、问句、编号等特征，不调模型、不花钱。
输出一行一篇：篇号、标题、字数、讲事/讲理/喊话段数、你/千字、问句数、编号体标志、开头、结尾。

用法（repo 根目录）：
    python scripts/voice/style_catalog.py            # 打印摘要
    python scripts/voice/style_catalog.py --json-out tmp/catalog-digest.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import voice_review as vr  # noqa: E402

DEFAULT_CORPUS = (
    REPO_ROOT / "voices/panchen-voice/corpus"
)
EXCLUDED = ("034", "037")
#: 每行展示多少字：够判断体例就行，长了读不动。
HEAD_CHARS = 24
TAIL_CHARS = 28


def _digest(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    shape = vr.text_shape(text)
    chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    paras = vr._paragraphs(text)
    sentences = vr._sentences(text)
    you = text.count("你")
    numbered = bool(re.search(r"^\s*[一二三四五六七八九十]+[、．.]", text, re.M)) or bool(
        re.search(r"^\s*\d+[、．.]", text, re.M)
    )
    return {
        "no": path.stem[:3],
        "title": path.stem.split("__", 1)[-1],
        "chars": chars,
        "paras": len(paras),
        "roles": shape["counts"],
        "suspects": len(shape["suspects"]),
        "you_per_k": round(you * 1000 / max(chars, 1), 2),
        "questions": sum(1 for s in sentences if s.rstrip().endswith(("？", "?"))),
        "numbered": numbered,
        "head": (paras[0] if paras else "")[:HEAD_CHARS],
        "tail": (paras[-1] if paras else "")[-TAIL_CHARS:],
        "excluded": path.stem[:3] in EXCLUDED,
    }


def main(argv: list[str] | None = None) -> int:
    vr._utf8_stdio()
    ap = argparse.ArgumentParser(description="全书摘要，供人工贴类型/格调标签")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)
    if not args.corpus.is_dir():
        print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
        return 2
    files = sorted(
        (p for p in args.corpus.glob("*.md") if p.stem[:3].isdigit()),
        key=lambda p: p.stem[:3],
    )
    rows = [_digest(p) for p in files]
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    print("| 篇 | 标题 | 字 | 事/理/喊 | 你k | 问 | 编号 | 开头 | 结尾 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for row in rows:
        roles = row["roles"]
        flag = "*" if row["excluded"] else ""
        print(
            f"| {row['no']}{flag} | {row['title'][:14]} | {row['chars']} | "
            f"{roles['讲事']}/{roles['讲理']}/{roles['喊话']} | {row['you_per_k']} | "
            f"{row['questions']} | {'Y' if row['numbered'] else '.'} | "
            f"{row['head']}… | …{row['tail']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
