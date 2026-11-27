"""本机花费台账：每次真调模型追加一行 JSONL。

为什么要有它：装成 wheel 后命令跑在别人的机器上，CAS 的花费账本看不到这些调用。
台账只记用量与钱，不记稿件正文；默认落在 `~/.cache/voicekit/usage.jsonl`，
用 `VOICEKIT_USAGE_LOG` 改路径；显式设空串 = 关掉。

写失败只警告、不中断：台账不是判定的前提。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

#: USD per 1M tokens (input, output)。源：CAS `src/config/llm_models.toml`
#: `[pricing_v2."deepseek-chat"]`（0.14 / 0.28，2026-08-03，状态 deprecated）。
#: 不认识模型就不算钱（cost_usd = null），不猜 0。
PRICE_PER_M: dict[str, tuple[float, float]] = {"deepseek-chat": (0.14, 0.28)}

ENV = "VOICEKIT_USAGE_LOG"


def log_path() -> Path | None:
    """台账路径；显式空串表示这次不记账。"""
    raw = os.environ.get(ENV)
    if raw is not None:
        return Path(raw) if raw.strip() else None
    return Path.home() / ".cache" / "voicekit" / "usage.jsonl"


def cost_usd(model: str, usage: dict | None) -> float | None:
    """按 1M token 单价算钱；模型不认识返回 None（不猜 0）。"""
    price = PRICE_PER_M.get(str(model))
    if price is None:
        return None
    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    return (prompt * price[0] + completion * price[1]) / 1_000_000


def record(entry: dict) -> Path | None:
    """追加一行；返回落地路径。写不进去只警告（判定已经做完，不能因此报错）。"""
    path = log_path()
    if path is None:
        return None
    row = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"), **entry}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[warn] 花费台账写不进去（不影响本次结论）：{path}：{exc}", file=sys.stderr)
        return None
    return path
