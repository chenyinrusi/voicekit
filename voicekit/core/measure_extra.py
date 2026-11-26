"""FIX-343：注册表「算」档里原先没有打分口、能按篇算的属性。

为什么单独一个文件：`voice_lib.measure()` 已经接近模块行数上限（设计原则 13：单模块 1000 行），
这批属性与原有 24 条互不依赖，拆出来按职责放。口径与逐条说明见
`dev/fixes/FIX-343-voice-negative-corpus-and-attribute-ports/DESIGN.md` §3.2，
属性 ID 对应 `docs/design/DD-VOICE-001-attribute-registry.md`。

调用方只传原始证据与几个纯函数助手（不 import voice_lib，避免循环依赖）。
"""

from __future__ import annotations

import re

#: L1-15 评价词（评好坏，不是情绪：情绪词在 L1-14）。
APPRAISAL = {
    "praise": ("值得", "不错", "挺好", "很好", "真好", "满意", "难得"),
    "fault": ("糟糕", "遗憾", "可惜", "差劲", "不怎么样", "一般般"),
}

#: L1-17 网络流行语（2020 后）。语料零命中，判据是"不该出现"。
SLANG = (
    "yyds", "绝绝子", "破防", "栓q", "芭比q", "内卷", "躺平", "摆烂",
    "无语子", "好家伙", "emo了",
)

#: L2-12 名词化后缀字。不用分词器：按"后缀字前一格是汉字"计数。
#: shortcut: 程度/态度/变化/温度/制度 这类已词汇化的词也会被计入；等有分词器或名词化词表时换成词级判定。
NOMINAL_SUFFIXES = ("主义", "性", "化", "度")

#: L6-08 夸张 vs 弱化。
HYPERBOLE = ("极度", "极其", "非常", "特别", "超级", "无比", "空前", "最")
DOWNPLAY = ("罢了", "而已", "不过", "只是", "也就")

#: L7-10 元话语（十类里取四类高频组；其余六类待 P1）。
METADISC = {
    "framing": ("换句话说", "也就是说", "简单说", "具体来说", "说到底", "总的来说"),
    "selfref": ("我认为", "我觉得", "我想说", "在我看来"),
    "comment": ("不得不说", "说实话", "老实说"),
    "boost": ("确实", "的确", "显然"),
}

#: L8 社会词 / 洞察词 / 差异词 / 否定词。
SOCIAL = ("我们", "朋友", "家人", "聊天", "同事", "聚会")
INSIGHT = ("明白", "意识到", "原来", "才发现", "懂了")
DIFFERENCE = ("应该", "本来", "要是", "按说")
NEGATION = ("不", "没", "别", "未")

#: L8-05 时空聚焦。
TIME_WORDS = {
    "past": ("过去", "以前", "当年", "小时候", "那时"),
    "present": ("现在", "如今", "当下", "此刻", "这时候"),
    "future": ("以后", "将来", "未来", "往后"),
    "here": ("这里", "这儿", "眼前", "身边"),
    "far": ("远方", "远处", "他乡", "外面"),
}

#: L10-02 回避标记（emoji 在 L1-06、感叹号在 L1-01，这里只收脏话与叠标点）。
#: 不收裸「操」：「操作/操场/操心」会被误判（2026-11-04 review P2）。
SWEAR = ("他妈", "我操", "操你", "操他", "卧槽", "牛逼", "傻逼")
STACKED_PUNCT = ("！！", "!!", "？？", "??")

#: 段首问句判据（L9-08 问题驱动）。
QUESTION_HEADS = ("为什么", "怎么", "是不是", "有没有", "难道", "凭什么")

#: L4-09 指代词。只收指代用法：「该」「其」常出现在「应该/其他」里，不收（review P3）。
ANAPHORA = ("这些", "这个", "这种", "这样", "这里", "那些", "那个", "那种", "那样", "那里", "它们", "它", "此")

#: L8-06 自我聚焦：我（不含「我们」）+ 自己，避免把「我们」算成自我聚焦（review P3）。
SELF_PRONOUN_RE = re.compile(r"我(?!们)")

#: L2-08 单句成段：一个段只有一句，且这句不超过这么多汉字（长段只带逗号不算成段，review P2）。
SINGLE_PARA_MAX_CJK = 40


