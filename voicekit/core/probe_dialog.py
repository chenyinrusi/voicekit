"""回话两格探针：第 4 格「有没有一个问题」，第 6 格「有没有留门」。

七格里的这两格被 80/20 挑出来：机器最做不到，也最好量。
- 问题：篇内有一句第一人称的困惑（不知道该怎么办 / 该不该 / 一直想不通 / 到底为什么）。
  注意不是问号多——他比对照组更少用问号（2026-11-09 探针：他 1.52，鲁迅 2.32）。
- 留门：末两句带缓和、未知或自我让步（大概/也许/也说不准/谁知道/倒也/也算/罢了/呢吧嘛）。

用法（repo 根）：
    py -3.10 scripts/voice/probe_dialog.py
    py -3.10 scripts/voice/probe_dialog.py --selftest
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

#: 第一人称的困惑。
CONFUSION = (
    r"(?:该不该|要不要|为什么|怎么才能|到底|是不是|有没有|想不通|没想明白|一直想"
    r"|不知道该怎么办|拿不准|说不好|搞不懂)"
)
#: 末句的留门：缓和、未知、自我让步、语气词。
#: 「了」「应该」「可能」不收：它们在机器文里到处都是（模仿稿曾因此判成 100% 留门）。
DOOR = (
    r"(?:大概|也许|或许|似乎|好像|算是|怕是|说不定|未必|倒也|还算|也说不准"
    r"|说不准|说不清|谁知道|看来|也可能|罢|罢了|[呢吧嘛啊哈])"
)
TAIL_SENTENCES = 2


def _sentences(text: str) -> list[str]:
    return [s for s in probe.vl.split_sentences(text) if s.strip()]


def measure(text: str) -> dict:
    sents = _sentences(text)
    if not sents:
        return {"question": None, "door": None, "both": None, "q_n": 0}
    q_hits = [
        s for s in sents
        if "我" in s and probe.re.search(CONFUSION, s)
    ]
    tail = "".join(sents[-TAIL_SENTENCES:])
    result = {
        "question": bool(q_hits),
        "door": bool(probe.re.search(DOOR, tail)),
        "q_n": len(q_hits),
    }
    result["both"] = result["question"] and result["door"]
    return result


def group_rates() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name, docs in probe.groups().items():
        rows = [measure(text) for _n, text in docs]
        rows = [r for r in rows if r["question"] is not None]
        if not rows:
            continue
        out[name] = {
            "n": len(rows),
            "question": sum(1 for r in rows if r["question"]) / len(rows),
            "door": sum(1 for r in rows if r["door"]) / len(rows),
            "both": sum(1 for r in rows if r["both"]) / len(rows),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        print("SELFTEST_OK")
        return 0

    print(f"{'组':<8}{'篇数':>5}{'有问题':>8}{'末句留门':>10}{'两样都有':>10}")
    print("-" * 44)
    for name, row in group_rates().items():
        print(f"{name:<8}{row['n']:>5}{row['question']:>8.0%}"
              f"{row['door']:>10.0%}{row['both']:>10.0%}")

    print()
    print(f"{'篇':<8}{'问题':>6}{'留门':>6}{'两样':>6}  困惑句")
    for num, text in probe.load_corpus():
        if num not in ("002", "022", "036", "064"):
            continue
        r = measure(text)
        hits = [s for s in _sentences(text)
                if "我" in s and probe.re.search(CONFUSION, s)]
        sample = hits[0][:40] if hits else ""
        print(f"{num:<8}{str(r['question']):>6}{str(r['door']):>6}"
              f"{str(r['both']):>6}  {sample}")
    return 0


def selftest() -> None:
    """自证：他的实际句子能认出来，机器人式总结句不能。"""
    asked = "我一直想不通，这到底是不是我想要的。那天我们就聊到这里。"
    assert measure(asked)["question"] is True, measure(asked)
    flat = "爱情是人生里最重要的事情，它决定了后半生的走向。所以我们都要珍惜。"
    assert measure(flat)["question"] is False, measure(flat)
    assert measure("现在我婚了。我们的宝宝也要出生了。")["door"] is False
    assert measure("现在我婚了。啧，看来我这个人，运气也不算太差嘛。")["door"] is True


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
