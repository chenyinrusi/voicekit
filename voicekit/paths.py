"""作者包路径约定：一个作者一个目录，包里有 pack.json / profile.json / corpus/。

查找顺序（`resolve_pack`）：
1. 显式 `--pack`
2. 环境变量 `VOICEKIT_PACK`
3. 当前目录下 `voices/<slug>-voice/`（有 pack.json 的那个）
"""

from __future__ import annotations

import os
from pathlib import Path

PACK_FILE = "pack.json"


def voices_dir() -> Path:
    return Path(os.environ.get("VOICEKIT_VOICES") or Path.cwd() / "voices")


def candidates() -> list[Path]:
    root = voices_dir()
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / PACK_FILE).is_file())


def resolve_pack(explicit: Path | str | None = None) -> Path:
    """返回作者包目录；找不到就抛 SystemExit，并列出找过的地方。"""
    if explicit:
        path = Path(explicit)
        if not (path / PACK_FILE).is_file():
            raise SystemExit(f"not a voice pack (no {PACK_FILE}): {path}")
        return path
    env = os.environ.get("VOICEKIT_PACK")
    if env:
        return resolve_pack(Path(env))
    found = candidates()
    if len(found) == 1:
        return found[0]
    if found:
        names = ", ".join(p.name for p in found)
        raise SystemExit(f"several packs under {voices_dir()}: {names}; pass --pack")
    raise SystemExit(
        f"no voice pack found; pass --pack or put one under {voices_dir()} "
        f"(each pack dir needs {PACK_FILE})"
    )
