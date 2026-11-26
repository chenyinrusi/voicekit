"""Direct LLM claim extraction with hard source-grounding constraints.

Primary arm (D): raw article -> LLM returns claims with verbatim `evidence_quotes`
plus attribution and classification fields. Reference arm (A): the same contract,
with deterministic sentence candidates offered as hints (kept only to compare
against the historical candidate-assisted arm).

Division of labour, enforced here and not in the prompt:
    model   semantic content (which propositions, who asserted them, what type)
    code    locate quotes verbatim, compute offsets, reject unfindable quotes,
            flag ambiguous quotes, flag missing/invalid enum values, assign ids

Code never repairs a claim. A missing field, an unknown enum value, an ambiguous
quote or an unlocatable quote all become explicit review/reject reasons.

This module does not judge factual truth and does not score style.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import claim_segment as cs  # noqa: E402

PROMPT_VERSION = "direct-v5"

ENUMS = {
    "attribution_type": ("author", "direct_quote", "indirect_quote", "institution",
                         "editorial", "unknown"),
    "claim_form": ("Fact", "Prediction", "Evaluation", "Causal", "Other"),
    "epistemic_status": ("Asserted", "Speculative", "Hypothetical", "Reserved"),
}

SYSTEM = """你是 Claim 提取器。任务：从文章里找出所有独立命题（Claim），并标出每个命题的归属与类型。

一个 Claim = 可以独立判断真假的最小命题。
- 有明确主体和命题内容，去掉上下文仍能判断真假。
- 碎片、纯引入语（「我第一眼看到的是另一个数：」）、悬空短语不算。
- 问句不算；问句的答案算；问句里夹带的预设算。
- 同一统计句里的数值与增速算一条。
- 切分粒度：不同对象、不同指标、不同人的言论各自成条，不要把多家公司或多个人的同类内容合成一条对比；
  同一对象、同一指标的数值、增速、单价、含量、克重算一条。
- 逐段扫描：每个段落里的每个独立命题都要列出来。不要因为条数多就合并，不要为了简洁少列。
  文章里同一件事在不同段落各说一次，算两条。
- 冒号或破折号后的具体化不拆，除非里面另有一个可独立判断的命题。

归属：
- 每条必须写 asserted_by：原文称呼的人名、机构名或角色名；指代不明写 UNKNOWN。
- 作者自己的叙述（没有引号、也没有「XX 表示/称/透露」）→ asserted_by 写「作者」，attribution_type 写 author。
- UNKNOWN 只用于文章本身没说清谁在说（「有分析认为」「业内人士称」且未点名）。不要拿 UNKNOWN 当「懒得标」。
- attribution_type 六选一：
  author 作者自述；direct_quote 引号内的直接引语；indirect_quote 有「XX表示/称/透露」但无引号；
  institution 机构、公司、部门发布的数据或表态；editorial 编辑部集体，仅当文中明确写出；unknown 归属不明。
- 同一句里作者叙述与引用观点并存时，拆成两条，分别写归属。
- 引语里的「我」是被引者，不是作者。
- 不要从署名、公众号或平台信息推断谁在说话。

分类：
- claim_form 五选一：Fact 已发生或已存在的事实（数据、事件、言论事实、亲身经历）；
  Prediction 关于未来或未定结果的断言；Evaluation 好坏、价值、意义、重要性的判断；
  Causal 明确给出原因与结果的关系；Other 定义、分类、类比或关于报道自身的元陈述（必须写 other_kind）。
- epistemic_status 四选一：Asserted 当作确定；Speculative 推测；Hypothetical 假设或条件；
  Reserved 明确保留或存疑。
- 字段不要混用：Speculative、Hypothetical、Reserved 只能写进 epistemic_status，不能写进 claim_form。
  例：带推测语气的预测 → {"claim_form":"Prediction","epistemic_status":"Speculative"}，不是 {"claim_form":"Speculative"}。

证据：
- 每条必须给 evidence_quotes：一段或多段逐字原文，不改写、不省略、不加字。
- 只输出原文里逐字存在的内容。找不到原文的命题不要输出。
- claim_text 是规范化后的命题，不是原文。
- 不要判断文章说的对不对。文章说错也是文章的主张。
- 不确定就写 notes，不要猜。

