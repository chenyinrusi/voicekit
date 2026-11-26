"""脾气尺子探针（PROBE）：只验证，不产出卡片。

为什么：之前十把尺子是「手法」（自嘲、具体顶抽象），换个作者也能用。
要的是「脾气」——碰到 X，他一定 Y，不会 Z。这一层能量吗？先量再说。

做法：每条尺子写成「触发 + 反应 + 反面」三段正则，在文本上算条件率
（触发都出现时，反应跟着出现的比例）或密度（每千字字数）。
然后比三组人：他的语料 / 别人的稿 / 我改的稿。区分得开就留，分不开就是编的。

用法（repo 根）：
    py -3.10 scripts/voice/probe_dispositions.py            # 表格
    py -3.10 scripts/voice/probe_dispositions.py --json tmp/x.json
    py -3.10 scripts/voice/probe_dispositions.py --hits hedge   # 看它命中了什么
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import voice_lib as vl  # noqa: E402

CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
SECOND = REPO_ROOT / "archive/dev/literary-editing/second_author_20261105/raw"  # 鲁迅，对照组
ESSAY = REPO_ROOT / "tmp/essay"
EXCLUDED = ("034", "037")  # 调参污染

HEDGE = (
    r"(?:大概|也许|可能|或许|似乎|好像|差不多|有点儿|有些|算是|我想|我觉得"
    r"|恐怕|说不定|未必|倒也|倒也未必|还算|应该是|也说不准|说不准|未见得)"
)
CONCRETE = (
    r"(?:[一二三四五六七八九十百千万两\d]+\s*(?:块|元|毛|次|天|年|岁|个|杯|部|小时"
    r"|分钟|公里|号|月|人|遍|趟|斤|米)|《[^》]{1,20}》)"
)

RULERS: list[dict] = [
    {
        "key": "hedge",
        "rule": "下判断时留门，不把话说死",
        "kind": "cond_high",
        "trigger": r"(?:是|就是|算是|应该|必须|都|会|该|要)[^。！？]{0,40}。",
        "response": HEDGE,
        "anti": r"(?:一定|必然|肯定|绝对|无疑|毫无疑问|全都|永远|百分之百)",
    },
    {
        "key": "guess_others",
        "rule": "说别人时揣摩他的心思，猜完留退路",
        "kind": "cond_high",
        "window": 0,
        "trigger": r"(?:(?<!其)他|她|对方|这人|那人|那位|这姑娘|那姑娘)"
        r"[^。！？]{0,14}(?:说|想|觉得|认为|以为|希望|怕|担心|喜欢|笑|哭"
        r"|沉默|懂|知道|在意|打算|决定|做|走|来|去)[^。！？]{0,20}。",
        "response": HEDGE + r"|(?:不知道|是不是|会不会|估计|怕是|也说不准|无从得知|想来)",
        "anti": None,
    },
    {
        "key": "land_concrete",
        "rule": "讲抽象的道理时，很快落到一个数或一件具体物上",
        "kind": "cond_high",
        "trigger": r"(?:爱情|婚姻|感情|人生|时间|成长|成熟|价值|幸福|孤独|自由"
        r"|关系|生活|年纪|年轻|世界|命运)[^。！？]{0,40}。",
        "response": CONCRETE,
        "anti": None,
    },
    {
        "key": "self_mock",
        "rule": "说自己出丑时，先把自己写成笑话",
        "kind": "cond_high",
        "trigger": r"(?:我|自己)[^。！？]{0,24}(?:胖|懒|怂|蠢|丑|笨|没用|失败"
        r"|糟糕|尴尬|丢人|中二|穷|肥肉|赘肉|毛病|熊样|自艾自怜)",
        "response": r"(?:哈哈|啧啧|呵呵|也算|至少|罢了|姑且|还真是|[啊呢嘛吧](?:[，。]|$))",
        "anti": None,
    },
    {
        "key": "step_back",
        "rule": "要说狠话时先退一步：不是A，只是B",
        "kind": "cond_high",
        "trigger": r"(?:不是|并非|倒不是|也不算|不至于)",
        "response": r"(?:只是|而是|只不过|主要是因为|不过是|只是说|其实是)",
        "anti": None,
    },
    {
        "key": "unknown",
        "rule": "不知道就说不知道",
        "kind": "density_high",
        "pattern": r"(?:不知道|无从得知|无从考证|说不准|说不清|谁知道|不清楚"
        r"|不得而知|未可知|也说不准)",
    },
    {
        "key": "indirect_emotion",
        "rule": "难受的时候说事，不点情绪的名字",
        "kind": "density_low",
        "pattern": r"(?:孤独|难过|悲伤|痛苦|遗憾|失望|焦虑|抑郁|害怕|委屈"
        r"|绝望|无奈|压抑|心酸|失落|无助|想哭|开心|快乐|幸福|感动)",
    },
    {
        "key": "no_advice",
        "rule": "不叫人照做，只说我是这么想的",
        "kind": "density_low",
        "pattern": r"(?:你应该|你们应该|你要|你们要|你必须|你们必须|一定要"
        r"|请务必|建议大家|不妨|加油|愿你)",
    },
    {
        "key": "wander",
        "rule": "话说一半会岔到自己脑子里",
        "kind": "density_high",
        "pattern": r"(?:转念|一想|细想|现在想来|回头想|说来|想起|说不定"
        r"|万一|要是能|如果能|如果真)",
    },
    {
        "key": "no_override",
        "rule": "不把话说死（少用一定/必须/绝对）",
        "kind": "density_low",
        "pattern": r"(?:一定|必然|肯定|绝对|无疑|毫无疑问|必须|务必|全都)",
    },
    {
        "key": "throw_question",
        "rule": "不给答案，把问题抛出来",
        "kind": "density_high",
        "pattern": r"[？?]",
    },
    {
        "key": "soft_ending",
        "rule": "一段说完，最后一句轻轻收住，不砸地",
        "kind": "para_high",
        "pattern": HEDGE + r"|(?:也算|至少|罢了|姑且|[啊呢嘛吧了](?:[。！]|$))",
        "max_len": 18,
    },
    {
        "key": "concrete_density",
        "rule": "写东西总带着具体的账：数字、单位、书名",
        "kind": "density_high",
        "pattern": CONCRETE,
    },
    {
        "key": "actually",
        "rule": "爱用「其实/说来/老实说」把话往回拉一格",
        "kind": "density_high",
        "pattern": r"(?:其实|说来|老实说|说实话|说到底|细想)",
    },
]


def load_corpus(root: Path = CORPUS, excluded: tuple[str, ...] = EXCLUDED) -> list[tuple[str, str]]:
    docs = []
    for path in sorted(root.glob("*.md")):
        if path.name[:3] in excluded:
            continue
        lines = [x.strip() for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]
        docs.append((path.name[:3], "\n".join(lines[1:])))
    return docs


def load_group(paths: list[Path]) -> list[tuple[str, str]]:
    out = []
    for path in paths:
        if path.exists():
            out.append((path.stem, path.read_text(encoding="utf-8")))
    return out


def groups() -> dict[str, list[tuple[str, str]]]:
    """四组人：他 / 另一个作者 / 别人的原稿 / 机器模仿他的稿 / 我改的稿。"""
    mine = sorted(ESSAY.glob("v[0-9]*.md")) + [ESSAY / "final.md", ESSAY / "draft.polish.md"]
    imitations = sorted(ESSAY.glob("other-v*.md")) + sorted(ESSAY.glob("other2-v*.md"))
    return {
        "他语料": load_corpus(),
        "鲁迅": load_corpus(SECOND),
        "别人原文": load_group([ESSAY / "other-src.md", ESSAY / "other2-src.md"]),
        "模仿稿": load_group(imitations),
        "我改稿": load_group(mine),
    }


def sentences(text: str) -> list[str]:
    return vl.split_sentences(text)


def cjk_len(text: str) -> int:
    return len(vl.CJK_RE.findall(text)) or 1


def evaluate(ruler: dict, text: str) -> dict:
    """返回 {命中数, 底数, 例句}。cond 底数=触发句数；density 底数=千字。"""
    kind = ruler["kind"]
    if kind.startswith("cond"):
        trig = re.compile(ruler["trigger"])
        resp = re.compile(ruler["response"])
        anti = re.compile(ruler["anti"]) if ruler.get("anti") else None
        ss = sentences(text)
        trig_n = hit_n = 0
        examples: list[str] = []
        for i, sent in enumerate(ss):
            if not trig.search(sent):
                continue
            trig_n += 1
            span = ruler.get("window", 1)
            window = sent
            for k in range(span):
                if i + 1 + k < len(ss):
                    window += ss[i + 1 + k]
            if resp.search(window) and not (anti and anti.search(sent)):
                hit_n += 1
                if len(examples) < 3:
                    examples.append(sent)
        return {"hits": hit_n, "trig": trig_n, "examples": examples}
    if kind == "para_high":
        pat = re.compile(ruler["pattern"])
        paras = [p for p in vl.split_paragraphs(text) if p.strip()]
        hits = 0
        examples: list[str] = []
        for para in paras:
            ss = sentences(para)
            if not ss:
                continue
            last = ss[-1].rstrip("。！？!?…，”")
            if pat.search(last) or len(last) <= ruler.get("max_len", 99):
                hits += 1
                if len(examples) < 3:
                    examples.append(last)
        return {"hits": hits, "trig": len(paras), "examples": examples}
    found = re.compile(ruler["pattern"]).findall(text)
    return {"hits": len(found), "trig": cjk_len(text) / 1000, "examples": []}


def pooled(ruler: dict, docs: list[tuple[str, str]]) -> dict:
    hits = sum(evaluate(ruler, text)["hits"] for _n, text in docs)
    trig = sum(evaluate(ruler, text)["trig"] for _n, text in docs)
    values = []
    for _n, text in docs:
        ev = evaluate(ruler, text)
        if ev["trig"] >= (2 if ruler["kind"] in ("cond_high", "para_high") else 0.5):
            values.append(ev["hits"] / ev["trig"])
    out = {"value": hits / trig if trig else None, "hits": hits, "trig": trig, "n": len(values)}
    if len(values) > 3:
        out["p25"] = round(statistics.quantiles(values, n=4)[0], 3)
        out["median"] = round(statistics.median(values), 3)
        out["p75"] = round(statistics.quantiles(values, n=4)[2], 3)
    return out


def verdict(ruler: dict, by_group: dict[str, dict]) -> tuple[str, float]:
    """返回 (判定, 差距)。差距：对 cond 为比率差，对 density 为倍数。"""
    his = by_group["他语料"]["value"]
    others = [by_group[g]["value"] for g in ("鲁迅", "别人原文", "模仿稿")
              if by_group[g]["value"] is not None]
    if his is None or not others:
        return "对照组太少", 0.0
    if ruler["kind"] in ("cond_high", "para_high"):
        rival = max(others)
        gap = his - rival
        if his < 0.15:
            return "空转", gap
        if gap >= 0.08:
            return "留", gap
        return ("微", gap) if gap >= 0.03 else ("分不开", gap)
    rival = max(others)
    if ruler["kind"] == "density_low":
        if rival < 0.05:
            return "空转", 0.0
        ratio = his / rival
        return ("留", ratio) if ratio <= 0.85 else ("分不开", ratio)
    ratio = his / rival if rival else 99.0
    return ("留", ratio) if ratio >= 1.3 else ("分不开", ratio)


def win_rate(ruler: dict, docs: list[tuple[str, str]], rival: float | None) -> float:
    """篇间胜率：多少篇的数值高过（或低过）对照组。看脾气稳不稳。"""
    if rival is None:
        return 0.0
    good = total = 0
    high = ruler["kind"] in ("cond_high", "para_high", "density_high")
    for _n, text in docs:
        ev = evaluate(ruler, text)
        floor = 2 if ruler["kind"] in ("cond_high", "para_high") else 0.5
        if ev["trig"] < floor:
            continue
        total += 1
        value = ev["hits"] / ev["trig"]
        if (value > rival) if high else (value < rival):
            good += 1
    return good / total if total else 0.0


def selftest() -> None:
    """判据自证：该中的中，不该中的不中。"""

    def hits(key: str, text: str) -> int:
        ruler = next(r for r in RULERS if r["key"] == key)
        return evaluate(ruler, text)["hits"]

    assert hits("step_back", "不是不愿意靠近温暖，而是这一点点的温暖，不足以治愈。") == 1
    assert hits("step_back", "今天很热。") == 0
    assert hits("no_override", "我一定去。我必须要说。") == 2
    assert hits("no_override", "大概会去吧。") == 0
    assert hits("wander", "现在想来，那时候真是可笑。") == 1
    assert hits("concrete_density", "三百六十五天，一天一块钱，《皮囊》也读完了。") == 4
    assert hits("concrete_density", "日子过得很快。") == 0
    assert len(load_corpus()) == 59, "语料应为 59 篇（61 篇减 034/037）"
    for name, docs in groups().items():
        assert docs, f"组为空：{name}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=Path)
    ap.add_argument("--hits", help="打印某条尺子在他语料里的命中例句")
    ap.add_argument("--selftest", action="store_true", help="判据自证")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        print("SELFTEST_OK")
        return 0

    data = groups()
    report = {}
    for ruler in RULERS:
        by_group = {gname: pooled(ruler, docs) for gname, docs in data.items()}
        label, gap = verdict(ruler, by_group)
        rival_pool = [by_group[g]["value"] for g in ("鲁迅", "别人原文", "模仿稿")
                      if by_group[g]["value"] is not None]
        rival = max(rival_pool) if rival_pool else None
        report[ruler["key"]] = {
            "rule": ruler["rule"],
            "kind": ruler["kind"],
            "groups": by_group,
            "verdict": label,
            "gap": round(gap, 3),
            "win_his": round(win_rate(ruler, data["他语料"], rival), 2),
            "win_mine": round(win_rate(ruler, data["我改稿"], rival), 2),
        }

    if args.hits:
        ruler = next(r for r in RULERS if r["key"] == args.hits)
        print(f"# {ruler['rule']}  — 命中例句")
        for name, text in data["他语料"]:
            for ex in evaluate(ruler, text)["examples"]:
                print(f"{name} {ex}")
        return 0

    def fmt(value: float | None) -> str:
        return f"{value:>7.3f}" if value is not None else f"{'-':>7}"

    print(f"{'尺子':<14}{'他':>6}{'鲁迅':>6}{'别人':>6}{'模仿':>6}{'我改':>6}{'差距':>6}{'他胜':>5}{'我胜':>5}  判定")
    print("-" * 80)
    for key, row in report.items():
        g = row["groups"]
        line = f"{key:<14}{fmt(g['他语料']['value'])}{fmt(g['鲁迅']['value'])}"
        line += f"{fmt(g['别人原文']['value'])}{fmt(g['模仿稿']['value'])}{fmt(g['我改稿']['value'])}"
        line += f"{row['gap']:>6.2f}{row['win_his']:>5.2f}{row['win_mine']:>5.2f}"
        line += f"  {row['verdict']}"
        print(line)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
