"""弧度探针：先升后落。

假设（2026-11-09，升维后）：人味不在句子、不在手法，在弧度上——
他讲一件小事，升到一个大问题，再落回一件更小的事，最后留个门。
机器（和我）只会往上升：小事 → 大道理 → 更大的道理 → 升华。

三段可测：
- 升：有「讲事」段，其后出现「讲理」段（段角色口径沿用 voice_review._role）
- 落：最后一段只有最长段的一半以下（从大道理摔回小事，字就短了）
- 轻收：最后一句 ≤25 汉字，且带留门词或语气词

用法（repo 根）：
    py -3.10 scripts/voice/probe_arc.py
    py -3.10 scripts/voice/probe_arc.py --file tmp/essay/v14.md
    py -3.10 scripts/voice/probe_arc.py --selftest
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import voice_review as vr  # noqa: E402


def _load(name: str, filename: str):
    return importlib.import_module(f"{__package__}.{Path(filename).stem}")


probe = _load("probe_dispositions", "probe_dispositions.py")

#: 留门：缓和词 + 语气词 + 「也不算/看来」这类自我让步。
SOFT = (
    r"(?:大概|也许|可能|或许|似乎|好像|算是|怕是|说不定|未必|倒也|还算"
    r"|也说不准|看来|只不过|不过|倒是|也算|[啊呢嘛吧了呀哦哈]|吧|嘛)"
)
LAST_MAX_CHARS = 25
FALL_RATIO = 0.5
#: 极短段是钩子/引子，不参与角色计数（064 开头「我们的故事，其实是从打羽毛球开始的」16 字，
#: 按角色规则会被算成「讲理」，把整篇骨架读反）。
ROLE_MIN_CHARS = 20


def cjk_len(text: str) -> int:
    return len(probe.vl.CJK_RE.findall(text))


def _last_sentence(para: str) -> str:
    sents = [s for s in probe.vl.split_sentences(para) if s.strip()]
    return sents[-1] if sents else para


def measure_arc(text: str) -> dict:
    """返回 {升, 落, 轻收, 弧度, 骨架, 最长段, 末段}。段太少时三个都为 None。"""
    paras = [p for p in vr._paragraphs(text) if cjk_len(p) >= 4]
    if len(paras) < 3:
        return {"rise": None, "fall": None, "soft": None, "arc": None,
                "skeleton": "", "longest": 0, "last": 0, "n": len(paras)}
    roles = [vr._role(p) for p in paras if cjk_len(p) >= ROLE_MIN_CHARS]
    lengths = [cjk_len(p) for p in paras]

    story_at = [i for i, r in enumerate(roles) if r == vr.ROLE_STORY]
    moral_at = [i for i, r in enumerate(roles) if r == vr.ROLE_MORAL]
    rise = bool(story_at) and bool(moral_at) and min(moral_at) > min(story_at)

    longest = max(lengths)
    fall = lengths[-1] <= longest * FALL_RATIO

    tail = _last_sentence(paras[-1])
    soft = cjk_len(tail) <= LAST_MAX_CHARS and bool(
        probe.re.search(SOFT, tail)
    )

    runs: list[list] = []
    for role in roles:
        if runs and runs[-1][0] == role:
            runs[-1][1] += 1
        else:
            runs.append([role, 1])
    skeleton = "→".join(f"{r}{n}" for r, n in runs)
    return {"rise": rise, "fall": fall, "soft": soft,
            "arc": rise and fall and soft,
            "skeleton": skeleton, "longest": longest, "last": lengths[-1], "n": len(paras)}


def group_rates() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, docs in probe.groups().items():
        rows = [measure_arc(text) for _n, text in docs]
        rows = [r for r in rows if r["arc"] is not None]
        if not rows:
            continue
        out[name] = {
            "n": len(rows),
            "rise": sum(1 for r in rows if r["rise"]) / len(rows),
            "fall": sum(1 for r in rows if r["fall"]) / len(rows),
            "soft": sum(1 for r in rows if r["soft"]) / len(rows),
            "arc": sum(1 for r in rows if r["arc"]) / len(rows),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, help="只看一份稿")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        print("SELFTEST_OK")
        return 0

    if args.file:
        result = measure_arc(args.file.read_text(encoding="utf-8"))
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0

    print(f"{'组':<8}{'篇数':>5}{'升':>8}{'落':>8}{'轻收':>8}{'弧度':>8}")
    print("-" * 46)
    for name, row in group_rates().items():
        print(f"{name:<8}{row['n']:>5}{row['rise']:>8.0%}{row['fall']:>8.0%}"
              f"{row['soft']:>8.0%}{row['arc']:>8.0%}")

    essay = REPO_ROOT / "tmp/essay"
    print()
    print(f"{'稿':<8}{'段数':>5}{'升':>6}{'落':>6}{'轻收':>6}{'弧度':>6}  骨架")
    for name in ("draft", "v10", "v13", "v14"):
        path = essay / f"{name}.md"
        if not path.exists():
            continue
        r = measure_arc(path.read_text(encoding="utf-8"))
        print(f"{name:<8}{r['n']:>5}{str(r['rise']):>6}{str(r['fall']):>6}"
              f"{str(r['soft']):>6}{str(r['arc']):>6}  {r['skeleton']}")
    return 0


def selftest() -> None:
    """自证：他的 064 三样全中；一路讲理、一路升到结尾的稿子三样全不中。"""
    final = (REPO_ROOT / "archive/dev/literary-editing/book_three_review_20261104"
             "/corpus/064__后记2 嗨 我婚了.md").read_text(encoding="utf-8")
    got = measure_arc(final)
    assert got["arc"] is True, got

    flat_moral = "\n\n".join([
        "爱情是人生里最重要的一件事情，它决定了一个人后半生的走向。",
        "婚姻之所以重要，是因为它把两个人的命运绑在了一起，需要彼此负责。",
        "所以我们要相信缘分的安排，也要相信自己在关系里的判断。",
        "未来的路上一定会有风雨，但只要彼此相信，就一定能走到最后。",
    ])
    bad = measure_arc(flat_moral)
    assert bad["arc"] is False, bad
    assert bad["fall"] is False, bad


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
