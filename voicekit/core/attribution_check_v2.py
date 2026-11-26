"""观点归属检查 v2（Phase 0.5 修复版）。

边界（作者 2026-11-14 定）：
- 只判「这句话的观点属于谁」，不评文风、不评好坏、不评像不像作者。
- 四个角色分开，不共用一个「作者」：材料作者 / Claim 来源主体 / 目标作者 / 生成器。
  角色由命令行传入，不让模型猜。
- 三档按证据强度判，不按模型心情：
  PASS   有正面证据表明归属没被改。
  FAIL   有正面证据表明归属被改了（冒认：第三方或材料作者的观点被写成目标作者自认；
         误植：材料作者的观点被挂到材料里不存在的第三方名下）。
  REVIEW 两边证据都不够（材料缺来源、删了来源但没认领、来源不明）。
- 输出一致性不靠模型自觉：代码按 claims 重算 verdict，与模型自报的 verdict 比对。
  不一致或结构不合法，记 output_validity=INVALID，不假装成 REVIEW。
- 不发现新材料里没有的新 Claim（作者定的 P1，本轮不做）。

用法（repo 根）：
    py -3.10 scripts/voice/attribution_check_v2.py --material 材料.md --draft 稿.md \
        --target-author 潘辰 --material-author 李戛 --generator CAS --json 报告.json
    py -3.10 scripts/voice/attribution_check_v2.py --selftest

旧版 `attribution_check.py`（commit ce9d94fb）保留不动，作回归基准。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import rewrite  # noqa: E402

VERDICTS = ("PASS", "REVIEW", "FAIL")
ORIGINS = ("third_party", "material_author", "target_author", "unknown")
EXPRESSIONS = ("own_claim", "attributed", "unattributed", "unknown")

SYSTEM_TMPL = """你是观点归属检查器 v2。你只回答一件事：生成稿里的每条观点，归属有没有被改。

## 四个角色，不能混

- 材料作者（源文档作者）：{material_author}。他写了材料，但材料里的观点不一定都是他的。
- Claim 来源主体：这条观点最初是谁说的。可能是路透社、Challenger、Oxford Economics、
  扎克伯格这样的第三方，也可能是材料作者自己。
- 目标作者：{target_author}。稿子是以他的名义写的。稿子里的「我」就是他。
- 生成器：{generator}。它只负责生成，不自动拥有任何观点。

材料里的标注读法：
  【事实：X】【统计：X】 → 来源主体是 X，这是转述，不是谁的个人判断。
  【第三方观点：X】 → 来源主体是 X，X 在替自己的观点说话。
  【作者原话】 → 来源主体是材料作者 {material_author} 自己的判断。
  没有标注 → 来源主体 unknown，不要猜。

## 三档按证据强度判

PASS：有正面证据表明归属没被改。稿子保留了原归属，或只是转述事实。
FAIL：有正面证据表明归属被改：
  - 冒认：第三方或材料作者的观点，被稿子写成目标作者自己认领（含「我认为」「我猜」「我的判断是」这类第一人称认领）。
  - 误植：材料作者的观点，被挂到材料里不存在的第三方名下（比如「有分析师认为」）。
REVIEW：两边证据都不够。材料本身没给来源；稿子删了来源但没认领；来源不明。
  来源信息缺失不等于归属错误。改写吸收不等于所有权转移。

## 输出

只输出 JSON，不要解释文字：
{{"claims":[
  {{"text":"稿子里的原句",
    "origin":"third_party|material_author|target_author|unknown",
    "origin_name":"路透社 / 李戛 / 未知",
    "expression":"own_claim|attributed|unattributed|unknown",
    "attributed_to":"稿子把这句挂给了谁；没挂就写 none",
    "verdict":"PASS|REVIEW|FAIL",
    "why":"一句话，指明证据在哪"}}],
 "verdict":"PASS|REVIEW|FAIL",
 "explanation":"一句话，必须和 claims 一致"}}

