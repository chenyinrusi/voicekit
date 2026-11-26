"""素材探针：「数得出来的东西」够不够，能不能当尺子。

一个检测器只说一件事：全篇有几处 `数词 + 物量词`（一个人、两件衣服、三张纸）。
量四组人是为了回答两个问题：
1. 用它当**保险丝**（全篇 0 处 = 素材真空）会不会误伤？—— 看「全篇 0 处」那列。
2. 用它当**尺子**（密度越高越像他）成不成立？—— 看每千字中位数。

2026-11-10 结论：保险丝成立（他 59 篇 0 篇为 0），尺子不成立（模仿稿比他还密）。
所以线上只做 gauge：不拦，只在「0 处」和「掉一半」时报。

用法（repo 根）：
    py -3.10 scripts/voice/probe_material.py
    py -3.10 scripts/voice/probe_material.py --selftest
"""

from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from .voice_text import countable_mentions  # noqa: E402


def _load(name: str, filename: str):
    return importlib.import_module(f"{__package__}.{Path(filename).stem}")


def table() -> None:
    probe = _load("probe_dispositions", "probe_dispositions.py")
    print(f"{'组':<8}{'n':>4}{'最少':>7}{'p10':>7}{'中位':>7}{'最多':>7}   全篇 0 处")
    print("-" * 58)
    for name, docs in probe.groups().items():
        if not docs:
            continue
        density = [
            countable_mentions(text) / max(len("".join(text.split())) / 1000, 1e-6)
            for _n, text in docs
        ]
        density.sort()
        zero = sum(1 for _n, text in docs if countable_mentions(text) == 0)
        print(
            f"{name:<8}{len(docs):>4}{density[0]:>7.1f}{density[len(density) // 10]:>7.1f}"
            f"{statistics.median(density):>7.1f}{density[-1]:>7.1f}   {zero}/{len(docs)}"
        )
    print("\n每千字，四舍五入到 0.1。他 59 篇最低 2.6、0 篇为 0 → 保险丝不误伤；"
          "中位却低于模仿稿 → 不能当尺子。")


def selftest() -> None:
    """自证：数得出来的东西能数到；抽象说法数不到。"""
    assert countable_mentions("我养了一只猫，买了两袋猫粮，三张照片贴在冰箱上。") == 3
    assert countable_mentions("养猫这件事让人踏实。") == 0  # 这件事/那种：不是数得出来的东西
    assert countable_mentions("我读了三遍。") == 0  # 动量词（遍/次/顿）不是东西


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="量「数得出来的东西」够不够")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        selftest()
        print("SELFTEST_OK")
        return 0
    table()
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
