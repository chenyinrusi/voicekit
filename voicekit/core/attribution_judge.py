"""Attribution judge: who asserted each claim, with three prompt variants.

The claims are fixed input (from the frozen extractor). Only the attribution
instructions change between arms:

    A  frozen P2 attribution rules (six attribution types, UNKNOWN allowed)
    B  A + hard rule: when the source is not determinable, output UNKNOWN,
       never default to the article author; also emit attribution_status
    C  B + name-prefix / identity-reference rules

Code validates what the model returns; it never fills in a speaker. An
asserted_by value that does not occur in the article is flagged, so the model
cannot invent a name.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

ENUMS = {
    "attribution_type": ("author", "direct_quote", "indirect_quote", "institution",
                         "editorial", "unknown"),
    "attribution_status": ("explicit", "inferred", "unnamed_source", "not_observable"),
    "prefix_kind": ("institution", "title", "role", "none"),
    "linked_by": ("same_sentence", "same_paragraph", "same_article", "none"),
}

SCHEMA = """输出 JSON，不要解释文字：
{"claims":[{"claim_id":"C1","asserted_by":"原文写法的人名/机构名/角色名或 UNKNOWN",
"attribution_type":"author|direct_quote|indirect_quote|institution|editorial|unknown"__EXTRA__}]}"""

BASE = """你是归属标注器。任务：给每条 Claim 标出「谁在说这句话」，以及这句话以什么方式出现。

Claim 已经给定，不要增删，不要改写 Claim。

归属：
- asserted_by 用原文的写法：人名、机构名、角色名（「一位大型险企分公司管理层人士」），指代不明写 UNKNOWN。
- 作者自己的叙述（没有引号、也没有「XX 表示/称/透露」）→ asserted_by 写「作者」，attribution_type 写 author。不要把作者叙述写成 UNKNOWN。
- attribution_type 六选一：
  author 作者自己的叙述；direct_quote 引号内的直接引语；indirect_quote 有「XX表示/称/透露」但无引号；
  institution 机构、公司、部门发布的数据或表态；editorial 编辑部集体，仅当文中明确写出；unknown 归属不明。
- 引语里的「我」是被引者，不是作者。
- 不要从署名、公众号或平台信息推断谁在说话。
"""

UNKNOWN = BASE + """
强制规则（本版新增）：
- 不确定谁在说时，必须写 asserted_by="UNKNOWN"。禁止默认归给「作者」。
  作者的判据只有一条：这句话是作者自己的叙述，没有归属动词，也不是引语。
- 额外写 attribution_status，四选一：
  explicit 原文点名或写出明确角色；
  inferred 本句没点名，但上下文能唯一确定是谁；
  unnamed_source 有来源类别但没点名（有分析师认为、业内人士指出、据报道、公开数据显示）；
  not_observable 没有任何归属线索（被动句「被吐槽为」、悬空引语）。
- 判断顺序：点名 → explicit；上下文唯一可补 → inferred；只有来源类别 → unnamed_source；都没有 → not_observable。
- unnamed_source 与 not_observable 的 asserted_by 一律 UNKNOWN。
"""

IDENTITY = UNKNOWN + """
名字与前缀（本版新增）：
- 同一个人以多种写法出现时，填 identity_reference：
  {"surface":"这句里的写法","canonical":"原文首次出现的完整写法",
   "prefix":"原文自带的前缀（高盛分析师、总裁、一位网友）或空字符串",
   "prefix_kind":"institution|title|role|none",
   "linked_by":"same_sentence|same_paragraph|same_article|none"}
- 只有能唯一确定是同一人时才填；同一篇里有两个同姓人物，linked_by 写 none。
- 不许自己补全身份：原文只写「Schneider」就不要补成「高盛分析师James Schneider」，
  除非同一篇里出现过完整写法且没有第二个人同姓。