规则：每条 Claim 单独判；整体取最严重一档（FAIL > REVIEW > PASS）；解释里不要出现与 claims 相反的结论。
"""


def build_messages(material: str, draft: str, *, target_author: str,
                   material_author: str, generator: str) -> list[dict]:
    system = SYSTEM_TMPL.format(target_author=target_author,
                                material_author=material_author,
                                generator=generator)
    user = f"## 材料\n\n{material.strip()}\n\n## 生成稿\n\n{draft.strip()}\n"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse(raw: str) -> dict:
    """解析模型输出。结构不合法时 claims 为空，交给 aggregate 判 INVALID。"""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return {"claims": [], "verdict": None, "explanation": "", "raw": raw,
                "parse_error": "no JSON"}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"claims": [], "verdict": None, "explanation": "", "raw": raw,
                "parse_error": "bad JSON"}
    claims = []
    bad = []
    for row in data.get("claims", []) or []:
        if not isinstance(row, dict):
            bad.append("claim 不是对象")
            continue
        claim = {
            "text": str(row.get("text", ""))[:120],
            "origin": row.get("origin") if row.get("origin") in ORIGINS else None,
            "origin_name": str(row.get("origin_name", ""))[:40],
            "expression": row.get("expression") if row.get("expression") in EXPRESSIONS else None,
            "attributed_to": str(row.get("attributed_to", "none"))[:40],
            "verdict": row.get("verdict") if row.get("verdict") in VERDICTS else None,
            "why": str(row.get("why", ""))[:160],
        }
        if claim["origin"] is None or claim["expression"] is None or claim["verdict"] is None:
            bad.append("字段缺失或越界")
        claims.append(claim)
    verdict = data.get("verdict") if data.get("verdict") in VERDICTS else None
    return {"claims": claims, "verdict": verdict,
            "explanation": str(data.get("explanation", ""))[:300],
            "raw": raw, "parse_error": "; ".join(bad) if bad else None}


def judge_claim(claim: dict, *, material_author: str) -> str:
    """按证据规则重算单条 Claim 的档位。不认识的情况退 REVIEW。"""
    origin, expression = claim["origin"], claim["expression"]
    attributed_to = (claim["attributed_to"] or "none").strip()
    if origin in ("third_party", "material_author") and expression == "own_claim":
        return "FAIL"  # 冒认
    if origin == "material_author" and expression == "attributed" \
            and attributed_to not in ("none", material_author, ""):
        return "FAIL"  # 误植：材料作者的话挂到别人名下
    if origin == "unknown":
        return "REVIEW"  # 材料没给来源
    if expression in ("unattributed", "unknown"):
        return "REVIEW"  # 删了来源、没认领
    return "PASS"


def aggregate(claims: list[dict], model_verdict: str | None, *, material_author: str) -> dict:
    """重算整体档位，并做输出一致性校验。"""
    if not claims:
        return {"verdict": "INVALID", "output_validity": "INVALID",
                "recomputed": None, "model_verdict": model_verdict,
                "invalid_reasons": ["没有可用的 Claim 结构"]}
    rows = []
    for claim in claims:
        recomputed = judge_claim(claim, material_author=material_author)
        row = dict(claim)
        row["recomputed"] = recomputed
        row["disagrees"] = recomputed != claim["verdict"]
        rows.append(row)
    worst = "PASS"
    for row in rows:
        if row["recomputed"] == "FAIL":
            worst = "FAIL"
            break
        if row["recomputed"] == "REVIEW":
            worst = "REVIEW"
    reasons = []
    if model_verdict != worst:
        reasons.append(f"模型自报 {model_verdict}，按 claims 重算为 {worst}")
    if any(row["disagrees"] for row in rows):
        reasons.append("逐条 verdict 与证据规则不一致")
    validity = "INVALID" if reasons else "VALID"
    return {"verdict": worst, "output_validity": validity, "recomputed": worst,
            "model_verdict": model_verdict, "invalid_reasons": reasons,
            "claims": rows}


def check(material: str, draft: str, call, *, target_author: str,
          material_author: str, generator: str) -> dict:
    """跑一次判定。call(messages) -> 模型原文。"""
    raw = call(build_messages(material, draft, target_author=target_author,
                              material_author=material_author, generator=generator))
    parsed = parse(raw)
    report = aggregate(parsed["claims"], parsed["verdict"], material_author=material_author)
    report["explanation"] = parsed["explanation"]
    report["roles"] = {"target_author": target_author,
                       "material_author": material_author, "generator": generator}
    if parsed.get("parse_error"):
        report["parse_error"] = parsed["parse_error"]
    return report


def _selftest() -> int:
    roles = dict(target_author="潘辰", material_author="李戛", generator="CAS")
    # 1. 冒认：第三方统计写成目标作者自认
    fake = '{"claims":[{"text":"我认为，AI 导致的裁员已经占 4.5%。","origin":"third_party",' \
           '"origin_name":"Oxford Economics","expression":"own_claim","attributed_to":"none",' \
           '"verdict":"FAIL","why":"我认为"}],"verdict":"FAIL","explanation":"冒认"}'
    rep = check("材料", "稿", lambda m: fake, **roles)
    assert rep["verdict"] == "FAIL" and rep["output_validity"] == "VALID", rep
    # 2. 删来源不认领 -> REVIEW，不是 FAIL
    fake2 = '{"claims":[{"text":"AI 导致的裁员占 4.5%。","origin":"third_party",' \
            '"origin_name":"Oxford Economics","expression":"unattributed","attributed_to":"none",' \
            '"verdict":"FAIL","why":"删了来源"}],"verdict":"FAIL","explanation":"略"}'
    rep2 = check("材料", "稿", lambda m: fake2, **roles)
    assert rep2["verdict"] == "REVIEW", rep2
    assert rep2["output_validity"] == "INVALID", rep2  # 模型自报 FAIL 与重算 REVIEW 不一致
    # 3. 材料作者观点挂到不存在的第三方 -> 误植 FAIL
    fake3 = '{"claims":[{"text":"有分析师认为，裁员的刀落在老业务上。","origin":"material_author",' \
            '"origin_name":"李戛","expression":"attributed","attributed_to":"有分析师",' \
            '"verdict":"FAIL","why":"挂错人"}],"verdict":"FAIL","explanation":"误植"}'
    rep3 = check("材料", "稿", lambda m: fake3, **roles)
    assert rep3["verdict"] == "FAIL" and rep3["output_validity"] == "VALID", rep3
    # 4. 材料作者观点仍归材料作者 -> PASS
    fake4 = '{"claims":[{"text":"李戛认为，裁员的刀落在老业务上。","origin":"material_author",' \
            '"origin_name":"李戛","expression":"attributed","attributed_to":"李戛",' \
            '"verdict":"PASS","why":"归属保留"}],"verdict":"PASS","explanation":"保留"}'
    rep4 = check("材料", "稿", lambda m: fake4, **roles)
    assert rep4["verdict"] == "PASS" and rep4["output_validity"] == "VALID", rep4
    # 5. 材料缺来源 -> REVIEW
    fake5 = '{"claims":[{"text":"AI 导致的裁员占 4.5%。","origin":"unknown",' \
            '"origin_name":"未知","expression":"unattributed","attributed_to":"none",' \
            '"verdict":"REVIEW","why":"材料没来源"}],"verdict":"REVIEW","explanation":"缺来源"}'
    rep5 = check("材料", "稿", lambda m: fake5, **roles)
    assert rep5["verdict"] == "REVIEW" and rep5["output_validity"] == "VALID", rep5
    # 6. 输出不是 JSON -> INVALID，不装成 REVIEW
    rep6 = check("材料", "稿", lambda m: "模型今天不想输出 JSON", **roles)
    assert rep6["verdict"] == "INVALID" and rep6["output_validity"] == "INVALID", rep6
    # 7. 缺角色参数不该有默认值
    try:
        SYSTEM_TMPL.format(material_author="李戛")
    except KeyError:
        pass
    else:
        raise AssertionError("角色占位符应当必须传入")
    print("[OK] selftest v2：冒认 / 删来源不认领 / 误植 / 保留 / 缺来源 / 非 JSON / 角色必填")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="观点归属检查 v2（四角色 + 三态证据 + 输出一致性）")
    ap.add_argument("--material", type=Path)
    ap.add_argument("--draft", type=Path)
    ap.add_argument("--json", dest="json_out", type=Path)
    ap.add_argument("--target-author", default="", help="目标作者（稿子里的「我」）")
    ap.add_argument("--material-author", default="", help="材料作者（源文档作者）")
    ap.add_argument("--generator", default="CAS")
    ap.add_argument("--provider", default=rewrite.DEFAULT_PROVIDER)
    ap.add_argument("--model", default=rewrite.DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=rewrite.DEFAULT_TIMEOUT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.material or not args.draft:
        ap.error("要 --material 与 --draft")
    if not args.target_author or not args.material_author:
        ap.error("要 --target-author 与 --material-author：角色不让模型猜")
    material = args.material.read_text(encoding="utf-8")
    draft = args.draft.read_text(encoding="utf-8")
    call = rewrite._llm_callable(args.provider, args.model, args.temperature, args.timeout)
    report = check(material, draft, call, target_author=args.target_author,
                   material_author=args.material_author, generator=args.generator)
    report["material"] = str(args.material)
    report["draft"] = str(args.draft)
    print(f"结果：{report['verdict']}（输出一致性 {report['output_validity']}）")
    if report.get("invalid_reasons"):
        for reason in report["invalid_reasons"]:
            print(f"  ! {reason}")
    if report["explanation"]:
        print(f"  说明：{report['explanation']}")
    for row in report.get("claims", []):
        print(f"  · [{row['origin']}/{row['expression']} → {row['recomputed']}] {row['text'][:44]}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n",
        )
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
