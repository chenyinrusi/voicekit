"""观点归属检查：生成稿之后跑的第二道检测，不是第二个裁判。

边界（作者 2026-11-14 定）：
- 只判「这句话是谁的观点」，不评文风、不评好坏、不评像不像作者。
- 输入必须同时有原始材料与生成稿。
- 只允许三种结果：PASS / REVIEW / FAIL。
- 与人工判断不一致时以人为准，差异另存。
- 解析不出结果时按 REVIEW 处理（fail-closed）：不确定就交给人。

用法：
    py -3.10 scripts/voice/attribution_check.py --material 材料.md --draft 稿.md
    py -3.10 scripts/voice/attribution_check.py --material ... --draft ... --json 报告.json
    py -3.10 scripts/voice/attribution_check.py --selftest
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

RESULTS = ("PASS", "REVIEW", "FAIL")
WHO = ("作者", "他人", "事实", "无法确定")
RELATION = ("自述判断", "直接引用", "改写吸收", "无法确定")

SYSTEM = (
    "你是观点归属检查器。你只回答一件事：生成稿里的每句话，观点是谁的。\n"
    "不评文风，不评好坏，不评像不像作者。发现只有一处错误也不会让你评价其他内容。\n\n"
    "材料里可能出现这些标注：「某评论员认为…」「据某人的说法…」——标注意味着那是别人的观点；"
    "标为「作者…原话」的是作者自己的判断；没有标注的按内容判断。\n\n"
    "对生成稿逐句判断两件事：\n"
    "  who：作者 / 他人 / 事实 / 无法确定\n"
    "  relation：自述判断 / 直接引用 / 改写吸收 / 无法确定\n"
    "「事实」指公开报道、材料里的事实句——它不属于任何一个在说话的人。事实句不参与冒认判断。\n"
    "「改写吸收」指：句子明显来自别人的观点，但被改写成不带归属的样子。\n\n"
    "最后只给三种结果之一：\n"
    "  PASS：每句判断都能在材料里找到同一归属，没有把别人的观点写成作者的。\n"
    "  REVIEW：有改写吸收，或归属无法确定。\n"
    "  FAIL：材料里标为别人/外部的观点，被写成作者自己的判断，且没有归属标记。\n\n"
    "只输出 JSON，不要任何解释文字：\n"
    '{"result":"PASS|REVIEW|FAIL","sentences":[{"text":"原句","who":"作者|他人|事实|无法确定",'
    '"relation":"自述判断|直接引用|改写吸收|无法确定","why":"一句理由"}],"summary":"一句话"}\n'
)


def build_messages(material: str, draft: str) -> list[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"## 材料\n\n{material.strip()}\n\n## 生成稿\n\n{draft.strip()}\n"},
    ]


def parse(raw: str) -> dict:
    """解析模型输出。解析不出来就 REVIEW——这是 fail-closed，不是判它有问题。"""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {"result": "REVIEW", "sentences": [], "summary": "解析失败，交给人看", "raw": raw}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"result": "REVIEW", "sentences": [], "summary": "JSON 坏了，交给人看", "raw": raw}
    result = str(data.get("result", "")).upper()
    if result not in RESULTS:
        result = "REVIEW"
    rows = []
    for row in data.get("sentences", []) or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            "text": str(row.get("text", ""))[:80],
            "who": row.get("who") if row.get("who") in WHO else "无法确定",
            "relation": row.get("relation") if row.get("relation") in RELATION else "无法确定",
            "why": str(row.get("why", ""))[:120],
        })
    return {"result": result, "sentences": rows, "summary": str(data.get("summary", ""))[:200]}


def _selftest() -> int:
    ok = parse('前面废话 {"result":"FAIL","sentences":[{"text":"x","who":"作者",'
               '"relation":"改写吸收","why":"y"}],"summary":"s"} 后面废话')
    assert ok["result"] == "FAIL", ok
    assert ok["sentences"][0]["relation"] == "改写吸收", ok
    bad = parse("模型今天不想输出 JSON")
    assert bad["result"] == "REVIEW", bad
    wrong = parse('{"result":"很好","sentences":[]}')
    assert wrong["result"] == "REVIEW", wrong
    msgs = build_messages("材料", "稿子")
    assert "## 材料" in msgs[1]["content"] and "## 生成稿" in msgs[1]["content"]
    assert "不评文风" in msgs[0]["content"]
    print("[OK] selftest: 解析、fail-closed、非法结果、两种输入都在")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="观点归属检查（PASS / REVIEW / FAIL）")
    ap.add_argument("--material", type=Path, help="原始材料")
    ap.add_argument("--draft", type=Path, help="生成稿")
    ap.add_argument("--json", dest="json_out", type=Path, help="机器可读报告写哪")
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
    material = args.material.read_text(encoding="utf-8")
    draft = args.draft.read_text(encoding="utf-8")
    call = rewrite._llm_callable(args.provider, args.model, args.temperature, args.timeout)
    report = parse(call(build_messages(material, draft)))
    report["material"] = str(args.material)
    report["draft"] = str(args.draft)
    print(f"结果：{report['result']}")
    if report["summary"]:
        print(f"  理由：{report['summary']}")
    for row in report["sentences"]:
        if row["who"] == "作者" and row["relation"] == "自述判断":
            continue
        print(f"  · [{row['who']}/{row['relation']}] {row['text'][:50]}")
        if row["why"]:
            print(f"      {row['why']}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8", newline="\n",
        )
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