输出 JSON，不要解释文字：
{"claims":[{"claim_text":"规范化命题","evidence_quotes":["逐字原文片段"],"asserted_by":"主体或 UNKNOWN","attribution_type":"author|direct_quote|indirect_quote|institution|editorial|unknown","claim_form":"Fact|Prediction|Evaluation|Causal|Other","other_kind":"","epistemic_status":"Asserted|Speculative|Hypothetical|Reserved","notes":""}]}
"""


def prompt_sha256() -> str:
    return hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest()


def build_messages(article: str, candidates: list[dict] | None = None,
                   part: tuple[int, int] | None = None) -> list[dict]:
    head = "## 文章\n\n"
    if part:
        head = f"## 文章（第 {part[0]}/{part[1]} 部分，只标这一部分）\n\n"
    user = f"{head}{article}\n"
    if candidates:
        listed = "\n".join(f"- {c['id']}: {c['text']}" for c in candidates)
        user += f"\n## 候选片段（规则切出来的，可能含碎片，可合并、可丢弃）\n\n{listed}\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


# ── parse ────────────────────────────────────────────────────

def _clean_quotes(row: dict) -> list[str]:
    raw = row.get("evidence_quotes")
    if isinstance(raw, str):
        raw = [raw]
    out: list[str] = []
    for q in raw or []:
        if not isinstance(q, str):
            continue
        q = q.strip()
        if q and q not in out:
            out.append(q)
    if not out and isinstance(row.get("quote"), str) and row["quote"].strip():
        out = [row["quote"].strip()]
    return out


def parse_claims(raw: str) -> list[dict]:
    """Parse the model reply. Never fills in missing fields; keeps what it got."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for row in data.get("claims", []) or []:
        if not isinstance(row, dict):
            continue
        text = str(row.get("claim_text") or "").strip()
        quotes = _clean_quotes(row)
        if not text and not quotes:
            continue
        out.append({
            "claim_text": text,
            "evidence_quotes": quotes,
            "asserted_by": str(row.get("asserted_by") or "").strip(),
            "attribution_type": str(row.get("attribution_type") or "").strip(),
            "claim_form": str(row.get("claim_form") or "").strip(),
            "other_kind": str(row.get("other_kind") or "").strip(),
            "epistemic_status": str(row.get("epistemic_status") or "").strip(),
            "notes": str(row.get("notes") or "")[:200],
        })
    return out


# ── locate ───────────────────────────────────────────────────

def occurrences(draft: str, quote: str) -> list[int]:
    """All start offsets of an exact substring match."""
    out, start = [], 0
    while True:
        idx = draft.find(quote, start)
        if idx < 0:
            return out
        out.append(idx)
        start = idx + 1


def span_for(draft: str, quote: str) -> dict:
    """Locate one quote. Returns {status, spans, candidates}.

    status: ok | missing | ambiguous
    """
    hits = occurrences(draft, quote)
    if not hits:
        return {"status": "missing", "spans": [], "candidates": []}
    if len(hits) > 1:
        return {"status": "ambiguous", "spans": [],
                "candidates": [{"start": h, "end": h + len(quote)} for h in hits]}
    s = hits[0]
    return {"status": "ok", "spans": [{"start": s, "end": s + len(quote)}], "candidates": []}


def _field_errors(c: dict) -> list[str]:
    errs = []
    if not c["claim_text"]:
        errs.append("claim_text 缺失")
    if not c["evidence_quotes"]:
        errs.append("evidence_quotes 缺失")
    if not c["asserted_by"]:
        errs.append("asserted_by 缺失")
    for field in ("attribution_type", "claim_form", "epistemic_status"):
        val = c[field]
        if not val:
            errs.append(f"{field} 缺失")
        elif val not in ENUMS[field]:
            errs.append(f"{field} 非法值 {val!r}")
    if c["claim_form"] == "Other" and not c["other_kind"]:
        errs.append("Other 缺 other_kind")
    return errs


def validate(draft: str, claims: list[dict]) -> tuple[list[dict], dict]:
    """Assign ids and spans. Rejects unlocatable claims; flags the rest."""
    kept, rejected = [], []
    for c in claims:
        errs = _field_errors(c)
        locs = [span_for(draft, q) for q in c["evidence_quotes"]]
        missing = [q for q, loc in zip(c["evidence_quotes"], locs, strict=False) if loc["status"] == "missing"]
        ambiguous = [loc for loc in locs if loc["status"] == "ambiguous"]

        if missing:
            rejected.append({**c, "reason": "quote 不在原文里", "missing_quotes": missing})
            continue
        review = list(errs)
        if ambiguous:
            review.append(f"{len(ambiguous)} 段 quote 重复出现，位置不唯一")

        spans = [s for loc in locs for s in loc["spans"]]
        uniq: list[dict] = []
        for s in sorted(spans, key=lambda x: x["start"]):
            if uniq and s["start"] <= uniq[-1]["end"]:
                uniq[-1]["end"] = max(uniq[-1]["end"], s["end"])
            else:
                uniq.append(dict(s))
        kept.append({**c,
                     "claim_id": f"C{len(kept) + 1}",
                     "source_spans": uniq,
                     "ambiguity_candidates": [s for loc in ambiguous for s in loc["candidates"]],
                     "status": "REVIEW" if review else "VALID",
                     "review_reasons": review})

    dedup = []
    for c in kept:
        if any(_overlap_ratio(c["source_spans"], d["source_spans"]) >= 0.9 for d in dedup):
            rejected.append({**c, "reason": "与已收 Claim 重复（span 重叠 ≥0.9）"})
            continue
        dedup.append(c)
    for i, c in enumerate(dedup):
        c["claim_id"] = f"C{i + 1}"

    stats = {
        "raw": len(claims),
        "kept": len(dedup),
        "valid": sum(1 for c in dedup if c["status"] == "VALID"),
        "review": sum(1 for c in dedup if c["status"] == "REVIEW"),
        "rejected": len(rejected),
        "rejected_reasons": [r["reason"] for r in rejected],
    }
    return dedup, {"stats": stats, "rejected": rejected}


