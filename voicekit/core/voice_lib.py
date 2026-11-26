"""中文文风计量引擎（纯 stdlib）。

只算能算的：字面、句法、节奏、篇章位置、语气与心理词表。
不判断"写得好不好"，也不判断"哪句算文言味"——那是 style-card 与人。

属性 ID 与 `docs/design/DD-VOICE-001-attribute-registry.md`（v2.0 锁定版）对应；
本模块实现 §4 的 P0 `style_lint` 24 条；FIX-343 补的 18 条在 `measure_extra.py`。

两段式：`measure()` 只出原始证据（计数、切分结果）；`resolve()` 系列函数把证据换算成
可比较的值 —— 签名类属性（高频词/搭配/句首）需要作者侧榜单，来自 profile.json。
这样"哪个词算高频""哪句算句首模式"由语料决定，不写死在代码里。
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path
from statistics import median

# 同目录兄弟模块（measure_extra）：直接按脚本运行时 sys.path 不含本目录，
# 与 build_profile/style_lint 的处理方式一致。
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from . import measure_extra  # noqa: E402

# ---------------------------------------------------------------------------
# 字符类与词表
# ---------------------------------------------------------------------------

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]{2,}")
DIGIT_RUN_RE = re.compile(r"\d+")
#: 常见 emoji/图形符号：1F300–1FAFF、2600–27BF、2B00–2BFF，加 ©®™。
EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u00a9\u00ae\u2122]"
)
AA_RE = re.compile(r"([\u4e00-\u9fff])\1")

#: 全角标点谱（L1-01 取样集合）。顺序稳定，便于 diff。
PUNCT_MARKS = "，。、；：？！…—「」『』“”‘’（）《》【】"

#: 功能字：过滤 2/3 字串里的虚词串（无分词器时的近似）。
FUNCTION_CHARS = set(
    "的了是在不我你他她它这那就都也又很才只把被给对从向往还更和与及而而且或者呢吧啊嘛哦"
    "么什怎样这么那为以于其之乎者矣们个"
)
#: 榜单过滤：以虚词开头 / 以「的了着过」结尾的串是切分产物，不是词。
GLUE_HEAD = set("的了着过是吗呢吧啊嘛哦是在和与就都也又很才只把被给对从向往还更及而但或者个所")
GLUE_TAIL = set("的了着过吗呢吧啊嘛哦")

#: L1-12 文白夹杂：只认文言专用标记。**不数** 以/而/之/于/其——它们在现代词里
#: 出现更多（可以/所以/之后/于是），2026-11-02 review 实测占旧口径 88%。
#: 也**不数** 故/尔（故事/偶尔 是常用词）。
CLASSICAL_MARKERS = (
    "而已", "乃至", "亦然", "亦不", "未尝", "何以", "可谓", "莫非", "莫不", "何其",
    "莫过于", "如此而已", "云云", "焉知", "尚可", "尤为", "颇有", "若是", "若此",
    "皆可", "皆不", "岂", "矣", "焉", "哉", "兮", "乎哉",
    "亦", "乃", "尚", "犹", "遂", "殊", "颇", "皆",
)

#: L1-13 四字格：表内成语/千字。只认表，不猜。
CHENGYU = (
    "画蛇添足", "守株待兔", "刻舟求剑", "掩耳盗铃", "亡羊补牢", "井底之蛙", "杯弓蛇影",
    "叶公好龙", "自相矛盾", "滥竽充数", "买椟还珠", "南辕北辙", "缘木求鱼", "饮鸩止渴",
    "拔苗助长", "邯郸学步", "东施效颦", "望洋兴叹", "班门弄斧", "对牛弹琴", "门可罗雀",
    "车水马龙", "人山人海", "川流不息", "络绎不绝", "熙熙攘攘", "沸沸扬扬", "纷纷扬扬",
    "不约而同", "不以为然", "不假思索", "不由自主", "不知所措", "不遗余力", "不谋而合",
    "不言而喻", "不可思议", "不厌其烦", "不寒而栗", "不翼而飞", "不胫而走", "不攻自破",
    "一干二净", "一如既往", "一蹴而就", "一针见血", "一目了然", "一览无余", "一意孤行",
    "一丝不苟", "一五一十", "一无所有", "一厢情愿", "一脉相承", "一视同仁", "一言难尽",
    "三言两语", "三番五次", "千言万语", "千头万绪", "万紫千红", "与众不同", "相辅相成",
    "相得益彰", "相提并论", "息息相关", "休戚与共", "惺惺相惜", "心有灵犀", "心平气和",
    "心照不宣", "心旷神怡", "心安理得", "心甘情愿", "心急如焚", "心灰意冷", "无独有偶",
    "无可奈何", "无动于衷", "无济于事", "无所适从", "无微不至", "无影无踪", "无关紧要",
    "无与伦比", "目中无人", "目瞪口呆", "耳濡目染", "耳目一新", "发人深省", "意味深长",
    "语重心长", "苦口婆心", "良苦用心", "理所当然", "理直气壮", "义无反顾", "义正词严",
    "众所周知", "众说纷纭", "念念不忘", "恋恋不舍", "依依不舍", "津津有味", "兴致勃勃",
    "生机勃勃", "娓娓道来", "侃侃而谈", "滔滔不绝", "若有所思", "恍然大悟", "茅塞顿开",
    "豁然开朗", "顺理成章", "水到渠成", "循序渐进", "潜移默化", "耳熟能详", "司空见惯",
    "习以为常", "屡见不鲜", "层出不穷", "应运而生", "油然而生",
)

#: L7-02 缓和 / 增强。
HEDGES = (
    "大概", "也许", "可能", "或许", "似乎", "好像", "差不多", "有点儿", "有些",
    "算是", "我想", "我觉得", "应该是", "大概是", "恐怕", "说不定", "某种程度",
)
BOOSTERS = (
    "一定", "必然", "肯定", "绝对", "毫无疑问", "无疑", "必须", "务必",
    "完全", "彻底", "十分", "极其", "非常",
)

#: L1-16 情态与认识立场。
MODAL_SPEC = ("可能", "也许", "大概", "或许", "似乎", "好像", "说不定", "恐怕", "大约")
MODAL_CERT = ("一定", "必然", "肯定", "绝对", "无疑", "务必", "必须", "当然")

#: L2-02 连接结构六类。同一位置只归一类：类内按长度降序匹配后扣掉。
CONNECTIVES = {
    "并列": ("以及", "还有", "并且", "同时", "和", "与", "及"),
    "从属": ("因为", "由于", "虽然", "尽管", "如果", "要是", "即使", "哪怕"),
    "因果": ("所以", "因此", "于是", "故而", "以致"),
    "条件": ("只要", "只有", "除非", "无论", "不管"),
    "递进": ("而且", "甚至", "何况", "况且", "更"),
    "转折": ("但是", "可是", "然而", "只是", "不过", "却", "但"),
}

#: L7-05 读者称呼（最长匹配，`你们` 不再额外算一次 `你`）。
READER_TERMS = ("你们", "各位", "诸位", "读者", "看官", "大家", "你")

#: L7-09 语气词与插入语。
MODAL_PARTICLES = "呢吧啊呀嘛哦哈咯啦"
INSERTIONS = ("说实话", "你看", "说起来", "老实说", "怎么说呢", "归根结底", "其实")

#: L4-05 过渡标记。
TRANSITIONS = ("好了", "所以", "只是", "于是", "其实", "不过", "然后")

#: L1-14 情绪词。
EMO_POS = ("温暖", "开心", "快乐", "喜欢", "幸福", "美好", "感动", "温柔", "希望", "爱")
EMO_NEG = (
    "孤独", "难过", "悲伤", "痛苦", "遗憾", "失望", "焦虑", "抑郁",
    "害怕", "委屈", "绝望", "无奈", "压抑",
)

#: L1-19 欧化句式（正则计数）。
EUROPEAN_PATTERNS = (
    r"一个[^，。！？]{1,10}的存在",
    r"被[^，。！？]{1,8}所",
    r"进行[^，。！？]{1,6}",
    r"对此[^，。！？]{0,6}",
    r"在[^，。！？]{1,10}方面",
)

#: 平台噪声行（简书导出头 + 关注/赞赏尾巴）。剥掉，不算风格。
PLATFORM_LINE_RE = re.compile(
    r"^\s*(?:"
    r"Title:|URL Source:|Published Time:|Markdown Content:|Author:|Source:"
    r"|本文首发于|首发于|关注公众号|往期推荐|编辑：|责任编辑"
    r"|!\[.*?\]\(.*?\)"
    r")",
)
PLATFORM_PHRASES = ("（图源网络）", "（题图来自网络）", "未经授权", "赞赏作者")


# ---------------------------------------------------------------------------
# 清洗与切分
# ---------------------------------------------------------------------------


def clean_platform_noise(text: str) -> tuple[str, dict]:
    """剥平台注入，保留作者写的 PS 与自拟小标题。

    返回 (正文, 统计)。判据（设计文档 §2.3）：位置固定 + 措辞模板化 = 平台生成。
    """
    dropped = {"header_lines": 0, "dup_title": 0, "phrases": 0}
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        if PLATFORM_LINE_RE.match(line):
            dropped["header_lines"] += 1
            continue
        for phrase in PLATFORM_PHRASES:
            if phrase in line:
                line = line.replace(phrase, "")
                dropped["phrases"] += 1
        kept.append(line)
    # 去顶部重复标题：首个非空行与第二个非空行相同则删第二个。
    nonempty = [i for i, line in enumerate(kept) if line.strip()]
    if len(nonempty) >= 2 and kept[nonempty[0]].strip() == kept[nonempty[1]].strip():
        del kept[nonempty[1]]
        dropped["dup_title"] = 1
    body = "\n".join(kept).strip("\n")
    return body, dropped


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


TITLE_RE = re.compile(r"^Title:\s*(.+?)\s*$", re.M)


def export_title(raw: str) -> str:
    """导出文件元数据里的标题（简书导出）；没有就返回空串。"""
    match = TITLE_RE.search(raw)
    return match.group(1).strip() if match else ""


def split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n+", text) if p.strip()]


def split_sentences(text: str) -> list[str]:
    """切句：句末标点或换行都算边界（导出稿常用换行代替标点）。

    `……` 当作一个边界，吞掉后面连续的 `…`，避免产生以 `…` 开头的伪句。
    """
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch == "\n":
            segment = "".join(buf).strip()
            if segment:
                out.append(segment)
            buf = []
            continue
        buf.append(ch)
        if ch in "。！？!?":
            segment = "".join(buf).strip()
            if segment:
                out.append(segment)
            buf = []
        elif ch == "…":
            segment = "".join(buf).rstrip("…").strip()
            if segment:
                out.append(segment)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    merged: list[str] = []
    for segment in out:
        if merged and not CJK_RE.search(segment):
            merged[-1] += segment  # 纯标点片段（"？！"）并回上一句，不单独成句
        else:
            merged.append(segment)
    return merged


def split_clauses(sentence: str) -> list[str]:
    return [c.strip() for c in re.split(r"[，、；：,;:]", sentence) if c.strip()]


def _cjk_only(text: str) -> str:
    return "".join(m.group(0) for m in CJK_RE.finditer(text))


def _per_k(count: float, chars: int) -> float:
    return 0.0 if chars <= 0 else count * 1000.0 / chars


def _count_any(text: str, terms: tuple[str, ...] | str) -> int:
    return sum(text.count(t) for t in terms)


def _count_nonoverlap(text: str, terms: tuple[str, ...]) -> int:
    """最长匹配计数：命中后扣掉（换成一个非汉字哨兵），避免 `但是` 又算一次 `但`。"""
    hits = 0
    for term in sorted(terms, key=len, reverse=True):
        if term in text:
            hits += text.count(term)
            text = text.replace(term, "\x00")
    return hits


def _safe_ratio(num: float, den: float) -> float:
    return 0.0 if den == 0 else num / den


def _variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _ngram_counts(text: str, n: int) -> dict[str, int]:
    """连续汉字串内的 n 字窗口计数，滤掉虚词串与切分产物。"""
    counts: dict[str, int] = {}
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(run) - n + 1):
            gram = run[i : i + n]
            if all(ch in FUNCTION_CHARS for ch in gram):
                continue
            if gram[0] in GLUE_HEAD or gram[-1] in GLUE_TAIL:
                continue
            counts[gram] = counts.get(gram, 0) + 1
    return counts


def _msttr(text: str, window: int = 50) -> float:
    """固定窗 TTR（MATTR 简版），按篇长归一，避免 TTR 随长度漂移。"""
    chars = CJK_RE.findall(text)
    if not chars:
        return 0.0
    if len(chars) < window:
        return len(set(chars)) / len(chars)
    windows = [chars[i : i + window] for i in range(0, len(chars) - window + 1, window)]
    return sum(len(set(w)) / window for w in windows) / len(windows)


def _hapax_ratio(text: str) -> float:
    counts: dict[str, int] = {}
    for ch in CJK_RE.findall(text):
        counts[ch] = counts.get(ch, 0) + 1
    if not counts:
        return 0.0
    return sum(1 for v in counts.values() if v == 1) / len(counts)


def _yules_k(text: str) -> float:
    """Yule's K（字频口径，不是词频）：10^4 * (Σ v²·f_v − N) / N²。"""
    counts: dict[str, int] = {}
    for ch in CJK_RE.findall(text):
        counts[ch] = counts.get(ch, 0) + 1
    n = sum(counts.values())
    if n <= 1:
        return 0.0
    spectrum: dict[int, int] = {}
    for v in counts.values():
        spectrum[v] = spectrum.get(v, 0) + 1
    total = sum(v * v * f for v, f in spectrum.items())
    return 10000.0 * (total - n) / (n * n)


