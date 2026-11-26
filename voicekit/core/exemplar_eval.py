"""小样本裁判的评测（FIX-348）：全样本、样本量下限、验收线。

从 `exemplar_judge.py` 拆出，守「单模块 ≤1000 行」；判定侧的定义只从
`exemplar_judge` 取，不复制数字。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import build_profile as bp
from . import exemplar_judge as ej
from . import judge_lib as jl
from . import style_lint as sl

lib = jl.lib

PROTOCOL = ej.PROTOCOL
PROMPT_VERSION = ej.PROMPT_VERSION
MEASURED_SIZES = ej.MEASURED_SIZES
expected_sizes = ej.expected_sizes
VERDICTS = ej.VERDICTS
HOLDOUT_COUNT = ej.HOLDOUT_COUNT
DEFAULT_CORPUS = ej.DEFAULT_CORPUS
DEFAULT_SET = ej.DEFAULT_SET
holdout_names = ej.holdout_names
build_set = ej.build_set
check_set_compatible = ej.check_set_compatible
write_set = ej.write_set
judge_text = ej.judge_text


# ------------------------------------------------------------------- eval


def eval_rows(set_obj: dict, corpus: Path = DEFAULT_CORPUS) -> list[dict]:
    """作者全量（含盲评留出 12）+ 别人 46 + AI 2。

    作者语料里 12 篇是 rubric 基线样本，10 篇做了样例，剩下 2 篇也不进评测：
    样例集和评测集必须分干净。少一类就报错——评测没跑全不许静默通过。
    """
    accepted = sorted(doc["file"] for doc in bp.load_corpus(corpus)[0])
    baseline = jl.load_baseline()
    if baseline is None:
        raise jl.JudgeError("no judge baseline; cannot separate the exemplar sample")
    sample = {str(name) for name in baseline.get("sample", {}).get("docs", [])}
    probes = set(holdout_names(set_obj))
    pool = [name for name in accepted if name not in sample]
    stale = sorted(probes - set(pool))
    if stale:
        raise jl.JudgeError(f"holdout doc(s) not in the evaluation pool: {stale[:3]}")
    rows = [
        {"name": name, "class": "author", "holdout": name in probes,
         "raw": (corpus / name).read_text(encoding="utf-8")}
        for name in pool
    ]
    for path in sorted(sl.OTHER_AUTHOR_DIR.glob(sl.OTHER_AUTHOR_GLOB)):
        rows.append({"name": path.name, "class": "other", "raw": path.read_text(encoding="utf-8")})
    for doc in jl.generic_negatives():
        rows.append({"name": doc["file"], "class": "ai", "raw": doc["text"]})
    got = {
        key: sum(1 for row in rows if row["class"] == cls)
        for cls, key in (("author", "author_pool"), ("other", "other_author"), ("ai", "generic_ai"))
    }
    want = expected_sizes(corpus)
    if got != {key: want[key] for key in got}:
        raise jl.JudgeError(
            f"eval set incomplete: {got}; need "
            f"author {want['author_pool']} / other {want['other_author']} / "
            f"ai {want['generic_ai']} (negatives dir moved?)"
        )
    if sum(1 for row in rows if row.get("holdout")) != HOLDOUT_COUNT:
        raise jl.JudgeError("eval set holdout count mismatch")
    return rows


def _stats(rows: list[dict]) -> dict:
    n = len(rows)
    same = sum(1 for row in rows if row["same"])
    combined = sum(1 for row in rows if row["same"] and row["det_pass"])
    return {
        "n": n,
        "det_pass": sum(1 for row in rows if row["det_pass"]),
        "same": same,
        "combined": combined,
        "ub_same": round(lib.false_accept_upper_bound(same, n), 4) if n else 1.0,
        "ub_combined": round(lib.false_accept_upper_bound(combined, n), 4) if n else 1.0,
    }


def summarize(rows: list[dict], usage: dict, flips: list[str], runs: int) -> dict:
    authors = [row for row in rows if row["class"] == "author"]
    holdout = [row for row in authors if row.get("holdout")]
    others = [row for row in rows if row["class"] == "other"]
    ai = [row for row in rows if row["class"] == "ai"]
    errors = sum(
        1
        for row in rows
        for answer in (row.get("answers") or [row["answer"]])
        if answer == "error"
    )
    return {
        "protocol": PROTOCOL,
        "prompt_version": PROMPT_VERSION,
        "runs": runs,
        "author_pool": _stats(authors),
        "author_holdout": _stats(holdout),
        "other_author": _stats(others),
        "generic_ai": _stats(ai),
        "verdict_counts": {
            "author": {
                answer: sum(1 for row in authors if row["answer"] == answer)
                for answer in VERDICTS
            },
            "author_holdout": {
                answer: sum(1 for row in holdout if row["answer"] == answer)
                for answer in VERDICTS
            },
            "other": {
                answer: sum(1 for row in others if row["answer"] == answer)
                for answer in VERDICTS
            },
            "ai": {
                answer: sum(1 for row in ai if row["answer"] == answer) for answer in VERDICTS
            },
        },
        "errors": errors,
        "flips": flips,
        "usage": usage,
    }


#: 验收线，写进 DESIGN §5；改这里要同时改 DESIGN 和 tests 里的字面量钉子。
#: **2026-11-10 重锚（语料 61→59，池子 49→47）**：实测 `--repeat 2` —— 作者池 same 44/47
#: （93.6%，旧口径 45/49 = 91.8%，比例没变）、留出 10/12、两层过 41/47、别人 0/46、AI 0/2、翻转 1 篇。
#: 口径沿用旧规矩「按通过率折算 + 每项留 1 篇给 LLM 抖动」，所以目标是实测 − 1：
#:   作者池 43/47 = 91.5%（旧 91.8%）、两层过 40/47 = 85.1%（旧 41/49 = 83.7%，更严）。
#: 这不是「不达标就改标准」：判官行为没变（93.9% → 93.6%），变的是语料篇数。
TARGETS = {
    "author_pool_same": 43,
    "author_holdout_same": 9,
    "other_same_max": 1,
    "ai_same_max": 0,
    "other_combined_max": 0,
    "author_combined_min": 40,
}


def check_targets(summary: dict, sizes: dict | None = None) -> list[str]:
    failures = []
    for key, expected in (sizes or expected_sizes()).items():
        got = summary[key]["n"]
        if got != expected:
            failures.append(f"{key} n {got} != {expected}: eval set incomplete")
    pool = summary["author_pool"]
    holdout = summary["author_holdout"]
    other = summary["other_author"]
    ai = summary["generic_ai"]
    if pool["same"] < TARGETS["author_pool_same"]:
        failures.append(f"author pool same {pool['same']}/{pool['n']} < {TARGETS['author_pool_same']}")
    if holdout["same"] < TARGETS["author_holdout_same"]:
        failures.append(
            f"author holdout same {holdout['same']}/{holdout['n']} < {TARGETS['author_holdout_same']}"
        )
    if other["same"] > TARGETS["other_same_max"]:
        failures.append(f"other author same {other['same']}/{other['n']} > {TARGETS['other_same_max']}")
    if ai["same"] > TARGETS["ai_same_max"]:
        failures.append(f"AI same {ai['same']}/{ai['n']} > {TARGETS['ai_same_max']}")
    if other["combined"] > TARGETS["other_combined_max"]:
        failures.append(
            f"other author combined {other['combined']}/{other['n']} > {TARGETS['other_combined_max']}"
        )
    if pool["combined"] < TARGETS["author_combined_min"]:
        failures.append(
            f"author combined {pool['combined']}/{pool['n']} "
            f"< {TARGETS['author_combined_min']}"
        )
    if summary["errors"]:
        failures.append(f"{summary['errors']} judgement(s) failed to parse")
    return failures


def _fmt_eval(summary: dict) -> str:
    pool = summary["author_pool"]
    holdout = summary["author_holdout"]
    other = summary["other_author"]
    ai = summary["generic_ai"]
    lines = [
        f"== 小样本裁判评测（{summary['protocol']}，{summary['runs']} 遍）",
        f"作者 {pool['n']}（其中盲评留出 {holdout['n']}）：裁判 same {pool['same']}/{pool['n']}，"
        f"确定性过 {pool['det_pass']}/{pool['n']}，两层过（诊断） {pool['combined']}/{pool['n']}",
        f"留出 {holdout['n']}（与 prompt 设计同源，弱留出）：裁判 same {holdout['same']}/{holdout['n']}，"
        f"确定性过 {holdout['det_pass']}/{holdout['n']}，两层过（诊断） {holdout['combined']}/{holdout['n']}",
        f"别人 {other['n']}：裁判 same {other['same']}/{other['n']}（上界 {other['ub_same']:.1%}），"
        f"两层过（诊断） {other['combined']}/{other['n']}（上界 {other['ub_combined']:.1%}）",
        f"AI {ai['n']}：裁判 same {ai['same']}/{ai['n']}",
        f"解析失败 {summary['errors']}；翻转 {len(summary['flips'])}",
    ]
    if summary["flips"]:
        lines.append("  翻转篇目：" + "、".join(summary["flips"][:10]))
    tokens = summary["usage"]
    if tokens:
        lines.append(
            "token 合计：prompt {} / completion {}".format(
                tokens.get("prompt_tokens", tokens.get("prompt", 0)),
                tokens.get("completion_tokens", tokens.get("completion", 0)),
            )
        )
    return "\n".join(lines)


async def judge_all(rows: list[dict], call, repeat: int, concurrency: int) -> tuple[list[list[str]], dict, dict]:
    """并发判整批，回答按下标落位（完成顺序和行顺序不同，不能 append）。

    返回 (answers[run][index], reasons[name], usage)。
    """
    answers: list[list[str | None]] = [[None] * len(rows) for _ in range(repeat)]
    reasons: dict[str, str] = {}
    usage: dict = {}
    sem = asyncio.Semaphore(concurrency)

    async def one(index: int, run: int) -> None:
        async with sem:
            try:
                verdict, reason, used = await call(rows[index])
            except jl.JudgeError as exc:
                verdict, reason, used = "error", str(exc)[:120], {}
            answers[run][index] = verdict
            if run == 0:
                reasons[rows[index]["name"]] = reason
            for key_name, value in (used or {}).items():
                if isinstance(value, int):
                    usage[key_name] = usage.get(key_name, 0) + value

    for run in range(repeat):
        await asyncio.gather(*(one(index, run) for index in range(len(rows))))
    return [[answer or "error" for answer in row] for row in answers], reasons, usage


async def run_eval(
    set_obj: dict,
    *,
    provider: str,
    model: str,
    out: Path | None,
    write: bool,
    repeat: int,
    concurrency: int,
    timeout: float,
) -> int:
    from ..compat import release_llm_client
    from ..compat import create_llm, ensure_provider_sdks_loaded

    profile = jl.load_profile()
    if profile is None:
        print("[FAIL] no profile.json", file=sys.stderr)
        return 2
    rows = eval_rows(set_obj)
    for row in rows:
        det = jl.run_deterministic(row["raw"], profile)
        row["det_pass"] = bool(det and det["passed"])

    await ensure_provider_sdks_loaded()
    try:
        llm = create_llm(provider, model)
    except (ValueError, RuntimeError) as exc:
        print(f"[FAIL] setup error: {exc}", file=sys.stderr)
        return 2
    model_name = getattr(llm, "model", model)
    # 评测本身就是来产 measured 的，所以不要求 baseline 已带 measured。
    incompatible = check_set_compatible(set_obj, model_name, require_measured=False)
    if incompatible:
        print(f"[FAIL] {incompatible}", file=sys.stderr)
        await release_llm_client(llm)
        return 2

    async def call(row: dict) -> tuple[str, str, dict]:
        payload = await judge_text(
            row["raw"], set_obj, llm, model_name,
            use_cache=False, timeout=timeout,
        )
        return payload["verdict"], payload.get("reason", ""), payload.get("usage", {})

    try:
        answers, reasons, usage = await judge_all(rows, call, repeat, concurrency)
    finally:
        await release_llm_client(llm)

    for index, row in enumerate(rows):
        row["answer"] = answers[0][index]
        row["answers"] = [answers[run][index] for run in range(repeat)]
        row["same"] = row["answer"] == "same"
        row["combined"] = bool(row["same"] and row["det_pass"])
        row["reason"] = reasons.get(row["name"], "")
    flips = []
    if repeat > 1:
        for index, row in enumerate(rows):
            if len({answers[run][index] for run in range(repeat)}) > 1:
                flips.append(row["name"])
    summary = summarize(rows, usage, flips, repeat)
    failures = check_targets(summary)

    print(_fmt_eval(summary))
    for row in rows:
        if row["class"] == "author" and not row["same"]:
            print(f"  - 作者判不是同一人：{row['name']}  ({row['answer']}) {row['reason'][:40]}")
        if row["class"] == "other" and row["same"]:
            print(f"  - 别人判同一人：{row['name']}  {row['reason'][:40]}")
    for failure in failures:
        print(f"[FAIL] {failure}", file=sys.stderr)
    if not failures:
        print("== 达标")

    payload = {
        "protocol": PROTOCOL,
        "model": model_name,
        "prompt_version": PROMPT_VERSION,
        "set_sha256": set_obj.get("set_sha256"),
        "holdout_sha256": ej.set_sha256(set_obj.get("holdout") or []),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
        "failures": failures,
        "docs": [
            {
                "name": row["name"],
                "class": row["class"],
                "holdout": bool(row.get("holdout")),
                "answers": row["answers"],
                "answer": row["answer"],
                "det_pass": row["det_pass"],
                "combined": row["combined"],
                "reason": row["reason"][:40],
            }
            for row in rows
        ],
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        tmp.replace(out)
        print(f"wrote {out}")
    if write:
        if failures:
            print("[FAIL] eval did not pass; exemplar baseline NOT written", file=sys.stderr)
            return 1
        fresh = build_set(model=model_name)
        fresh["measured"] = {
            key: summary[key]
            for key in ("author_pool", "author_holdout", "other_author", "generic_ai")
        }
        fresh["measured"]["runs"] = repeat
        fresh["measured"]["checked_at"] = payload["generated_at"]
        write_set(fresh, DEFAULT_SET)
        print(f"exemplar baseline written: {DEFAULT_SET}")
    return 0 if not failures else 1