def _covered(spans: list[dict]) -> int:
    return sum(s["end"] - s["start"] for s in spans)


def _overlap_ratio(a: list[dict], b: list[dict]) -> float:
    inter = 0
    for x in a:
        for y in b:
            inter += max(0, min(x["end"], y["end"]) - max(x["start"], y["start"]))
    union = _covered(a) + _covered(b) - inter
    return inter / union if union else 0.0


# ── extract ──────────────────────────────────────────────────

def candidates_for(article: str, mode: str = "semicolon") -> list[dict]:
    doc = cs.segment(article, mode)
    return [{"id": f"cand-{i}", "text": c.text,
             "start": c.spans[0].start, "end": c.spans[0].end}
            for i, c in enumerate(doc.claims)]


def split_chunks(article: str, chunk_chars: int = 1200) -> list[str]:
    """Split at paragraph breaks so each call stays inside one output budget."""
    paras = [p for p in article.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        if cur and len(cur) + len(p) > chunk_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


def extract(article: str, call, arm: str = "D", chunk_chars: int | None = None) -> dict:
    """arm D: raw article. arm A: with deterministic candidates as hints.

    chunk_chars set -> extract the article in paragraph windows and merge.
    """
    if arm == "A":
        cands = candidates_for(article)
        raw_text, usage, seconds = _timed_call(call, build_messages(article, cands))
        calls = 1
        parsed = parse_claims(raw_text)
    elif chunk_chars:
        chunks = split_chunks(article, chunk_chars)
        parsed, raws, usages, secs = [], [], [], []
        for i, ch in enumerate(chunks, 1):
            raw, usage, sec = _timed_call(call, build_messages(ch, None, part=(i, len(chunks))))
            raws.append(raw)
            usages.append(usage)
            secs.append(sec)
            parsed += parse_claims(raw)
        raw_text = "\n".join(raws)
        usage = {k: sum(u.get(k, 0) for u in usages) for k in
                 {k for u in usages for k in u}}
        seconds = sum(secs)
        calls = len(chunks)
    else:
        raw_text, usage, seconds = _timed_call(call, build_messages(article))
        calls = 1
        parsed = parse_claims(raw_text)

    claims, extra = validate(article, parsed)
    return {
        "arm": arm,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256(),
        "claims": claims,
        "rejected": extra["rejected"],
        "stats": {**extra["stats"], "arm": arm, "calls": calls,
                  "chunk_chars": chunk_chars,
                  "candidates": len(candidates_for(article)) if arm == "A" else None,
                  "raw_chars": len(raw_text),
                  "usage": usage, "seconds": round(seconds, 2)},
        "raw_output": raw_text,
    }


def _timed_call(call, messages):
    import time
    t0 = time.time()
    result = call(messages)
    seconds = time.time() - t0
    if isinstance(result, tuple):
        text, usage = result
        return text, usage, seconds
    return result, {}, seconds


def make_call(provider: str | None = None, model: str | None = None):
    """LLM call that returns (text, usage). Mirrors rewrite._llm_callable."""
    import asyncio
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from ..compat import release_llm_client
    from ..compat import create_llm, ensure_provider_sdks_loaded

    async def _run(messages: list[dict]):
        from ..compat import Message
        await ensure_provider_sdks_loaded()
        llm = create_llm(provider or rewrite.DEFAULT_PROVIDER, model or rewrite.DEFAULT_MODEL)
        built = []
        for message in messages:
            role, content = message["role"], message["content"]
            if role == "system":
                built.append(Message.system(content))
            elif role == "assistant":
                built.append(Message.assistant(content))
            else:
                built.append(Message.user(content))
        try:
            response = await llm.chat(built, temperature=0.0)
            return response.content or "", dict(response.usage or {})
        finally:
            await release_llm_client(llm)

    return lambda messages: asyncio.run(_run(messages))


# ── selftest ─────────────────────────────────────────────────

def _selftest() -> int:
    problems: list[str] = []
    article = (
        "11月5日晚，董宇辉首发自有品牌蛋糕。\n\n"
        "该款蛋糕保质期标注为9个月。有网友质疑，一块奶油蛋糕能放9个月。\n\n"
        "11月10日，董宇辉回应：「冷冻环境下保质期变长是常识」。\n\n"
        "该款蛋糕保质期标注为9个月。"
    )

    def check(name, cond):
        if not cond:
            problems.append(name)

    # locate: unique / repeated / missing
    check("唯一 quote 定位失败", span_for(article, "董宇辉首发自有品牌蛋糕")["status"] == "ok")
    amb = span_for(article, "该款蛋糕保质期标注为9个月。")
    check("重复 quote 没被标 ambiguous", amb["status"] == "ambiguous" and len(amb["candidates"]) == 2)
    check("不存在的 quote 没被标 missing", span_for(article, "根本不存在的句子")["status"] == "missing")

    # validate: unlocatable rejected, duplicate handled, offsets point at text
    kept, extra = validate(article, [{
        "claim_text": "董宇辉在11月5日发布自营蛋糕",
        "evidence_quotes": ["11月5日晚，董宇辉首发自有品牌蛋糕"],
        "asserted_by": "作者", "attribution_type": "author",
        "claim_form": "Fact", "other_kind": "", "epistemic_status": "Asserted", "notes": ""}])
    check("合法 Claim 没被收", len(kept) == 1 and kept[0]["status"] == "VALID")
    span = kept[0]["source_spans"][0]
    check("offset 不对", article[span["start"]:span["end"]] == "11月5日晚，董宇辉首发自有品牌蛋糕")

    bad_enum = [{
        "claim_text": "x", "evidence_quotes": ["11月5日晚，董宇辉首发自有品牌蛋糕"],
        "asserted_by": "作者", "attribution_type": "writer",  # invalid
        "claim_form": "Fact", "other_kind": "", "epistemic_status": "Asserted", "notes": ""}]
    kept2, _ = validate(article, bad_enum)
    check("非法枚举没被标记为 REVIEW", kept2 and kept2[0]["status"] == "REVIEW")
    check("非法枚举被静默改写", kept2 and kept2[0]["attribution_type"] == "writer")

    missing_quote = [{
        "claim_text": "x", "evidence_quotes": ["原文里没有这句话"],
        "asserted_by": "作者", "attribution_type": "author",
        "claim_form": "Fact", "other_kind": "", "epistemic_status": "Asserted", "notes": ""}]
    kept3, extra3 = validate(article, missing_quote)
    check("找不到原文时没拒收", not kept3 and extra3["stats"]["rejected"] == 1)

    # multi-span merge
    multi = [{
        "claim_text": "董宇辉回应保质期", "evidence_quotes": ["董宇辉回应", "冷冻环境下保质期变长是常识"],
        "asserted_by": "董宇辉", "attribution_type": "direct_quote",
        "claim_form": "Fact", "other_kind": "", "epistemic_status": "Asserted", "notes": ""}]
    kept4, _ = validate(article, multi)
    check("多段证据没合并成一条 Claim", len(kept4) == 1 and len(kept4[0]["source_spans"]) >= 1)

    check("prompt hash 长度不对", len(prompt_sha256()) == 64)
    if problems:
        print("SELFTEST FAILED:", problems)
        return 1
    print("selftest ok")
    return 0


REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from . import rewrite  # noqa: E402

if __name__ == "__main__":
    import argparse
    import json as _json

    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--prompt-sha", action="store_true")
    ap.add_argument("--article")
    ap.add_argument("--out")
    ap.add_argument("--arm", default="D", choices=["D", "A"])
    ap.add_argument("--chunk", type=int, default=None,
                    help="按段落窗口分块提取，值为每块字符上限")
    ap.add_argument("--provider", default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(_selftest())
    if args.prompt_sha:
        print(prompt_sha256())
        raise SystemExit(0)
    if not args.article:
        print("usage: claim_extract_v2.py --article FILE --out FILE [--arm D|A]")
        raise SystemExit(2)

    text = Path(args.article).read_text(encoding="utf-8")
    result = extract(text, make_call(args.provider, args.model), arm=args.arm, chunk_chars=args.chunk)
    payload = _json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"{args.out}: {result['stats']}")
    else:
        print(payload)


