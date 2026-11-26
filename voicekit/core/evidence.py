"""把「没通过」的属性尽量落回段落/句子：只给位置，不改口径、不新增判据。

两条硬规矩（独立评审判定，见 tmp/design-20261127-quick-evidence.md §8）：
1. **段号基准和 measure() 同一份正文**：先 `clean_platform_noise()`（会剥平台注入、
   去顶部重复标题），再用 `split_paragraphs()` 编号。否则 ¶ 整体错位。
2. **没有句级证据就说没有**：TTR / Yule's K / 签名 n-gram / 句长方差是全篇统计，
   硬指一句就是编证据。`spots()` 返回 `[]`，报告里写「无句级证据（全文统计）」。

词表全部复用 `voice_lib` / `measure_extra` 的既有常量，本模块不新增任何判据口径。
"""

from __future__ import annotations

from . import measure_extra as mx
from . import voice_lib as lib

#: 词表类属性 → 能在句子里直接找到的标记。表里没有的属性一律返回 []（全篇统计）。
TOKENS: dict[str, tuple[str, ...]] = {
    "L1-12": lib.CLASSICAL_MARKERS,
    "L1-13": lib.CHENGYU,
    "L1-14": lib.EMO_POS + lib.EMO_NEG,
    "L1-15": mx.APPRAISAL["praise"] + mx.APPRAISAL["fault"],
    "L1-16": lib.MODAL_SPEC + lib.MODAL_CERT,
    "L1-17": mx.SLANG,
    "L2-02": tuple(t for group in lib.CONNECTIVES.values() for t in group),
    "L2-11": ("了", "过", "着", "正在", "曾经"),
    "L4-05": lib.TRANSITIONS,
    "L6-08": mx.HYPERBOLE + mx.DOWNPLAY,
    "L7-02": lib.HEDGES + lib.BOOSTERS,
    "L7-05": lib.READER_TERMS,
    "L7-09": tuple(lib.MODAL_PARTICLES) + lib.INSERTIONS,
    "L7-10": tuple(t for group in mx.METADISC.values() for t in group),
    "L8-01": mx.SOCIAL,
    "L8-02": mx.INSIGHT,
    "L8-03": mx.DIFFERENCE,
    "L8-05": tuple(t for group in mx.TIME_WORDS.values() for t in group),
    "L10-02": mx.SWEAR,
}

#: 句子本身可数的属性 → 判定函数。只有「判的就是句子这个性质」的才在这里：
#: 碎片句（L2-08）、问句/感叹句（L2-05）。句长分布（L2-01）、句读节奏（L3-05）、
#: 标点谱（L1-01）判的是全篇统计量，指几句会指错，一律「无句级证据」。
STRUCT_ATTRS = {
    "L2-08": "碎片句",  # 汉字 ≤6 的句子
    "L2-05": "问句/感叹句",
}

#: 一句话里的汉字（和 `_doc.cjk_chars` 同一口径）。
CJK_RE = lib.CJK_RE

QUOTE_WIDTH = 32

#: `attach()` 数「全文共几处」时的上限；够用即可，别为了数全把长稿扫爆。
_COUNT = 999

#: 全篇统计量没有句级证据时写这句；测试和报告都认这一个字符串。
NO_EVIDENCE = "无句级证据（全文统计）"


def sentences(text: str) -> list[dict]:
    """按清洗后的正文切句：`[{para, sent, text}]`，段号/句号都从 1 起。"""
    body, _ = lib.clean_platform_noise(text)
    out: list[dict] = []
    for para_no, para in enumerate(lib.split_paragraphs(body), 1):
        for sent_no, sent in enumerate(lib.split_sentences(para), 1):
            if sent.strip():
                out.append({"para": para_no, "sent": sent_no, "text": sent.strip()})
    return out


def _quote(sentence: str, anchor: str, width: int = QUOTE_WIDTH) -> str:
    """截锚点前后一小段；截断处补 `…`，保证锚点一定在引文里。"""
    if len(sentence) <= width:
        return sentence
    start = sentence.find(anchor)
    start = 0 if start < 0 else max(0, start - width // 3)
    piece = sentence[start : start + width]
    if start > 0:
        piece = "…" + piece
    if start + width < len(sentence):
        piece = piece + "…"
    return piece


def _spot(para_no: int, sent_no: int, sentence: str, hit: str = "", label: str = "") -> dict:
    """一个位置。`hit` 给词表命中或句末标点（`hit in quote` 必须成立）；句子特征放 `label`。"""
    return {
        "para": para_no,
        "sent": sent_no,
        "hit": hit,
        "label": label,
        "quote": _quote(sentence, hit or sentence[:1]),
    }


def _token_spots(sentence: str, terms: tuple[str, ...]) -> list[str]:
    """最长匹配优先，避免「但是」又算一次「但」；一句里同一词只报一次。"""
    found: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        if term and term in sentence and not any(term in seen for seen in found):
            found.append(term)
    return found


def _cjk_len(text: str) -> int:
    return len(CJK_RE.findall(text))


def _struct_spots(parsed: list[dict], attr_id: str) -> list[dict]:
    rows: list[dict] = []
    if attr_id == "L2-08":
        for p in parsed:
            n = _cjk_len(p["text"])
            if 0 < n <= 6:
                rows.append(_spot(p["para"], p["sent"], p["text"], label=f"{n} 字"))
    elif attr_id == "L2-05":
        for p in parsed:
            tail = p["text"][-1:] if p["text"] else ""
            if tail in "？?":
                rows.append(_spot(p["para"], p["sent"], p["text"], hit=tail, label="问句"))
            elif tail in "！!":
                rows.append(_spot(p["para"], p["sent"], p["text"], hit=tail, label="感叹句"))
    return rows


def spots(text: str, attribute_id: str, limit: int = 3) -> list[dict]:
    """这条属性的句级证据；全篇统计类返回 `[]`（不编位置）。"""
    if limit <= 0:
        return []
    parsed = sentences(text)
    if attribute_id in STRUCT_ATTRS:
        return _struct_spots(parsed, attribute_id)[:limit]
    terms = TOKENS.get(attribute_id)
    if not terms:
        return []
    out: list[dict] = []
    for p in parsed:
        for hit in _token_spots(p["text"], terms):
            out.append(_spot(p["para"], p["sent"], p["text"], hit))
            if len(out) >= limit:
                return out
    return out


def describe(found: list[dict]) -> str:
    """证据的短说明：`¶3 句2「…」；¶5 句1「…」`。"""
    return "；".join(f"¶{s['para']} 句{s['sent']}「{s['quote']}」" for s in found)


def attach(rows: list[dict], text: str, limit: int = 2) -> None:
    """给不过的行补 `evidence` / `evidence_note`；过的行不动。

    说明里写「全文共几处」，这样指标偏低时也不会只剩两句被当成「该改的地方」。
    """
    for row in rows:
        if row.get("ok"):
            continue
        found_all = spots(text, row["attribute_id"], limit=_COUNT)
        found = found_all[:limit]
        row["evidence"] = found
        if not found_all:
            row["evidence_note"] = NO_EVIDENCE
        elif len(found_all) <= limit:
            row["evidence_note"] = f"全文 {len(found_all)} 处：{describe(found)}"
        else:
            row["evidence_note"] = f"全文共 {len(found_all)} 处，前 {limit} 处：{describe(found)}"
