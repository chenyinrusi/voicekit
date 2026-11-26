"""裁判层 band 校准：用人工「像/不像」标签找每维的最低可接受 band。

只读输入：
- `--bands`：`tmp/capture_bands.py` 抓的逐篇 21 维 band（不含正文）。
- `--labels`：盲标导出的 `labels.jsonl`（source/class/label/note）。

输出一套 proposal（每维 floor、通过比例、建议剔除的维），不改 `rubrics.yaml` 或基线；
人看过再决定要不要写进 `judge_baseline.json`。

判据与 `judge_lib.evaluate()` 对齐：每维「band >= floor」算过；可计分维的通过数
>= ceil(维数 × pass_ratio) 且关键维全过 = like。缺维/证据坏的维按现行规则剔除。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from . import voice_lib as lib  # noqa: E402

BANDS = (0, 1, 2, 3, 4)
DEFAULT_RATIO = 0.75
DEFAULT_TOLERANCE = 1


def load_labels(path: Path) -> dict[str, str]:
    labels: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("label") in ("like", "not_like"):
            labels[row["source"]] = row["label"]
    return labels


def load_bands(path: Path) -> dict[str, dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("docs") or {}


def _auc(pos: list[int], neg: list[int]) -> float:
    """band 当作序数；作者（正类）随机一篇的 band 高于别人（负类）的概率。"""
    if not pos or not neg:
        return 0.5
    wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
    return wins / (len(pos) * len(neg))


def dim_rows(bands: dict[str, dict], labels: dict[str, str], rubrics: dict,
             baseline: dict) -> list[dict]:
    base_dims = (baseline or {}).get("dims") or {}
    rows = []
    for dim in rubrics["dims"]:
        did = str(dim["id"])
        pos, neg = [], []
        for source, label in labels.items():
            doc = bands.get(source)
            if not doc or did not in doc.get("bands", {}):
                continue
            (pos if label == "like" else neg).append(int(doc["bands"][did]))
        base = base_dims.get(did) or {}
        target = base.get("median")
        rows.append({
            "id": did,
            "name": dim["name"],
            "author_n": len(pos),
            "other_n": len(neg),
            "author_bands": pos,
            "other_bands": neg,
            "author_median": _median(pos),
            "other_median": _median(neg),
            "auc": round(_auc(pos, neg), 3),
            "baseline_median": target,
            "discriminative": bool(base.get("discriminative", True)),
        })
    return rows


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    return float(ordered[mid]) if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def floor_choice(row: dict, min_author_pass: float = 0.9) -> int:
    """在作者稿通过率不低于 min_author_pass 的前提下，取使 Youden's J 最大的 floor。"""
    pos, neg = row["author_bands"], row["other_bands"]
    best_floor, best_j = 0, -2.0
    for floor in BANDS:
        if not pos:
            continue
        tpr = sum(1 for b in pos if b >= floor) / len(pos)
        if tpr + 1e-9 < min_author_pass:
            continue
        fpr = sum(1 for b in neg if b >= floor) / len(neg) if neg else 0.0
        youden = tpr - fpr
        if youden > best_j + 1e-9 or (abs(youden - best_j) <= 1e-9 and floor > best_floor):
            best_floor, best_j = floor, youden
    return best_floor


def decide(doc: dict, floors: dict[str, int], pass_ratio: float, key_dims: list[str],
           baseline_dims: dict) -> dict:
    scored, failed, key_failed = 0, 0, []
    for did, floor in floors.items():
        base = baseline_dims.get(did) or {}
        if not base.get("discriminative", True):
            continue
        band = doc.get("bands", {}).get(did)
        if band is None:
            continue
        scored += 1
        if int(band) < floor:
            failed += 1
            if did in key_dims:
                key_failed.append(did)
    required = max(1, math.ceil(scored * pass_ratio)) if scored else 1
    if scored < max(1, math.ceil(len(floors) * 0.6)):
        return {"verdict": "inconclusive", "reason": f"only {scored} dims scored"}
    if key_failed:
        return {"verdict": "not-like", "reason": "key failed: " + ",".join(key_failed)}
    passed = scored - failed
    if passed < required:
        return {"verdict": "not-like", "reason": f"passed {passed}/{scored}"}
    return {"verdict": "like", "reason": f"passed {passed}/{scored}"}


