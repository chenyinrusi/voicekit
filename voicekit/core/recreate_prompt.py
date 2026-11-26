"""生成提示渲染（FIX-358 从 recreate_eval 拆出：验收模块只管跑与证据，提示文本在这里）。

把 profile 数字翻成指令、挑风格样例（短样例 / 全文）、拼生成与改写提示。不碰模型、不碰判定。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from . import exemplar_judge as ej  # noqa: E402

#: 风格样例两种形式（FIX-358）：short = 2×160 字（FIX-356 口径）；full = 4 篇整文。
ANCHOR_MODES = ("short", "full")
SHORT_ANCHOR_COUNT, SHORT_ANCHOR_CHARS = 2, 160
FULL_ANCHOR_COUNT = 4
#: 抄自己的语料（全 corpus）到这个长度就拦。
COPY_VIOLATION_LCS = 40


def _g(obj, key, default=0.0) -> float:
    value = obj.get(key, default) if isinstance(obj, dict) else default
    return float(value) if isinstance(value, (int, float)) else float(default)


def _n(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def render_style(profile: dict) -> str:
    """把 profile 的数字翻成可执行指令（第一轮只给数字表，生成稿看不出作者，见 DESIGN §3）。

    不含原文、不含作者名；不引用 `generation_guidance`（它对所有作者一样，是通用话术）。
    """
    by_id = {
        str(attr.get("attribute_id")): attr
        for attr in (profile.get("attributes") or [])
        if isinstance(attr, dict) and isinstance(attr.get("observed_value"), (dict, int, float))
    }
    lines: list[str] = []

    punct = by_id.get("L1-01", {}).get("observed_value")
    if isinstance(punct, dict):
        names = {
            "、": "顿号", "；": "分号", "！": "感叹号", "…": "省略号", "—": "破折号",
            "「": "直角引号", "『": "直角引号", "（": "圆括号", "【": "方头括号", "‘": "单引号",
        }
        zeros = [label for name, label in names.items() if _g(punct, name) == 0]
        tail = f"不用：{'、'.join(dict.fromkeys(zeros))}。" if zeros else "其余标点照常。"
        lines.append(
            "标点谱：逗号 {}、句号 {}、问号 {}、引号 {}（每千字）；{}".format(
                _n(_g(punct, "，")), _n(_g(punct, "。")), _n(_g(punct, "？")),
                _n(_g(punct, "“")), tail,
            )
        )

    sent = by_id.get("L2-01", {}).get("observed_value")
    if isinstance(sent, dict):
        lines.append(
            "句长与分句密度：平均 {} 字、中位 {} 字；每句约 {} 个小句；≤10 字短句占 {}%。".format(
                _n(_g(sent, "mean")), _n(_g(sent, "median")),
                _n(_g(sent, "clauses_per_sentence")), _n(_g(sent, "share_le10") * 100),
            )
        )

    conn = by_id.get("L2-02", {}).get("observed_value")
    if isinstance(conn, dict):
        parts = "、".join(
            f"{label} {_n(_g(conn, label))}"
            for label in ("并列", "从属", "因果", "条件", "递进")
            if _g(conn, label) > 0
        )
        lines.append(f"连接结构配比：每千字 {_n(_g(conn, 'total_per_k'))} 个（{parts or '无'}）。")

    func = by_id.get("L1-09", {}).get("observed_value")
    if isinstance(func, dict):
        lines.append(
            "虚词向量与分析性：每千字 的 {}、是 {}、了 {}、不 {}、在 {}。".format(
                *(_n(_g(func, key)) for key in ("de", "shi", "le", "bu", "zai"))
            )
        )

    self_focus = by_id.get("L8-06", {}).get("observed_value")
    if isinstance(self_focus, (int, float)):
        lines.append(f"自我聚焦：我/自己 {_n(float(self_focus))}/千字（第一人称浓度）。")

    redup = by_id.get("L3-02", {}).get("observed_value")
    if isinstance(redup, (int, float)):
        lines.append(f"叠词：每千字 {_n(float(redup))} 个。")

    trans = by_id.get("L4-05", {}).get("observed_value")
    if isinstance(trans, dict):
        used = "、".join(
            f"{key} {_n(float(value))}"
            for key, value in trans.items()
            if isinstance(value, (int, float)) and value > 0
        )
        zero = "、".join(
            key for key, value in trans.items() if isinstance(value, (int, float)) and value == 0
        )
        line = f"过渡标记：每千字 {used or '无'}。"
        if zero:
            line += f" 不用：{zero}。"
        lines.append(line)

    particles = by_id.get("L7-09", {}).get("observed_value")
    if isinstance(particles, dict):
        lines.append(
            "语气词与插入语：语气词 {}/千字、插入语 {}/千字。".format(
                _n(_g(particles, "particles_per_k")), _n(_g(particles, "insertions_per_k"))
            )
        )

    emotion = by_id.get("L1-14", {}).get("observed_value")
    if isinstance(emotion, dict):
        lines.append(
            "情绪词：正面 {}/千字、负面 {}/千字。".format(
                _n(_g(emotion, "pos_per_k")), _n(_g(emotion, "neg_per_k"))
            )
        )

    classical = by_id.get("L1-12", {}).get("observed_value")
    if isinstance(classical, (int, float)):
        lines.append(f"文白夹杂：文言词 {_n(float(classical))}/千字。")

    sentence_kind = by_id.get("L2-05", {}).get("observed_value")
    if isinstance(sentence_kind, dict):
        lines.append(
            "句类配比：问句 {}%、感叹句 {}%。".format(
                _n(_g(sentence_kind, "question") * 100), _n(_g(sentence_kind, "exclam") * 100)
            )
        )

    exaggeration = by_id.get("L6-08", {}).get("observed_value")
    if isinstance(exaggeration, dict):
        lines.append(
            "夸张与弱化：极度类 {}/千字、弱化类（罢了/而已/只是）{}/千字。".format(
                _n(_g(exaggeration, "hyper_per_k")), _n(_g(exaggeration, "downplay_per_k"))
            )
        )

    if not lines:
        raise ValueError("profile has no usable attributes")
    lines.append("写法：每一段都按上面的数字校准——句子长度、标点密度、人称、过渡词都要对得上。")
    return "\n".join(f"- {line}" for line in lines)


def _anchor_paths(
    corpus: Path,
    exemplar_names: set[str],
    *,
    mode: str = "short",
    count: int | None = None,
    exclude: set[str] | tuple[str, ...] = (),
) -> list[Path]:
    """挑哪些语料当风格样例：**不取判官用的样例，也不取留出稿**（防泄漏）。"""
    if mode not in ANCHOR_MODES:
        raise ValueError(f"unknown anchor mode: {mode!r} (expect {ANCHOR_MODES})")
    if count is None:
        count = SHORT_ANCHOR_COUNT if mode == "short" else FULL_ANCHOR_COUNT
    if count < 1:
        raise ValueError("anchor count must be >= 1")
    skip = set(exemplar_names) | set(exclude)
    picked: list[Path] = []
    for path in sorted(corpus.glob("*.md")):
        if path.name in skip:
            continue
        body = ej.clean_body(path.read_text(encoding="utf-8"))
        if cjk_count(body) < 300:
            continue
        picked.append(path)
        if len(picked) >= count:
            break
    return picked


def _format_anchors(paths: list[Path], *, mode: str, max_chars: int | None = None) -> str:
    if max_chars is None:
        max_chars = SHORT_ANCHOR_CHARS if mode == "short" else 0
    blocks: list[str] = []
    for path in paths:
        body = ej.clean_body(path.read_text(encoding="utf-8"))
        # 标题/章节行等于告诉模型作者是谁（r1 P2-4），只留正文。
        body = "\n".join(
            line for line in body.split("\n") if not line.lstrip().startswith(("#", "="))
        ).strip()
        if max_chars > 0:
            cut = body[:max_chars]
            stop = cut.rfind("。")
            if stop >= 80:
                cut = cut[: stop + 1]
            body = cut
        label = f"### 样例 {len(blocks) + 1}" + ("（全文）" if mode == "full" and not max_chars else "")
        blocks.append(f"{label}\n{body}")
    return "\n\n".join(blocks)


def render_anchors(
    corpus: Path,
    exemplar_names: set[str],
    *,
    mode: str = "short",
    count: int | None = None,
    max_chars: int | None = None,
    exclude: set[str] | tuple[str, ...] = (),
) -> str:
    """挑语料当风格样例：**不取判官用的那 10 篇样例，也不取留出稿**，减少照抄/泄漏。

    mode="short"（FIX-356 口径）：2 段各 160 字，供体会笔调；抄超过 40 字会被抄袭检查抓住。
    mode="full"（FIX-358）：4 篇整文，给足私人标记（口头禅、转折、收尾套路），
    代价是提示更长、更可能被照抄——抄句检查两种模式都生效。
    """
    return _format_anchors(
        _anchor_paths(corpus, exemplar_names, mode=mode, count=count, exclude=exclude),
        mode=mode,
        max_chars=max_chars,
    )



def _common(brief: dict | str, must_include: list[str] | None = None) -> str:
    if isinstance(brief, dict):
        text = str(brief["brief"])
        tokens = [str(token) for token in brief.get("must_include") or []]
    else:
        text = str(brief)
        tokens = [str(token) for token in (must_include or [])]
    required = f"- 下面这些词要原样出现：{'、'.join(tokens)}\n" if tokens else ""
    return (
        "## 要求\n\n"
        "- 中文随笔，第一人称，600–900 汉字，分 4–8 段。\n"
        "- 先想清楚「说什么」（照下面的骨架），再决定「怎么说」。\n"
        "- 不写标题，不署名，不引用任何现有文章的原句。\n"
        f"{required}\n"
        "## 内容骨架\n\n"
        f"{text}\n\n只输出正文。"
    )


def build_generation_prompt(
    brief: dict | str,
    style: str | None,
    must_include: list[str] | None = None,
    anchors: str | None = None,
    markers: str | None = None,
) -> str:
    """style=None 是对照组：同样的骨架和长度，只把风格信息拿掉。"""
    if style is None:
        head = "你在写一篇中文随笔。语言平实、清楚，不要刻意模仿任何作者的风格。\n\n"
    else:
        head = (
            "你在替一位中文随笔作者代笔。下面是从他/她的一批语料里量出来的文风数字，"
            "按这些数字决定「怎么说」。\n\n## 文风数字\n\n"
            f"{style}\n\n"
        )
        if markers:
            head += f"## 这位作者的独有笔法（读样例后总结，照它写）\n\n{markers}\n\n"
        if anchors:
            head += f"## 风格样例（同一位作者，体会笔调，不许照抄）\n\n{anchors}\n\n"
    return head + _common(brief, must_include)


def build_revision_prompt(draft: str, anchors: str) -> str:
    """第二遍：拿初稿再对着全文样例改一次（内容不动，只改笔性）。"""
    return (
        "下面是同一位作者的几篇原文（只用来体会笔法），后面是你按骨架写的初稿。\n\n"
        f"## 原文样例\n\n{anchors}\n\n"
        f"## 初稿\n\n{draft}\n\n"
        "## 任务\n\n"
        "把初稿改成同一个人写的：句长、分句、转折词、口头禅、收尾方式都要像原文；"
        "骨架内容、事实和必现词保持不变；不要标题，不要署名；"
        f"不许照抄原文连续 {COPY_VIOLATION_LCS} 字以上。只输出改好的正文。"
    )


def cjk_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