- 其余 Claim 的 identity_reference 写 null。
"""

ARMS = {"A": BASE, "B": UNKNOWN, "C": IDENTITY}


def prompt_for(arm: str) -> str:
    return ARMS[arm].strip() + "\n\n" + SCHEMA.replace(
        "__EXTRA__",
        "" if arm == "A" else ', "attribution_status":"explicit|inferred|unnamed_source|not_observable"'
        + ("" if arm != "C" else ', "identity_reference":{...}' ))


def prompt_sha256(arm: str) -> str:
    return hashlib.sha256(prompt_for(arm).encode("utf-8")).hexdigest()


def build_messages(article: str, claims: list[dict], arm: str) -> list[dict]:
    listed = "\n".join(
        f'- {c["claim_id"]} | 命题：{c["claim_text"]} | 证据：{c.get("evidence_text", "")}'
        for c in claims)
    user = f"## 文章\n\n{article}\n\n## 待标注的 Claim\n\n{listed}\n"
    return [{"role": "system", "content": prompt_for(arm)}, {"role": "user", "content": user}]


def parse(raw: str) -> list[dict]:
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
        cid = str(row.get("claim_id") or "").strip()
        if not cid:
            continue
        out.append({
            "claim_id": cid,
            "asserted_by": str(row.get("asserted_by") or "").strip(),
            "attribution_type": str(row.get("attribution_type") or "").strip(),
            "attribution_status": str(row.get("attribution_status") or "").strip(),
            "identity_reference": row.get("identity_reference") or None,
        })
    return out


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def validate(article: str, labels: list[dict], arm: str) -> tuple[list[dict], dict]:
    clean_article = _norm(article)
    out, problems = [], []
    for row in labels:
        review = []
        if not row["asserted_by"]:
            review.append("asserted_by 缺失")
        elif row["asserted_by"] not in ("作者", "UNKNOWN") and _norm(row["asserted_by"]) not in clean_article:
            review.append(f"asserted_by 不在原文里：{row['asserted_by'][:20]}")
        if row["attribution_type"] not in ENUMS["attribution_type"]:
            review.append(f"attribution_type 非法或缺失：{row['attribution_type']!r}")
        if arm != "A":
            if row["attribution_status"] not in ENUMS["attribution_status"]:
                review.append(f"attribution_status 非法或缺失：{row['attribution_status']!r}")
        if arm == "C" and row["identity_reference"]:
            ir = row["identity_reference"]
            if not isinstance(ir, dict):
                review.append("identity_reference 不是对象")
            else:
                for k in ("surface", "canonical", "prefix"):
                    if _norm(str(ir.get(k) or "")) and _norm(str(ir.get(k))) not in clean_article:
                        review.append(f"identity_reference.{k} 不在原文里")
                if ir.get("prefix_kind") not in ENUMS["prefix_kind"]:
                    review.append(f"prefix_kind 非法：{ir.get('prefix_kind')!r}")
                if ir.get("linked_by") not in ENUMS["linked_by"]:
                    review.append(f"linked_by 非法：{ir.get('linked_by')!r}")
        out.append({**row, "status": "REVIEW" if review else "VALID", "review_reasons": review})
        for r in review:
            problems.append({"claim_id": row["claim_id"], "reason": r})
    return out, {"labels": len(out), "valid": sum(1 for x in out if x["status"] == "VALID"),
                 "review": sum(1 for x in out if x["status"] == "REVIEW"),
                 "problems": problems}


def judge(article: str, claims: list[dict], arm: str, call, batch_size: int = 60) -> dict:
    """claims: [{"claim_id","claim_text","evidence_text"}], returns labels per claim."""
    labels, raws, usage = [], [], {}
    for i in range(0, len(claims), batch_size):
        batch = claims[i:i + batch_size]
        raw, u = call(build_messages(article, batch, arm))
        raws.append(raw)
        for k, v in (u or {}).items():
            usage[k] = usage.get(k, 0) + v
        labels += parse(raw)
    out, stats = validate(article, labels, arm)
    stats.update({"arm": arm, "prompt_sha256": prompt_sha256(arm),
                  "asked": len(claims), "answered": len(out),
                  "missing": [c["claim_id"] for c in claims
                              if c["claim_id"] not in {x["claim_id"] for x in out}],
                  "usage": usage})
    return {"arm": arm, "labels": out, "stats": stats, "raw_output": "\n".join(raws)}


def make_call(provider: str | None = None, model: str | None = None):
    import asyncio
    repo_root = SCRIPT_DIR.parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))
    from ..compat import release_llm_client
    from ..compat import create_llm, ensure_provider_sdks_loaded

    async def _run(messages: list[dict]):
        from ..compat import Message
        await ensure_provider_sdks_loaded()
        llm = create_llm(provider or "deepseek", model or "deepseek-chat")
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
    article = ("11月5日晚，董宇辉首发自有品牌蛋糕。\n\n"
               "有分析师认为，这笔生意被高估了。\n\n"
               "高盛分析师James Schneider表示，销量会回落。Schneider认为毛利撑不住。\n\n"
               "该产品被吐槽为「充满科技与狠活」。")

    if prompt_sha256("A") == prompt_sha256("B") or prompt_sha256("B") == prompt_sha256("C"):
        problems.append("三组 prompt 不应相同")

    labels = parse(json.dumps({"claims": [
        {"claim_id": "C1", "asserted_by": "董宇辉", "attribution_type": "author"},
        {"claim_id": "C2", "asserted_by": "有分析师", "attribution_type": "unknown"},
        {"claim_id": "C3", "asserted_by": "张三", "attribution_type": "indirect_quote"},
        {"claim_id": "C4", "asserted_by": "UNKNOWN", "attribution_type": "unknown"},
    ]}, ensure_ascii=False))
    if len(labels) != 4:
        problems.append("解析条数不对")
    out, stats = validate(article, labels, "A")
    by_id = {x["claim_id"]: x for x in out}
    if by_id["C2"]["status"] != "VALID":
        problems.append("原文里存在的「有分析师」被误标为 REVIEW")
    if by_id["C3"]["status"] != "REVIEW":
        problems.append("编造的人名没被标出")
    if by_id["C1"]["status"] != "VALID" or by_id["C4"]["status"] != "VALID":
        problems.append("合法 LABEL 被误标")

    out_b, stats_b = validate(article, parse(json.dumps({"claims": [
        {"claim_id": "C1", "asserted_by": "董宇辉", "attribution_type": "author"},
    ]}, ensure_ascii=False)), "B")
    if out_b[0]["status"] != "REVIEW":
        problems.append("B 组缺 attribution_status 没被标出")

    out_c, _ = validate(article, parse(json.dumps({"claims": [
        {"claim_id": "C5", "asserted_by": "Schneider", "attribution_type": "indirect_quote",
         "attribution_status": "inferred",
         "identity_reference": {"surface": "Schneider", "canonical": "James Schneider",
                                "prefix": "高盛分析师", "prefix_kind": "institution",
                                "linked_by": "same_paragraph"}},
        {"claim_id": "C6", "asserted_by": "Schneider", "attribution_type": "indirect_quote",
         "attribution_status": "inferred",
         "identity_reference": {"surface": "Schneider", "canonical": "张伟",
                                "prefix": "", "prefix_kind": "none", "linked_by": "none"}},
    ]}, ensure_ascii=False)), "C")
    if out_c[0]["status"] != "VALID":
        problems.append("合法的身份关联被误标")
    if out_c[1]["status"] != "REVIEW":
        problems.append("原文里不存在的 canonical 没被标出")

    if problems:
        print("SELFTEST FAILED:", problems)
        return 1
    print("attribution judge selftest ok")
    return 0


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--prompt-sha", nargs="*", default=None)
    ap.add_argument("--article")
    ap.add_argument("--claims", help="JSON 文件：[{claim_id,claim_text,evidence_text}]")
    ap.add_argument("--arm", default="A", choices=["A", "B", "C"])
    ap.add_argument("--out")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(_selftest())
    if args.prompt_sha is not None:
        for arm in (args.prompt_sha or ["A", "B", "C"]):
            print(arm, prompt_sha256(arm))
        raise SystemExit(0)
    article = Path(args.article).read_text(encoding="utf-8")
    claims = json.loads(Path(args.claims).read_text(encoding="utf-8"))
    result = judge(article, claims, args.arm, make_call())
    payload = json.dumps(result, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"{args.out}: {result['stats']}")
    else:
        print(payload)
