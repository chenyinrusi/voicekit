"""大模型裁判：按 `--protocol` 判一篇稿「像不像」作者。

默认 `exemplar`（FIX-348）：给 10 篇作者样例，判是不是同一个人。
`rubric` 是旧协议（21 维 band + 原文证据），保留作诊断，不再参与通过判定。

规则与数据在 `judge_lib`（纯函数），本文件只做：调模型、补判、缓存、CLI。
设计见 `dev/fixes/FIX-322-voice-llm-judge/DESIGN.md` 与 `dev/fixes/FIX-348-exemplar-judge/DESIGN.md`。

用法（repo 根目录）：
    python scripts/voice/judge.py --file 稿.md                  # 默认：10 篇样例同人判定
    python scripts/voice/judge.py --file 稿.md --json            # 机器读
    python scripts/voice/judge.py --file 稿.md --protocol rubric  # 21 维诊断（不参与判定）
    python scripts/voice/judge.py --build-baseline --sample 12    # 真实大模型建 rubric 基线（烧 token）
    python scripts/voice/judge.py --heldout --count 5             # rubric 样本外通过率（真实大模型）
    python scripts/voice/judge.py --selftest                     # 无网络自检（check 入口）

退出码：0 像 / 1 不像 / 2 用法、环境或数据不足（inconclusive 也算 2，不当作通过）。
`--heldout`：0 达标 / 1 未达标 / 2 环境或数据不足。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from . import judge_lib as jl  # noqa: E402
from . import exemplar_judge as ej  # noqa: E402
from . import pack_lib as pk  # noqa: E402
from .judge_lib import (  # noqa: E402
    BAND_MIN,
    BAND_MAX,
    BAND_TOLERANCE,
    DEFAULT_BASELINE,
    DEFAULT_CORPUS,
    DEFAULT_MODEL,
    DEFAULT_PROFILE,
    DEFAULT_PROVIDER,
    DEFAULT_RUBRICS,
    DISCRIMINATIVE_GAP,
    EVIDENCE_MAX_CHARS,
    EVIDENCE_MIN_CHARS,
    HELDOUT_MIN_LIKE_RATIO,
    JUDGE_LOGIC_VERSION,
    MAX_MISSING,
    MIN_POSITIVE_PER_DIM,
    MIN_SAMPLE_DOCS,
    PASS_DIM_RATIO,
    REPO_ROOT,
    SYSTEM_PROMPT,
    JudgeError,
    _baseline_metrics,
    _negatives,
    _read_cache,
    _sample_docs,
    _strip_fence,
    _utf8_stdio,
    _write_cache,
    build_messages,
    check_baseline_compatible,
    check_profile_match,
    cache_key,
    combine,
    evaluate,
    generic_negatives,
    heldout_docs,
    load_baseline,
    load_profile,
    load_rubrics,
    judge_input,
    min_discriminative_dims,
    min_scored_dims,
    parse_response,
    profile_sha256,
    registry_ids,
    render_user_prompt,
    run_deterministic,
    validate_rubrics,
    verify_evidence,
    write_baseline,
)

lib = jl.lib


async def _call_llm(llm, messages, timeout: float) -> tuple[str, dict]:
    # temperature=0：判分要可复现，不要模型的随机文采。
    try:
        response = await asyncio.wait_for(llm.chat(messages, temperature=0.0), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise JudgeError(f"LLM timeout after {timeout:.0f}s") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        # 不把远程错误体整段抛出去：里面可能带回显的请求头。
        raise JudgeError(f"LLM call failed: {type(exc).__name__}") from exc
    text = getattr(response, "content", None) or ""
    usage = getattr(response, "usage", None) or {}
    return str(text), usage


async def judge_text(
    text: str,
    rubrics: dict,
    profile: dict | None,
    llm,
    model: str,
    use_cache: bool = True,
    timeout: float = 120.0,
    retries: int = 2,
) -> dict:
    """判一篇稿：返回 {rows, usage, cached, model}。证据校验已在内部完成。"""
    body = judge_input(text)
    expected = [str(d["id"]) for d in rubrics["dims"]]
    key = cache_key(body, rubrics, model, profile)
    if use_cache:
        cached = _read_cache(key, expected)
        if cached is not None:
            cached["cached"] = True
            return cached

    last_error: Exception | None = None
    last_raw = ""
    usage_total: dict = {}
    parsed_all: dict[str, dict] = {}
    for attempt in range(retries + 1):
        # 每轮都重算：证据失败即没判过，进补判队列。
        rows = verify_evidence(parsed_all, body, rubrics) if parsed_all else []
        pending = [row["id"] for row in rows if not row["evidence_ok"]]
        pending += [did for did in expected if did not in parsed_all]
        pending = list(dict.fromkeys(pending))
        if not pending:
            break
        messages = build_messages(body, rubrics, profile)
        if attempt:
            # 温度 0 下重发同一提示会得到同一份坏输出：把坏输出和要补的维度带回去。
            from ..compat import Message

            bad_notes = ", ".join(
                f"{row['id']}:{row['note']}" for row in rows if not row["evidence_ok"]
            )
            messages = messages + [
                Message.assistant(last_raw[:2000]),
                Message.user(
                    "上面这轮有维度没给、不能解析，或 quote 与正文对不上"
                    + (f"（{bad_notes}）" if bad_notes else "")
                    + (f"（{last_error}）" if last_error else "")
                    + "。补判要求：quote 从正文原样照抄 "
                    + f"{EVIDENCE_MIN_CHARS}–{EVIDENCE_MAX_CHARS} 字，必须是正文里连续的一段"
                    + "（相邻两句可以连着引），不许把两处文字拼起来；"
                    + "太短多半是抄了标题行或半句，请改抄正文的完整句子。"
                    + "现在只补这些维度："
                    + ",".join(pending)
                    + "。只输出 JSONL，每行一个对象。"
                ),
            ]
        try:
            raw, usage = await _call_llm(llm, messages, timeout)
        except JudgeError as exc:
            last_error = exc
            continue
        last_raw = raw
        for k, v in usage.items():
            if isinstance(v, int):
                usage_total[k] = usage_total.get(k, 0) + v
        try:
            parsed = parse_response(raw, rubrics)
        except JudgeError as exc:
            last_error = exc
            continue
        parsed_all.update(
            {k: v for k, v in parsed.items() if k in pending or k not in parsed_all}
        )
        last_error = None
    if not parsed_all:
        raise JudgeError(
            f"model output unusable: {last_error} | raw: {last_raw[:300]!r}"
        )
    # 循环外无条件重算：最后一次补判的结果必须进最终 rows（否则补判白花钱）。
    rows = verify_evidence(parsed_all, body, rubrics)
    payload = {"rows": rows, "usage": usage_total, "cached": False, "model": model}
    if use_cache:
        _write_cache(key, payload, expected)
    return payload


async def build_baseline(
    rubrics: dict,
    profile: dict | None,
    llm,
    model: str,
    corpus: Path,
    sample: int,
    use_cache: bool,
) -> dict:
    docs = _sample_docs(corpus, sample)
    if len(docs) < MIN_SAMPLE_DOCS:
        raise JudgeError(f"corpus too small: {len(docs)} doc(s) >= 800 CJK under {corpus}")

    positive: list[dict] = []
    failures: list[dict] = []
    for doc in docs:
        # 一篇判不了不该废掉整次 build：记下来，最后看每维样本够不够。
        try:
            judged = await judge_text(doc["text"], rubrics, profile, llm, model, use_cache=use_cache)
        except JudgeError as exc:
            failures.append({"file": doc["file"], "error": str(exc)[:200]})
            continue
        positive.append(
            {r["id"]: r["band"] for r in judged["rows"] if r["evidence_ok"] and r["band"] is not None}
        )

    negatives = _negatives(corpus, docs)
    neg_bands: list[dict] = []
    for doc in negatives:
        try:
            judged = await judge_text(doc["text"], rubrics, profile, llm, model, use_cache=use_cache)
        except JudgeError as exc:
            failures.append({"file": doc["file"], "error": str(exc)[:200]})
            continue
        neg_bands.append(
            {r["id"]: r["band"] for r in judged["rows"] if r["evidence_ok"] and r["band"] is not None}
        )

    dims: dict[str, dict] = {}
    discriminative_count = 0
    for dim in rubrics["dims"]:
        did = str(dim["id"])
        pos = [row[did] for row in positive if did in row]
        neg = [row[did] for row in neg_bands if did in row]
        if len(pos) < MIN_POSITIVE_PER_DIM:
            dims[did] = {
                "name": dim["name"],
                "median": 0,
                "bands": pos,
                "positive_count": len(pos),
                "negative_median": None,
                "gap": None,
                "discriminative": False,
                "reason": f"only {len(pos)} positive sample(s)",
            }
            continue
        # round-half-up：偶数样本的 .5 不向下截断，否则中位偏低一档。
        median = int(statistics.median(pos) + 0.5)
        neg_median = int(statistics.median(neg) + 0.5) if neg else None
        gap = None if neg_median is None else median - neg_median
        discriminative = bool(neg_median is not None and gap >= DISCRIMINATIVE_GAP)
        if discriminative:
            discriminative_count += 1
        dims[did] = {
            "name": dim["name"],
            "median": median,
            "bands": pos,
            "positive_count": len(pos),
            "negative_median": neg_median,
            "gap": gap,
            "discriminative": discriminative,
        }

    try:
        corpus_ref = corpus.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        corpus_ref = corpus.as_posix()
    ndims = len(rubrics["dims"])
    need = min_discriminative_dims(ndims)
    return {
        "schema": 1,
        "author_slug": rubrics.get("author_slug"),
        "model": model,
        "rubric_version": rubrics.get("version"),
        "prompt_version": rubrics.get("prompt_version"),
        "judge_logic_version": JUDGE_LOGIC_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sample": {"corpus": corpus_ref, "docs": [d["file"] for d in docs], "count": len(docs)},
        "negatives": [d["file"] for d in negatives],
        "failures": failures,
        "dims": dims,
        "calibration": {
            "discriminative_dims": discriminative_count,
            "required": need,
            "passed": discriminative_count >= need,
        },
        "pass_rule": {
            "band_tolerance": BAND_TOLERANCE,
            "pass_ratio": PASS_DIM_RATIO,
            "min_scored_dims": min_scored_dims(ndims),
            "key_dims": [str(k) for k in rubrics["key_dims"]],
        },
    }


class _FakeLLM:
    """假裁判：返回预置 JSON，记录调用次数。"""

    def __init__(self, payloads: list[str]):
        self.payloads = payloads
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]

        class _Resp:
            content = payload
            usage = {"prompt": 1, "completion": 1}

        return _Resp()


def _fake_body() -> str:
    return (
        "前两天，有人来我们公司开会，一见面就伸手与我握手，说久仰久仰。"
        "我其实不记得他是谁。"
    )


def _fake_payload(
    body: str, bands: dict[str, int] | None = None, rubrics: dict | None = None
) -> str:
    quotes = {
        "1": "我其实不记得他是谁",
        "2": "一见面就伸手与我握手",
        "3": "前两天，有人来我们公司开会",
        "4": "握手，说久仰久仰",
        "5": "伸手与我握手",
        "6": "我其实不记得",
        "7": "前两天，有人来我们公司开会",
        "8": "一见面就伸手",
    }
    # 9 维之后复用上面这些已在正文里的证据句：每维证据独立校验，不必新增句子。
    fallback = list(dict.fromkeys(quotes.values()))
    dims = []
    for idx, dim in enumerate((rubrics or load_rubrics())["dims"]):
        dim_id = str(dim["id"])
        quote = quotes.get(dim_id) or fallback[idx % len(fallback)]
        band = (bands or {}).get(dim_id, 3)
        dims.append({"id": dim_id, "band": band, "quote": quote, "why": "自检"})
    return json.dumps({"dims": dims}, ensure_ascii=False)


def selftest() -> int:
    _utf8_stdio()
    rubrics = load_rubrics()
    known = registry_ids()
    problems = validate_rubrics(rubrics, known)
    if problems:
        print("[FAIL] rubric structure:")
        for problem in problems:
            print("  -", problem)
        return 1
    print(f"rubric ok: {len(rubrics['dims'])} dims, version {rubrics.get('version')}")

    body = _fake_body()
    profile = load_profile()
    fake = _FakeLLM([_fake_payload(body)])
    payload = asyncio.run(
        judge_text(body, rubrics, profile, fake, "fake-model", use_cache=False)
    )
    ok_rows = {r["id"]: r for r in payload["rows"]}
    if len(ok_rows) != len(rubrics["dims"]) or not all(
        r["evidence_ok"] for r in payload["rows"]
    ):
        print("[FAIL] valid payload should give evidence-backed dims for every rubric dim")
        return 1

    # 证据不在正文里 → 该维拒收
    bad = json.loads(_fake_payload(body))
    bad["dims"][0]["quote"] = "这句原文里没有"
    bad_payload = asyncio.run(
        judge_text(
            body, rubrics, profile, _FakeLLM([json.dumps(bad, ensure_ascii=False)]),
            "fake-model", use_cache=False,
        )
    )
    if bad_payload["rows"][0]["evidence_ok"]:
        print("[FAIL] hallucinated quote was accepted")
        return 1

    # band 越界 → 该维丢弃、进补判；重试用尽后该维判 missing，其它维照常
    out_of_range = json.loads(_fake_payload(body))
    out_of_range["dims"][0]["band"] = 9
    ranged = asyncio.run(
        judge_text(
            body, rubrics, profile,
            _FakeLLM([json.dumps(out_of_range, ensure_ascii=False)]),
            "fake-model", use_cache=False, retries=1,
        )
    )
    ranged_rows = {row["id"]: row for row in ranged["rows"]}
    if ranged_rows["1"]["note"] != "missing":
        print("[FAIL] band 9 should drop that dim, not the whole round")
        return 1
    if not sum(1 for row in ranged["rows"] if row["evidence_ok"]) == len(rubrics["dims"]) - 1:
        print("[FAIL] band 9 should keep the other dims")
        return 1

    # 相邻两句连着引 → 仍是正文里连续的一段，算证据（2026-11-03 实测：拒掉会让
    # 通用 AI 腔样本整维剔除、判 inconclusive）。
    adjacent = json.loads(_fake_payload(body))
    adjacent["dims"][0]["quote"] = "久仰久仰我其实不记得他是谁"
    adjacent_rows = asyncio.run(
        judge_text(
            body, rubrics, profile,
            _FakeLLM([json.dumps(adjacent, ensure_ascii=False)]),
            "fake-model", use_cache=False, retries=0,
        )
    )
    if not next(row for row in adjacent_rows["rows"] if row["id"] == "1")["evidence_ok"]:
        print("[FAIL] adjacent-sentence quote should be evidence")
        return 1

    # 两处各取一段拼起来 → 在正文里不连续，拒收
    stitched = json.loads(_fake_payload(body))
    stitched["dims"][0]["quote"] = "前两天久仰久仰"
    stitched_rows = asyncio.run(
        judge_text(
            body, rubrics, profile,
            _FakeLLM([json.dumps(stitched, ensure_ascii=False)]),
            "fake-model", use_cache=False, retries=0,
        )
    )
    if next(row for row in stitched_rows["rows"] if row["id"] == "1")["evidence_ok"]:
        print("[FAIL] stitched quote was accepted")
        return 1

    # 最后一次补判成功的结果必须进最终 rows（曾经被旧 rows 盖掉）
    stale = json.loads(_fake_payload(body))
    stale["dims"][0]["quote"] = "编造的句子"
    merged = asyncio.run(
        judge_text(
            body, rubrics, profile,
            _FakeLLM([json.dumps(stale, ensure_ascii=False), _fake_payload(body)]),
            "fake-model", use_cache=False, retries=1,
        )
    )
    if not next(row for row in merged["rows"] if row["id"] == "1")["evidence_ok"]:
        print("[FAIL] last-attempt repair was dropped (stale rows)")
        return 1

    # 模型把换行写进字符串 / 包了代码块 → 仍要能解析（真实调用踩过）
    fenced = "```json\n" + _fake_payload(body).replace("我其实不记得他是谁", "我其实\n不记得他是谁") + "\n```"
    try:
        repaired = parse_response(fenced, rubrics)
    except JudgeError as exc:
        print(f"[FAIL] fenced/newline payload should be repaired: {exc}")
        return 1
    if repaired["1"]["band"] != 3:
        print("[FAIL] repaired payload lost band")
        return 1

    # 缓存：同一文本第二次不再调用模型
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DSH_VOICE_JUDGE_CACHE"] = tmp
        try:
            counting = _FakeLLM([_fake_payload(body)])
            asyncio.run(judge_text(body, rubrics, profile, counting, "fake-model"))
            asyncio.run(judge_text(body, rubrics, profile, counting, "fake-model"))
        finally:
            os.environ.pop("DSH_VOICE_JUDGE_CACHE", None)
        if counting.calls != 1:
            print(f"[FAIL] cache did not dedupe calls: {counting.calls}")
            return 1

    # 判定数学：基线 3、允许低一档 → band 2 过，band 1 不过
    baseline = {
        "model": "fake-model",
        "dims": {
            str(d["id"]): {"median": 3, "discriminative": True} for d in rubrics["dims"]
        },
    }
    rows = [
        {"id": str(d["id"]), "name": d["name"], "band": 2, "evidence_ok": True, "note": ""}
        for d in rubrics["dims"]
    ]
    verdict = evaluate(rows, baseline, rubrics)
    if verdict["verdict"] != "like":
        print(f"[FAIL] band 2 vs baseline 3 should pass: {verdict['reason']}")
        return 1
    for row in rows:
        row["band"] = 1
    verdict = evaluate(rows, baseline, rubrics)
    if verdict["verdict"] != "not-like":
        print("[FAIL] band 1 vs baseline 3 should fail")
        return 1
    for row in rows:
        row["band"] = 3
    rows[0]["evidence_ok"] = False
    verdict = evaluate(rows, baseline, rubrics)
    if verdict["verdict"] != "inconclusive" or "1" not in verdict["evidence_missing"]:
        print("[FAIL] evidence-less key dim must be inconclusive, not failed")
        return 1
    # 关键维没有可用基线 → 不许凑分，判 inconclusive
    baseline_no_key = {
        "model": "fake-model",
        "dims": {
            str(d["id"]): {"median": 3, "discriminative": str(d["id"]) != "1"}
            for d in rubrics["dims"]
        },
    }
    rows = [
        {"id": str(d["id"]), "name": d["name"], "band": 4, "evidence_ok": True, "note": ""}
        for d in rubrics["dims"]
    ]
    if evaluate(rows, baseline_no_key, rubrics)["verdict"] != "inconclusive":
        print("[FAIL] unscored key dim must be inconclusive")
        return 1
    if combine({"passed": True}, None)["verdict"] != "inconclusive":
        print("[FAIL] missing layer must be inconclusive")
        return 1

    print("selftest ok: rubric / evidence / band / cache / verdict")
    return 0


async def run_heldout(
    rubrics: dict,
    profile: dict | None,
    baseline: dict,
    llm,
    model: str,
    corpus: Path,
    count: int,
    timeout: float = 120.0,
    use_cache: bool = True,
) -> dict:
    """样本外验收：基线没用过的作者稿判 like 率 + 通用 AI 腔判 not-like 率。

    区分度只用 12 篇作者样本算，样本内全过不能说明新稿能过；这个模式补的是
    「过松」的反证。抽哪几篇由 `heldout_docs` 定，同一语料同一基线永远同一批。
    """
    exclude = {str(doc) for doc in (baseline.get("sample") or {}).get("docs") or []}
    author_docs = heldout_docs(corpus, exclude, count)
    if not author_docs:
        raise JudgeError("no held-out docs: corpus exhausted or sample covers everything")
    negatives = generic_negatives()[:2]
    if not negatives:
        raise JudgeError("no generic AI negative fixture found")

    async def _one(doc: dict, kind: str) -> dict:
        judged = await judge_text(
            doc["text"], rubrics, profile, llm, model, use_cache=use_cache, timeout=timeout
        )
        verdict = evaluate(judged["rows"], baseline, rubrics)
        det = run_deterministic(doc["text"], profile)
        return {
            "file": doc["file"],
            "kind": kind,
            "verdict": verdict["verdict"],
            "reason": verdict["reason"],
            "deterministic_passed": det["passed"] if det else None,
        }

    rows: list[dict] = []
    for doc in author_docs:
        rows.append(await _one(doc, "author"))
    for doc in negatives:
        rows.append(await _one(doc, "generic-ai"))

    author = [row for row in rows if row["kind"] == "author"]
    neg = [row for row in rows if row["kind"] == "generic-ai"]
    like = sum(1 for row in author if row["verdict"] == "like")
    need = max(1, math.ceil(len(author) * HELDOUT_MIN_LIKE_RATIO))
    neg_not_like = sum(1 for row in neg if row["verdict"] == "not-like")
    return {
        "docs": len(author),
        "like": like,
        "need": need,
        "negative_count": len(neg),
        "negative_not_like": neg_not_like,
        "passed": like >= need and neg_not_like == len(neg),
        "rows": rows,
        "model": model,
    }


def _fmt_heldout(result: dict) -> str:
    lines = [f"样本外 · 模型 {result['model']}"]
    for row in result["rows"]:
        whole = ""
        if row["deterministic_passed"] is not None:
            if row["verdict"] == "inconclusive":
                whole = "  整层 inconclusive"
            elif row["deterministic_passed"] and row["verdict"] == "like":
                whole = "  整层 like"
            else:
                whole = "  整层 not-like"
        lines.append(
            f"  {row['kind']:<10} {row['verdict']:<12} {row['reason']}{whole}  {row['file']}"
        )
    lines.append(
        "作者稿 like {}/{}（线 {}）；通用 AI 腔 not-like {}/{}".format(
            result["like"],
            result["docs"],
            result["need"],
            result["negative_not_like"],
            result["negative_count"],
        )
    )
    lines.append("判定：达标" if result["passed"] else "判定：未达标")
    return "\n".join(lines)


def _heldout(args) -> int:
    rubrics = load_rubrics(Path(args.rubrics))
    problems = validate_rubrics(rubrics, registry_ids())
    if problems:
        print("[FAIL] rubric structure: " + "; ".join(problems), file=sys.stderr)
        return 2
    if args.count < 1:
        print("[FAIL] --count must be >= 1", file=sys.stderr)
        return 2
    profile = load_profile(Path(args.profile)) if args.profile else None
    baseline = load_baseline(Path(args.baseline))
    if baseline is None:
        print("[FAIL] no judge baseline; run --build-baseline first", file=sys.stderr)
        return 2
    mismatch = check_profile_match(baseline, Path(args.profile))
    if mismatch:
        print(f"[FAIL] {mismatch}", file=sys.stderr)
        return 2

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        try:
            llm = create_llm(args.provider, args.model)
        except (ValueError, RuntimeError) as exc:
            raise JudgeError(f"setup error: {exc}") from exc
        model = getattr(llm, "model", args.model or args.provider)
        incompatible = check_baseline_compatible(baseline, rubrics, model)
        if incompatible:
            await release_llm_client(llm)
            raise JudgeError(incompatible)
        try:
            return await run_heldout(
                rubrics,
                profile,
                baseline,
                llm,
                model,
                Path(args.corpus),
                args.count,
                timeout=args.timeout,
                use_cache=not args.no_cache,
            )
        finally:
            await release_llm_client(llm)

    try:
        result = asyncio.run(_run())
    except JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 环境错误不冒充判定
        print(f"[FAIL] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(_fmt_heldout(result))
    return 0 if result["passed"] else 1


def _fmt_deterministic(det: dict | None) -> list[str]:
    return jl.format_deterministic(det)


def _fmt_report(result: dict) -> str:
    judge = result["judge"]
    lines = _fmt_deterministic(result.get("deterministic"))
    lines.append("")
    lines.append(
        "裁判层 · {}（{}）  模型 {}".format(
            judge["verdict"], judge["reason"], result.get("model") or judge.get("baseline_model")
        )
    )
    lines.append("  维  基线  本稿  证据  结论  证据原文")
    for dim in judge["dims"]:
        mark = "过" if dim["passed"] else "未过"
        lines.append(
            f"  {dim['id']}. {dim['name']:<8} {dim['baseline']:>4} {str(dim['band']):>4}  "
            f" 有  {mark:<2}  {dim.get('quote', '')[:40]}"
        )
    for dim in judge["excluded"]:
        lines.append(f"  {dim['id']}. {dim['name']:<8} 不计分（{dim['reason']}）")
    if judge["evidence_missing"]:
        lines.append("  证据对不上的维：" + "、".join(judge["evidence_missing"]))
    lines.append("")
    lines.append(f"总判定：{result['verdict']}（{result['verdict_reason']}）")
    lines.append("  （rubric 诊断模式：这个判定只作诊断，不是 skill 的放行依据）")
    return "\n".join(lines)


def _judge_one(args) -> int:
    if args.protocol == "exemplar":
        return ej.judge_file(args)
    return _judge_rubric(args)


def _judge_rubric(args) -> int:
    rubrics = load_rubrics(Path(args.rubrics))
    known = registry_ids()
    problems = validate_rubrics(rubrics, known)
    if problems:
        print("[FAIL] rubric structure: " + "; ".join(problems), file=sys.stderr)
        return 2
    text = Path(args.file).read_text(encoding="utf-8")
    profile = load_profile(Path(args.profile)) if args.profile else None
    baseline = load_baseline(Path(args.baseline)) if args.baseline else None
    if baseline is None:
        print("[FAIL] no judge baseline; run --build-baseline first", file=sys.stderr)
        return 2
    mismatch = check_profile_match(baseline, Path(args.profile))
    if mismatch:
        print(f"[FAIL] {mismatch}", file=sys.stderr)
        return 2

    # 先算确定性层：不算大模型、不花钱，坏输入在这里就拦下。
    try:
        deterministic = run_deterministic(text, profile)
    except (ValueError, KeyError) as exc:
        print(f"[FAIL] deterministic layer failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        try:
            llm = create_llm(args.provider, args.model)
        except (ValueError, RuntimeError) as exc:
            raise JudgeError(f"setup error: {exc}") from exc
        model = getattr(llm, "model", args.model or args.provider)
        incompatible = check_baseline_compatible(baseline, rubrics, model)
        if incompatible:
            await release_llm_client(llm)
            raise JudgeError(incompatible)
        try:
            return await judge_text(
                text, rubrics, profile, llm, model,
                use_cache=not args.no_cache, timeout=args.timeout,
            )
        finally:
            await release_llm_client(llm)

    try:
        judged = asyncio.run(_run())
    except JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 任何未预期异常都按环境错误，不冒充判定
        print(f"[FAIL] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    rows = judged["rows"]
    judge = evaluate(rows, baseline, rubrics)
    combined = combine(deterministic, judge)
    out = {
        "file": args.file,
        "model": judged.get("model"),
        "cached": judged.get("cached", False),
        "usage": judged.get("usage", {}),
        "deterministic": deterministic,
        "judge": judge,
        "rows": rows,
        "verdict": combined["verdict"],
        "verdict_reason": combined["reason"],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(_fmt_report(out))
    return {"like": 0, "not-like": 1}.get(combined["verdict"], 2)


def _build(args) -> int:
    rubrics = load_rubrics(Path(args.rubrics))
    known = registry_ids()
    problems = validate_rubrics(rubrics, known)
    if problems:
        print("[FAIL] rubric structure: " + "; ".join(problems), file=sys.stderr)
        return 2
    profile = load_profile(Path(args.profile)) if args.profile else None
    corpus = Path(args.corpus)

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        try:
            llm = create_llm(args.provider, args.model)
        except (ValueError, RuntimeError) as exc:
            raise JudgeError(f"setup error: {exc}") from exc
        model = getattr(llm, "model", args.model or args.provider)
        try:
            return await build_baseline(
                rubrics, profile, llm, model, corpus, args.sample, use_cache=not args.no_cache
            )
        finally:
            await release_llm_client(llm)

    if args.sample < 1:
        print("[FAIL] --sample must be >= 1", file=sys.stderr)
        return 2
    try:
        baseline = asyncio.run(_run())
    except JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    for dim_id, dim in baseline["dims"].items():
        mark = "discriminative" if dim["discriminative"] else "NOT discriminative"
        print(
            f"  {dim_id}. {dim.get('name', ''):<8} median={dim['median']} "
            f"neg={dim.get('negative_median')} gap={dim.get('gap')}  {mark}"
        )
    calibration = baseline["calibration"]
    print(
        f"calibration: {calibration['discriminative_dims']}/{len(baseline['dims'])} dims "
        f"(require {calibration['required']}) -> {'PASS' if calibration['passed'] else 'FAIL'}"
    )
    if baseline.get("failures"):
        print(f"  {len(baseline['failures'])} doc(s) failed to judge: {baseline['failures'][:3]}")
    if not calibration["passed"]:
        # 校准不过不写盘：半个基线会让后续判定全 inconclusive，还会盖掉旧基线。
        print("[FAIL] calibration failed; baseline NOT written", file=sys.stderr)
        return 1
    baseline["profile_sha256"] = profile_sha256(Path(args.profile))
    write_baseline(baseline, Path(args.baseline))
    print(f"baseline written: {args.baseline}  model={baseline['model']}")
    return 0


def _resolve_paths(args) -> None:
    """--pack 提供四个默认路径；显式参数优先；没有包就退回 panchen 默认。"""
    if args.pack:
        pack = pk.load_pack(Path(args.pack))
        args.profile = args.profile or str(pack["profile"])
        args.baseline = args.baseline or str(pack["baseline"])
        args.exemplar_baseline = args.exemplar_baseline or str(pack["exemplar"])
        args.corpus = args.corpus or str(pack["corpus"])
        return
    args.profile = args.profile or str(DEFAULT_PROFILE)
    args.baseline = args.baseline or str(DEFAULT_BASELINE)
    args.exemplar_baseline = args.exemplar_baseline or str(ej.DEFAULT_SET)
    args.corpus = args.corpus or str(DEFAULT_CORPUS)


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="大模型裁判：默认 10 篇样例同人判定；rubric 是诊断模式")
    ap.add_argument("--file", help="待判稿路径")
    ap.add_argument("--pack", default=None, help="作者包目录；profile/baseline/样例/语料从包里取")
    ap.add_argument(
        "--protocol",
        choices=("exemplar", "rubric"),
        default="exemplar",
        help="exemplar=10 篇样例同人判定（默认，参与通过判定）；rubric=21 维 band 诊断（只诊断，不放行）",
    )
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--rubrics", default=str(DEFAULT_RUBRICS))
    ap.add_argument("--profile", default=None)
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--exemplar-baseline", default=None)
    ap.add_argument("--corpus", default=None)
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--sample", type=int, default=8, help="建基线的作者样本篇数")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--build-baseline", action="store_true")
    ap.add_argument("--heldout", action="store_true", help="样本外通过率验收")
    ap.add_argument("--count", type=int, default=5, help="--heldout 抽几篇作者稿")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    try:
        _resolve_paths(args)
    except pk.PackError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if args.selftest:
        # 两个协议各有一套自检；--protocol 不再静默改变 selftest 的内容。
        rubric_rc = selftest()
        exemplar_rc = ej.selftest()
        return 0 if rubric_rc == 0 and exemplar_rc == 0 else 1
    if (args.build_baseline or args.heldout) and args.protocol != "rubric":
        print(
            "[FAIL] --build-baseline/--heldout 只属于 --protocol rubric；"
            "exemplar 评测用 python scripts/voice/exemplar_judge.py --eval",
            file=sys.stderr,
        )
        return 2
    if args.build_baseline:
        return _build(args)
    if args.heldout:
        return _heldout(args)
    if not args.file:
        ap.print_help()
        return 2
    if not Path(args.file).is_file():
        print(f"[FAIL] file not found: {args.file}", file=sys.stderr)
        return 2
    return _judge_one(args)


if __name__ == "__main__":
    raise SystemExit(main())