def _punct_counts(text: str) -> dict[str, int]:
    return {mark: text.count(mark) for mark in PUNCT_MARKS}


# ---------------------------------------------------------------------------
# 第一段：原始证据
# ---------------------------------------------------------------------------


def measure(text: str) -> dict:
    """算一篇稿的原始证据。

    返回：已能算的属性值（键 = attribute_id）+ 原始证据
    （`_chars` / `_sentences` / `L1-08_grams` / `L1-10_grams` / `L2-03_initials` / `_doc`）。
    签名类四条（L1-08 / L1-10 / L2-03 / L2-04）不在这里出值，见 `signature_density()`。
    """
    body, _ = clean_platform_noise(text)
    chars = cjk_count(body)
    if chars == 0:
        raise ValueError("no CJK text after cleaning")
    paragraphs = split_paragraphs(body)
    sentences = split_sentences(body)
    sent_lens = [cjk_count(s) for s in sentences]
    clause_lens = [cjk_count(c) for s in sentences for c in split_clauses(s)]
    punct_counts = _punct_counts(body)

    total_sent = len(sentences) or 1
    buckets = {"le10": 0, "11_20": 0, "21_30": 0, "31_50": 0, "gt50": 0}
    for n in sent_lens:
        if n <= 10:
            buckets["le10"] += 1
        elif n <= 20:
            buckets["11_20"] += 1
        elif n <= 30:
            buckets["21_30"] += 1
        elif n <= 50:
            buckets["31_50"] += 1
        else:
            buckets["gt50"] += 1

    terminal = {"statement": 0, "question": 0, "exclam": 0}
    for s in sentences:
        if s.endswith("？") or s.endswith("?"):
            terminal["question"] += 1
        elif s.endswith("！") or s.endswith("!"):
            terminal["exclam"] += 1
        else:
            terminal["statement"] += 1

    initials_count: dict[str, int] = {}
    for s in sentences:
        head = s[:2]
        if len(head) == 2 and CJK_RE.match(head):
            initials_count[head] = initials_count.get(head, 0) + 1

    paragraphs_len = [cjk_count(p) for p in paragraphs]
    para_rhythm = {"LL": 0, "SS": 0, "LS": 0, "SL": 0}
    for a, b in zip(paragraphs_len, paragraphs_len[1:], strict=False):
        key = ("L" if a > 40 else "S") + ("L" if b > 40 else "S")
        para_rhythm[key] += 1
    ps_paragraphs = sum(1 for p in paragraphs if re.match(r"^(?:PS|Ps|ps)[:：]", p))

    # 连接词：类内最长匹配后扣掉；类间共用一个游标，避免一词进两类。
    conn_detail: dict[str, float] = {}
    conn_total = 0
    cursor = body
    for name, words in CONNECTIVES.items():
        hits = 0
        for word in sorted(words, key=len, reverse=True):
            if word in cursor:
                hits += cursor.count(word)
                cursor = cursor.replace(word, "\x00")
        conn_detail[name] = round(_per_k(hits, chars), 3)
        conn_total += hits

    hedge = _count_any(body, HEDGES)
    booster = _count_any(body, BOOSTERS)
    european = sum(len(re.findall(p, body)) for p in EUROPEAN_PATTERNS)

    insert_extra = measure_extra.extra_values(
        body,
        chars,
        sentences,
        paragraphs,
        punct_counts,
        cjk_only=_cjk_only,
        cjk_count=cjk_count,
        per_k=_per_k,
        safe_ratio=_safe_ratio,
        count_any=_count_any,
        count_nonoverlap=_count_nonoverlap,
        ngram_counts=_ngram_counts,
        split_sentences=split_sentences,
    )

    values: dict = {
        # L1 字面
        "L1-01": {m: round(_per_k(c, chars), 3) for m, c in punct_counts.items()},
        "L1-02": round(
            _safe_ratio(
                punct_counts.get("、", 0), punct_counts.get("、", 0) + punct_counts.get("，", 0)
            ),
            4,
        ),
        "L1-05": round(_per_k(len(DIGIT_RUN_RE.findall(body)), chars), 3),
        "L1-06": {
            "latin_per_k": round(_per_k(len(LATIN_RE.findall(body)), chars), 3),
            "emoji_per_k": round(_per_k(len(EMOJI_RE.findall(body)), chars), 3),
        },
        "L1-09": {
            "de": round(_per_k(body.count("的"), chars), 2),
            "shi": round(_per_k(body.count("是"), chars), 2),
            "le": round(_per_k(body.count("了"), chars), 2),
            "bu": round(_per_k(body.count("不"), chars), 2),
            "zai": round(_per_k(body.count("在"), chars), 2),
        },
        "L1-11": {
            "msttr": round(_msttr(body), 4),
            "hapax": round(_hapax_ratio(body), 4),
            "yules_k": round(_yules_k(body), 1),
        },
        "L1-12": round(_per_k(_count_nonoverlap(body, CLASSICAL_MARKERS), chars), 2),
        "L1-13": round(_per_k(_count_any(body, CHENGYU), chars), 2),
        "L1-14": {
            "pos_per_k": round(_per_k(_count_any(body, EMO_POS), chars), 2),
            "neg_per_k": round(_per_k(_count_any(body, EMO_NEG), chars), 2),
        },
        "L1-16": {
            "spec_per_k": round(_per_k(_count_any(body, MODAL_SPEC), chars), 2),
            "cert_per_k": round(_per_k(_count_any(body, MODAL_CERT), chars), 2),
        },
        "L1-19": round(_per_k(european, chars), 2),
        # L2 句法
        "L2-01": {
            "mean": round(sum(sent_lens) / len(sent_lens), 2),
            "median": float(median(sent_lens)),
            "variance": round(_variance([float(n) for n in sent_lens]), 1),
            "clauses_per_sentence": round(_safe_ratio(len(clause_lens), total_sent), 3),
            **{f"share_{k}": round(v / total_sent, 4) for k, v in buckets.items()},
        },
        "L2-02": {"total_per_k": round(_per_k(conn_total, chars), 2), **conn_detail},
        # 只judge 问句/感叹句占比：陈述句占比恒等于 1-两问句，恒为 1.0 的项不判。
        "L2-05": {
            "question": round(terminal["question"] / total_sent, 4),
            "exclam": round(terminal["exclam"] / total_sent, 4),
        },
        "L2-11": {
            "le": round(_per_k(body.count("了"), chars), 2),
            "guo": round(_per_k(body.count("过"), chars), 2),
            "zhe": round(_per_k(body.count("着"), chars), 2),
            "zhengzai": round(_per_k(body.count("正在"), chars), 2),
            "cengjing": round(_per_k(body.count("曾经"), chars), 2),
        },
        # L3 节奏
        "L3-02": round(_per_k(len(AA_RE.findall(_cjk_only(body))), chars), 2),
        "L3-05": {
            "clause_mean": round(sum(clause_lens) / len(clause_lens), 2) if clause_lens else 0.0,
            "clause_variance": round(_variance([float(n) for n in clause_lens]), 1),
            # 每句真实标点数（2026-11-02 review 修正：旧口径是"每千字密度之和/句数"）
            "punct_per_sentence": round(sum(punct_counts.values()) / total_sent, 3),
            "comma_colon": round(
                _safe_ratio(punct_counts.get("，", 0), punct_counts.get("。", 0)), 4
            ),
        },
        # L4 篇章
        "L4-05": {t: round(_per_k(body.count(t), chars), 3) for t in TRANSITIONS},
        # L7 语气
        "L7-02": {
            "hedge_per_k": round(_per_k(hedge, chars), 2),
            "booster_per_k": round(_per_k(booster, chars), 2),
        },
        "L7-05": round(_per_k(_count_nonoverlap(body, READER_TERMS), chars), 2),
        "L7-09": {
            "particles_per_k": round(_per_k(_count_any(body, MODAL_PARTICLES), chars), 2),
            "insertions_per_k": round(_per_k(_count_any(body, INSERTIONS), chars), 2),
        },
        # 原始证据（签名类属性要靠作者侧榜单换算）
        # FIX-343：18 条补口的属性在 measure_extra（口径见该文件头）。
        **insert_extra,
        "L1-08_grams": _ngram_counts(body, 2),
        "L1-10_grams": _ngram_counts(body, 3),
        "L2-03_initials": initials_count,
        "_chars": chars,
        "_doc": {
            "cjk_chars": chars,
            "sentences": len(sentences),
            "paragraphs": len(paragraphs),
            "paragraph_mean": round(sum(paragraphs_len) / len(paragraphs_len), 2)
            if paragraphs_len
            else 0.0,
            "paragraph_median": float(median(paragraphs_len)) if paragraphs_len else 0.0,
            "short_para_share": round(
                sum(1 for n in paragraphs_len if n <= 25) / len(paragraphs), 4
            )
            if paragraphs
            else 0.0,
            "para_rhythm": para_rhythm,
            "ps_paragraphs": ps_paragraphs,
            "first_para_len": paragraphs_len[0] if paragraphs_len else 0,
            "last_para_len": paragraphs_len[-1] if paragraphs_len else 0,
        },
    }
    return values


