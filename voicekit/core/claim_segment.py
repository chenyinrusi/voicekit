"""Claim segmentation: deterministic levels Document → Paragraph → Sentence → Candidate → Claim.

v1 scope: no embeddings, no LLM segmentation. Rules are punctuation- and
subject-predicate-based, and every level keeps parent references.

Modes (cumulative):
    sentence        split on sentence terminators
    semicolon       + split on ； ;
    colon           + split on ： :  (only when both sides carry a predicate)
    comma           + split on ， when the right side starts with a subject and carries a predicate
    v2supplement    + v2 claim spans as extra candidates (caller passes them)

Design notes:
- Claim = candidate span until a later stage normalises text. We keep `spans`
  as a list so a future non-contiguous normalisation can record parts separately.
- Do not assume punctuation defines a claim; every rule has a guard.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SENT_END = re.compile(r"[^。！？\n]+[。！？]?")
SUBJECT_START = re.compile(
    r"^(我|我们|你|他|她|它|这|那|该|其|这家|那家|公司|企业|行业|数据|报告|机构|平台|"
    r"产品|收入|保费|销量|车险|非车|AI|Microchip|OpenAI|Anthropic|季薇|吕缙勇|桑吉|姜鑫|王晗玉|"
    r"传统产品|数据中心|小公司|大公司|投资人|客户|市场|增长|增速)"
)
PREDICATE = re.compile(
    r"(是|不是|有|没有|会|将|要|能|可以|已|正在|不|没|被|让|使|导致|增长|下降|延长|缩|涨|跌|"
    r"占|需要|看|算|说|认为|觉得|怕是|来自|成为|撑|好看|难看|空着|难看|贴地|起量|拉动|补)"
)
MIN_LEN = 8


@dataclass
class Span:
    start: int
    end: int


@dataclass
class Claim:
    spans: list[Span]
    text: str
    sentence_id: str
    paragraph_id: str
    document_id: str


@dataclass
class Sentence:
    spans: list[Span]
    text: str
    paragraph_id: str
    document_id: str
    id: str


@dataclass
class Paragraph:
    start: int
    end: int
    document_id: str
    id: str


@dataclass
class Document:
    text: str
    id: str
    paragraphs: list[Paragraph] = field(default_factory=list)
    sentences: list[Sentence] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


def _split(text: str, base: int, pattern: str) -> list[Span]:
    """Split text on separators, keeping the separator with the left part."""
    spans: list[Span] = []
    start = 0
    for m in re.finditer(pattern, text):
        cut = m.end()
        piece = text[start:cut]
        if piece.strip():
            spans.append(Span(base + start, base + cut))
        start = cut
    if text[start:].strip():
        spans.append(Span(base + start, base + len(text)))
    return spans


def _trim(text: str) -> str:
    return text.strip()


def _has_pred(s: str) -> bool:
    return bool(PREDICATE.search(s))


def segment(text: str, mode: str = "sentence", extra_spans: list[Span] | None = None,
            document_id: str = "doc") -> Document:
    doc = Document(text=text, id=document_id)
    # paragraphs: blank-line separated blocks
    for pi, block in enumerate(re.split(r"\n\s*\n", text)):
        if not block.strip():
            continue
        offset = text.find(block)
        doc.paragraphs.append(Paragraph(offset, offset + len(block), document_id, f"{document_id}-p{pi}"))

    order = ["sentence", "semicolon", "colon", "comma"]
    level = order.index(mode) if mode in order else 0
    for _pi, paragraph in enumerate(doc.paragraphs):
        body = text[paragraph.start:paragraph.end]
        sentences = _split(body, paragraph.start, r"[。！？]")
        pieces: list[Span] = []
        if level >= 1:
            for s in sentences:
                pieces.extend(_split(text[s.start:s.end], s.start, r"[；;]"))
        else:
            pieces = sentences
        if level >= 2:
            out = []
            for piece in pieces:
                seg = text[piece.start:piece.end]
                if "：" in seg or ":" in seg:
                    left, right = re.split(r"[：:]", seg, maxsplit=1)
                    if len(left.strip()) >= MIN_LEN and _has_pred(right) and len(right.strip()) >= MIN_LEN:
                        mid = piece.start + len(left) + 1
                        out.append(Span(piece.start, mid))
                        out.append(Span(mid, piece.end))
                        continue
                out.append(piece)
            pieces = out
        if level >= 3:
            out = []
            for piece in pieces:
                seg = text[piece.start:piece.end]
                parts = seg.split("，")
                if len(parts) == 1:
                    out.append(piece)
                    continue
                cursor = piece.start
                current = parts[0]
                for part in parts[1:]:
                    if len(current.strip()) >= MIN_LEN and SUBJECT_START.search(part.strip()) \
                            and _has_pred(part) and len(part.strip()) >= MIN_LEN:
                        out.append(Span(cursor, cursor + len(current)))
                        cursor += len(current) + 1
                        current = part
                    else:
                        current = current + "，" + part
                out.append(Span(cursor, cursor + len(current)))
            pieces = out

        kept: list[Span] = []
        for piece in pieces:
            stripped = text[piece.start:piece.end].strip()
            if len(stripped) < MIN_LEN or stripped.startswith("〔") or stripped.startswith("#"):
                continue
            if kept and _overlap(text, kept[-1], piece) >= 0.9:
                continue
            kept.append(piece)
        for si, piece in enumerate(kept):
            sid = f"{paragraph.id}-s{si}"
            doc.sentences.append(Sentence([piece], text[piece.start:piece.end], paragraph.id, document_id, sid))
            doc.claims.append(Claim([Span(piece.start, piece.end)], text[piece.start:piece.end].strip(),
                                    sid, paragraph.id, document_id))

    if extra_spans:
        for sp in extra_spans:
            if not any(_overlap(text, sp, c.spans[0]) >= 0.5 for c in doc.claims):
                sid = f"{document_id}-x{len(doc.claims)}"
                doc.claims.append(Claim([Span(sp.start, sp.end)], text[sp.start:sp.end].strip(),
                                        sid, doc.paragraphs[0].id if doc.paragraphs else sid, document_id))
    return doc


def _overlap(text: str, a: Span, b: Span) -> float:
    inter = max(0, min(a.end, b.end) - max(a.start, b.start))
    union = max(a.end, b.end) - min(a.start, b.start)
    return inter / union if union else 0.0


MODES = ("sentence", "semicolon", "colon", "comma")
