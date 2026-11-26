"""Two-track evaluation of claim extraction against frozen ground truth.

Track A (extraction): finding the right propositions, with boundaries.
Track B (attributes): attribution and classification, scored only on matches.

The two tracks are never merged into one score. Matching keeps one-to-many and
many-to-one relations instead of forcing a one-to-one fit:

    match threshold      character-interval Jaccard >= 0.5, greedy one-to-one
    split / merge scan   any overlap (Jaccard >= 0.2) is counted, matched or not

Inputs are plain dicts, so this module works for GT built by hand and for model
output. Spans are character offsets into the same article text.
"""

from __future__ import annotations

import json
from pathlib import Path

MATCH_THR = 0.5
OVERLAP_MIN = 0.2


def _inter(a: dict, b: dict) -> int:
    return max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def _covered(spans: list[dict]) -> int:
    return sum(s["end"] - s["start"] for s in spans)


def jaccard(a: list[dict], b: list[dict]) -> float:
    if not a or not b:
        return 0.0
    inter = sum(_inter(x, y) for x in a for y in b)
    if not inter:
        return 0.0
    return inter / (_covered(a) + _covered(b) - inter)


def spans_text(text: str, spans: list[dict]) -> str:
    return "".join(text[s["start"]:s["end"]] for s in sorted(spans, key=lambda x: x["start"]))


def match(gt: list[dict], pred: list[dict]) -> dict:
    """Greedy one-to-one matching above MATCH_THR; keeps the rest as groups."""
    scored = []
    for i, g in enumerate(gt):
        for j, p in enumerate(pred):
            score = jaccard(g["source_spans"], p["source_spans"])
            if score >= MATCH_THR:
                scored.append((score, i, j))
    scored.sort(reverse=True)
    used_g, used_p, pairs = set(), set(), []
    for score, i, j in scored:
        if i in used_g or j in used_p:
            continue
        used_g.add(i)
        used_p.add(j)
        pairs.append({"gt": i, "pred": j, "score": round(score, 3)})

    groups = {i: [] for i in range(len(gt))}
    pred_groups = {j: [] for j in range(len(pred))}
    for i, g in enumerate(gt):
        for j, p in enumerate(pred):
            if jaccard(g["source_spans"], p["source_spans"]) >= OVERLAP_MIN:
                groups[i].append(j)
                pred_groups[j].append(i)

    return {"pairs": pairs, "gt_groups": groups, "pred_groups": pred_groups,
            "unmatched_gt": [i for i in range(len(gt)) if i not in used_g],
            "unmatched_pred": [j for j in range(len(pred)) if j not in used_p]}


def _acc(pairs: list[tuple[str, str]]) -> dict:
    n = len(pairs)
    hits = sum(1 for a, b in pairs if a == b)
    return {"n": n, "hits": hits, "acc": round(hits / n, 3) if n else None}