# ---------------------------------------------------------------------------
# 第二段：签名类属性（需要作者侧榜单）
# ---------------------------------------------------------------------------

#: 签名类属性 → 跨篇合并取前 N。
SIGNATURE_ATTRS = {"L1-08": 20, "L1-10": 12, "L2-03": 12, "L2-04": 12}


def _signature_key(attribute_id: str) -> str:
    return "L2-03_initials" if attribute_id == "L2-03" else f"{attribute_id}_grams"


def signature_density(raw: dict, attribute_id: str, grams: list[str]) -> float:
    """签名词密度：表内词条命中次数 / 千字。"""
    counts = raw[_signature_key(attribute_id)]
    total = sum(counts.get(g, 0) for g in grams)
    return round(_per_k(total, raw["_chars"]), 3)


def merge_signature(per_doc: list[dict], attribute_id: str, top_n: int) -> list[list]:
    """跨篇合并签名榜（取前 top_n），返回 [[gram, 篇合计次数], ...]。"""
    key = _signature_key(attribute_id)
    totals: dict[str, int] = {}
    for doc in per_doc:
        for gram, count in doc[key].items():
            totals[gram] = totals.get(gram, 0) + count
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return [[g, c] for g, c in ranked[:top_n]]


# ---------------------------------------------------------------------------
# 属性登记（顺序 = 注册表 §4 取用顺序；名称与注册表一致）
# ---------------------------------------------------------------------------

