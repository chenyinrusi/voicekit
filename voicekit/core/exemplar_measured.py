"""通用作者包实测块（`measured`）的校验与负样本指纹（FIX-353）。

纯函数：只校验结构、数字范围、时间戳、与样例集/留出集/负样本的绑定；不调用模型。
拆成小模块是为了把 `exemplar_judge.py` 控制在 1000 行以内。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def set_sha256(docs: list[dict]) -> str:
    """10 篇样例/留出集的指纹：名字 + 清洗后字节，按给定顺序。"""
    return sha256(
        "\n".join(
            f"{d.get('name', '')}\x00{d.get('sha256', '')}"
            for d in docs
            if isinstance(d, dict)
        )
    )


def negatives_digest(root: Path, names: list[str]) -> str:
    """负样本指纹：文件名 + 内容，按名字排序；评测后换稿就变。

    按**换行归一化后的文本**取指纹（不是字节）：`.gitattributes` 把 `*.md` 存成 LF，
    工作区可能是 CRLF；按字节算会让干净 checkout 后的 digest 与评测时的对不上（FIX-355 r1）。
    """
    parts = []
    for name in sorted(names):
        path = Path(root) / name
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
            parts.append(f"{name}\x00{hashlib.sha256(text.encode('utf-8')).hexdigest()}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def verify_measured(
    set_obj: dict,
    *,
    protocol: str,
    prompt_version: str,
    prompt_sha256: str,
    judge_logic_version: int,
    holdout_sha256: str,
) -> list[str]:
    """实测块必须完整、数字自洽，并绑在当前的样例集/留出集/提示上。"""
    problems: list[str] = []
    measured = set_obj.get("measured")
    if not isinstance(measured, dict) or not measured:
        return ["measured block missing"]
    for key, expected in (
        ("protocol", protocol),
        ("prompt_version", prompt_version),
        ("prompt_sha256", prompt_sha256),
        ("judge_logic_version", judge_logic_version),
    ):
        if measured.get(key) != expected:
            problems.append(f"measured {key} {measured.get(key)!r} != {expected!r}")
    if not isinstance(measured.get("model"), str) or not measured["model"].strip():
        problems.append("measured model missing")
    if not _is_int(measured.get("runs")) or measured["runs"] < 1:
        problems.append("measured runs must be an integer >= 1")
    checked_at = measured.get("checked_at")
    if not isinstance(checked_at, str) or not checked_at.strip():
        problems.append("measured checked_at missing")
    else:
        try:
            datetime.fromisoformat(checked_at)
        except ValueError:
            problems.append("measured checked_at is not ISO 8601")
    flips = measured.get("flips", [])
    if not isinstance(flips, list) or not all(isinstance(name, str) for name in flips):
        problems.append("measured flips must be a list of names")
    elif flips:
        problems.append("measured flips non-empty (a failed eval must not be measured)")
    for key in ("author_holdout", "other_author", "generic_ai"):
        block = measured.get(key)
        if not isinstance(block, dict):
            problems.append(f"measured {key} block missing")
            continue
        n, same = block.get("n"), block.get("same")
        if not _is_int(n) or n < 0:
            problems.append(f"measured {key}.n must be a non-negative integer")
        if not _is_int(same) or same < 0:
            problems.append(f"measured {key}.same must be a non-negative integer")
        if _is_int(n) and _is_int(same) and same > n:
            problems.append(f"measured {key}.same {same} > n {n}")
        ub = block.get("ub_same")
        if ub is not None and not (
            isinstance(ub, (int, float)) and not isinstance(ub, bool) and 0 <= ub <= 1
        ):
            problems.append(f"measured {key}.ub_same must be a number in [0, 1]")
    if measured.get("set_sha256") != set_obj.get("set_sha256"):
        problems.append("measured set_sha256 != current exemplar set")
    if measured.get("holdout_sha256") != holdout_sha256:
        problems.append("measured holdout_sha256 != current holdout docs")
    negatives_sha = measured.get("negatives_sha256")
    if not isinstance(negatives_sha, str) or len(negatives_sha) != 64:
        problems.append("measured negatives_sha256 missing")
    return problems