def _confusion(pairs: list[tuple[str, str]]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for a, b in pairs:
        out.setdefault(a, {}).setdefault(b, 0)
        out[a][b] += 1
    return out


def evaluate(gt: list[dict], pred: list[dict], text: str) -> dict:
    m = match(gt, pred)
    tp = len(m["pairs"])
    n_gt, n_pred = len(gt), len(pred)
    precision = tp / n_pred if n_pred else 0.0
    recall = tp / n_gt if n_gt else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    matched = [(gt[p["gt"]], pred[p["pred"]]) for p in m["pairs"]]
    dice = [2 * _inter_span(g, p) / (_covered(g["source_spans"]) + _covered(p["source_spans"]))
            for g, p in matched if _covered(g["source_spans"]) + _covered(p["source_spans"])]
    exact = sum(1 for g, p in matched
                if spans_text(text, g["source_spans"]) == spans_text(text, p["source_spans"]))

    splits = [i for i, js in m["gt_groups"].items() if len(js) >= 2]
    merges = [j for j, is_ in m["pred_groups"].items() if len(is_) >= 2]

    # granularity-tolerant view: how much of each GT span any single prediction covers
    coverage = []
    for g in gt:
        best = 0.0
        total = _covered(g["source_spans"])
        for p in pred:
            if not total:
                continue
            best = max(best, sum(_inter(x, y) for x in g["source_spans"] for y in p["source_spans"]) / total)
        coverage.append(best)

    track_a = {
        "gt_n": n_gt, "pred_n": n_pred, "matched": tp,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "omission_rate": round(len(m["unmatched_gt"]) / n_gt, 3) if n_gt else None,
        "unsupported_rate": round(len(m["unmatched_pred"]) / n_pred, 3) if n_pred else None,
        "split_n": len(splits), "split_rate": round(len(splits) / n_gt, 3) if n_gt else None,
        "merge_n": len(merges), "merge_rate": round(len(merges) / n_gt, 3) if n_gt else None,
        "span_dice_mean": round(sum(dice) / len(dice), 3) if dice else None,
        "exact_span_n": exact, "exact_span_rate": round(exact / tp, 3) if tp else None,
        "gt_coverage_mean": round(sum(coverage) / len(coverage), 3) if coverage else None,
        "gt_coverage_half": round(sum(1 for c in coverage if c >= 0.5) / len(coverage), 3) if coverage else None,
        "unmatched_gt_ids": [gt[i].get("claim_id") for i in m["unmatched_gt"]],
        "unmatched_pred_ids": [pred[j].get("claim_id") for j in m["unmatched_pred"]],
    }

    attr = _acc([(g.get("attribution_type", ""), p.get("attribution_type", "")) for g, p in matched])
    status_acc = _acc([(g.get("attribution_status", ""), p.get("attribution_status", "")) for g, p in matched])
    ir_pairs = [(g.get("identity_reference") or {}, p.get("identity_reference") or {}) for g, p in matched
                if (g.get("identity_reference") or {}).get("canonical")]
    ir_hits = sum(1 for gi, pi in ir_pairs if _who_loose(gi.get("canonical", ""), pi.get("canonical", "")))
    wrong_links = sum(1 for gi, pi in ir_pairs
                      if pi.get("canonical") and not _who_loose(gi.get("canonical", ""), pi.get("canonical", "")))
    form = _acc([(g.get("claim_form", ""), p.get("claim_form", "")) for g, p in matched])
    status = _acc([(g.get("epistemic_status", ""), p.get("epistemic_status", "")) for g, p in matched])
    who = _acc([(g.get("asserted_by", ""), p.get("asserted_by", "")) for g, p in matched])
    who_loose_n = sum(1 for g, p in matched if _who_loose(g.get("asserted_by", ""), p.get("asserted_by", "")))
    unknown_pairs = [(g, p) for g, p in matched if g.get("asserted_by") == "UNKNOWN"]
    unknown_handled = sum(1 for g, p in unknown_pairs if p.get("asserted_by") == "UNKNOWN")
    review = sum(1 for p in pred if p.get("status") == "REVIEW")

    track_b = {
        "matched_n": tp,
        "attribution_type": attr, "claim_form": form,
        "attribution_status": status_acc,
        "identity_canonical": {"n": len(ir_pairs), "hits": ir_hits, "wrong_links": wrong_links,
                               "acc": round(ir_hits / len(ir_pairs), 3) if ir_pairs else None},
        "epistemic_status": status, "asserted_by_exact": who,
        "unknown_gt_n": len(unknown_pairs),
        "unknown_pred_n": sum(1 for p in pred if p.get("asserted_by") == "UNKNOWN"),
        "unknown_handled": unknown_handled,
        "review_n": review,
        "asserted_by_loose": {"n": len(matched), "hits": who_loose_n,
                              "acc": round(who_loose_n / len(matched), 3) if matched else None},
        "confusion_attribution": _confusion(
            [(g.get("attribution_type", ""), p.get("attribution_type", "")) for g, p in matched]),
        "confusion_form": _confusion(
            [(g.get("claim_form", ""), p.get("claim_form", "")) for g, p in matched]),
    }
    return {"track_a": track_a, "track_b": track_b,
            "pairs": m["pairs"], "split_ids": [gt[i].get("claim_id") for i in splits],
            "merge_ids": [pred[j].get("claim_id") for j in merges]}


def _who_loose(a: str, b: str) -> bool:
    """One asserted_by contains the other: '高盛分析师Schneider' vs 'Schneider'."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return a == b
    return a in b or b in a


def _inter_span(g: dict, p: dict) -> int:
    return sum(_inter(x, y) for x in g["source_spans"] for y in p["source_spans"])


def aggregate(results: dict[str, dict]) -> dict:
    """Micro-aggregate across articles, plus macro means, plus per-article table."""
    a = {k: sum(r["track_a"][k] for r in results.values())
         for k in ("gt_n", "pred_n", "matched", "split_n", "merge_n", "exact_span_n")}
    precision = a["matched"] / a["pred_n"] if a["pred_n"] else 0.0
    recall = a["matched"] / a["gt_n"] if a["gt_n"] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    micro_a = {
        **a,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "omission_rate": round((a["gt_n"] - a["matched"]) / a["gt_n"], 3) if a["gt_n"] else None,
        "unsupported_rate": round((a["pred_n"] - a["matched"]) / a["pred_n"], 3) if a["pred_n"] else None,
        "split_rate": round(a["split_n"] / a["gt_n"], 3) if a["gt_n"] else None,
        "merge_rate": round(a["merge_n"] / a["gt_n"], 3) if a["gt_n"] else None,
        "exact_span_rate": round(a["exact_span_n"] / a["matched"], 3) if a["matched"] else None,
        "span_dice_mean": _mean([r["track_a"]["span_dice_mean"] for r in results.values()]),
        "per_article_f1": {k: r["track_a"]["f1"] for k, r in results.items()},
        "per_article_recall": {k: r["track_a"]["recall"] for k, r in results.items()},
        "per_article_precision": {k: r["track_a"]["precision"] for k, r in results.items()},
    }

    def collect(field: str) -> dict:
        n = sum(r["track_b"][field]["n"] for r in results.values())
        hits = sum(r["track_b"][field]["hits"] for r in results.values())
        return {"n": n, "hits": hits, "acc": round(hits / n, 3) if n else None}

    micro_b = {
        "matched_n": a["matched"],
        "attribution_type": collect("attribution_type"),
        "claim_form": collect("claim_form"),
        "attribution_status": collect("attribution_status"),
        "identity_canonical": {
            "n": sum(r["track_b"]["identity_canonical"]["n"] for r in results.values()),
            "hits": sum(r["track_b"]["identity_canonical"]["hits"] for r in results.values()),
            "wrong_links": sum(r["track_b"]["identity_canonical"]["wrong_links"] for r in results.values()),
        },
        "epistemic_status": collect("epistemic_status"),
        "asserted_by_exact": collect("asserted_by_exact"),
        "asserted_by_loose": collect("asserted_by_loose"),
        "unknown_gt_n": sum(r["track_b"]["unknown_gt_n"] for r in results.values()),
        "unknown_handled": sum(r["track_b"]["unknown_handled"] for r in results.values()),
        "review_n": sum(r["track_b"]["review_n"] for r in results.values()),
    }
    return {"track_a": micro_a, "track_b": micro_b,
            "per_article": {k: {"track_a": r["track_a"], "track_b": r["track_b"]}
                            for k, r in results.items()}}


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 3) if vals else None


# ── GT builder ───────────────────────────────────────────────

def locate_all(article: str, quote: str) -> list[int]:
    out, start = [], 0
    while True:
        idx = article.find(quote, start)
        if idx < 0:
            return out
        out.append(idx)
        start = idx + 1


def build_gt(article: str, annotated: list[dict], article_id: str) -> tuple[dict, list[dict]]:
    """Turn annotator output (verbatim quotes) into spans. Flags bad quotes."""
    claims, problems = [], []
    for row in annotated:
        quotes = row.get("evidence_quotes") or row.get("evidence_quote") or []
        if isinstance(quotes, str):
            quotes = [quotes]
        spans = []
        for q in quotes:
            hits = locate_all(article, q)
            if len(hits) != 1:
                problems.append({"claim_ref": row.get("claim_text", "")[:40],
                                 "quote": q[:60], "hits": len(hits),
                                 "reason": "quote 不存在" if not hits else "quote 重复出现"})
                continue
            spans.append({"start": hits[0], "end": hits[0] + len(q)})
        if not spans:
            continue
        claims.append({**row, "claim_id": row.get("claim_id") or f"G{len(claims) + 1}",
                       "source_spans": spans, "evidence_text": "".join(
                           article[s["start"]:s["end"]] for s in spans)})
    return {"article_id": article_id, "claims": claims}, problems


# ── selftest ─────────────────────────────────────────────────

def _selftest() -> int:
    problems: list[str] = []
    text = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def claim(cid, start, end, who="作者", attr="author", form="Fact", status="Asserted"):
        return {"claim_id": cid, "source_spans": [{"start": start, "end": end}],
                "asserted_by": who, "attribution_type": attr, "claim_form": form,
                "epistemic_status": status}

    gt = [claim("G1", 0, 10), claim("G2", 10, 20)]
    perfect = evaluate(gt, [claim("P1", 0, 10), claim("P2", 10, 20)], text)
    if (perfect["track_a"]["precision"], perfect["track_a"]["recall"]) != (1.0, 1.0):
        problems.append("完全匹配没算出 1.0")
    if perfect["track_b"]["attribution_type"]["acc"] != 1.0:
        problems.append("属性一致时准确率不是 1.0")

    missing = evaluate(gt, [claim("P1", 0, 10)], text)
    if missing["track_a"]["recall"] != 0.5 or missing["track_a"]["omission_rate"] != 0.5:
        problems.append("漏一条时 recall/omission 不对")

    extra = evaluate(gt, [claim("P1", 0, 10), claim("P2", 10, 20), claim("P3", 25, 30)], text)
    if extra["track_a"]["precision"] != round(2 / 3, 3):
        problems.append(f"多一条时 precision 不对：{extra['track_a']['precision']}")

    split = evaluate([claim("G1", 0, 20)], [claim("P1", 0, 10), claim("P2", 10, 20)], text)
    if split["track_a"]["split_n"] != 1 or split["track_a"]["matched"] != 1:
        problems.append("拆分没被识别")

    merge = evaluate([claim("G1", 0, 10), claim("G2", 10, 20)], [claim("P1", 0, 20)], text)
    if merge["track_a"]["merge_n"] != 1:
        problems.append("合并没被识别")

    wrong_attr = evaluate([claim("G1", 0, 10)], [claim("P1", 0, 10, attr="direct_quote")], text)
    if wrong_attr["track_b"]["attribution_type"]["acc"] != 0.0:
        problems.append("属性错误没被记成 0")

    article = "甲说：“乙会来。”丙表示，明天会下雨。"
    built, bad = build_gt(article, [
        {"claim_text": "乙会来", "evidence_quotes": ["乙会来"],
         "asserted_by": "甲", "attribution_type": "direct_quote"},
        {"claim_text": "不存在", "evidence_quotes": ["不存在的话"],
         "asserted_by": "丙", "attribution_type": "indirect_quote"},
    ], "t")
    if len(bad) != 1 or len(built["claims"]) != 1:
        problems.append("GT 构建没拒收错误 quote")
    if built["claims"][0]["source_spans"][0] != {"start": 4, "end": 7}:
        problems.append(f"GT 构建偏移不对：{built['claims'][0]['source_spans']}")

    if problems:
        print("EVAL SELFTEST FAILED:", problems)
        return 1
    print("eval selftest ok")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(_selftest())
    print("usage: claim_eval_v2.py --selftest")