METRICS: list[dict] = [
    {"id": "L1-01", "name": "标点谱", "category": "L1 字面", "mtype": "分布",
     "method": "全角标点/千字", "guidance": "逗号密度高、句号中等；少用感叹与问号"},
    {"id": "L1-02", "name": "顿号与并列标点", "category": "L1 字面", "mtype": "定量",
     "method": "顿号/(顿号+逗号)", "guidance": "并列项用逗号少用顿号"},
    {"id": "L1-05", "name": "数字与日期格式", "category": "L1 字面", "mtype": "定量",
     "method": "阿拉伯数字串/千字", "guidance": "数字多写成汉字，年月少写"},
    {"id": "L1-06", "name": "非汉字符号使用", "category": "L1 字面", "mtype": "定量",
     "method": "拉丁词、emoji/千字", "guidance": "几乎不用 emoji；英文词只在专名里出现"},
    {"id": "L1-08", "name": "高频实词", "category": "L1 字面", "mtype": "分布",
     "method": "签名 2 字词密度/千字", "guidance": "沿用榜上的词，别换同义书面词"},
    {"id": "L1-09", "name": "虚词向量与分析性", "category": "L1 字面", "mtype": "分布",
     "method": "的/是/了/不/在 /千字", "guidance": "的密度高；判断句多用是"},
    {"id": "L1-10", "name": "专属搭配", "category": "L1 字面", "mtype": "分布",
     "method": "签名 3 字串密度/千字", "guidance": "用榜上的固定搭配，别临时造新词"},
    {"id": "L1-11", "name": "词汇多样性", "category": "L1 字面", "mtype": "分布",
     "method": "MSTTR / hapax / Yule's K（字口径）", "guidance": "重复自己常用的字词是特征，不是缺点"},
    {"id": "L1-12", "name": "文白夹杂", "category": "L1 字面", "mtype": "分布",
     "method": "文言专用标记/千字", "guidance": "每千字留几个文言标记，别通篇白话"},
    {"id": "L1-13", "name": "四字格与成语", "category": "L1 字面", "mtype": "定量",
     "method": "表内成语/千字", "guidance": "成语密度低，别堆"},
    {"id": "L1-14", "name": "情绪词", "category": "L1 字面", "mtype": "分布",
     "method": "正/负情绪词表/千字", "guidance": "情绪词直说，不靠夸张"},
    {"id": "L1-16", "name": "情态与认识立场", "category": "L1 字面", "mtype": "分布",
     "method": "推测词/确定词 千字", "guidance": "推测词远多于确定词"},
    {"id": "L1-19", "name": "欧化句式", "category": "L1 字面", "mtype": "定量",
     "method": "欧化正则/千字", "guidance": "不写「一个…的存在」「被…所」「进行…」"},
    {"id": "L2-01", "name": "句长与分句密度", "category": "L2 句法", "mtype": "分布",
     "method": "句长分布/方差/每句小句数", "guidance": "句子平均三四十字，长句靠逗号串"},
    {"id": "L2-02", "name": "连接结构配比", "category": "L2 句法", "mtype": "分布",
     "method": "六类连接词/千字（最长匹配，不重复计数）", "guidance": "转折与并列多，因果少"},
    {"id": "L2-03", "name": "句首模式", "category": "L2 句法", "mtype": "分布",
     "method": "签名句首词密度/千字", "guidance": "沿用榜上的句首，如「只是」「其实」「于是」"},
    {"id": "L2-05", "name": "句类配比", "category": "L2 句法", "mtype": "分布",
     "method": "问句/感叹句占比（陈述句占比 = 1−两者）", "guidance": "几乎全是陈述句，问句极少，感叹几乎没有"},
    {"id": "L2-11", "name": "体貌标记", "category": "L2 句法", "mtype": "分布",
     "method": "了/过/着/正在/曾经/千字", "guidance": "用「了」表完成，少用「着」「正在」"},
    {"id": "L3-02", "name": "叠词", "category": "L3 韵律", "mtype": "定量",
     "method": "AA 式/千字", "guidance": "偶尔叠字，不连续堆"},
    {"id": "L3-05", "name": "句读与停顿节奏", "category": "L3 韵律", "mtype": "分布",
     "method": "小句长/标点每句/逗号句号比", "guidance": "一句三四个小句，靠逗号推进"},
    {"id": "L4-05", "name": "过渡标记", "category": "L4 篇章", "mtype": "分布",
     "method": "好了/所以/只是/于是/千字", "guidance": "转场词整体少用；好了只在少数篇出现，别硬加"},
    {"id": "L7-02", "name": "缓和与增强", "category": "L7 语气", "mtype": "定量",
     "method": "hedges / boosters /千字", "guidance": "结论加限定，缓和词远多于增强词"},
    {"id": "L7-05", "name": "读者称呼", "category": "L7 语气", "mtype": "分布",
     "method": "你/你们/大家/千字（最长匹配）", "guidance": "直接说「你」，不用「各位读者」"},
    {"id": "L7-09", "name": "语气词与插入语", "category": "L7 语气", "mtype": "分布",
     "method": "呢/吧/啊 + 说实话/你看 /千字", "guidance": "句尾偶尔带语气词，插入语用「其实」"},
    # FIX-343：注册表"算"档里原先没有打分口的属性，按篇能算的补在这里。
    {"id": "L1-15", "name": "评价词", "category": "L1 字面", "mtype": "分布",
     "method": "值得/不错/挺好 vs 糟糕/遗憾/可惜 /千字", "guidance": "评好坏用轻的词，不用大词"},
    {"id": "L1-17", "name": "网络流行语", "category": "L1 字面", "mtype": "定量",
     "method": "2020 后流行语命中 /千字", "guidance": "不用网络梗"},
    {"id": "L2-04", "name": "句尾模式", "category": "L2 句法", "mtype": "分布",
     "method": "句末 2 字签名榜密度/千字", "guidance": "句尾收在榜上的字眼上"},
    {"id": "L2-08", "name": "碎片句", "category": "L2 句法", "mtype": "分布",
     "method": "≤8 字句占比 + 单句成段占比", "guidance": "偶尔一个短句成段，不连续碎"},
    {"id": "L2-12", "name": "名词化", "category": "L2 句法", "mtype": "定量",
     "method": "性/化/度/主义 后缀字 /千字", "guidance": "少用「XX性」「XX化」这类抽象后缀"},
    {"id": "L4-09", "name": "衔接与呼应", "category": "L4 篇章", "mtype": "分布",
     "method": "这/那/它/此/该 /千字 + 首末段 2 字词复现率", "guidance": "靠指代串起来，结尾回扣开头"},
    {"id": "L5-06", "name": "对话与引语", "category": "L5 修辞", "mtype": "分布",
     "method": "引号对/千字 + 引语占比", "guidance": "引原话用引号，不转述成长句"},
    {"id": "L6-08", "name": "夸张与弱化", "category": "L6 修辞", "mtype": "分布",
     "method": "极度/非常/最 vs 罢了/而已/只是 /千字", "guidance": "弱化多、夸张少"},
    {"id": "L6-10", "name": "顶针与回环", "category": "L6 修辞", "mtype": "定量",
     "method": "上句尾 2 字 = 下句首 2 字 的次数/千字", "guidance": "极少用顶针，别刻意回环"},
    {"id": "L7-10", "name": "元话语", "category": "L7 语气", "mtype": "分布",
     "method": "框架/自指/注释/增强 四组 /千字", "guidance": "少谈自己怎么写，直接写"},
    {"id": "L8-01", "name": "社会词", "category": "L8 语义场", "mtype": "定量",
     "method": "我们/朋友/家人/聊天 /千字", "guidance": "人和人的场景多，抽象的少"},
    {"id": "L8-02", "name": "洞察词", "category": "L8 语义场", "mtype": "定量",
     "method": "明白/意识到/原来/才发现 /千字", "guidance": "把「想通了什么」写出来"},
    {"id": "L8-03", "name": "差异词", "category": "L8 语义场", "mtype": "定量",
     "method": "应该/本来/要是 /千字", "guidance": "多用「本来」「应该」这类对照词"},
    {"id": "L8-04", "name": "否定词", "category": "L8 语义场", "mtype": "定量",
     "method": "不/没/别/未 /千字", "guidance": "否定直说，不用「并非」"},
    {"id": "L8-05", "name": "时空聚焦", "category": "L8 语义场", "mtype": "分布",
     "method": "过去/现在/未来 + 这里/远方 /千字", "guidance": "落在具体时间和地点上"},
    {"id": "L8-06", "name": "自我聚焦", "category": "L8 语义场", "mtype": "定量",
     "method": "我/自己 /千字（不作心理判断）", "guidance": "第一人称密度高"},
    {"id": "L9-08", "name": "问题驱动", "category": "L9 论证", "mtype": "分布",
     "method": "问句段占比 + 问号/千字", "guidance": "不用设问推进，直接陈述"},
    {"id": "L10-02", "name": "回避标记", "category": "L10 负空间", "mtype": "分布",
     "method": "脏话/叠标点 /千字", "guidance": "不骂人、不堆感叹"},
]

