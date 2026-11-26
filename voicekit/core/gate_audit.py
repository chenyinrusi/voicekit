"""作者本人的草稿 → 成稿，过一遍改稿器的门：门会不会拦他自己？

结论（2026-11-09 实测）：**会，8 条红**。连他自己的成稿都过不了自己的门。
所以这套门量的是「距离」，不是「是不是他」。证据与修法：
`docs/design/DD-VOICE-002-voice-unit-layers.md` §18.2 / §18.4。

草稿冻结在 `fixtures/064-draft.md`（他私稿里唯一有「草稿 → 成稿」一对的篇目；
成稿 = 语料 064）。冻结是为了这一条证据能复算——原来只在 `tmp/` 里，会丢。

用法（repo 根）：
    py -3.10 scripts/voice/gate_audit.py
    py -3.10 scripts/voice/gate_audit.py --json
    py -3.10 scripts/voice/gate_audit.py --draft <草稿> --final <成稿>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DRAFT = HERE / "fixtures" / "064-draft.md"
FINAL = (
    REPO_ROOT
    / "voices/panchen-voice/corpus/064__后记2 嗨 我婚了.md"
)


def _rewrite():
    from . import rewrite as mod  # noqa: PLC0415
    return mod


def audit(draft: Path | str | None = None, final: Path | str | None = None) -> dict:
    """返回 {"problems", "warnings", "metrics"}. problems 非空 = 作者本人也过不了门。"""
    rw = _rewrite()
    src = Path(draft or DRAFT).read_text(encoding="utf-8")
    out = Path(final or FINAL).read_text(encoding="utf-8")
    return {
        "problems": rw.guard(src, out),
        "warnings": rw.warnings_of(src, out),
        "metrics": rw.metrics(src, out),
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="作者本人的草稿→成稿，过一遍改稿器的门")
    parser.add_argument("--draft", help=f"草稿（默认 {DRAFT.name}）")
    parser.add_argument("--final", help="成稿（默认语料 064）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    result = audit(args.draft, args.final)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print(f"他本人的改法（草稿 → 成稿）过门结果：{len(result['problems'])} 条红")
    for problem in result["problems"]:
        print(f"  ✗ {problem}")
    if result["warnings"]:
        print()
        for warning in result["warnings"]:
            print(f"  [提示] {warning}")
    print()
    for key, value in result["metrics"].items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
