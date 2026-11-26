"""Claim extraction: two LLM arms plus a deterministic validator.

Arms:
    A  deterministic candidates (sentence + semicolon) handed to the LLM,
       which merges fragments, drops non-claims, and may split further
    B  raw draft handed to the LLM, no candidate hints
    C  deterministic only (no LLM): candidates that pass structural rules

Contract (same for A and B):
    the model returns claims with a verbatim `quote` plus fields; it must NOT
    invent offsets. The validator locates each quote in the draft, rejects
    quotes that do not occur exactly, and assigns claim_id + source_spans.
    No claim is accepted without a locatable quote.

This module does not judge style or quality; it extracts claims only.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import claim_segment as cs  # noqa: E402
from . import rewrite  # noqa: E402

ENUMS = {
    "claim_type": ("Fact", "Evaluation", "Prediction", "Causal", "Value", "Experience", "Meta"),
    "epistemic_status": ("Asserted", "Speculative", "Hypothetical", "Reserved"),
}

SYSTEM = """你是 Claim 提取器。任务：从稿子里找出所有独立命题（Claim）。

一个 Claim = 可以独立判断真假的最小命题。判断标准：
- 有明确主体和命题内容，去掉上下文仍能判断真假。
- 碎片、纯引入语（「我第一眼看到的是另一个数：」）、悬空短语不算。
- 问句不算；问句的答案算。
- 归属短语（谁说、哪家媒体）不需要单独成 Claim，写进 asserted_by。

规则：
1. 可以合并碎片，可以拆开一句里的多个命题。
2. 必须给出逐字引用的原文片段 quote，不能改写、不能编造位置。
3. 找不到原文出处的内容不要输出。
4. claim_text 是规范化后的命题，不是原文。
5. 不确定的写进 notes，不要猜。

输出 JSON，不要解释文字：
{"claims":[{"quote":"逐字原文片段","claim_text":"规范化命题","claim_type":"Fact|Evaluation|Prediction|Causal|Value|Experience|Meta","asserted_by":"具体主体或 UNKNOWN","epistemic_status":"Asserted|Speculative|Hypothetical|Reserved","confidence":0.0}]}
"""


def build_messages(draft: str, candidates: list[dict] | None) -> list[dict]:
    user = f"## 稿子\n\n{draft}\n"
    if candidates:
        listed = "\n".join(f"- {c['id']}: {c['text']}" for c in candidates)
        user += f"\n## 候选片段（规则切出来的，可能含碎片，可合并、可丢弃）\n\n{listed}\n"
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse_claims(raw: str) -> list[dict]:
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
        quote = str(row.get("quote", "")).strip()
        text = str(row.get("claim_text", "")).strip()
        if not quote or not text:
            continue
        out.append({
            "quote": quote,
            "claim_text": text,
            "claim_type": row.get("claim_type") if row.get("claim_type") in ENUMS["claim_type"] else "Fact",
            "asserted_by": str(row.get("asserted_by", "UNKNOWN"))[:40] or "UNKNOWN",
            "epistemic_status": row.get("epistemic_status") if row.get("epistemic_status") in ENUMS["epistemic_status"] else "Asserted",
            "confidence": row.get("confidence"),
            "notes": str(row.get("notes", ""))[:120],
        })
    return out


def locate(draft: str, quote: str, start_hint: int = 0) -> tuple[int, int] | None:
    """Exact substring only. Prefer the first occurrence after start_hint."""
    idx = draft.find(quote, start_hint)
    if idx < 0:
        idx = draft.find(quote)
    if idx < 0:
        return None
    return idx, idx + len(quote)


def validate(draft: str, claims: list[dict]) -> tuple[list[dict], dict]:
    kept, rejected = [], []
    cursor = 0
    for c in claims:
        span = locate(draft, c["quote"], cursor)
        if span is None:
            rejected.append({**c, "reason": "quote 不在原文里"})
            continue
        cursor = span[1]
        kept.append({**c, "claim_id": f"X{len(kept) + 1}",
                     "source_spans": [{"start": span[0], "end": span[1]}]})
    # drop near-duplicates
    dedup = []
    for c in kept:
        if any(_jaccard(c["source_spans"][0], d["source_spans"][0]) >= 0.9 for d in dedup):
            continue
        dedup.append(c)
    stats = {"raw": len(claims), "kept": len(dedup), "rejected": len(rejected),
             "rejected_reasons": [r["reason"] for r in rejected]}
    return dedup, stats


def _jaccard(a: dict, b: dict) -> float:
    inter = max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    union = max(a["end"], b["end"]) - min(a["start"], b["start"])
    return inter / union if union else 0.0


def candidates_for(draft: str, mode: str = "semicolon") -> list[dict]:
    doc = cs.segment(draft, mode)
    out = []
    for i, c in enumerate(doc.claims):
        out.append({"id": f"cand-{i}", "text": c.text, "start": c.spans[0].start,
                    "end": c.spans[0].end})
    return out


def extract_arm(draft: str, arm: str, call) -> dict:
    """arm: 'A' (candidates) or 'B' (raw). Returns validated payload."""
    cands = candidates_for(draft) if arm == "A" else None
    raw = call(build_messages(draft, cands))
    claims, stats = validate(draft, parse_claims(raw))
    stats["arm"] = arm
    stats["candidates"] = len(cands) if cands else None
    stats["raw_chars"] = len(raw)
    return {"claims": claims, "stats": stats}


def extract_deterministic(draft: str, mode: str = "semicolon") -> dict:
    """arm C: candidates only, no LLM, plus structural filters."""
    doc = cs.segment(draft, mode)
    claims = []
    for c in doc.claims:
        text = c.text.strip()
        if not text or text.startswith("〔") or text.startswith("#"):
            continue
        if re.search(r"[？?]$", text):
            continue
        claims.append({"claim_id": f"X{len(claims) + 1}", "quote": text, "claim_text": text,
                       "claim_type": "Fact", "asserted_by": "UNKNOWN",
                       "epistemic_status": "Asserted", "confidence": None,
                       "source_spans": [{"start": c.spans[0].start, "end": c.spans[0].end}]})
    return {"claims": claims, "stats": {"arm": "C", "raw": len(doc.claims), "kept": len(claims),
                                        "rejected": len(doc.claims) - len(claims),
                                        "rejected_reasons": [], "candidates": len(doc.claims)}}


def llm_call(provider: str | None = None, model: str | None = None):
    return rewrite._llm_callable(provider or rewrite.DEFAULT_PROVIDER,
                                 model or rewrite.DEFAULT_MODEL, 0.0, rewrite.DEFAULT_TIMEOUT)