METRIC_IDS = [m["id"] for m in METRICS]

#: 逐条比较口径：scalar = 标量；dict = 逐子项；签名类四条仍是 scalar，
#: 但值要靠 profile 里的签名表换算（`signature_density()`）。
KINDS = {
    "L1-01": "dict", "L1-02": "scalar", "L1-05": "scalar", "L1-06": "dict",
    "L1-08": "scalar", "L1-09": "dict", "L1-10": "scalar", "L1-11": "dict",
    "L1-12": "scalar", "L1-13": "scalar", "L1-14": "dict", "L1-16": "dict",
    "L1-19": "scalar", "L2-01": "dict", "L2-02": "dict", "L2-03": "scalar",
    "L2-05": "dict", "L2-11": "dict", "L3-02": "scalar", "L3-05": "dict",
    "L4-05": "dict", "L7-02": "dict", "L7-05": "scalar",
    "L7-09": "dict",
    # FIX-343
    "L1-15": "dict", "L1-17": "scalar", "L2-04": "scalar", "L2-08": "dict",
    "L2-12": "scalar", "L4-09": "dict", "L5-06": "dict", "L6-08": "dict",
    "L6-10": "scalar", "L7-10": "dict", "L8-01": "scalar", "L8-02": "scalar",
    "L8-03": "scalar", "L8-04": "scalar", "L8-05": "dict", "L8-06": "scalar",
    "L9-08": "dict", "L10-02": "dict",
}

