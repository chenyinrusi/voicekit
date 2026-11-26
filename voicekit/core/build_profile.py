"""从作者语料算 `profile.json`：基线值 + 公差（校准来自语料自身分布）。

数字全部由这里算，LLM 不参与（设计文档 §6 硬边界）。
公差规则（拍板 D3）：每条属性取作者逐篇值相对中位数的偏差分布 P85 ——
"比作者八成半的稿子还偏"才算跑偏，不是调到变绿的数。
量纲下限（`voice_lib.FLOOR_BY_UNIT`）防"基数为 0 的属性"拿到无意义公差。

语料清洗（拍板 D5）：剥平台头；<200 汉字丢；**标点被导出成空格的稿子也丢**
（0 个全角逗号且 ≥200 汉字 = 上游清洗残留，句长/标点类指标对它无意义）；
034/037 也丢（调参时看过，出题侧早排除，校准侧同样排除）。

用法（repo 根目录）：
    python scripts/voice/build_profile.py                 # 写 skills/<slug>-voice/profile.json
    python scripts/voice/build_profile.py --check         # 只比，不写；漂移 rc=1
    python scripts/voice/build_profile.py --corpus DIR --out FILE
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent))

from . import voice_lib as lib  # noqa: E402


def _find_repo_root(start: Path) -> Path:
    """向上找 pyproject.toml；装进 site-packages 后没有仓库根，退回包目录（路径只在没给 --pack 时兜底）。"""
    for parent in [start, *start.parents]:
        if (parent / "pyproject.toml").is_file():
            return parent
    return start.parent


REPO_ROOT = _find_repo_root(Path(__file__).resolve().parent)
DEFAULT_CORPUS = REPO_ROOT / "voices" / "panchen-voice" / "corpus"
DEFAULT_OUT = REPO_ROOT / "voices" / "panchen-voice" / "profile.json"

SLUG = "panchen"
AUTHOR = "潘辰（《孤岛成林》语料作者）"
MIN_CJK = 200  # 短稿只学措辞，不参与基线（设计文档 §2.3 清洗规则）

#: 相对公差的下限与上限。下限防极稳定属性把作者自己也判红；
#: 上限防噪声属性把公差放到无意义（那时靠别的属性判）。
REL_MIN, REL_MAX = 0.15, 1.0
#: 公差取作者逐篇偏差的这个分位。0.85 = 作者八成半的稿子落在带内。
TOL_QUANTILE = 0.85
#: 复合决策规则的校准目标：作者自己的稿子约 90% 通过。
#: 参考分布上的误拒率是产品质量决策（Hamel：pass rate 是产品决策），不是越多越好。
TARGET_AUTHOR_PASS = 0.90
#: 核心轴峰值上限的分位：只拦"粗偏"，不参与平均分。
CORE_LIMIT_QUANTILE = 0.95
#: 切点下限：低于它说明语料内部太挤，判据已没有区分力（告警仍写进 meta）。
PASS_SCORE_FLOOR = 50.0
#: 校准最少篇数：与 style_lint --calibrate / --selftest 的下限一致，避免建出来的 profile 量不动。
MIN_CALIBRATION_DOCS = 8
#: 已被调参污染的篇：永久退出校准（FIX-368 切段规则、可疑句优先级都看着 034 改过）。
#: 与 `preference_probe.EXCLUDED` 同一口径——出题侧与校准侧不许用两套语料（DD-VOICE-002 §12.1）。
EXCLUDED_DOC_PREFIXES = ("034", "037")

#: 负样本（不该被判成"像"的稿子）。两类：机器腔 + 别的作者写的同文体随笔。
FIXTURES = Path(__file__).resolve().parent / "fixtures"
GENERIC_AI_GLOB = "generic-ai-zh*.md"
OTHER_AUTHOR_DIR = FIXTURES / "negatives"
OTHER_AUTHOR_GLOB = "other-author-zh*.md"

FLOOR_BY_UNIT = lib.FLOOR_BY_UNIT


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数（q ∈ [0,1]，0.9 = P90）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _calibrate(per_doc: list[float], unit: str, quantile: float = TOL_QUANTILE) -> dict:
    """基线 = 中位数；公差 = 作者自己偏差分布的分位（默认 P85）。"""
    base = median(per_doc)
    devs = [abs(v - base) for v in per_doc]
    spread = percentile(devs, quantile)
    rel = spread / abs(base) if base else REL_MAX
    rel = round(min(max(rel, REL_MIN), REL_MAX), 4)
    # floor 上限 0.8*|base|：floor 只用来吸收噪声，不能大到"取 0 也过"
    # （2026-11-02 review r2 P1：8 个子项 floor==base，指标取 0 必过）。
    capped = min(spread, 0.8 * abs(base)) if base else 0.0
    floor = round(max(capped, FLOOR_BY_UNIT[unit]), 3)
    mean = sum(per_doc) / len(per_doc)
    sd = (sum((v - mean) ** 2 for v in per_doc) / len(per_doc)) ** 0.5
    cv = sd / abs(mean) if mean else 0.0
    return {
        "observed_value": round(base, 4),
        "tolerance": {"rel": rel, "floor": floor},
        "distribution": {
            "p10": round(percentile(per_doc, 0.1), 4),
            "p50": round(base, 4),
            "p90": round(percentile(per_doc, 0.9), 4),
        },
        "stability": "高" if cv < 0.25 else ("中" if cv < 0.5 else "低"),
    }


def load_corpus(corpus: Path) -> tuple[list[dict], list[dict]]:
    """读语料，返回 (可用稿的原始证据, 跳过记录)。"""
    skipped: list[dict] = []
    raw_docs: list[dict] = []
    for path in sorted(corpus.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        body, _ = lib.clean_platform_noise(text)
        chars = lib.cjk_count(body)
        if path.name.startswith(EXCLUDED_DOC_PREFIXES):
            skipped.append(
                {"file": path.name, "reason": "contaminated (tuned on it)", "cjk": chars}
            )
            continue
        if chars < MIN_CJK:
            skipped.append({"file": path.name, "reason": f"cjk<{MIN_CJK}", "cjk": chars})
            continue
        if body.count("，") == 0:
            # 导出时逗号等被替换成空格：句长/标点/节奏类指标对它没有意义（拍板 D5）。
            skipped.append(
                {"file": path.name, "reason": "punctuation-stripped", "cjk": chars}
            )
            continue
        raw_docs.append({"file": path.name, "raw": lib.measure(text)})
    return raw_docs, skipped


def doc_values(raw: dict, signatures: dict[str, list[str]]) -> dict:
    """把一篇稿的原始证据换算成全部登记属性的可比值。"""
    values = {aid: raw[aid] for aid in lib.METRIC_IDS if aid in raw}
    for aid, grams in signatures.items():
        values[aid] = lib.signature_density(raw, aid, grams)
    values["_chars"] = raw["_chars"]
    values["_doc"] = raw["_doc"]
    return values


def build_profile(
    corpus: Path,
    *,
    author: str = AUTHOR,
    slug: str = SLUG,
    negatives_root: Path | None = FIXTURES,
) -> dict:
    """算基线。返回可直接 json.dump 的 dict（不含时间戳，便于 --check 比较）。"""
    raw_docs, skipped = load_corpus(corpus)
    if len(raw_docs) < MIN_CALIBRATION_DOCS:
        raise SystemExit(
            f"corpus too small: {len(raw_docs)} usable doc(s) < "
            f"{MIN_CALIBRATION_DOCS} (calibration目标 90% 需要足够样本)"
        )

    # 第一遍：签名榜（作者惯用词由语料决定，不写在代码里）
    signatures: dict[str, list[str]] = {}
    signature_lists: dict[str, list[list]] = {}
    for aid, top_n in lib.SIGNATURE_ATTRS.items():
        merged = lib.merge_signature([d["raw"] for d in raw_docs], aid, top_n)
        signatures[aid] = [g for g, _ in merged]
        signature_lists[aid] = merged

    # 第二遍：逐篇换算 + 校准
    per_doc = [doc_values(d["raw"], signatures) for d in raw_docs]
    attributes: list[dict] = []
    for spec in lib.METRICS:
        aid = spec["id"]
        kind = lib.KINDS[aid]
        entry = {
            "attribute_id": aid,
            "attribute_name": spec["name"],
            "category": spec["category"],
            "measurement_type": spec["mtype"],
            "measurement_method": spec["method"],
            "kind": kind,
            "confidence": "高" if len(per_doc) >= 20 else "中",
            "sample_count": len(per_doc),
            "generation_guidance": spec["guidance"],
        }
        if kind == "scalar":
            entry.update(_calibrate([float(doc[aid]) for doc in per_doc], lib.unit_of(aid)))
            if aid in signature_lists:
                entry["signature_list"] = signature_lists[aid]
        else:  # dict
            subkeys = list(per_doc[0][aid])
            calibrated = {
                key: _calibrate(
                    [float(doc[aid][key]) for doc in per_doc], lib.unit_of(aid, key)
                )
                for key in subkeys
            }
            entry["observed_value"] = {k: v["observed_value"] for k, v in calibrated.items()}
            entry["tolerance"] = {"per_key": {k: v["tolerance"] for k, v in calibrated.items()}}
            entry["distribution"] = {k: v["distribution"] for k, v in calibrated.items()}
            # 取最弱子项的稳定度（保守），不报"最好看的那个"
            entry["stability"] = max(
                (v["stability"] for v in calibrated.values()), key=lambda s: "高中低".index(s)
            )
        attributes.append(entry)

    result = {
        "schema": 2,
        "author": author,
        "slug": slug,
        "corpus": {
            "root": corpus.as_posix().replace(REPO_ROOT.as_posix() + "/", ""),
            "files_used": len(per_doc),
            "files_skipped": skipped,
            "cjk_chars": sum(doc["_chars"] for doc in per_doc),
            "clean_rule": (
                "剥平台注入（导出头/平台尾巴/重复标题）；<200 汉字丢；"
                "0 个全角逗号（标点被导成空格）丢；PS 保留；"
                "034/037 丢（调参看过，已退出校准）"
            ),
        },
        "meta": {
            "generated_by": "scripts/voice/build_profile.py",
            "tolerance_rule": (
                f"标量：base = 中位数，rel = clip(P{int(TOL_QUANTILE * 100)}"
                "(|篇值-base|)/|base|, "
                f"{REL_MIN}, {REL_MAX})，floor = max(该分位偏差, 量纲下限)。"
                "判定 |观测-base| <= max(rel*|base|, floor)。"
                "多子项属性：每个子项同样口径；判定允许 n<=2 全中、n>=3 时出界 <= max(1, n//4)。"
                "签名类（L1-08/L1-10/L2-03）：值 = 签名表命中/千字。"
                "总判定（复合规则）：score >= pass_score 且 core_dev <= core_limit；"
                "两条切点都在作者语料上校准到目标通过率，见 meta.calibration。"
            ),
            "pass_score": 0,  # 由 score_distribution() 填
            "sample_count": len(per_doc),
        },
        "attributes": attributes,
    }
    return score_distribution(result, corpus, negatives_root=negatives_root)


def calibrate_thresholds(rows: list[dict]) -> dict:
    """在一批 `{score, dev}` 上校准切点，返回 {pass_score, core_limit, achieved}。

    单独成函数是为了让 `style_lint --calibrate` 的 2 折留出用同一口径，
    不复制算法（复制两份迟早漂移）。
    """
    if not rows:
        return {"pass_score": PASS_SCORE_FLOOR, "core_limit": 0.0, "achieved": 0.0}
    core_limit = percentile([row["dev"] for row in rows], CORE_LIMIT_QUANTILE)
    cuts = sorted({row["score"] for row in rows if row["dev"] <= core_limit})
    pass_score = PASS_SCORE_FLOOR
    for cut in cuts:
        rate = sum(
            1 for row in rows if row["dev"] <= core_limit and row["score"] >= cut
        ) / len(rows)
        if rate + 1e-9 >= TARGET_AUTHOR_PASS:
            # 向下取整到 0.1：`compare` 用四舍五入到 0.1 的分数判 like，
            # 切点向上取整会把刚好压线的稿子判红（2026-11-03 实测 3 篇）。
            pass_score = max(PASS_SCORE_FLOOR, math.floor(cut * 10) / 10)
    achieved = sum(
        1 for row in rows if row["dev"] <= core_limit and row["score"] >= pass_score
    ) / len(rows)
    return {"pass_score": pass_score, "core_limit": round(core_limit, 3), "achieved": achieved}


def load_negatives(root: Path | None = FIXTURES) -> dict[str, list[dict]]:
    """读负样本，按类分组：`generic_ai`（机器腔）/ `other_author`（别的作者的同文体随笔）。

    `root=None` = 没有负样本（新作者首次建包，不编造误放数）。
    默认读 panchen 夹具：根目录放机器腔，`negatives/` 放别的作者；
    自定义目录把根目录所有 `*.md`（除 `generic-ai-zh*` 和说明文件）当「别的作者」。
    """
    if root is None:
        return {}
    root = Path(root)
    generic_paths = sorted(root.glob(GENERIC_AI_GLOB))
    if (root / "negatives").is_dir():
        other_paths = sorted((root / "negatives").glob(OTHER_AUTHOR_GLOB))
    else:
        other_paths = sorted(
            path
            for path in root.glob("*.md")
            if path not in generic_paths
            and path.name.upper() not in {"SOURCES.MD", "README.MD", "INDEX.MD"}
        )
    groups: dict[str, list[dict]] = {}
    for name, paths in (("generic_ai", generic_paths), ("other_author", other_paths)):
        docs = []
        for path in paths:
            body, _ = lib.clean_platform_noise(path.read_text(encoding="utf-8"))
            docs.append({"file": path.name, "raw": lib.measure(body)})
        groups[name] = docs
    return groups


def negative_stats(profile: dict, root: Path | None = FIXTURES) -> dict:
    """量误放：负样本里被判"像"的篇数与 95% 上界。

    写进 `profile.meta.calibration.negatives`，`style_lint --calibrate` 拿它当**棘轮**：
    逐组记下量过哪几份（`files`）和哪几份被判"像"，语料只许增，旧负样本不许由拒变收。
    这样"负样本刚加进来时误放是几"有账可查，不会随代码改动漂。
    """
    stats: dict = {}
    for name, docs in load_negatives(root).items():
        accepted = [
            doc["file"]
            for doc in docs
            if lib.compare(profile, lib.resolve_values(profile, doc["raw"]))["like"]
        ]
        stats[name] = {
            "docs": len(docs),
            "files": [doc["file"] for doc in docs],
            "accepted": len(accepted),
            "accepted_files": accepted,
            "upper_bound_95": round(lib.false_accept_upper_bound(len(accepted), len(docs)), 3),
        }
    return stats


def rule_fingerprint(profile: dict) -> str:
    """判定规则的指纹：pass_score / core_limit / 每条属性的 tolerance。

    负样本判定只反映「规则在这批稿上的结果」：规则放宽时同一批稿可能一篇不变
    （review R-01：pass_score 66.7→66.0、core_limit 4.004→4.5，7/47 一篇不变）。
    所以基线除了文件名，还要记规则指纹。
    """
    rule = {
        "pass_score": profile["meta"]["pass_score"],
        "core_limit": profile["meta"]["core_limit"],
        "tolerances": [[a["attribute_id"], a.get("tolerance")] for a in profile["attributes"]],
    }
    return hashlib.sha256(json.dumps(rule, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def negative_regressions(old_neg: dict, new_neg: dict) -> list[str]:
    """比较两版误放基线，列出「变松」的地方（FIX-344）。

    语料扩张本身不算回归：新增的负样本被判"像"是要如实记账的测量结果。
    回归有三类：组消失、旧负样本没了（缩小分母藏账）、旧负样本由拒变收（规则放宽）。
    规则本身的放宽由 `rule_fingerprint()` 在 main() 里另比。
    """
    problems: list[str] = []
    for name, old in old_neg.items():
        if name not in new_neg:
            problems.append(f"{name} 组消失")
            continue
        new = new_neg[name]
        if int(old.get("docs", 0)) == 0:
            continue  # 空组没什么可丢的，也别当成 legacy 基线
        if "files" not in old:
            # FIX-343 之前写下的基线没有文件清单，没法点名核对；只能显式放行重建。
            problems.append(f"{name} 旧基线没有文件清单，无法核对（重建请加 --accept-negative-regression）")
            continue
        # 基线的「误放数」必须等于名单长度：只改数字能绕过下面的逐文件名比较（review V-01）。
        if int(old.get("accepted", 0)) != len(old.get("accepted_files") or []):
            problems.append(f"{name} 基线自相矛盾：accepted 数与 accepted_files 名单不符（疑似手改）")
        old_files = set(old.get("files") or [])
        vanished = sorted(old_files - set(new.get("files") or []))
        if vanished:
            problems.append(f"{name} 基线里有 {len(vanished)} 份现场找不到：{vanished[:3]}")
        flipped = sorted(
            (set(new.get("accepted_files") or []) - set(old.get("accepted_files") or []))
            & old_files
        )
        if flipped:
            problems.append(f"{name} 旧负样本由拒变收：{flipped[:3]}")
    return problems


def score_distribution(
    profile: dict, corpus: Path, *, negatives_root: Path | None = FIXTURES
) -> dict:
    """校准复合决策规则，写进 profile.meta。

    规则 = `score >= pass_score` **且** `core_dev <= core_limit`。
    校准顺序（参考分布上做，不碰负样本）：
    1. `core_limit` = 作者语料核心轴合成偏差（RMS）的 P95 —— 只拦粗偏，不做逐条 AND；
    2. `pass_score` = 满足"作者通过率 >= 90%"的最严分数切点。
     同语料拟合是已知偏差：`style_lint --calibrate` 会另报 2 折留出数，见 DESIGN §5。

    为什么不再用"逐条核心轴都过 + 分数 P15"：那是 4 条子规则各有 15% 误拒再 AND，
    作者自己约一半稿子被判红（2026-11-03 实测 21/54）。依据见
    `dev/fixes/FIX-328-voice-decisions-calibrated/DESIGN.md`。
    """
    raw_docs, _ = load_corpus(corpus)
    signatures = {
        a["attribute_id"]: [g for g, _ in a["signature_list"]]
        for a in profile["attributes"]
        if "signature_list" in a
    }
    rows: list[dict] = []
    for doc in raw_docs:
        result = lib.compare(profile, doc_values(doc["raw"], signatures))
        dev = result["core_dev"]
        rows.append({"score": result["score"], "dev": float("inf") if dev is None else dev})
    scores = sorted(row["score"] for row in rows)
    thresholds = calibrate_thresholds(rows)
    pass_score = thresholds["pass_score"]
    core_limit = thresholds["core_limit"]
    achieved = thresholds["achieved"]

    warnings: list[str] = []
    if achieved + 1e-9 < TARGET_AUTHOR_PASS:
        warnings.append(f"achieved author pass {achieved:.3f} < target {TARGET_AUTHOR_PASS}")
    if pass_score <= PASS_SCORE_FLOOR + 1e-9:
        warnings.append("pass_score hit the floor; score axis has little separation")
    profile["meta"]["pass_score"] = pass_score
    profile["meta"]["core_limit"] = round(core_limit, 3)
    profile["meta"]["calibration"] = {
        "rule": "score >= pass_score and core_dev <= core_limit",
        "target_author_pass": TARGET_AUTHOR_PASS,
        "achieved_author_pass": round(achieved, 3),
        "author_docs": len(rows),
        "core_limit_quantile": CORE_LIMIT_QUANTILE,
        "core_limit": round(core_limit, 3),
        "pass_score": pass_score,
        "warnings": warnings,
    }
    profile["meta"]["corpus_scores"] = {
        "p10": round(percentile(scores, 0.1), 1),
        "p15": round(percentile(scores, 0.15), 1),
        "p50": round(percentile(scores, 0.5), 1),
        "p75": round(percentile(scores, 0.75), 1),
        "p90": round(percentile(scores, 0.9), 1),
        "core_dev_p50": round(percentile([row["dev"] for row in rows], 0.5), 3),
        "core_dev_p95": round(core_limit, 3),
    }
    # 误放基线（FIX-343/344）：负样本上量一遍，写进 meta 当棘轮；规则指纹单独记。
    profile["meta"]["calibration"]["negatives"] = negative_stats(profile, negatives_root)
    profile["meta"]["calibration"]["negatives_rule_sha256"] = rule_fingerprint(profile)
    return profile


def _comparable(profile: dict) -> dict:
    """供 --check / --selftest 比较的字段：含 meta（pass_score 决定过线，不能漏检）。"""
    return dict(profile)


def write_profile(profile: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：跨平台一致，避免 Windows 写 CRLF、Linux 写 LF 造成整文件 diff。
    with out.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(profile, ensure_ascii=False, indent=1) + "\n")


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="build or check a writer-voice profile")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--author", default=AUTHOR, help="作者显示名（默认本书作者）")
    ap.add_argument("--slug", default=SLUG, help="作者 slug（默认 panchen）")
    ap.add_argument(
        "--negatives",
        type=Path,
        default=FIXTURES,
        help="负样本目录（默认 panchen 夹具；其它作者用自己的「不是他写的」稿子）",
    )
    ap.add_argument(
        "--no-negatives",
        action="store_true",
        help="不量误放（新作者没有负样本时用；meta.calibration.negatives 记空）",
    )
    ap.add_argument("--check", action="store_true", help="compare against --out, do not write")
    ap.add_argument(
        "--accept-negative-regression",
        action="store_true",
        help="允许写出的 profile 比现有文件多误放（默认拒绝：重建不该把棘轮归零）",
    )
    args = ap.parse_args(argv)

    if not args.corpus.is_dir():
        print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
        return 2
    profile = build_profile(
        args.corpus,
        author=args.author,
        slug=args.slug,
        negatives_root=None if args.no_negatives else args.negatives,
    )

    if args.check:
        if not args.out.is_file():
            print(f"[FAIL] profile missing: {args.out}", file=sys.stderr)
            return 1
        stored = json.loads(args.out.read_text(encoding="utf-8"))
        if _comparable(stored) == _comparable(profile):
            print(f"[OK] profile up to date: {args.out} ({len(profile['attributes'])} attrs)")
            return 0
        drift = [
            a["attribute_id"]
            for a, b in zip(stored.get("attributes", []), profile["attributes"], strict=False)
            if a != b
        ]
        if drift:
            print(f"[FAIL] profile drift in {len(drift)} attribute(s): {drift[:10]}", file=sys.stderr)
        else:
            meta_keys = [
                key
                for key in set(stored.get("meta", {})) | set(profile.get("meta", {}))
                if stored.get("meta", {}).get(key) != profile.get("meta", {}).get(key)
            ]
            print(f"[FAIL] meta drift (attributes identical): {meta_keys[:10]}", file=sys.stderr)
        print("       rerun without --check to regenerate", file=sys.stderr)
        return 1

    if not args.accept_negative_regression and args.out.is_file():
        old = json.loads(args.out.read_text(encoding="utf-8"))
        old_neg = old.get("meta", {}).get("calibration", {}).get("negatives") or {}
        # 旧 profile 没有误放基线 = 首次迁移，不算回归（否则升级路径走不通，见 review r1 P1）。
        if old_neg:
            problems = negative_regressions(old_neg, profile["meta"]["calibration"]["negatives"])
            old_rule = old.get("meta", {}).get("calibration", {}).get("negatives_rule_sha256")
            new_rule = profile["meta"]["calibration"].get("negatives_rule_sha256")
            if old_rule and old_rule != new_rule:
                problems.append("判定规则指纹变了（pass_score / core_limit / tolerance）")
            if problems:
                print(
                    "[FAIL] 负样本误放基线变松：" + "；".join(problems) + "。"
                    "语料只许增、旧负样本不许由拒变收、判定规则不许放宽。"
                    "确认是有意放宽再加 --accept-negative-regression",
                    file=sys.stderr,
                )
                return 1
    if args.accept_negative_regression:
        print(
            "[WARN] --accept-negative-regression：本次重建跳过了误放基线与规则指纹检查；"
            "放过了什么、为什么，必须写进本包 REPORT",
            file=sys.stderr,
        )

    write_profile(profile, args.out)
    meta = profile["corpus"]
    print(
        f"[OK] wrote {args.out} — {len(profile['attributes'])} attrs, "
        f"{meta['files_used']} docs, {meta['cjk_chars']} CJK chars, "
        f"pass_score={profile['meta']['pass_score']}, skipped={len(meta['files_skipped'])}"
    )
    return 0


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把 ≥、中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