def simulate(bands: dict[str, dict], labels: dict[str, str], floors: dict[str, int],
             ratio: float, key_dims: list[str], baseline_dims: dict) -> dict:
    counts = {"author": {"like": 0, "n": 0}, "other": {"like": 0, "n": 0}}
    for source, label in labels.items():
        doc = bands.get(source)
        if not doc:
            continue
        cls = "author" if label == "like" else "other"
        counts[cls]["n"] += 1
        if decide(doc, floors, ratio, key_dims, baseline_dims)["verdict"] == "like":
            counts[cls]["like"] += 1
    author = counts["author"]
    other = counts["other"]
    tpr = author["like"] / author["n"] if author["n"] else 0.0
    fpr = other["like"] / other["n"] if other["n"] else 0.0
    return {
        "author_like": author["like"], "author_n": author["n"],
        "other_like": other["like"], "other_n": other["n"],
        "tpr": round(tpr, 3), "fpr": round(fpr, 3),
        "youden": round(tpr - fpr, 3),
        "other_upper_95": round(lib.false_accept_upper_bound(other["like"], other["n"]), 3)
        if other["n"] else None,
    }


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台是 cp936：argparse 的 --help 里有 cp936 编不出来的字符（’这个），
    # 不锁 UTF-8 就 UnicodeEncodeError，测试里 `--help` 直接崩。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="用人工标签校准裁判层 band floor")
    ap.add_argument("--bands", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--rubrics", type=Path,
                    default=Path(__file__).resolve().parent / "rubrics.yaml")
    ap.add_argument("--baseline", type=Path,
                    default=Path(__file__).resolve().parent.parent.parent /
                    "voices/panchen-voice/judge_baseline.json")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    from judge_lib import load_rubrics  # noqa: WPS433 - 延迟导入，测试可替换

    rubrics = load_rubrics(args.rubrics)
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    labels = load_labels(args.labels)
    bands = load_bands(args.bands)
    rows = dim_rows(bands, labels, rubrics, baseline)
    key_dims = [str(k) for k in rubrics["key_dims"]]
    base_dims = baseline.get("dims") or {}

    current = {row["id"]: max(0, int(round(row["baseline_median"] or 0)) - DEFAULT_TOLERANCE)
               for row in rows if row["baseline_median"] is not None}
    strict = {row["id"]: int(round(row["baseline_median"] or 0))
              for row in rows if row["baseline_median"] is not None}
    calibrated = {row["id"]: floor_choice(row) for row in rows if row["baseline_median"] is not None}
    kept = {did: floor for did, floor in calibrated.items()
            if (next(r for r in rows if r["id"] == did)["auc"] or 0) > 0.5}

    variants = {
        "V0 现行（baseline-1，0.75）": (current, DEFAULT_RATIO),
        "V1 收紧一档（baseline，0.75）": (strict, DEFAULT_RATIO),
        "V2 每维 J 最优 floor（0.75）": (calibrated, DEFAULT_RATIO),
        "V3 每维 J 最优 floor（0.85）": (calibrated, 0.85),
        "V4 去掉 AUC<=0.5 的维（0.75）": (kept, DEFAULT_RATIO),
        "V5 去掉噪声维 + floor 收紧（0.85）": (kept, 0.85),
    }
    results = {name: simulate(bands, labels, floors, ratio, key_dims, base_dims)
               for name, (floors, ratio) in variants.items()}

    print(f"标签 {len(labels)} 篇（作者 {sum(1 for v in labels.values() if v == 'like')} / "
          f"别人 {sum(1 for v in labels.values() if v == 'not_like')}）")
    print("\n逐维分离度（band 当序数）：")
    for row in sorted(rows, key=lambda r: -(r["auc"] or 0.5)):
        print(f"  {row['id']:>2} {row['name']:<8} 作者中位 {row['author_median']} "
              f"别人中位 {row['other_median']} AUC {row['auc']:.3f} "
              f"floor 建议 {floor_choice(row)}（基线 {row['baseline_median']}）")
    print("\n方案对比：")
    for name, result in results.items():
        print(f"  {name:<28} 作者像 {result['author_like']}/{result['author_n']}"
              f" 别人像 {result['other_like']}/{result['other_n']}"
              f"（上界 {result['other_upper_95']}） Youden {result['youden']:+.3f}")
    best = max(results.items(), key=lambda kv: (kv[1]["youden"], kv[1]["tpr"]))
    proposal = {
        "chosen": best[0],
        "floors": variants[best[0]][0],
        "pass_ratio": variants[best[0]][1],
        "metrics": best[1],
        "all_variants": results,
        "dim_stats": rows,
        "label_count": len(labels),
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(proposal, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        print(f"\nproposal 写出 {args.out}；人类确认前不改 rubrics/基线")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
