"""比对一篇稿与作者基线：给出分项偏差与通过率。

只判 Style（像不像），不判 Quality（好不好）。设计文档 §8 / B.4。
比对逻辑在 `voice_lib`（纯数据），本文件只做 CLI + 自检 + 负样本。

用法（repo 根目录）：
    python scripts/voice/style_lint.py --file 稿.md            # 打表 + 通过率
    python scripts/voice/style_lint.py --file 稿.md --json      # 机器读
    python scripts/voice/style_lint.py --selftest               # 双向自检（check 入口）
    python scripts/voice/style_lint.py --calibrate              # 量误拒率/误放率（check 入口）

退出码：0 像 / 1 不像 / 2 用法、输入或环境错；`--calibrate` 用 0/1 表示达标/不达标。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from . import build_profile as bp  # noqa: E402
from . import pack_lib as pk  # noqa: E402
from . import voice_lib as lib  # noqa: E402
from .. import paths  # noqa: E402

#: 没给 `--pack` 时**不再**回落到开发树的 `voices/panchen-voice/profile.json`
#: （wheel 里那个路径不存在，报错只会指向 site-packages/voices）。要基线就显式给 --pack。
DEFAULT_PROFILE: Path | None = None
#: 语料只有 `--selftest` / `--calibrate` 用，是仓库内自检，允许指开发树。
DEFAULT_CORPUS = bp.DEFAULT_CORPUS

#: 比对与判定全部来自 voice_lib（build_profile 也用它，避免两份实现）。
compare = lib.compare
resolve_values = lib.resolve_values
validate_profile = lib.validate_profile
pass_score = lib.pass_score
FALLBACK_PASS_SCORE = lib.FALLBACK_PASS_SCORE

#: 低于这个字数只给"低置信"提示，不直接判。
LOW_CONFIDENCE_CJK = 500
#: 每条属性在作者语料里的通过率必须落在带内，否则视为"死了"（永远红/永远绿）。
ALIVE_PASS_RATE_MIN = 0.45
ALIVE_PASS_RATE_MAX = 0.995

#: 负样本判据分两类，因为两类负样本"该失败多少"不一样：
#: ①通用 AI 腔（换了一个人的声音）：必须落到作者语料底部（低于 p15），打掉 ≥1/3 的属性；
#: ②同一篇原稿机械压平（词还是作者的，只压平句式）：三篇里至少两篇比原稿低 ≥2 条属性，
#:   并且并集必须打掉它真正动到的三条轴（句长、句读节奏、缓和/增强）。
#
#: 2026-11-04 FIX-343：原来写死"低于通过线 15 分 / 打掉 8 条"，那是 24 条属性时的口径
#: （24 条时 15 分 = 3.6 条属性）。属性从 24 条扩到 42 条后，每条只值 2.38 分，
#: 同样的 15 分会变成 6.3 条属性——判据会静默变松/变紧。改成按属性条数表达。
#: 2026-11-05 FIX-350：通用腔的绝对分差（3.6 条）在成书语料上不可达（通过线降到 59.5），
#: 改成相对作者 p15；压平稿的单篇 3 轴改成三篇并集（单篇 L7-02 只差 0.4 条，会压线假红）。
NEG_FIXTURE_MIN_FAIL_SHARE = 8 / 24
REWRITE_MARGIN_ATTRS = 1.92
REWRITE_MIN_FAIL_SHARE = 4 / 24
REWRITE_MUST_FAIL = ("L2-01", "L3-05", "L7-02")
#: 两类负样本合起来必须覆盖这些属性（实测：通用腔打掉词/搭配/情绪，压平稿打掉句法/标点）。
SELFTEST_NEG_MUST_FAIL = ("L1-08", "L1-13", "L1-16", "L2-01", "L3-05")


def _attrs_to_points(attrs: float, total: int) -> float:
    """把"几条属性"换算成"几分"：分数是等权的，条数变了分数刻度就变了。"""
    return attrs * 100.0 / total if total else 0.0


def _zero_baseline(attr: dict) -> bool:
    """零基线属性：作者语料里从没出现过这个特征，只在别人出现时才失效。

    这类属性（网络流行语、脏话、叠标点）在作者语料里通过率必然 100%，
    不能用「语料内通过率」判死活（FIX-350：L1-17 / L10-02 就是这种）。
    """
    observed = attr.get("observed_value")
    if attr.get("kind") == "scalar":
        return abs(float(observed or 0.0)) < 1e-9
    if not isinstance(observed, dict) or not observed:
        return False
    return all(abs(float(value)) < 1e-9 for value in observed.values())

#: 机器腔对照稿：手写的通用 AI 文风（无 emoji），放在 fixtures/ 当第二类负样本。
GENERIC_AI_FIXTURE = bp.FIXTURES / "generic-ai-zh.md"
#: 别人写的中文随笔（网上公开文章，见 fixtures/negatives/SOURCES.md）。
#: 这一类才是真正的误放检查：同一文体、不同作者，判成"像"就是误放。
OTHER_AUTHOR_DIR = bp.OTHER_AUTHOR_DIR
GENERIC_AI_GLOB = bp.GENERIC_AI_GLOB
OTHER_AUTHOR_GLOB = bp.OTHER_AUTHOR_GLOB
#: 校准体检允许的误差：实测通过率与 profile.meta.calibration 目标差不超过这个值。
CALIBRATION_TOLERANCE = 0.05

#: 误放率上界（二项精确解）在 voice_lib 里：build_profile 写棘轮时也要用同一份实现。
false_accept_upper_bound = lib.false_accept_upper_bound


def _machine_rewrite(text: str) -> str:
    """构造负样本：把原稿机械压平（确定性，非任何模型真实输出）。

    1. 逗号拆成句号、长句切短 → 句长、节奏、标点谱（L2-01 / L3-05 / L1-01）；
    2. 缓和词换增强词 → 缓和与增强（L7-02）；
    3. 文言标记换白话 → 文白夹杂（L1-12）；
    4. 段首加「首先/其次/综上所述」→ 句首模式（L2-03）。
    """
    body, _ = lib.clean_platform_noise(text)
    for weak, strong in (
        ("只是", "然而"), ("其实", "事实上"), ("于是", "因此"), ("好了", "综上"),
        ("大概", "一定"), ("或许", "必然"), ("也许", "必定"), ("可能", "肯定"),
        ("我觉得", "我认为"), ("而已", "罢了"), ("矣", "了"), ("焉", "呢"),
        ("亦", "也"), ("乃", "就"), ("尚", "还"), ("犹", "像"), ("遂", "于是"),
        ("殊", "很"), ("颇", "很"), ("皆", "都"), ("岂", "难道"),
    ):
        body = body.replace(weak, strong)
    openers = ("首先，", "其次，", "综上所述，")
    paragraphs = []
    for index, paragraph in enumerate(lib.split_paragraphs(body)):
        pieces = [p.strip() for p in re.split(r"[，、；：]", paragraph) if p.strip()]
        short: list[str] = []
        for piece in pieces:
            while len(piece) > 10:
                short.append(piece[:10] + "。")
                piece = piece[10:]
            if piece:
                short.append(piece + "。")
        paragraphs.append(openers[index % len(openers)] + "".join(short).rstrip("。") + "！")
    return "\n\n".join(paragraphs)


def _usable_docs(corpus: Path) -> list[Path]:
    raw_docs, _ = bp.load_corpus(corpus)
    return [corpus / d["file"] for d in raw_docs]


def _print_report(result: dict, label: str, *, verbose: bool) -> None:
    print(f"== {label}: {result['score']}% ({result['passed']}/{result['total']})")
    for row in result["rows"]:
        if verbose or not row["ok"]:
            mark = "PASS" if row["ok"] else "FAIL"
            print(f"  {mark} {row['attribute_id']} {row['attribute_name']}: {row['detail']}")


def _print_evidence(result: dict) -> None:
    """只给不过的条目：能指到句就贴原句，全篇统计就明说没有。"""
    fails = [r for r in result["rows"] if not r["ok"]]
    if not fails:
        return
    print("-- 句级证据（¶段 句N，只标位置，不判好坏）")
    for row in fails:
        print(f"  {row['attribute_id']} {row['attribute_name']}: {row['evidence_note']}")


def selftest(profile_path: Path, corpus: Path) -> int:
    """双向自检。

    ①剖面与语料一致；②每条属性都不是死的（零基线属性除外，它们在语料里本来就不失效）；
    ③作者自己的稿子整体分高（用剖面里记的分布比）；④负样本落到作者语料底部：
    通用 AI 腔低于作者 p15 且打掉足够多属性，机械压平稿掉分并打掉句法轴。
    """
    failures: list[str] = []
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    problem = validate_profile(profile)
    if problem:
        print(f"[FAIL] bad profile: {problem}", file=sys.stderr)
        return 2

    rebuilt = bp.build_profile(corpus)
    if bp._comparable(rebuilt) != bp._comparable(profile):
        drift = [
            a["attribute_id"]
            for a, b in zip(profile.get("attributes", []), rebuilt["attributes"], strict=False)
            if a != b
        ]
        failures.append(f"profile is stale vs corpus (attrs drift: {drift[:8]})")
        print(f"[FAIL] profile freshness: drift in {drift[:8]}")
    for key in ("pass_score", "corpus_scores", "core_limit", "calibration"):
        if rebuilt["meta"].get(key) != profile.get("meta", {}).get(key):
            failures.append(f"profile meta '{key}' differs from a fresh rebuild")

    raw_docs, _ = bp.load_corpus(corpus)
    if len(raw_docs) < 8:
        print(f"[FAIL] corpus has only {len(raw_docs)} usable doc(s)", file=sys.stderr)
        return 2
    results = [compare(profile, resolve_values(profile, d["raw"])) for d in raw_docs]
    scores = sorted(r["score"] for r in results)
    corpus_scores = profile.get("meta", {}).get("corpus_scores", {})
    below = sum(1 for s in scores if s < pass_score(profile))
    print(
        f"== corpus: p15={corpus_scores.get('p15')} p50={corpus_scores.get('p50')} "
        f"p75={corpus_scores.get('p75')}; recomputed median={scores[len(scores) // 2]}"
    )
    print(f"== pass_score={pass_score(profile)} (author's own docs below it: {below}/{len(scores)})")
    calibration = profile.get("meta", {}).get("calibration", {})
    target = float(calibration.get("target_author_pass", 0.9))
    likes = sum(1 for result in results if result["like"])
    like_rate = likes / len(results)
    print(
        f"== 复合规则通过率：{likes}/{len(results)} = {like_rate:.1%} "
        f"(目标 {target:.0%}，core_limit={profile.get('meta', {}).get('core_limit')})"
    )
    if like_rate < target - CALIBRATION_TOLERANCE:
        failures.append(
            f"author pass rate {like_rate:.1%} below target {target:.0%} "
            f"(tolerance {CALIBRATION_TOLERANCE:.0%})"
        )

    # ②逐属性存活检查
    for index, attr in enumerate(profile["attributes"]):
        rate = sum(1 for r in results if r["rows"][index]["ok"]) / len(results)
        if rate > ALIVE_PASS_RATE_MAX and _zero_baseline(attr):
            print(f"  ZERO {attr['attribute_id']} pass_rate={rate:.2f} (零基线，只在特征出现时失效)")
            continue
        flag = "OK " if ALIVE_PASS_RATE_MIN <= rate <= ALIVE_PASS_RATE_MAX else "DEAD"
        print(f"  {flag} {attr['attribute_id']} pass_rate={rate:.2f}")
        if flag == "DEAD":
            failures.append(
                f"attribute {attr['attribute_id']} pass_rate {rate:.2f} outside "
                f"[{ALIVE_PASS_RATE_MIN}, {ALIVE_PASS_RATE_MAX}]"
            )

    # ③负样本：手写通用 AI 腔（无 emoji）+ 三篇机械压平稿
    # 2026-11-05 FIX-350：作者语料换成成书版后，确定性层的负样本线不再按
    # 「离通过线 3.6 条」判（成书文本更标准，这条线已不可达）；改成相对判据：
    # 通用腔必须落到作者语料的 p15 之下，压平稿必须掉分且覆盖句法轴。
    p15 = corpus_scores.get("p15")
    if p15 is None:
        p15 = scores[len(scores) * 15 // 100]
    must_fail_union: set[str] = set()
    rewrite_failed_union: set[str] = set()
    rewrite_drops: list[float] = []
    if GENERIC_AI_FIXTURE.is_file():
        fixture = compare(
            profile, resolve_values(profile, lib.measure(GENERIC_AI_FIXTURE.read_text(encoding="utf-8")))
        )
        _print_report(fixture, "generic AI fixture", verbose=False)
        print(f"  like={fixture['like']}（确定性层判定；放行看 judge.py）")
        total = len(profile["attributes"])
        min_fails = max(1, round(NEG_FIXTURE_MIN_FAIL_SHARE * total))
        if fixture["score"] >= p15:
            failures.append(
                f"generic AI fixture scored {fixture['score']} (must be < author p15 {p15})"
            )
        if len(fixture["failed"]) < min_fails:
            failures.append(
                f"generic AI fixture only failed {len(fixture['failed'])} attribute(s) "
                f"(need >= {min_fails})"
            )
        must_fail_union |= set(fixture["failed"])
        # 覆盖判断看这一「类」，不能只看一份。2026-11-07 实测：L1-08 当时只靠
        # 机器压平稿偶然覆盖（抽到的 031 恰好掉了高频实词），语料去掉 2 篇后抽样
        # 位置一变就假红；另一份通用腔打掉了 L1-08（obs 15.0 vs base 37.5）。
        # 句法轴（L2-01/L3-05）不靠这份名单：任何压平都会打掉它们。
        for extra in sorted(GENERIC_AI_FIXTURE.parent.glob("generic-ai-zh*.md")):
            if extra == GENERIC_AI_FIXTURE:
                continue
            extra_result = compare(
                profile, resolve_values(profile, lib.measure(extra.read_text(encoding="utf-8")))
            )
            must_fail_union |= set(extra_result["failed"])
    else:
        failures.append(f"missing negative fixture: {GENERIC_AI_FIXTURE}")

    for index in (0, len(raw_docs) // 4, len(raw_docs) // 2):
        doc = raw_docs[index]
        path = corpus / doc["file"]
        positive = compare(profile, resolve_values(profile, doc["raw"]))["score"]
        rewritten = lib.measure(_machine_rewrite(path.read_text(encoding="utf-8")))
        result = compare(profile, resolve_values(profile, rewritten))
        label = f"machine rewrite {doc['file']}"
        _print_report(result, label, verbose=False)
        total = len(profile["attributes"])
        rewrite_drops.append(positive - result["score"])
        min_fails = max(1, round(REWRITE_MIN_FAIL_SHARE * total))
        if len(result["failed"]) < min_fails:
            failures.append(
                f"{label} only failed {len(result['failed'])} attribute(s) "
                f"(need >= {min_fails})"
            )
        must_fail_union |= set(result["failed"])
        rewrite_failed_union |= set(result["failed"])

    # 压平稿是三种改写的集合：每篇至少掉 1.92 条属性的分，三篇里至少两篇达标；
    # 三个自己的轴（句长/节奏/缓和增强）由并集覆盖即可——单篇里 L7-02 只差 0.4 条，
    # 按篇要求会在语料换版时变成压线假红（FIX-350 实测）。
    margin = _attrs_to_points(REWRITE_MARGIN_ATTRS, len(profile["attributes"]))
    if sum(1 for drop in rewrite_drops if drop >= margin) < 2:
        failures.append(
            f"machine rewrites dropped {[round(d, 1) for d in rewrite_drops]} points; "
            f"need at least 2 of 3 >= {margin:.1f}"
        )
    missed = [a for a in REWRITE_MUST_FAIL if a not in rewrite_failed_union]
    if missed:
        failures.append(f"machine rewrites never failed {missed}")

    missing = [a for a in SELFTEST_NEG_MUST_FAIL if a not in must_fail_union]
    if missing:
        failures.append(f"negatives never failed {missing}")

    # ④核心轴：只把逗号换成句号的稿子（词一个没换）必须判"不像"，
    # 不能靠 24 条等权平均稀释过去（2026-11-02 review r2 P0）。
    first = raw_docs[0]
    comma_only = lib.measure((corpus / first["file"]).read_text(encoding="utf-8").replace("，", "。"))
    comma_result = compare(profile, resolve_values(profile, comma_only))
    print(
        f"== comma-only rewrite: score={comma_result['score']} "
        f"core_failed={comma_result['core_failed']} like={comma_result['like']}"
    )
    if comma_result["like"]:
        failures.append("comma-only rewrite is still accepted (core axes must catch it)")

    if failures:
        print("\n[FAIL] selftest failed:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(
        "\n[OK] selftest: profile fresh; every attribute alive; "
        f"pass_score={pass_score(profile)}, core_limit={profile.get('meta', {}).get('core_limit')}, "
        f"author pass rate {like_rate:.1%}; "
        f"generic AI fixture < author p15 ({p15}); "
        f"machine rewrites dropped {[round(d, 1) for d in rewrite_drops]} points "
        f"(need 2/3 >= {_attrs_to_points(REWRITE_MARGIN_ATTRS, len(profile['attributes'])):.1f}); "
        f"negatives covered {', '.join(SELFTEST_NEG_MUST_FAIL)}"
    )
    return 0


def calibrate(profile_path: Path, corpus: Path) -> int:
    """校准体检：在参考语料 + 负样本上量误拒率（作者稿）与误放率（通用 AI 腔）。

    判据不是"分数好看"，是两类错误率：
    - 误拒：把作者自己的稿子判红，应接近 meta.calibration 的目标误拒率；
    - 误放：把别人的声音判成作者，负样本应全部"不像"。
    这就是验收抽样里的 producer risk / consumer risk，见
    `dev/reports/voice-threshold-calibration-best-practice.md`。
    """
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    problem = validate_profile(profile)
    if problem:
        print(f"[FAIL] bad profile: {problem}", file=sys.stderr)
        return 2
    raw_docs, _ = bp.load_corpus(corpus)
    if len(raw_docs) < 8:
        print(f"[FAIL] corpus has only {len(raw_docs)} usable doc(s)", file=sys.stderr)
        return 2

    results = [compare(profile, resolve_values(profile, d["raw"])) for d in raw_docs]
    passed = sum(1 for result in results if result["like"])
    rate = passed / len(results)
    calibration = profile.get("meta", {}).get("calibration", {})
    target = float(calibration.get("target_author_pass", 0.9))
    failures: list[str] = []
    print(
        f"== 参考语料（作者稿）：{passed}/{len(results)} 通过 = {rate:.1%}"
        f"（目标 {target:.0%}，容差 {CALIBRATION_TOLERANCE:.0%}）"
    )
    if rate < target - CALIBRATION_TOLERANCE:
        failures.append(f"误拒率过高：作者稿只过 {rate:.1%}")

    groups = (
        ("通用 AI 腔", "generic_ai", sorted(GENERIC_AI_FIXTURE.parent.glob(GENERIC_AI_GLOB))),
        ("其他作者随笔", "other_author",
         sorted(OTHER_AUTHOR_DIR.glob(OTHER_AUTHOR_GLOB)) if OTHER_AUTHOR_DIR.is_dir() else []),
    )
    if not any(paths for _, _, paths in groups):
        # 负样本为空 = 误放检查被静默关掉（独立评审 P2）。宁可红，不假装通过。
        print(
            f"[FAIL] 没有负样本可量误放率（{GENERIC_AI_FIXTURE.parent}/{GENERIC_AI_GLOB} "
            f"或 {OTHER_AUTHOR_DIR}/）",
            file=sys.stderr,
        )
        return 2
    recorded = calibration.get("negatives", {})
    measured: dict[str, dict] = {}
    negative_scores: list[float] = []
    accepted_negatives = 0
    for label, key, fixtures in groups:
        accepted = 0
        accepted_files: list[str] = []
        for fixture in fixtures:
            try:
                body, _ = lib.clean_platform_noise(fixture.read_text(encoding="utf-8"))
                result = compare(profile, resolve_values(profile, lib.measure(body)))
            except (ValueError, OSError) as exc:  # 坏 fixture 报错，不静默跳过
                failures.append(f"负样本 {fixture.name} 读不出可用文本：{exc}")
                continue
            print(
                f"== 负样本（{label}）{fixture.name}: score={result['score']} "
                f"core_dev={result['core_dev']} mean_dev={result['mean_dev']} "
                f"-> {'误放' if result['like'] else '拒绝'}"
            )
            negative_scores.append(result["score"])
            if result["like"]:
                accepted += 1
                accepted_files.append(fixture.name)
        if fixtures:
            print(
                f"== {label} 误放 {accepted}/{len(fixtures)}；"
                f"95% 上界 {false_accept_upper_bound(accepted, len(fixtures)):.1%}"
            )
        measured[key] = {
            "docs": len(fixtures),
            "files": [path.name for path in fixtures],
            "accepted": accepted,
            "accepted_files": accepted_files,
            "upper_bound_95": round(false_accept_upper_bound(accepted, len(fixtures)), 3),
        }
        accepted_negatives += accepted
    # 棘轮（FIX-345 合并）：与 build_profile 同一份判据——语料只许增，旧负样本不许由拒变收。
    # 合法扩语料但没重建基线时，这里只提示「过期」，不把新误放当成变松。
    problems = bp.negative_regressions(recorded, measured)
    # 规则指纹（review V-02）：profile 被手改（pass_score/core_limit/tolerance）时，
    # 负样本判定可能一篇不变，只有指纹能看出来。
    stored_rule = calibration.get("negatives_rule_sha256")
    if stored_rule and stored_rule != bp.rule_fingerprint(profile):
        problems.append("判定规则指纹与当前规则不一致（profile 被手改或规则已变）")
    for problem in problems:
        failures.append(f"负样本基线过期或变松：{problem}；先重建 profile（build_profile.py）再量")
    stale = [
        f"{key}: 基线 {recorded.get(key, {}).get('docs', 0)} 份/误放 "
        f"{int(recorded.get(key, {}).get('accepted', 0))} → 现场 {measured[key]['docs']} 份/误放 "
        f"{measured[key]['accepted']}"
        for _, key, _paths in groups
        if int(recorded.get(key, {}).get("docs", 0)) != measured[key]["docs"]
    ]
    if stale and not problems:
        print(f"== 提示：负样本语料与基线不一致（{'; '.join(stale)}）；"
              "误放数按现场算，重建 profile 可更新基线。")
    print(
        f"== 判定表：作者稿 误拒 {len(results) - passed}/{len(results)}；"
        f"负样本 误放 {accepted_negatives}/{len(negative_scores)}"
        f"（95% 上界 {false_accept_upper_bound(accepted_negatives, len(negative_scores)):.1%}；"
        f"基线 {sum(int(v.get('accepted', 0)) for v in recorded.values()) if recorded else 0} 份，"
        "语料只许增、旧负样本不许由拒变收）"
    )
    # L11-07 签名强度口径：作者稿与负样本的分数分离度（Cohen's d）。
    if negative_scores:
        author_scores = [result["score"] for result in results]
        m1 = sum(author_scores) / len(author_scores)
        m2 = sum(negative_scores) / len(negative_scores)
        v1 = sum((v - m1) ** 2 for v in author_scores) / len(author_scores)
        v2 = sum((v - m2) ** 2 for v in negative_scores) / len(negative_scores)
        pooled = ((v1 + v2) / 2) ** 0.5
        d = (m1 - m2) / pooled if pooled else 0.0
        print(
            f"== 分离度（L11-07 签名强度口径）：作者均值 {m1:.1f}，负样本均值 {m2:.1f}，"
            f"Cohen's d = {d:.2f}（d<1 说明分数轴分不开两类）"
        )
    # 2 折留出：同语料既拟合又验收的偏差有多大，必须自己量出来（独立评审 P2）。
    # 只当信息打印、不改 rc：每折只有 27 篇，波动本来就大；不用它冒充覆盖率保证。
    entries = [{"score": result["score"], "dev": result["core_dev"]} for result in results]
    for offset, label in ((0, "A→B"), (1, "B→A")):
        train = [row for index, row in enumerate(entries) if index % 2 == offset]
        test = [row for index, row in enumerate(entries) if index % 2 != offset]
        if not train or not test:
            continue
        thresholds = bp.calibrate_thresholds(train)
        held = sum(
            1
            for row in test
            if row["score"] >= thresholds["pass_score"] and row["dev"] <= thresholds["core_limit"]
        ) / len(test)
        print(
            f"== 2 折留出（同语料拟合偏差）：{label} 留出通过 {held:.1%}"
            f"（训练集切点 pass={thresholds['pass_score']} dev<={thresholds['core_limit']}）"
        )
    if failures:
        print("\n[FAIL] calibration failed:", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)
        return 1
    print(
        f"\n[OK] calibration: 作者稿误拒 {len(results) - passed}/{len(results)}"
        f"（目标误拒 ≤{(1 - target) + CALIBRATION_TOLERANCE:.0%}）；"
        f"负样本误放 {accepted_negatives}/{len(negative_scores)}"
        f"（基线 {sum(int(v.get('accepted', 0)) for v in recorded.values()) if recorded else 0} 份，"
        "语料只许增、旧负样本未由拒变收）。"
    )
    if accepted_negatives:
        print(
            "     注意：误放不是零。上界与具体哪几份被判'像'见上面的逐份输出；"
            "裁判层（judge.py）的误放另测，别拿这一层的绿当整层结论。"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(
        prog="voicekit lint", description="score a draft against a writer-voice profile"
    )
    ap.add_argument("--file", type=Path, help="draft to score")
    ap.add_argument("--profile", type=Path, default=None)
    ap.add_argument("--corpus", type=Path, default=None)
    ap.add_argument("--pack", type=Path, default=None, help="作者包目录；profile/语料从包里取")
    ap.add_argument("--selftest", action="store_true", help="run the bidirectional check")
    ap.add_argument("--calibrate", action="store_true", help="measure 误拒率/误放率 on corpus + negatives")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--verbose", action="store_true", help="print passing attributes too")
    ap.add_argument("--annotate", action="store_true", help="每条不过的属性给出段落/句子位置")
    args = ap.parse_args(argv)

    try:
        if args.pack:
            pack = pk.load_pack(args.pack)
            args.profile = args.profile or pack["profile"]
            args.corpus = args.corpus or pack["corpus"]
            # 钉住包：加权表（weights.json）也从这里取，别退回开发树。
            paths.use_pack(args.pack)
            if args.selftest or args.calibrate:
                # 两个入口都拿 panchen 负样本当参照，不能拿来量别的作者（F-02）。
                print(
                    "[FAIL] --selftest/--calibrate 绑 panchen 负样本，不接受 --pack；"
                    "新作者包用 python scripts/voice/author_pack.py check --pack <dir>",
                    file=sys.stderr,
                )
                return 2
        else:
            args.profile = args.profile or DEFAULT_PROFILE
            args.corpus = args.corpus or DEFAULT_CORPUS
    except pk.PackError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2

    if not args.profile:
        print(
            "[FAIL] 没有 --pack 也没有 --profile：体检要作者基线；"
            "用 --pack <作者包>，或 --profile <profile.json>",
            file=sys.stderr,
        )
        return 2
    if not args.profile.is_file():
        print(f"[FAIL] profile not found: {args.profile}", file=sys.stderr)
        return 2
    if args.calibrate:
        if not args.corpus.is_dir():
            print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
            return 2
        return calibrate(args.profile, args.corpus)
    if args.selftest:
        if not args.corpus.is_dir():
            print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
            return 2
        return selftest(args.profile, args.corpus)
    if not args.file:
        ap.error("need --file or --selftest")
    if not args.file.is_file():
        print(f"[FAIL] file not found: {args.file}", file=sys.stderr)
        return 2

    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    problem = validate_profile(profile)
    if problem:
        print(f"[FAIL] bad profile: {problem}", file=sys.stderr)
        return 2
    try:
        draft_text = args.file.read_text(encoding="utf-8")
        raw = lib.measure(draft_text)
    except ValueError as exc:  # 没有汉字：输入不可评，不是脚本坏了
        print(f"[FAIL] cannot score {args.file}: {exc}", file=sys.stderr)
        return 2
    if raw["_chars"] < bp.MIN_CJK:
        print(
            f"[FAIL] cannot score {args.file}: only {raw['_chars']} CJK chars "
            f"(< {bp.MIN_CJK}); too short for a stable score",
            file=sys.stderr,
        )
        return 2
    if raw["_chars"] < LOW_CONFIDENCE_CJK:
        print(
            f"[WARN] low confidence: only {raw['_chars']} CJK chars "
            f"(< {LOW_CONFIDENCE_CJK}); sample too small for stable scores",
            file=sys.stderr,
        )
    result = compare(profile, resolve_values(profile, raw))
    if args.annotate:
        from . import evidence

        evidence.attach(result["rows"], draft_text)
    if args.json:
        print(json.dumps(dict(result, diagnostic_only=True), ensure_ascii=False, indent=1))
    else:
        _print_report(result, args.file.name, verbose=args.verbose)
        if args.annotate:
            _print_evidence(result)
        print(
            f"-- pass line {pass_score(profile)}；"
            f"核心轴合成偏差 {result['core_dev']}（上限 {result['core_limit']}）"
        )
        if result["core_failed"]:
            print(f"-- core axes failed: {', '.join(result['core_failed'])}")
        if not result["like"] and result["score"] >= pass_score(profile):
            print("-- 分数过线但核心轴合成偏差超上限：确定性层判不像")
        print("-- 确定性层只作诊断：放行只看 `judge.py` 的同人判定")
        print(
            f"-- rc {0 if result['like'] else 1} 是「像不像这个人」的结论，"
            f"不是「{result['total']} 条全过」；未过的 {result['total'] - result['passed']} 条只作改稿线索"
        )
    return 0 if result["like"] else 1


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把 ≥、中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