#: 每千字 / 占比 / 计数——决定公差绝对下限的量纲。
UNITS: dict = {
    "L1-01": "density", "L1-02": "share", "L1-05": "density", "L1-06": "density",
    "L1-08": "density", "L1-09": "density", "L1-10": "density", "L1-11": "share",
    "L1-12": "density", "L1-13": "density", "L1-14": "density", "L1-16": "density",
    "L1-19": "density",
    "L2-01": {
        "mean": "density", "median": "density", "variance": "density",
        "clauses_per_sentence": "count",
        "share_le10": "share", "share_11_20": "share", "share_21_30": "share",
        "share_31_50": "share", "share_gt50": "share",
    },
    "L2-02": "density", "L2-03": "density",
    "L2-05": {"question": "share", "exclam": "share"}, "L2-11": "density",
    "L3-02": "density",
    "L3-05": {
        "clause_mean": "density", "clause_variance": "density",
        "punct_per_sentence": "count", "comma_colon": "count",
    },
    "L4-05": "density",
    "L7-02": "density", "L7-05": "density", "L7-09": "density",
    # FIX-343
    "L1-15": "density", "L1-17": "density", "L2-04": "density",
    "L2-08": {"short_share": "share", "single_para_share": "share"},
    "L2-12": "density",
    "L4-09": {"anaphora_per_k": "density", "echo": "share"},
    "L5-06": {"quotes_per_k": "density", "quoted_share": "share"},
    "L6-08": "density", "L6-10": "density",
    "L7-10": "density",
    "L8-01": "density", "L8-02": "density", "L8-03": "density", "L8-04": "density",
    "L8-05": "density", "L8-06": "density",
    "L9-08": {"question_para_share": "share", "question_per_k": "density"},
    "L10-02": "density",
}

#: 各量纲的绝对下限：防基线接近 0 时相对公差失效。
FLOOR_BY_UNIT = {"density": 0.4, "share": 0.03, "count": 0.3}


def unit_of(attribute_id: str, key: str | None = None) -> str:
    """取某条属性的计量单位（dict 类属性可按键取）。"""
    spec = UNITS[attribute_id]
    if isinstance(spec, dict) and key is not None:
        return spec[key]
    return spec  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# 第三段：比对与判定（纯数据逻辑；build_profile 与 style_lint 共用）
# ---------------------------------------------------------------------------

#: 通过线兜底（正常从 profile.meta.pass_score 读 = 校准出来的切点；P15 只是分位参考）。
FALLBACK_PASS_SCORE = 70.0