def extra_values(
    body: str,
    chars: int,
    sentences: list[str],
    paragraphs: list[str],
    punct_counts: dict,
    *,
    cjk_only,
    cjk_count,
    per_k,
    safe_ratio,
    count_any,
    count_nonoverlap,
    ngram_counts,
    split_sentences,
) -> dict:
    """算 FIX-343 补的 18 条属性的原始值（键名与 `voice_lib.measure()` 一致）。"""
    nominal = sum(
        len(re.findall(r"[\u4e00-\u9fff]" + suffix, body)) for suffix in NOMINAL_SUFFIXES
    )
    quoted = re.findall(r"“([^”]*)”", body)
    quoted_chars = sum(cjk_count(q) for q in quoted)
    finals: dict[str, int] = {}
    for sentence in sentences:
        cjk = cjk_only(sentence)
        if len(cjk) >= 2:
            finals[cjk[-2:]] = finals.get(cjk[-2:], 0) + 1
    # 单句成段：段内只有一句，且这一句是短的（长段只用逗号串起来不算成段）。
    single_sent_paras = 0
    for paragraph in paragraphs:
        if len(split_sentences(paragraph)) == 1 and cjk_count(paragraph) <= SINGLE_PARA_MAX_CJK:
            single_sent_paras += 1
    short_sents = sum(1 for n in (cjk_count(s) for s in sentences) if n <= 8)
    echo = _echo_ratio(paragraphs, ngram_counts)
    repeated = 0
    for prev, nxt in zip(sentences, sentences[1:], strict=False):
        tail, head = cjk_only(prev)[-2:], cjk_only(nxt)[:2]
        if len(tail) == 2 and tail == head:
            repeated += 1
    question_paras = sum(
        1
        for p in paragraphs
        if p.endswith("？") or p.endswith("?") or p.startswith(QUESTION_HEADS)
    )
    total_sent = len(sentences) or 1
    return {
        "L1-15": {
            "praise_per_k": round(per_k(count_nonoverlap(body, APPRAISAL["praise"]), chars), 2),
            "fault_per_k": round(per_k(count_nonoverlap(body, APPRAISAL["fault"]), chars), 2),
        },
        "L1-17": round(per_k(count_nonoverlap(body.lower(), SLANG), chars), 2),
        "L2-08": {
            "short_share": round(safe_ratio(short_sents, total_sent), 4),
            "single_para_share": round(safe_ratio(single_sent_paras, len(paragraphs)), 4),
        },
        "L2-12": round(per_k(nominal, chars), 2),
        "L4-09": {
            "anaphora_per_k": round(per_k(count_nonoverlap(body, ANAPHORA), chars), 2),
            "echo": round(echo, 4),
        },
        "L5-06": {
            "quotes_per_k": round(per_k(len(quoted), chars), 2),
            "quoted_share": round(safe_ratio(quoted_chars, chars), 4),
        },
        "L6-08": {
            "hyper_per_k": round(per_k(count_nonoverlap(body, HYPERBOLE), chars), 2),
            "downplay_per_k": round(per_k(count_nonoverlap(body, DOWNPLAY), chars), 2),
        },
        "L6-10": round(per_k(repeated, chars), 2),
        "L7-10": {
            f"metadisc_{name}": round(per_k(count_nonoverlap(body, words), chars), 2)
            for name, words in METADISC.items()
        },
        "L8-01": round(per_k(count_nonoverlap(body, SOCIAL), chars), 2),
        "L8-02": round(per_k(count_nonoverlap(body, INSIGHT), chars), 2),
        "L8-03": round(per_k(count_nonoverlap(body, DIFFERENCE), chars), 2),
        "L8-04": round(per_k(count_any(body, NEGATION), chars), 2),
        "L8-05": {
            f"time_{name}": round(per_k(count_nonoverlap(body, words), chars), 2)
            for name, words in TIME_WORDS.items()
        },
        "L8-06": round(
            per_k(len(SELF_PRONOUN_RE.findall(body)) + body.count("自己"), chars), 2
        ),
        "L9-08": {
            "question_para_share": round(safe_ratio(question_paras, len(paragraphs)), 4),
            "question_per_k": round(
                per_k(sum(punct_counts.get(m, 0) for m in ("？", "?")), chars), 2
            ),
        },
        "L10-02": {
            "swear_per_k": round(per_k(count_any(body, SWEAR), chars), 2),
            "stacked_punct_per_k": round(per_k(count_any(body, STACKED_PUNCT), chars), 2),
        },
        # 签名类原始计数（L2-04 句尾模式走 profile 里的跨篇榜单换算）
        "L2-04_grams": finals,
    }


def _echo_ratio(paragraphs: list[str], ngram_counts) -> float:
    """首末段 2 字词复现率：交集 / 首段词种类数。

    单段没有"首尾"可比，记 0（2026-11-04 review P2：单段时首段=末段，恒得 1.0）。
    """
    if len(paragraphs) < 2:
        return 0.0
    first = set(ngram_counts(paragraphs[0], 2))
    last = set(ngram_counts(paragraphs[-1], 2))
    return len(first & last) / len(first) if first else 0.0
