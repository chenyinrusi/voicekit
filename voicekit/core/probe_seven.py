"""七格探针：把「回话七格」一格一格量一遍，统一出结论。

每格只能找代理指标（原格是意思，不是数），所以每行都写清代理是什么：
1 对面   —— 篇内出现第二人称（你/你们），说明有个明确的谈话对象
2 由头   —— 前两段出现时间触发（最近/前几天/那天/今年…），说明为什么现在说
3 冲突   —— 第一人称句里出现难受/放不下/尴尬这类处境词，说明心里有那点不对劲
4 问题   —— 第一人称的困惑（该不该/要不要/一直想不通…）
5 凭据   —— 第一人称句里出现经历标记（当时/那年/有一次/我认识…）
6 留门   —— 末两句带缓和、未知、自我让步
7 目的   —— 反过来量：篇内有没有劝人/教育人的话（他几乎没有）

用法（repo 根）：
    py -3.10 scripts/voice/probe_seven.py
    py -3.10 scripts/voice/probe_seven.py --selftest
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))


def _load(name: str, filename: str):
    return importlib.import_module(f"{__package__}.{Path(filename).stem}")


probe = _load("probe_dispositions", "probe_dispositions.py")
dialog = _load("probe_dialog", "probe_dialog.py")

YOU = r"(?:你|你们)"
TIME_TRIGGER = (
    r"(?:最近|前几天|前两天|这几天|这段时间|上个月|今年|去年|那天|那天晚上"
    r"|今天|昨天|年前|年后|月前|小时候|那年)"
)
TROUBLE = (
    r"(?:被拒|失望|放不下|难受|尴尬|遗憾|可惜|不甘|心疼|郁闷|委屈|慌"
    r"|后悔|舍不得|过不去|怕)"
)
EVIDENCE = (
    r"(?:当时|那年|那时候|有一次|从前|以前|我记得|我认识|我身边|我做过"
    r"|我去过|我读过|我见过|我第一次)"
)
ADVICE = r"(?:你应该|你们应该|你要|你们要|你必须|一定要|请务必|建议大家|不妨|愿你)"

#: 第 5 格「身份＋凭据」里**身份**那半的候选代理（凭据那半在 CELLS 里，已量过：分不开）。
#: 2026-11-10 补量：格子含义是「我是谁、我凭什么说」，所以只测「自陈是谁」和「出身」两类。
EXTRA_CELLS = [
    {"key": "身份-自陈", "proxy": "第一人称句里自陈性格/一辈子",
     "kind": "first_person", "pattern": r"(?:我这个人|我这人|我这辈子|我从小|我是那种人|我向来|我从来|我天生)"},
    {"key": "身份-出身", "proxy": "第一人称句里提家里人",
     "kind": "first_person", "pattern": r"(?:我爸|我妈|我们家|我老家|我父亲|我母亲|我奶奶|我外婆|我姥姥|我爷爷)"},
]

CELLS = [
    {"key": "对面", "proxy": "篇内有第二人称", "kind": "has", "pattern": YOU},
    {"key": "由头", "proxy": "前两段有时间触发", "kind": "lead", "pattern": TIME_TRIGGER},
    {"key": "冲突", "proxy": "第一人称句里有处境词", "kind": "first_person", "pattern": TROUBLE},
    {"key": "问题", "proxy": "第一人称句里有困惑", "kind": "first_person",
     "pattern": dialog.CONFUSION},
    {"key": "凭据", "proxy": "第一人称句里有经历标记", "kind": "first_person",
     "pattern": EVIDENCE},
    {"key": "留门", "proxy": "末两句有缓和/让步", "kind": "tail", "pattern": dialog.DOOR},
    {"key": "目的", "proxy": "篇内没有劝人话（他没有）", "kind": "absent",
     "pattern": ADVICE},
]


def _paras(text: str) -> list[str]:
    return [p for p in probe.vl.split_paragraphs(text) if p.strip()]


def _sents(text: str) -> list[str]:
    return [s for s in probe.vl.split_sentences(text) if s.strip()]


def hit(cell: dict, text: str) -> bool:
    kind = cell["kind"]
    pat = probe.re.compile(cell["pattern"])
    if kind == "has":
        return bool(pat.search(text))
    if kind == "absent":
        return not pat.search(text)
    if kind == "lead":
        return bool(pat.search("".join(_paras(text)[:2])))
    if kind == "tail":
        return bool(pat.search("".join(_sents(text)[-2:])))
    return any("我" in s and pat.search(s) for s in _sents(text))


def rates(cells: list[dict] | None = None) -> dict[str, dict]:
    data = probe.groups()
    out: dict[str, dict] = {}
    for cell in cells or CELLS:
        row: dict[str, float] = {}
        for name, docs in data.items():
            if not docs:
                continue
            row[name] = sum(1 for _n, t in docs if hit(cell, t)) / len(docs)
        rival = max(row["鲁迅"], row.get("模仿稿", 0.0))
        his = row["他语料"]
        if rival < 0.05 and his < 0.05:
            label = "空转"
        elif his - rival >= 0.10:
            label = "留"
        elif his - rival >= 0.05:
            label = "弱"
        else:
            label = "分不开"
        if cell["kind"] == "absent":
            # 「他没有劝人话」：他要比所有对照都更少，才算这格带他的指纹。
            label = "留" if his - rival >= 0.10 else ("弱" if his - rival >= 0.05 else "分不开")
        out[cell["key"]] = {"proxy": cell["proxy"], "groups": row, "verdict": label}
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--extra", action="store_true",
                    help="另量第 5 格「身份」那半的候选代理（凭据那半已量过，分不开）")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        print("SELFTEST_OK")
        return 0

    table(rates())
    if args.extra:
        print("\n第 5 格「身份」候选（2026-11-10 量）：")
        table(rates(EXTRA_CELLS))
    return 0


def table(rows: dict[str, dict]) -> None:
    print(f"{'格':<8}{'他':>7}{'鲁迅':>7}{'模仿':>7}{'我改':>7}  判定  代理")
    print("-" * 78)
    for key, row in rows.items():
        g = row["groups"]
        line = f"{key:<8}{g['他语料']:>7.0%}{g['鲁迅']:>7.0%}"
        line += f"{g.get('模仿稿', 0):>7.0%}{g.get('我改稿', 0):>7.0%}"
        line += f"  {row['verdict']:<4}{row['proxy']}"
        print(line)


def selftest() -> None:
    """自证：每格的代理句都能认出来，空句都不中。"""
    assert hit(CELLS[0], "你们有没有过这种感觉？") is True
    assert hit(CELLS[0], "今天天气很好。") is False
    assert hit(CELLS[1], "最近我总在想一件事。\n\n第二段。") is True
    assert hit(CELLS[1], "爱情是重要的。\n\n第二段。\n\n最近") is False
    assert hit(CELLS[2], "我当时挺失望的。") is True
    assert hit(CELLS[3], "我一直想不通，这到底该不该。") is True
    assert hit(CELLS[4], "我认识一个朋友，他当时也是这样。") is True
    assert hit(CELLS[6], "你应该早点睡觉。") is False
    assert hit(CELLS[6], "我是这么想的。") is True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