#: 核心轴（最像作者的四条：句长、句读节奏、文白、缓和/增强）。
#: 2026-11-03 改：不再"逐条都要过"（那会把作者自己一半的稿子判红，见
#: docs/todo/voice-deterministic-core-axes.md），改成"合成偏差不超过
#: profile.meta.core_limit"；判定的复合规则见 `compare`。
CORE_AXES = ("L2-01", "L3-05", "L1-12", "L7-02")
#: 核心轴合成偏差上限的兜底：profile.meta.core_limit 缺失时用（旧 profile 兼容）。
CORE_LIMIT_FALLBACK = 4.5

#: 加权分配置。路径由 `paths` 定：钉住的包里有 weights.json 就用它；
#: 没有（老脚本、--profile 单跑）才退回开发树 `voices/panchen-voice/weights.json`。
#: 以前这里直接写 `Path(__file__).parents[2] / "voices/..."`：装成 wheel 后指向
#: site-packages/voices（不存在），加权表静默退化成等权，`score_weighted` 少报真实值。
_LEGACY_WEIGHTS_FILE = Path(__file__).resolve().parents[2] / "voices/panchen-voice/weights.json"
_WEIGHTS: dict[str, dict] = {}


def weights_path() -> Path:
    """加权表文件：包优先（`<pack>/weights.json`），否则退回开发树旧路径。"""
    from ..paths import maybe_active_pack

    pack = maybe_active_pack()
    if pack is not None:
        candidate = pack / "weights.json"
        if candidate.is_file():
            return candidate
    return _LEGACY_WEIGHTS_FILE


def reset_weights() -> None:
    """换包后清缓存（同一进程连开两个包时用）。"""
    _WEIGHTS.clear()


def load_weights() -> dict:
    """{属性号: 权重} 与配置里的 pass_score。文件缺了/坏了 → 全 1.0（退化成等权，不崩）。

    缓存按文件路径分键：换包（路径变了）就重读，不会沿用上一个包的权重。
    """
    path = weights_path()
    key = str(path)
    if key not in _WEIGHTS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        table: dict[str, float] = {}
        for tier in data.get("tiers") or []:
            for aid in tier.get("ids") or []:
                table[aid] = float(tier.get("weight", 1.0))
        _WEIGHTS[key] = {
            "table": table,
            "pass_score": data.get("pass_score"),
            "calibration": data.get("calibration") or {},
        }
    return _WEIGHTS[key]


def num_pass(obs: float, base: float, rel: float, floor: float) -> bool:
    return abs(obs - base) <= max(rel * abs(base), floor)


def false_accept_upper_bound(accepted: int, total: int, alpha: float = 0.05) -> float:
    """误放率的单侧 Clopper-Pearson 上界（二项分布精确解，二分求根，不依赖 scipy）。

    意义：`0/2 份误放` 不等于"误放率 0"，只说明 95% 置信下不超过这个上界。
    没有这个数，报告里写"零误放"就是拿样本量当结论。
    """
    if total <= 0:
        return 1.0
    if accepted >= total:
        return 1.0
    if accepted <= 0:
        return 1.0 - alpha ** (1.0 / total)

    def tail(p: float) -> float:  # P(X <= accepted | total, p)
        prob = (1.0 - p) ** total
        acc = prob
        for i in range(1, accepted + 1):
            prob *= (total - i + 1) / i * p / (1.0 - p)
            acc += prob
        return acc

    low, high = 0.0, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        if tail(mid) > alpha:
            low = mid
        else:
            high = mid
    return high


