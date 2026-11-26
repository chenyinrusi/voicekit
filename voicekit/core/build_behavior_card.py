"""行为库：把「6 条底色」升级成「处境 → 动作」表，并从语料里取证据句。

为什么：属性学的是「句子长什么样」，学不到「他是个什么人」。四轮盲测里盲读者说穿的
原因都是「没有他的私人材料」；属性分 81.0 的重写还高过他自己 064 的 73.8。
所以学习单位从「整篇属性均值」换成「他在什么处境下做什么动作」。

做法（全部可复算，不调模型）：
- 9 个动作，每个动作一套机械判据（正则/词表），在 59 篇语料（034/037 除外）里数命中；
- 每个动作取最多 2 句原文当证据（≤120 字，来自不同的篇）；
- 生成 `voices/panchen-voice/references/behaviors.md`，`--check` 防漂移。

用法（repo 根）：
    python scripts/voice/build_behavior_card.py            # 写 references/behaviors.md
    python scripts/voice/build_behavior_card.py --check    # 只比，不写；漂移 rc=1
    python scripts/voice/build_behavior_card.py --selftest # 判据自证
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402

CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
OUT = REPO_ROOT / "voices/panchen-voice/references/behaviors.md"

#: 每个动作：名字、处境（什么时候用）、机械判据、改稿时看什么。
ACTIONS: list[dict] = [
    {
        "name": "对自己狠",
        "situation": "写自己的毛病、丢脸、失败时",
        "pattern": r"(胖|懒|怂|蠢|丑|笨|社畜|中二|自艾自怜|心疼我自己|丢人|尴尬)",
        "look": "自嘲可以多；但只许拿自己开刀，不许顺手评判别人",
    },
    {
        "name": "怕被误会",
        "situation": "说完一句可能被误解的判断之后",
        # 实测：他不用「我不是那个意思」这种说法，而是「倒不是X，只是Y」——先否一个误读，再说真话
        "pattern": r"(倒不是|并不是|也不是)[^。！？]{2,30}?(只是|而是|主要是因为)",
        "look": "一篇里最多一次；两次以上就变成辩解文",
    },
    {
        "name": "替对方想",
        "situation": "写到别人的反应、别人的处境时",
        "pattern": r"((她|他|对方)[^。！？]{0,4}(可能|大概|也许|怕是|应该是)|大概她也|想来他)",
        "look": "写别人几乎不评判，替对方留余地；评判别人的句子应当是零",
    },
    {
        "name": "用想起岔开",
        "situation": "一段事讲完、要转去讲别的时",
        "pattern": r"(想起|想到|说着说着|说来)",
        "look": "岔开要以一个物件或一句话当桥，岔完必须拐回来",
    },
    {
        "name": "身体先行",
        "situation": "要写情绪的时候",
        "pattern": r"(后颈|脚腕|手腕|掌心|手心|指尖|喉咙|脸.?红|耳根|腿软|手抖|出汗|发僵|发麻|发烫)",
        "look": "一段里只有心情形容词、没有一个身体动作，就标出来",
    },
    {
        "name": "具体顶抽象",
        "situation": "一句抽象判断之后",
        # 汉字数字也算（他的语料用「二〇一六年」「一百二十块」）
        "pattern": r"((?:\d+|[一二三四五六七八九十百千万两]+)\s*(块|元|毛|钱|分钟|小时|公里|岁|月|号|年)|\d+部|\d+个问题)",
        "look": "抽象判断后面至少跟一个具体的东西（钱、时间、地名、牌子）",
    },
    {
        "name": "装没事",
        "situation": "受了委屈、被拒绝、心里不痛快时",
        # 实测：「假装」这种词他基本不用（0 篇），只认嘴上说/装作没事这一型
        "pattern": r"(嘴上说|装作没事|说没事|没说什么)",
        "look": "他写难受不写难受，写「嘴上说没事」；实测很少见，改写时别硬套",
        "first_person": True,
        "exclude": r"(骗|虚报|诱骗|谎称)",
        "rare": True,
    },
    {
        "name": "收在动作上",
        "situation": "文章最后一段",
        "pattern": r"(推门|合上|关上|起身|站起来|点了下头|往回走|上楼|开门|关灯|收好|躺下|睡了|刷完)",
        "look": "结尾落在动作上不是错；但只许停住，不许再加一句总结；实测很少见",
        "last_paragraphs": 1,
        "rare": True,
    },
]

SENT = re.compile(r"[^。！？!?…]+[。！？!?…]+")
MAX_EVIDENCE = 2
EVIDENCE_CHARS = 110


def load_chapters() -> list[tuple[str, str]]:
    """[(篇号, 正文)]；034/037 调参污染，排除。"""
    docs = []
    for path in sorted(CORPUS.glob("*.md")):
        if path.name.startswith(bp.EXCLUDED_DOC_PREFIXES):
            continue
        lines = [x.strip() for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]
        docs.append((path.name[:3], "\n".join(lines[1:])))
    return docs


def scan(docs: list[tuple[str, str]]) -> list[dict]:
    """每个动作：命中的篇数、句数、证据句（不同篇，最多 2 条）。"""
    out = []
    for action in ACTIONS:
        pattern = re.compile(action["pattern"])
        doc_hits, sent_hits = set(), 0
        evidence: list[tuple[str, str]] = []
        for num, text in docs:
            hit_this_doc = False
            paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
            scope = action.get("last_paragraphs")
            if scope:
                paragraphs = paragraphs[-scope:]
            # 段间单换行：折成空格，避免一句话被换行切断后拼不上
            body = " ".join(paragraphs)
            for sentence in SENT.findall(body):
                sentence = sentence.strip()
                if len(sentence) < 8 or not pattern.search(sentence):
                    continue
                if action.get("first_person") and "我" not in sentence:
                    continue
                if action.get("exclude") and re.search(action["exclude"], sentence):
                    continue
                sent_hits += 1
                hit_this_doc = True
                if len(evidence) < MAX_EVIDENCE and all(num != n for n, _ in evidence):
                    evidence.append((num, sentence[:EVIDENCE_CHARS]))
            if hit_this_doc:
                doc_hits.add(num)
        out.append(
            {
                **action,
                "docs": len(doc_hits),
                "sentences": sent_hits,
                "evidence": evidence,
            }
        )
    return out


def build() -> str:
    docs = load_chapters()
    rows = scan(docs)
    total = len(docs)
    lines = [
        "# 行为库：处境 → 动作（从 59 篇语料里数出来的）",
        "",
        "> 为什么有这页：属性学的是「句子长什么样」，学不到「他是个什么人」。",
        "> 盲读者三次说穿的原因都是「没有他的私人材料」；属性 81.0 的重写还高过他自己 064 的 73.8。",
        "> 所以学习单位从「整篇属性均值」换成「**他在什么处境下做什么动作**」。",
        f"> 由 `scripts/voice/build_behavior_card.py` 从 {total} 篇语料机械统计生成，可复算（`--check`）。",
        "> 用法：改写或生成时，先看处境对上哪条，再按「他怎么动」写；证据句只当样子，不许照抄。",
        "",
        "> 结尾「收在讲理还是收在事上」看 `strategy-card.md` 的段角色口径，不在这页重算（重复测量会互相打架）。",
        "",
        "## 表",
        "",
        "| 动作 | 处境（什么时候用） | 篇数 / 59 | 句数 | 改稿时看什么 |",
        "|---|---|---|---|---|",
    ]
    for row in [r for r in rows if not r.get("rare")]:
        lines.append(
            f"| {row['name']} | {row['situation']} | {row['docs']} | {row['sentences']} | {row['look']} |"
        )
    rare = [row for row in rows if row.get("rare")]
    if rare:
        lines += [
            "",
            "## 少见（语料里 0–1 篇，别当规则硬套）",
            "",
            "| 动作 | 处境 | 篇数 / 59 | 句数 | 说明 |",
            "|---|---|---|---|---|",
        ]
        for row in rare:
            lines.append(
                f"| {row['name']} | {row['situation']} | {row['docs']} | {row['sentences']} | {row['look']} |"
            )
    lines += ["", "## 证据（每条约两句原文）", ""]
    for row in [row for row in rows if not row.get("rare")]:
        lines.append(f"### {row['name']}｜{row['situation']}")
        lines.append("")
        if not row["evidence"]:
            lines.append("- （语料里没有命中，判据可能需要改）")
        for num, sentence in row["evidence"]:
            lines.append(f"- `{num}`：{sentence}")
        lines.append("")
    lines.append("一条读法：先按处境找动作，再按动作写；不要拿证据句去拼稿子（那是抄，不是学）。")
    lines.append("")
    return "\n".join(lines)


def _selftest() -> int:
    problems = []
    if len(ACTIONS) != 8:
        problems.append(f"动作数 {len(ACTIONS)}，want 8")
    if any(not a.get("situation") or not a.get("look") for a in ACTIONS):
        problems.append("有动作缺处境或改稿提示")
    # 判据自证：每个动作自己的例句要命中，中立句不许命中
    neutral = "今天天气不错，我出门买了点菜，回来做了饭。"
    samples = {
        "对自己狠": "我这个人是真懒，还胖，说出来都丢人。",
        "怕被误会": "我倒不是嫌弃她，只是那件事我确实有点介意。",
        "替对方想": "大概她也累了，所以那天没怎么说话。",
        "用想起岔开": "说着说着忽然想起小时候那台打火机。",
        "身体先行": "我后颈一下就僵了，手心也出了汗。",
        "具体顶抽象": "那天排队排了四十分钟，花了一百二十块钱。",
        "装没事": "我嘴上说没事，其实心里堵得慌。",
        "收在动作上": "我合上书，起身把椅子推回去。",

    }
    for action in ACTIONS:
        pattern = re.compile(action["pattern"])
        sample = samples.get(action["name"])
        if sample is None:
            problems.append(f"{action['name']} 没有自证例句")
            continue
        if not pattern.search(sample):
            problems.append(f"{action['name']} 判据没命中自己的例句")
        if pattern.search(neutral):
            problems.append(f"{action['name']} 判据命中了中立句")
    docs = load_chapters()
    rows = scan(docs)
    if len(docs) != 59:
        problems.append(f"语料 {len(docs)} 篇，want 59")
    thin = [row["name"] for row in rows if not row.get("rare") and row["docs"] < 3]
    if thin:
        problems.append(f"这些主表动作命中篇数 <3，判据或语料有问题：{thin}")
    if not any(row.get("rare") for row in rows):
        problems.append("没有「少见」动作，跟实测（装没事/收动作基本为 0）不符")
    if not any(row["evidence"] for row in rows):
        problems.append("一条证据句都没取到")
    if build() != build():
        problems.append("不可复算")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print(f"[OK] selftest: {len(ACTIONS)} 个动作，判据自证 + 59 篇统计 + 可复算都过")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="行为库：处境 → 动作")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--check", action="store_true", help="只比，不写；漂移 rc=1")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    text = build()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.is_file() else ""
        if current == text:
            print(f"[OK] behavior card up to date: {args.out}")
            return 0
        print(f"[FAIL] behavior card drift: {args.out}", file=sys.stderr)
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"[OK] wrote {args.out} — {len(text.splitlines())} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
