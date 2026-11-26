"""素材提问清单：写稿前先问这九类（三个轴），问不够就别动笔。

为什么：四轮盲测里，盲读者说穿的原因都不是句子，是「没有他的私人材料」。
风格能编，私事编不出来——所以生成前必须先把私有信息问出来。

分三个轴、九个类（2026-11-09 从 064《后记2》与「咖啡稿 78.6 / 重写 81.0」的对照里抽象出来）：

  事实层（世界怎么发生的）：具体物、代价、他人原话
  态度层（他怎么想自己）：  自嘲、辩解、欲望
  连接层（他怎么把话串起来）：岔路、对照、归因

门（机械判据）：≥4 类有实质回答（每类 ≥2 条），且**事实层与态度层各至少 1 类**。
只有事实层 → 腔对人不对（那篇咖啡稿就是这样：78.6、判官 not-like）。
连接层的类只提示，不拦。

用法：
    py -3.10 scripts/voice/intake.py --print                  # 九类问题
    py -3.10 scripts/voice/intake.py --template tmp/in.md     # 空白模板（Q／A 行）
    py -3.10 scripts/voice/intake.py --check tmp/in.md        # 不够就 rc=1
    py -3.10 scripts/voice/intake.py --selftest

答法：每题下面一行 `A：你的回答`；`--check` 只数 `A：` 后面有实质内容（≥4 字）的行。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: 轴 → 类（顺序就是问卷顺序）。
AXES: dict[str, tuple[str, ...]] = {
    "事实层": ("具体物", "代价", "他人原话"),
    "态度层": ("自嘲", "辩解", "欲望"),
    "连接层": ("岔路", "对照", "归因"),
}

CATEGORIES: dict[str, list[str]] = {
    # ---- 事实层：世界怎么发生的
    "具体物": [
        "那天几点、在哪儿、地方长什么样？你身上或手里拿着什么？",
        "花了多少钱、多少时间？有没有牌子、颜色、大小能说清的物件？",
        "当时天气、声音、气味怎么样？",
    ],
    "代价": [
        "你为这件事吃了什么亏——钱、时间、面子、身体？",
        "身上哪块地方遭罪（疼、破、肿、酸）？后来落下什么毛病没有？",
        "你花了多久才缓过来？缓过来之前做过什么蠢事？",
    ],
    "他人原话": [
        "这事里有谁？他在场时做的哪件事你还记得？",
        "他/她说了什么原话？你怎么回的？",
        "他/她当时可能是什么心情？你替他/她想过什么？",
    ],
    # ---- 态度层：他怎么想自己
    "自嘲": [
        "你干了什么蠢事、说了什么后悔的话？",
        "你怎么给自己找台阶的？当时嘴上说的是什么？",
        "你心里骂了自己一句什么？",
    ],
    "辩解": [
        "你怕被误会成什么？",
        "你后来补了哪句解释？或者想说没说的那句是什么？",
        "你有没有替自己辩护、又觉得辩护很没意思的时刻？",
    ],
    "欲望": [
        "你当时最想要的是什么（具体到一件事）？",
        "得到了吗？得到的那一刻你在想什么？",
        "为了这个你愿意放弃什么？最后放弃了吗？",
    ],
    # ---- 连接层：他怎么把话串起来
    "岔路": [
        "这件事让你想起哪件不相干的事？",
        "为什么会想起——两件事的共同点是什么？",
        "岔开之后你是怎么拐回来的？",
    ],
    "对照": [
        "你拿它跟哪段过去比？",
        "你拿自己跟谁比？比完更确认了什么？",
        "以前你会怎么处理？现在为什么不一样？",
    ],
    "归因": [
        "你觉得这事为什么会这样？",
        "你后来改过看法吗？改成什么了？",
        "如果重来一次，你会先做什么？",
    ],
}

#: 至少几类有实质回答（每类 ≥MIN_LINES 条）。
MIN_CATEGORIES = 4
#: 这两层各至少要有一类：事实是「可认」，态度是「是他」。
REQUIRED_AXES = ("事实层", "态度层")
MIN_LINES = 2
MIN_CHARS = 4

#: 他的语料：出处字段写「｜出处：064」，要能在里面找到这一篇。
CORPUS = (
    Path(__file__).resolve().parents[2]
    / "voices/panchen-voice/corpus"
)

_HEADING = re.compile(r"^#{2,3}\s*(.+?)\s*$")
_ANSWER = re.compile(r"^\s*[-*•]?\s*(?:A[:：]|答[:：])\s*(.*)$")
#: 一行答案后面可以挂出处：「｜出处：064」「| 出处: 013 那篇」。
_SOURCE = re.compile(r"\s*[|｜]\s*出处\s*[:：]\s*(.+?)\s*$")


def axis_of(category: str) -> str:
    for axis, members in AXES.items():
        if category in members:
            return axis
    return ""


def parse_answers(text: str) -> dict[str, list[str]]:
    """按二级/三级标题切类，只收 `A：` 行；标题要能对上九类之一（允许后缀）。"""
    out: dict[str, list[str]] = {key: [] for key in CATEGORIES}
    current = ""
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1)
            current = next((key for key in CATEGORIES if key in title), "")
            continue
        if not current:
            continue
        answer = _ANSWER.match(line)
        if not answer:
            continue
        payload = answer.group(1).strip()
        if len(payload) >= MIN_CHARS:
            out[current].append(payload)
    return out


def covered(answers: dict[str, list[str]]) -> list[str]:
    return [key for key, lines in answers.items() if len(lines) >= MIN_LINES]


def check(answers: dict[str, list[str]]) -> tuple[bool, list[str]]:
    """门：≥4 类，且事实层与态度层各至少一类。返回 (过没过, 缺什么)。"""
    done = covered(answers)
    problems = []
    if len(done) < MIN_CATEGORIES:
        problems.append(f"只答了 {len(done)} 类（要 ≥{MIN_CATEGORIES}）：{('、'.join(done) or '无')}")
    for axis in REQUIRED_AXES:
        if not any(axis_of(key) == axis for key in done):
            members = "、".join(AXES[axis])
            problems.append(f"「{axis}」一类都没答（{members}）")
    return (not problems), problems


def missing(answers: dict[str, list[str]]) -> list[str]:
    """没答的类（只作提示，不拦）。"""
    return [key for key, lines in answers.items() if len(lines) < MIN_LINES]


def answer_sources(answers: dict[str, list[str]]) -> list[tuple[str, str]]:
    """把带出处的答案抽出来：[(答案, 出处)]。没写出处的答案不返回。"""
    found = []
    for lines in answers.values():
        for line in lines:
            match = _SOURCE.search(line)
            if match:
                found.append((_SOURCE.sub("", line).strip(), match.group(1)))
    return found


def check_sources(answers: dict[str, list[str]]) -> list[str]:
    """出处指不到他的语料就提示；只提示，不拦（材料可能是新事，还没有篇号）。"""
    known = {path.name.split("__")[0] for path in CORPUS.glob("*.md")} if CORPUS.is_dir() else set()
    problems = []
    for _, source in answer_sources(answers):
        doc = re.search(r"\d{3}", source)
        if doc and known and doc.group(0) not in known:
            problems.append(f"出处「{source}」在他的语料里找不到对应篇目")
    return problems


def template() -> str:
    lines = [
        "# 素材提问（写完再动笔）",
        "",
        "答法：`A：` 后面写内容，想标来路就在行尾补 `｜出处：064`（他的语料里第几篇）。",
        "带出处的材料，改稿时可以当「有出处」放行；没出处的只能当新事，别编成他以前说过。",
        "",
    ]
    for axis, members in AXES.items():
        lines.append(f"# {axis}")
        lines.append("")
        for key in members:
            lines.append(f"## {key}")
            lines.append("")
            for question in CATEGORIES[key]:
                lines.append(f"Q：{question}")
                lines.append("A：")
            lines.append("")
    return "\n".join(lines) + "\n"


def print_bank() -> None:
    for axis, members in AXES.items():
        print(f"\n# {axis}")
        for key in members:
            print(f"\n## {key}")
            for question in CATEGORIES[key]:
                print(f"- {question}")


def _selftest() -> int:
    problems = []
    if len(CATEGORIES) != 9:
        problems.append(f"类数 {len(CATEGORIES)}")
    if any(len(q) < 2 for q in CATEGORIES.values()):
        problems.append("有的类问题少于 2 个")
    if sum(len(v) for v in AXES.values()) != len(CATEGORIES):
        problems.append("轴里的类跟 CATEGORIES 对不上")
    # 只有事实层：不过（就是那篇咖啡稿）
    facts_only = "## 具体物\nA：下午三点，巷口咖啡厅\nA：一杯热美式\n"
    ok, why = check(parse_answers(facts_only))
    if ok:
        problems.append("只有事实层竟然过门了")
    if not any("态度层" in p for p in why):
        problems.append(f"没过门但没点出态度层：{why}")
    # 事实 3 + 态度 1 = 4 类：过
    good = (
        "## 具体物\nA：下午三点，巷口咖啡厅\nA：一杯热美式，二十来块\n"
        "## 代价\nA：晚上熬夜补活到两点\nA：后颈又僵了\n"
        "## 他人原话\nA：老板说今天人少\nA：要坐到打烊吗\n"
        "## 自嘲\nA：想请假又没敢说\nA：心里骂自己怂\n"
    )
    ok, why = check(parse_answers(good))
    if not ok:
        problems.append(f"四类齐全却没过：{why}")
    # 四类全在事实层（具体物/代价/他人原话 + 一个假的事实类不行）→ 缺态度层
    # 空白模板不许过门（问题行不是答案）
    if check(parse_answers(template()))[0]:
        problems.append("空白模板竟然过门了")
    if len(parse_answers(template())) != 9:
        problems.append("模板解析不出九类")
    # 出处：行尾「｜出处：064」能抽出来；假篇号会被点出来
    sourced = answer_sources(parse_answers("## 具体物\nA：下午三点，巷口咖啡厅｜出处：064\n"))
    if sourced != [("下午三点，巷口咖啡厅", "064")]:
        problems.append(f"出处没抽对：{sourced}")
    if not check_sources(parse_answers("## 具体物\nA：那天下雨｜出处：999\n")):
        problems.append("假篇号 999 没被点出来")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print("[OK] selftest: 三轴九类、门（≥4 类 + 事实层与态度层）、空白模板不过门、模板可解析、出处可抽可验")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="素材提问清单与过门检查")
    ap.add_argument("--print", action="store_true", help="打印九类问题")
    ap.add_argument("--template", type=Path, help="写一份空白模板")
    ap.add_argument("--check", type=Path, help="检查答案够不够（不够 rc=1）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if args.print:
        print_bank()
        return 0
    if args.template:
        args.template.parent.mkdir(parents=True, exist_ok=True)
        args.template.write_text(template(), encoding="utf-8", newline="\n")
        print(f"[OK] 模板写到 {args.template}")
        return 0
    if args.check:
        answers = parse_answers(args.check.read_text(encoding="utf-8"))
        ok, problems = check(answers)
        print(f"已答：{ {k: len(v) for k, v in answers.items() if v} }")
        sourced = answer_sources(answers)
        if sourced:
            docs = ", ".join(sorted({source for _, source in sourced}))
            print(f"带出处：{len(sourced)} 条（{docs}）")
        for warning in check_sources(answers):
            print(f"[提示] {warning}")
        for key in missing(answers):
            print(f"[提示] 「{key}」还没答（{axis_of(key)}），能补更好")
        if ok:
            print(f"[OK] 够写了（≥{MIN_CATEGORIES} 类，事实层与态度层都有）")
            return 0
        for problem in problems:
            print(f"[缺] {problem}")
        return 1
    ap.error("要 --print / --template / --check / --selftest 之一")


if __name__ == "__main__":
    raise SystemExit(main())