def allowed_misses(n_subkeys: int) -> int:
    """多子项属性允许几个子项出界：n<=2 全中；n>=3 允许 max(1, n//4)。"""
    return 0 if n_subkeys <= 2 else max(1, n_subkeys // 4)


def attribute_deviation_ratio(attr: dict, obs) -> float:
    """该属性的归一化偏差：|观测-基线| / 允许公差；多子项取子项最大。

    1.0 = 正好卡在 P85 公差边界；>1 = 比作者 85% 的稿子还偏。
    判定复合规则用它，而不是"逐条 tolerance 过/不过"。
    """
    if obs is None:
        return float("inf")
    if attr["kind"] == "scalar":
        base = float(attr["observed_value"])
        tol = attr["tolerance"]
        limit = max(tol["rel"] * abs(base), tol["floor"])
        return abs(float(obs) - base) / limit if limit else float("inf")
    worst = 0.0
    for key, tol in attr["tolerance"]["per_key"].items():
        base = float(attr["observed_value"][key])
        limit = max(tol["rel"] * abs(base), tol["floor"])
        if not limit:
            return float("inf")
        worst = max(worst, abs(float(obs[key]) - base) / limit)
    return worst


def core_deviation(profile: dict, values: dict) -> float:
    """核心轴归一化偏差的 RMS（均方根）。

    为什么用 RMS 而不是 max：max 只惩罚单轴极值，四条轴一起偏时看不见
    （2026-11-04 独立评审实测：四轴各偏 5.9 倍公差，max 仍 ≤ 上限、判 like）。
    RMS 让多轴同偏按平方放大，单轴粗偏也不会被平均掉。
    缺任何一条核心轴的测量 → 无穷大（安全失败；不要静默跳过）。
    """
    by_id = {a["attribute_id"]: a for a in profile["attributes"]}
    devs: list[float] = []
    for aid in CORE_AXES:
        attr = by_id.get(aid)
        if attr is None:
            return float("inf")
        devs.append(attribute_deviation_ratio(attr, values.get(aid)))
    return math.sqrt(sum(dev * dev for dev in devs) / len(devs)) if devs else float("inf")


def core_limit_of(profile: dict) -> float:
    """核心轴合成偏差上限；profile 没记就用兜底（旧 profile 兼容）。"""
    value = profile.get("meta", {}).get("core_limit")
    return float(value) if isinstance(value, (int, float)) and value > 0 else CORE_LIMIT_FALLBACK


def validate_profile(profile: dict) -> str | None:
    """检查 profile.json 形状；返回错误说明或 None。"""
    if not isinstance(profile, dict) or "attributes" not in profile:
        return "profile missing 'attributes'"
    for attr in profile["attributes"]:
        for key in ("attribute_id", "kind", "observed_value", "tolerance"):
            if key not in attr:
                return f"attribute {attr.get('attribute_id', '?')} missing '{key}'"
        if attr["kind"] not in {"scalar", "dict"}:
            return f"attribute {attr['attribute_id']}: unknown kind {attr['kind']!r}"
        if attr["kind"] == "dict":
            per_key = attr["tolerance"].get("per_key")
            if not isinstance(per_key, dict) or not per_key:
                return f"attribute {attr['attribute_id']}: dict without per_key tolerance"
            if set(per_key) != set(attr["observed_value"]):
                return f"attribute {attr['attribute_id']}: tolerance keys != observed keys"
    # 核心轴缺一条，复合判定会静默退化成只看分数（独立评审 P3）；宁可在校验层拦住。
    present = {attr.get("attribute_id") for attr in profile["attributes"]}
    missing_core = [aid for aid in CORE_AXES if aid not in present]
    if missing_core:
        return f"profile missing core axes: {missing_core}"
    return None


def resolve_values(profile: dict, raw: dict) -> dict:
    """把 measure() 的原始证据补成全部属性的可比值（签名类看 profile 的表）。"""
    values = {aid: raw[aid] for aid in METRIC_IDS if aid in raw}
    for attr in profile["attributes"]:
        grams = attr.get("signature_list")
        if grams:
            values[attr["attribute_id"]] = signature_density(
                raw, attr["attribute_id"], [g for g, _ in grams]
            )
    return values


def compare(profile: dict, values: dict) -> dict:
    """逐属性比对，再按复合规则判定。`score` = 等权通过占比（百分制，**放行用**）。

    `score_weighted` = 加权通过占比，**只用来量进步**：改它不用动内容的属性（标点、断句、
    虚词、句式）0.5，必须动内容的（材料、引语、时空、他的词、真问题）2.0。
    为什么不拿加权分放行：同样校准到作者 90% 通过，加权会把 1 篇通用 AI 稿和 3 篇别的作者的稿
    从「拒」变成「收」——便宜属性恰恰是区分他和别人的那部分（2026-11-09 实测）。
    判定 = `score` 过线 **且** 核心轴合成偏差不超上限（目标：作者自己的稿子约 90% 通过）。
    返回 {score, score_weighted, passed, total, rows, failed, core_failed, core_dev, core_limit, like}。
    """
    rows: list[dict] = []
    for attr in profile["attributes"]:
        aid = attr["attribute_id"]
        obs = values.get(aid)
        kind = attr["kind"]
        row = {
            "attribute_id": aid,
            "attribute_name": attr.get("attribute_name", aid),
            "kind": kind,
            "detail": "",
            "ok": False,
        }
        if obs is None:
            row["detail"] = "missing measurement"
            row["deviation"] = None
        elif kind == "scalar":
            base = attr["observed_value"]
            tol = attr["tolerance"]
            limit = max(tol["rel"] * abs(base), tol["floor"])
            row["detail"] = f"obs={float(obs):.3f} base={float(base):.3f} tol=±{limit:.3f}"
            row["ok"] = num_pass(float(obs), base, tol["rel"], tol["floor"])
            row["deviation"] = round(attribute_deviation_ratio(attr, obs), 3)
        else:
            misses: list[str] = []
            parts: list[str] = []
            for key, tol in attr["tolerance"]["per_key"].items():
                o = float(obs[key])
                b = float(attr["observed_value"][key])
                limit = max(tol["rel"] * abs(b), tol["floor"])
                parts.append(f"{key}={o:.2f}/{b:.2f}")
                if abs(o - b) > limit:
                    misses.append(f"{key}: {o:.2f} vs {b:.2f} (±{limit:.2f})")
            allowed = allowed_misses(len(attr["tolerance"]["per_key"]))
            row["ok"] = len(misses) <= allowed
            row["detail"] = (
                " ".join(parts)
                if row["ok"]
                else f"{len(misses)} subkey(s) out: " + "; ".join(misses)
            )
            row["deviation"] = round(attribute_deviation_ratio(attr, obs), 3)
        rows.append(row)
    passed = sum(1 for r in rows if r["ok"])
    # 分数先四舍五入到 0.1 再判：报告里显示的数和判定用的数必须是同一个，
    # 否则 66.66 显示成 66.7、切点 66.7，报告说"过"而判定说"不过"。
    score = round(100.0 * passed / len(rows), 1) if rows else 0.0
    # 加权分只用来量「进步」：便宜属性（标点、断句）减重，要动内容的属性加重。
    # 不拿它放行 —— 实测代价：同样校准到作者 90% 通过时，加权会把 1 篇通用 AI 稿和
    # 3 篇别的作者的稿从「拒」变成「收」（便宜属性恰恰是区分他和别人的那部分）。
    weight_table = load_weights()["table"]
    total_weight = sum(weight_table.get(r["attribute_id"], 1.0) for r in rows) or 1.0
    got_weight = sum(
        weight_table.get(r["attribute_id"], 1.0) for r in rows if r["ok"]
    )
    score_weighted = round(100.0 * got_weight / total_weight, 1) if rows else 0.0
    failed = [r["attribute_id"] for r in rows if not r["ok"]]
    core_failed = [aid for aid in CORE_AXES if aid in failed]
    core_dev_raw = core_deviation(profile, values)
    core_dev = round(core_dev_raw, 3)
    core_limit = round(core_limit_of(profile), 3)
    # L11-04 单篇偏离度：全属性归一化偏差的均值（核心轴 RMS 只看四条；这条看整体）。
    devs = [row["deviation"] for row in rows if row["deviation"] is not None]
    mean_dev = round(sum(devs) / len(devs), 3) if devs else None
    return {
        "score": score,
        "score_weighted": score_weighted,
        "passed": passed,
        "total": len(rows),
        "rows": rows,
        "failed": failed,
        "core_failed": core_failed,
        "core_dev": None if core_dev_raw == float("inf") else core_dev,
        "core_limit": core_limit,
        "mean_dev": mean_dev,
        "like": (score >= pass_score_of(profile)) and core_dev <= core_limit,
    }


def pass_score_of(profile: dict) -> float:
    """过线：profile.meta.pass_score（等权分校准出来的）。加权分不放行，见 compare 的说明。"""
    value = profile.get("meta", {}).get("pass_score")
    return float(value) if isinstance(value, (int, float)) and value > 0 else FALLBACK_PASS_SCORE


def pass_score(profile: dict) -> float:
    return pass_score_of(profile)
