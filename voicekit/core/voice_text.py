"""从 rewrite.py 拆出来的共用小件：作者包路径函数 + 段落工具 + 具体账。

拆出来的原因只有一个：`scripts/voice/rewrite.py` 触到「模块 ≤1000 行」的棘轮
（2026-11-10 实测 1098 行；2026-11-11 账本函数再搬一次）。这里放两边都要用的东西，避免各写一份。
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..paths import active_pack
from .pack_lib import PackError, load_pack


def _pack() -> dict:
    """作者包：路径的唯一来源（`--pack` → `VOICEKIT_PACK` → `./voices/`）。

    以前这里是 `Path(__file__).resolve().parents[2] / "voices/..."`：只在开发树成立，
    装成 wheel 后指向 site-packages/voices（不存在）。所以改成懒函数，由 `paths` 定包。
    """
    try:
        return load_pack(active_pack())
    except PackError as exc:  # 包的 pack.json 坏了：给人话，不抛 traceback
        raise SystemExit(f"[FAIL] {exc}") from exc


def profile_path() -> Path:
    return _pack()["profile"]


def corpus_path() -> Path | None:
    """作者的语料目录；只发基线的包没有 corpus（`None`），调用方要降级不能报错。"""
    return _pack()["corpus"]


def voice_core_path() -> Path:
    """改稿纪律的来源；包内约定放在 `references/voice-core.md`。"""
    return _pack()["dir"] / "references" / "voice-core.md"


def split_paragraphs(text: str) -> list[str]:
    """一段一行（空行丢掉）。语料是「一段一个换行」，一般草稿是空行分段——两种都认。"""
    return [block.strip() for block in text.strip().split("\n") if block.strip()]


def uses_blank_lines(text: str) -> bool:
    """原稿是不是空行分段：决定改后的排版跟谁走。"""
    return bool(re.search(r"\n\s*\n", text.strip()))


def looks_like_title(line: str) -> bool:
    """短、不带句末标点、不像一句完整叙述 —— 那它多半是标题。"""
    body = "".join(line.split())
    return 0 < len(body) <= 30 and not re.search(r"[。！？!?；…]$", body)


def title_line(text: str) -> str:
    """原稿第一行如果是标题，返回它；否则空串。"""
    first = next(iter(split_paragraphs(text)), "")
    return first if looks_like_title(first) else ""


_COUNT_NUM = r"[一二三四五六七八九十百千两半几\d]+"
#: 物量词：数得出来的「东西/事」。动量词（次/遍/顿）不算东西，故意不放进来。
_COUNT_THING = "个件条张块根口把碗杯本支双套台辆座间层句片段行页首幅颗粒滴束堆摞串包袋瓶盒箱家位名群队排部篇只头匹"
_COUNT_RE = re.compile(rf"{_COUNT_NUM}[{_COUNT_THING}]")


def countable_mentions(text: str) -> int:
    """全篇「数得出来的东西」有几处（一个人、两件衣服、三张纸）。

    只当保险丝，不当尺子（复算 `probe_material.py`，2026-11-10 实测）：
    他 59 篇最低 2.6 处/千字、0 篇为 0，所以「0 处」只在素材真空时响；
    而密度高低**不**代表像他——别人模仿他的稿中位 14.8，比他自己 8.3 还高，鲁迅 7.2 更低。
    """
    return len(_COUNT_RE.findall(text))


def material(source: str, out: str) -> dict:
    """素材读数：原稿有没有「数得出来的东西」，改后还剩几成。

    素材门是软门（gauge）：只进报告，不拦。密度高低不代表像他
    （模仿稿中位 14.8/千字 > 他 8.3 > 鲁迅 7.2），只有「一处都没有」才说明素材真空，
    而素材缺是改语言补不了的，交人。
    """
    src, dst = countable_mentions(source), countable_mentions(out)
    return {"src": src, "out": dst, "missing": src == 0}


def join_paragraphs(paras: list[str], blank_line: bool = True) -> str:
    return ("\n\n" if blank_line else "\n").join(paras).strip() + "\n"


#: 具体账：数字＋单位（含单字数词，如「三遍」「一天」）。只查少没少。
_LEDGER_UNIT = re.compile(
    r"(?:[0-9]+|[一二三四五六七八九十百千万两]+)\s*(?:块|元|毛|次|天|年|岁|个|杯|部"
    r"|小时|分钟|公里|号|月|人|遍|趟|斤|米|组|份|倍)"
)


def ledger_tokens(text: str) -> Counter:
    """具体账：数字＋单位。只用来查「少了没有」，不查「多了」。

    书名与引语**不在这里**：它们已经由 `protected_tokens` 报「被改掉 / 多出来」，
    再进具体账就是同一件事报两遍（2026-11-10 MECE 审核发现的重复）。
    """
    return Counter(re.sub(r"\s+", "", m) for m in _LEDGER_UNIT.findall(text))


def _numeral_is_bare_one(token: str) -> bool:
    """这个账 token 的数目字是不是就是孤零零一个「一」（不是「一百四十」那种）。"""
    return token[:1] == "一" and not re.search(r"[一二三四五六七八九十百千万两0-9]", token[1:])


def _fact_still_recoverable(source: str, out: str, token: str) -> bool:
    """账灯的判据：丢一个 token，先看「读者还能不能从成稿里对出这件事」。

    账要管的是信息，不是写法。虚数「一」只把名词的单数说出来，不是信息量：
    「一个角落」→「个角落」读起来还是「一条角落」，更口语，改好不是改坏。所以只要
    量词和它后面那个词还在，这件事就对得出，不算丢账（064 审计里「一个,一遍,一年」
    这条红也是同一类假警）。
    反过来「三遍」→「几遍」、「相处的一年」→「相处的时间里」：读者对不出数了，照旧拦。
    汉字数字（「二〇一六年」）是腔不是写法，另有保护项严查，这里不参与放行。
    """
    if not _numeral_is_bare_one(token):
        return False
    at = source.find(token)
    if at < 0:
        return False
    tail = source[at + len(token): at + len(token) + 2]
    if not tail.strip("，。！？、；：…—"):
        return False  # 后面没有字可对照：宁严，照旧拦
    # 量词＋它后面那个词都还在 → 只剩虚数「一」没了
    return token[1:] + tail in out


def split_lost_ledger(source: str, out: str,
                      strict_counts: bool = True) -> tuple[Counter, Counter]:
    """丢掉的账分两堆：(信息真丢的, 只丢了写法的)。

    `strict_counts=False`（起稿用）：只在成稿里**一次都找不到**才算丢。起稿不是重写原稿，
    同一件事在原稿里写两遍（页眉「2026年11月」+ 条目「11月8日」）、草稿只写一遍，
    是正常删重，不是丢账——2026-11-11 实测：这盏灯对一篇没丢事实的稿子报红。
    """
    want, got = ledger_tokens(source), ledger_tokens(out)
    gone = want - got
    if not strict_counts:
        gone = Counter({token: count for token, count in want.items() if got[token] == 0})
    only_wording = Counter(
        {token: n for token, n in gone.items() if _fact_still_recoverable(source, out, token)}
    )
    return gone - only_wording, only_wording
