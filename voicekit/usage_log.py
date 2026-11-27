"""本机花费台账：每次真调模型追加一行 JSONL。

为什么要有它：装成 wheel 后命令跑在别人的机器上，CAS 的花费账本看不到这些调用。
台账只记用量与钱，不记稿件正文。默认落在**当前项目**的 `logs/voicekit_usage.jsonl`
（往上找最近带 `.git` 的目录；CAS 与 voicekit 两个仓都适用），不在项目里跑才落
`~/.cache/voicekit/usage.jsonl`。用 `VOICEKIT_USAGE_LOG` 改路径；显式设空串 = 关掉。

写失败只警告、不中断：台账不是判定的前提。追加时加锁（同一台机多进程并发跑也不撕行）。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

#: USD per 1M tokens (input, output)。源：CAS `src/config/llm_models.toml`
#: [pricing_v2.*]（取值日期 2026-11-27）。不认识模型就不算钱（cost_usd = null），不猜 0。
#: 缓存读折扣不在这里算：usage 只有 prompt/completion，台账一律按「未命中缓存」的上限估。
PRICE_PER_M: dict[str, tuple[float, float]] = {
    "deepseek-flash": (0.3, 1.2),  # effective 2026-09-10，CAS 现默认模型
    "deepseek-v4-pro": (1.32, 3.96),  # effective 2026-08-17，工作日高峰价
    "deepseek-chat": (0.14, 0.28),  # deprecated 2026-08-03，历史追溯用
    "deepseek-v4-flash": (0.14, 0.28),  # deprecated 2026-08-03
}

ENV = "VOICEKIT_USAGE_LOG"
NAME = "voicekit_usage.jsonl"


def _repo_root(start: Path) -> Path | None:
    """最近的带 .git 的祖先目录；没有就是没在项目里跑。"""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def log_path() -> Path | None:
    """台账路径；显式空串表示这次不记账。"""
    raw = os.environ.get(ENV)
    if raw is not None:
        return Path(raw) if raw.strip() else None
    root = _repo_root(Path.cwd())
    if root is not None:
        return root / "logs" / NAME
    return Path.home() / ".cache" / "voicekit" / NAME


def cost_usd(model: str, usage: dict | None) -> float | None:
    """按 1M token 单价算钱；模型不认识返回 None（不猜 0）。"""
    price = PRICE_PER_M.get(str(model))
    if price is None:
        return None
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return (prompt * price[0] + completion * price[1]) / 1_000_000


def _append_locked(path: Path, line: str) -> None:
    """加锁追加一行：四五个进程同时跑 judge 时不能互相覆盖（丢账就是丢钱）。"""
    with path.open("a", encoding="utf-8") as handle:
        handle.seek(0)  # 拿第一字节当互斥锁，位置固定，解锁才对得上
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record(entry: dict) -> Path | None:
    """追加一行；返回落地路径。写不进去只警告（判定已经做完，不能因此报错）。"""
    path = log_path()
    if path is None:
        return None
    row = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), **entry}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _append_locked(path, json.dumps(row, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError) as exc:
        # TypeError/ValueError：钱已经花了，别在这里把命令打崩。
        print(f"[warn] 花费台账写不进去（不影响本次结论）：{path}：{exc}", file=sys.stderr)
        return None
    return path
