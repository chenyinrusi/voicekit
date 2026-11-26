"""通用作者包的实测（FIX-353）：留出稿 + 别人稿 + AI 稿，达标后写 measured。

判定协议本身在 `exemplar_judge`，本模块只做三件事：凑评测集、跑真模型、写证据。
验收线沿用 panchen 的口径（`exemplar_eval.TARGETS`）：留出 ≥9/12、别人误收 ≤1、
AI 误收 0、解析失败 0。少一类样本就是没跑全，直接报错，不静默通过。

用法（由 `author_pack.py eval` 调）：
    python scripts/voice/author_pack.py eval --pack DIR --write --out 结果.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import exemplar_judge as ej
from . import judge_lib as jl
from . import pack_lib as pk
from .exemplar_measured import negatives_digest

PROTOCOL = ej.PROTOCOL
PROMPT_VERSION = ej.PROMPT_VERSION
JUDGE_LOGIC_VERSION = ej.JUDGE_LOGIC_VERSION
HOLDOUT_COUNT = ej.HOLDOUT_COUNT
#: panchen 的验收线（exemplar_eval.TARGETS）：换作者不换口径。
HOLDOUT_SAME_MIN = 9
OTHER_AUTHOR_SAME_MAX = 1
GENERIC_AI_SAME_MAX = 0
MIN_NEGATIVES = 20
GENERIC_AI_COUNT = 2


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def negatives_digest_of(pack: dict) -> str | None:
    """负样本指纹（文件名 + 内容）。eval 载荷与 measured 用同一个口径。"""
    meta = pack["data"].get("negatives") or {}
    names = [str(name) for name in (meta.get("files") or [])]
    if not names or pack.get("negatives") is None:
        return None
    return negatives_digest(Path(pack["negatives"]), names)


def eval_rows(pack: dict, set_obj: dict) -> list[dict]:
    """评测集：12 篇留出 + 负样本名单（≥20）+ 2 篇通用 AI 稿。"""
    corpus = pk.pack_corpus(pack, "pack eval")
    rows: list[dict] = []
    holdout = set_obj.get("holdout")
    if not isinstance(holdout, list) or len(holdout) != HOLDOUT_COUNT:
        raise jl.JudgeError(
            f"holdout has {len(holdout or [])} docs; need {HOLDOUT_COUNT} "
            "(corpus of 22: 10 exemplars + 12 holdout)"
        )
    for doc in holdout:
        name = str(doc.get("name") or "")
        path = corpus / name
        if not name or not path.is_file():
            raise jl.JudgeError(f"holdout doc missing from corpus: {name!r}")
        rows.append({"name": name, "class": "author_holdout", "raw": path.read_text(encoding="utf-8")})

    meta = pack["data"].get("negatives") or {}
    names = [str(name) for name in (meta.get("files") or [])]
    root = pack.get("negatives")
    if not names or root is None or not Path(root).is_dir():
        raise jl.JudgeError("no negatives recorded in pack.json; rebuild with --negatives DIR")
    if len(names) < MIN_NEGATIVES:
        raise jl.JudgeError(f"only {len(names)} negatives; need >= {MIN_NEGATIVES}")
    for name in names:
        path = Path(root) / name
        if not path.is_file():
            raise jl.JudgeError(f"negative missing: {name}")
        rows.append({"name": name, "class": "other_author", "raw": path.read_text(encoding="utf-8")})

    ai = jl.generic_negatives()
    if len(ai) != GENERIC_AI_COUNT:
        raise jl.JudgeError(f"generic AI negatives n {len(ai)} != {GENERIC_AI_COUNT}")
    for doc in ai:
        rows.append({"name": doc["file"], "class": "generic_ai", "raw": doc["text"]})
    return rows


def _stats(rows: list[dict], *, with_ub: bool = False) -> dict:
    n = len(rows)
    same = sum(1 for row in rows if row["same"])
    stats = {"n": n, "same": same}
    if with_ub:
        # 误收率的上界只对「别人稿」有意义；留出集是 recall，不记上界。
        stats["ub_same"] = round(jl.lib.false_accept_upper_bound(same, n), 4) if n else 1.0
    return stats


def summarize(rows: list[dict], usage: dict, flips: list[str], runs: int, model: str) -> dict:
    holdout = [row for row in rows if row["class"] == "author_holdout"]
    other = [row for row in rows if row["class"] == "other_author"]
    ai = [row for row in rows if row["class"] == "generic_ai"]
    return {
        "protocol": PROTOCOL,
        "prompt_version": PROMPT_VERSION,
        "judge_logic_version": JUDGE_LOGIC_VERSION,
        "model": model,
        "runs": runs,
        "author_holdout": _stats(holdout),
        "other_author": _stats(other, with_ub=True),
        "generic_ai": _stats(ai),
        "errors": sum(
            1
            for row in rows
            for answer in (row.get("answers") or [row["answer"]])
            if answer == "error"
        ),
        "flips": flips,
        "usage": usage,
    }


def check_targets(summary: dict) -> list[str]:
    failures: list[str] = []
    holdout = summary["author_holdout"]
    other = summary["other_author"]
    ai = summary["generic_ai"]
    if holdout["n"] != HOLDOUT_COUNT:
        failures.append(f"holdout n {holdout['n']} != {HOLDOUT_COUNT}")
    elif holdout["same"] < HOLDOUT_SAME_MIN:
        failures.append(f"holdout same {holdout['same']}/{holdout['n']} < {HOLDOUT_SAME_MIN}")
    if other["n"] < MIN_NEGATIVES:
        failures.append(f"negative n {other['n']} < {MIN_NEGATIVES}")
    if other["same"] > OTHER_AUTHOR_SAME_MAX:
        failures.append(
            f"other-author false accept {other['same']}/{other['n']} > {OTHER_AUTHOR_SAME_MAX}"
        )
    if ai["same"] > GENERIC_AI_SAME_MAX:
        failures.append(f"generic AI false accept {ai['same']}/{ai['n']} > {GENERIC_AI_SAME_MAX}")
    if ai["n"] != GENERIC_AI_COUNT:
        failures.append(f"generic AI n {ai['n']} != {GENERIC_AI_COUNT}")
    if summary["flips"]:
        failures.append(f"{len(summary['flips'])} judgement(s) flipped between runs")
    if summary["errors"]:
        failures.append(f"{summary['errors']} judgement(s) failed to parse")
    return failures


def _fmt(summary: dict) -> str:
    holdout = summary["author_holdout"]
    other = summary["other_author"]
    ai = summary["generic_ai"]
    lines = [
        f"== 通用包实测（{summary['protocol']}，{summary['runs']} 遍，模型 {summary['model']}）",
        f"留出 {holdout['n']} 篇：判同人 {holdout['same']}/{holdout['n']}",
        f"别人 {other['n']} 篇：误收 {other['same']}/{other['n']}（95% 上界 {other['ub_same']:.1%}）",
        f"通用 AI {ai['n']} 篇：误收 {ai['same']}/{ai['n']}",
        f"解析失败 {summary['errors']}；翻转 {len(summary['flips'])}",
    ]
    if summary["usage"]:
        usage = summary["usage"]
        lines.append(
            "token 合计：prompt {} / completion {}".format(
                usage.get("prompt_tokens", usage.get("prompt", 0)),
                usage.get("completion_tokens", usage.get("completion", 0)),
            )
        )
    return "\n".join(lines)


async def _judge_all(rows: list[dict], call, repeat: int, concurrency: int) -> tuple[list[list[str]], dict, dict]:
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
            for key, value in (used or {}).items():
                if isinstance(value, int):
                    usage[key] = usage.get(key, 0) + value

    for run in range(repeat):
        await asyncio.gather(*(one(index, run) for index in range(len(rows))))
    return [[answer or "error" for answer in row] for row in answers], reasons, usage


def apply_measured(pack: dict, set_obj: dict, summary: dict) -> None:
    """把实测写进样例集与 pack.json，再重生成 SKILL.md；不达标时调用方已拦。"""
    import author_pack as ap  # 延迟导入：author_pack 依赖本模块，反向会在运行时解开

    measured = {
        "protocol": summary["protocol"],
        "prompt_version": summary["prompt_version"],
        "prompt_sha256": ej.prompt_sha256(),
        "judge_logic_version": summary["judge_logic_version"],
        "model": summary["model"],
        "runs": summary["runs"],
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "set_sha256": set_obj.get("set_sha256"),
        "holdout_sha256": ej.set_sha256(set_obj.get("holdout") or []),
        "author_holdout": summary["author_holdout"],
        "other_author": summary["other_author"],
        "generic_ai": summary["generic_ai"],
        "flips": summary["flips"],
    }
    digest = negatives_digest_of(pack)
    if digest is not None:
        measured["negatives_sha256"] = digest
    set_obj["calibration"] = "measured"
    set_obj["measured"] = measured
    set_obj["note"] = "已实测（数字见 measured 与包内 eval 证据）；误收率不再未知。"
    ej.write_set(set_obj, pack["exemplar"])

    data = pack["data"]
    data.setdefault("exemplar", {})["path"] = "exemplar_baseline.json"
    data["exemplar"]["sha256"] = _sha256_bytes(pack["exemplar"].read_bytes())
    data["calibration"] = "measured"
    tmp = pack["path"].with_suffix(pack["path"].suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(pack["path"])
    skill_text = ap.render_skill(pack["dir"])
    skill_tmp = (pack["dir"] / "SKILL.md").with_suffix(".md.tmp")
    skill_tmp.write_text(skill_text, encoding="utf-8", newline="\n")
    skill_tmp.replace(pack["dir"] / "SKILL.md")


async def run_eval(
    pack: dict,
    set_obj: dict,
    llm,
    model: str,
    *,
    repeat: int = 1,
    concurrency: int = 4,
    timeout: float = 120.0,
    out: Path | None = None,
    write: bool = False,
) -> int:
    """跑整批评测；达标且 write 时写 measured。返回 0 达标 / 1 未达标。"""
    rows = eval_rows(pack, set_obj)
    corpus = pk.pack_corpus(pack, "pack eval")

    async def call(row: dict) -> tuple[str, str, dict]:
        payload = await ej.judge_text(
            row["raw"], set_obj, llm, model, corpus,
            use_cache=False, timeout=timeout,
        )
        return payload["verdict"], payload.get("reason", ""), payload.get("usage", {})

    answers, reasons, usage = await _judge_all(rows, call, repeat, concurrency)
    flips = []
    for index, row in enumerate(rows):
        row["answers"] = [answers[run][index] for run in range(repeat)]
        row["answer"] = row["answers"][0]
        row["same"] = row["answer"] == "same"
        row["reason"] = reasons.get(row["name"], "")
        if len(set(row["answers"])) > 1:
            flips.append(row["name"])
    summary = summarize(rows, usage, flips, repeat, model)
    failures = check_targets(summary)

    print(_fmt(summary))
    for row in rows:
        if row["class"] == "author_holdout" and not row["same"]:
            print(f"  - 留出稿判不是同一人：{row['name']}（{row['answer']}）{row['reason'][:40]}")
        if row["class"] != "author_holdout" and row["same"]:
            print(f"  - 别人稿判同一人：{row['name']}（{row['answer']}）{row['reason'][:40]}")
    for failure in failures:
        print(f"[FAIL] {failure}", file=sys.stderr)
    if not failures:
        print("== 达标")

    payload = {
        "protocol": summary["protocol"],
        "model": model,
        "prompt_version": summary["prompt_version"],
        "prompt_sha256": ej.prompt_sha256(),
        "judge_logic_version": summary["judge_logic_version"],
        "set_sha256": set_obj.get("set_sha256"),
        "holdout_sha256": ej.set_sha256(set_obj.get("holdout") or []),
        "negatives_sha256": negatives_digest_of(pack),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
        "failures": failures,
        "docs": [
            {
                "name": row["name"],
                "class": row["class"],
                "answers": row["answers"],
                "answer": row["answer"],
                "reason": row["reason"][:60],
            }
            for row in rows
        ],
    }
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(out)
        print(f"wrote {out}")
    if not failures and write:
        apply_measured(pack, set_obj, summary)
        print(f"measured written: {pack['exemplar']}")
    return 0 if not failures else 1
