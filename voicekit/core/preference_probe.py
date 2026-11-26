"""出题器：从作者语料里挑 10 组「二选一」，每题一个判断维度。

本轮只验证出题机制与记录流程，不宣称学会了作者。规则与语料切分在出题前冻结：

- 语料：61 篇；034、037 永久排除（已被调参污染）。
- 切分（按篇号排序后）：训练前 40 篇 / 验证中 9 篇 / 测试末 10 篇。
- 出题只用非测试篇；每题两侧尽量来自不同篇、长度相近（避免「长的更好」偏见）。
- 机器只给「语料里哪一侧更常见」这一个先验；猜测写入 questions.json，不印给人看（盲答）。

指标叫 `Potential Coverage`：一个维度在非测试篇里出现得多广，只是「潜在影响范围」，不是信息增益。

用法（repo 根目录）：
    python scripts/voice/preference_probe.py --out-dir dev/fixes/<pkg>/evidence
    python scripts/voice/preference_probe.py --score <answers.jsonl> --questions <questions.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import voice_review as vr  # noqa: E402 - 复用切段/切句，口径与报告一致

DEFAULT_CORPUS = (
    REPO_ROOT / "voices/panchen-voice/corpus"
)
#: 已被调参污染的篇：永久退出验证（FIX-368 切段规则、可疑句优先级都看着 034 改过）。
EXCLUDED = ("034", "037")
#: 切分边界（出题前固定，跑完不许改）。
TRAIN_N, VAL_N = 40, 9

#: 每题的展示上限：太长就是整章，不是选择题。
SIDE_CHARS = 88

#: (key, 题干, 单位, 位置, A 侧特征正则, B 侧对照正则)
#: 单位 sentence＝一句；paragraph＝整段（用于「一句成段」「段末补道理」这类段落级判断）。
DIMENSIONS = (
    ("opening_scene", "开头：先讲具体的事，还是先给一句判断", "sentence", "first",
     r"^(最近|前些天|今天|昨天|有一次|前几天|前段时间|年初|那时候|那一年)",
     r"^(很多人|我们|人生|爱情|成长|每个人|一个人|很多)"),
    ("ending_open", "结尾：留一点余地，还是把话说过死", "sentence", "last",
     r"(或许|也许|算不上|谈不上|大概|未必|由它去吧)",
     r"(一定|正是|总会|必然|才是|便是如此|毫无疑问)"),
    ("long_sentence", "句子：长句用逗号串，还是切短句用句号收", "sentence", "any",
     r"^.{60,}$",
     r"^.{0,28}$"),
    ("question_close", "收束：用一个问句收，还是用陈述句收", "sentence", "last",
     r"[？?]$",
     r"[。]$"),
    ("second_person", "对读者说话，还是只说「我」", "sentence", "any",
     r"(你|你们)",
     r"^我"),
    ("classical", "用成语文言，还是换白话", "sentence", "any",
     r"(以至|穷极|必有|积跬步|明心见性)",
     r"(我|我们)"),
    ("reduplication", "用叠词，还是用单字动词", "sentence", "any",
     r"(颤颤|瑟瑟|匆匆|悄悄|轻轻|慢慢|安安静静|陆陆续续)",
     r"(我|我们)"),
    ("short_paragraph", "一句成段，还是并进上段", "paragraph", "any",
     r"^.{0,30}$",
     r"^.{60,140}$"),
    ("abstract_close", "段末补一句道理，还是停在事上", "paragraph", "any",
     r"(这便是|这些话|所谓|道理|人生|成长|值得|应该)",
     r"(我|我们)"),
    ("detail", "写具体的东西，还是写概括的感受", "sentence", "any",
     r"(面包|巧克力|火车|琴弓|松香|咖啡|陶笛|地图|公交|电梯|超市|阳台|热巧)",
     r"(情感|关系|人生|选择|成长|期待|心意)"),
)


def _cjk(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def _chapters(corpus: Path) -> list[dict]:
    """按篇号排序；排除污染篇；记录 fixed split 归属。"""
    files = sorted(
        (p for p in corpus.glob("*.md") if p.stem[:3].isdigit()),
        key=lambda p: p.stem[:3],
    )
    kept = [p for p in files if p.stem[:3] not in EXCLUDED]
    out = []
    for index, path in enumerate(kept):
        if index < TRAIN_N:
            split = "train"
        elif index < TRAIN_N + VAL_N:
            split = "val"
        else:
            split = "test"
        out.append(
            {
                "no": path.stem[:3],
                "title": path.stem.split("__", 1)[-1],
                "split": split,
                "text": path.read_text(encoding="utf-8"),
            }
        )
    return out


def _units(chapter: dict, unit: str, where: str) -> list[str]:
    paras = [p for p in vr._paragraphs(chapter["text"]) if _cjk(p) >= 8]
    if unit == "paragraph":
        return paras
    sentences: list[str] = []
    for para in paras:
        sentences.extend(vr._sentences(para))
    sentences = [s for s in sentences if _cjk(s) >= 8]
    if where == "first":
        return sentences[:1]
    if where == "last":
        return sentences[-1:]
    return sentences


def _pick(
    chapters: list[dict],
    unit: str,
    where: str,
    pattern: re.Pattern,
    *,
    want: bool,
    max_chars: int,
    avoid_no: str = "",
) -> dict | None:
    """按固定顺序挑第一条满足/不满足的例子；长度不超过 max_chars。"""
    for chapter in chapters:
        if chapter["no"] == avoid_no:
            continue
        for text in _units(chapter, unit, where):
            if not 8 <= _cjk(text) <= max_chars:
                continue
            hit = bool(pattern.search(text))
            if hit != want:
                continue
            return {"text": text, "no": chapter["no"], "title": chapter["title"]}
    return None


def _coverage(chapters: list[dict], unit: str, where: str, pattern: re.Pattern) -> float:
    hits = sum(
        1
        for c in chapters
        if any(pattern.search(t) for t in _units(c, unit, where))
    )
    return hits / len(chapters) if chapters else 0.0


def _freeze() -> dict:
    """冻结指纹：出题前记录，跑完不许改本轮实验规则。"""
    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]

    return {
        "probe": sha(Path(__file__)),
        "voice_review": sha(SCRIPT_DIR / "voice_review.py"),
        "rubrics": sha(SCRIPT_DIR / "rubrics.yaml"),
        "baseline": sha(REPO_ROOT / "voices/panchen-voice/judge_baseline.json"),
        "excluded": list(EXCLUDED),
        "split": {"train": TRAIN_N, "val": VAL_N, "test": "rest"},
    }


def build_questions(corpus: Path) -> dict:
    chapters = _chapters(corpus)
    pool = [c for c in chapters if c["split"] != "test"]  # 出题只用非测试篇
    questions = []
    used: set[str] = set()
    for key, label, unit, where, a_regex, b_regex in DIMENSIONS:
        a_pattern = re.compile(a_regex)
        b_pattern = re.compile(b_regex)
        side_a = _pick(pool, unit, where, a_pattern, want=True, max_chars=SIDE_CHARS)
        side_b = _pick(
            pool,
            unit,
            where,
            b_pattern,
            want=True,
            max_chars=SIDE_CHARS,
            avoid_no=side_a["no"] if side_a else "",
        )
        if not side_a or not side_b:
            continue
        coverage = _coverage(pool, unit, where, a_pattern)
        questions.append(
            {
                "id": key,
                "label": label,
                "unit": unit,
                "A": side_a,
                "B": side_b,
                "potential_coverage": round(coverage, 3),
                "machine_guess": "A" if coverage > 0.5 else "B",
                "guess_reason": f"该维度在非测试篇里占 {coverage:.0%}；按「语料里更常见的写法」猜",
            }
        )
        used.update((side_a["no"], side_b["no"]))
    return {"frozen": _freeze(), "questions": questions}


def candidates(corpus: Path, per_side: int = 6) -> str:
    """列每维的候选（给人工挑配对用）；不自动组题。"""
    pool = [
        c for c in _chapters(corpus) if c["split"] != "test" and c["no"] != "013"
    ]
    lines: list[str] = []
    for key, label, unit, where, a_regex, b_regex in DIMENSIONS:
        lines.append(f"## {key}｜{label}")
        for side, regex in (("A", a_regex), ("B", b_regex)):
            pattern = re.compile(regex)
            shown = []
            for chapter in pool:
                for text in _units(chapter, unit, where):
                    if 8 <= _cjk(text) <= SIDE_CHARS + 20 and pattern.search(text):
                        shown.append(f"  - {chapter['no']}｜{text}")
                        break
                if len(shown) >= per_side:
                    break
            lines.append(f"{side}：")
            lines.extend(shown or ["  - （无）"])
        lines.append("")
    return "\n".join(lines)


def render_md(payload: dict) -> str:
    """给人看的问题单：不印机器猜测（盲答）。"""
    lines = ["# 判断题（10 题）", ""]
    lines.append("每题两侧都是你自己写过的原文，选「你更希望自己写成哪样」的那一侧。")
    lines.append("可答 A / B / 不确定 / 视情况而定；有偏好可以补一句为什么。")
    lines.append("")
    for index, q in enumerate(payload["questions"], 1):
        lines.append(f"## {index}. {q['label']}")
        lines.append("")
        lines.append(f"- A（{q['A']['no']} {q['A']['title']}）：{q['A']['text']}")
        lines.append(f"- B（{q['B']['no']} {q['B']['title']}）：{q['B']['text']}")
        lines.append("")
    lines.append("---")
    lines.append(
        "出题前冻结："
        + json.dumps(payload["frozen"], ensure_ascii=False)
        + "；034/037 已排除。"
    )
    return "\n".join(lines) + "\n"


def score(questions_path: Path, answers_path: Path) -> int:
    payload = json.loads(questions_path.read_text(encoding="utf-8"))
    by_id = {q["id"]: q for q in payload["questions"]}
    rows = [
        json.loads(line)
        for line in answers_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    hit = decided = 0
    for row in rows:
        question = by_id.get(row["id"])
        if not question or row.get("choice") not in {"A", "B"}:
            continue
        decided += 1
        if row["choice"] == question["machine_guess"]:
            hit += 1
    print(f"回答 {len(rows)} 条，可判 {decided} 条（不确定/视情况不计）")
    print(f"语料先验猜中 {hit}/{decided}" + (f"，命中率 {hit / decided:.0%}" if decided else ""))
    print("本轮只验出题与记录流程；猜中率是后人要超过的基准，不是「学会了」")
    return 0


def main(argv: list[str] | None = None) -> int:
    vr._utf8_stdio()
    ap = argparse.ArgumentParser(description="出 10 组二选一，验出题机制")
    ap.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--questions", type=Path, default=None)
    ap.add_argument("--score", type=Path, default=None, help="answers.jsonl")
    ap.add_argument("--candidates", type=int, default=0, help="只列每维候选，不组题")
    args = ap.parse_args(argv)
    if args.score:
        if not args.questions:
            print("[FAIL] --score 需要 --questions", file=sys.stderr)
            return 2
        return score(args.questions, args.score)
    if not args.corpus.is_dir():
        print(f"[FAIL] corpus not found: {args.corpus}", file=sys.stderr)
        return 2
    if args.candidates:
        print(candidates(args.corpus, args.candidates))
        return 0
    payload = build_questions(args.corpus)
    if not payload["questions"]:
        print("[FAIL] 没生成题目：检查语料与维度正则", file=sys.stderr)
        return 2
    if args.out_dir:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "questions.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        (args.out_dir / "questions.md").write_text(render_md(payload), encoding="utf-8")
        print(f"题目：{args.out_dir / 'questions.md'}（盲答版）")
        print(f"机器猜测：{args.out_dir / 'questions.json'}")
    else:
        print(render_md(payload), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
