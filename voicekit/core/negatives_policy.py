"""负样本文本的发布闸门（FIX-345）。

开发期负样本正文留在 `scripts/voice/fixtures/negatives/`；开源/公开发布前必须删除。
这个脚本只有两个动作：
- `--release-check`：还有他人正文就失败（发布流水线/发布前一键跑）。
- 默认：打印现状与提醒（开发期 rc 0）。

删除动作不自动做：删完还要同时处理 `profile.json` 的误放基线、负样本测试与 `--calibrate`，
按 `docs/todo/voice-negatives-release-strip.md` 的步骤走。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent / "fixtures/negatives"
PATTERN = "other-author-zh*.md"


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="负样本文本发布策略检查")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--release-check", action="store_true", help="公开发布前用：有他人正文即失败")
    args = ap.parse_args(argv)
    if not args.dir.is_dir():
        print(f"[FAIL] 负样本目录不存在：{args.dir}", file=sys.stderr)
        return 2
    texts = sorted(args.dir.glob(PATTERN))
    if args.release_check:
        if texts:
            print(
                f"[FAIL] 发布前必须删除 {len(texts)} 份他人文章正文（{args.dir}）。"
                "步骤见 docs/todo/voice-negatives-release-strip.md",
                file=sys.stderr,
            )
            return 1
        print("[OK] 没有他人文章正文，可以公开发布")
        return 0
    sources = args.dir / "SOURCES.md"
    print(f"负样本正文 {len(texts)} 份；来源表 {'在' if sources.is_file() else '缺失'}：{args.dir}")
    if texts:
        print("提醒：开源/公开发布前跑 `python scripts/voice/negatives_policy.py --release-check`，"
              "并按 docs/todo/voice-negatives-release-strip.md 删除正文。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
