"""改稿提示词：纪律、底色、诊断项、三种模式的写法，全部在这里拼。

从 `rewrite.py` 拆出来的（模块行数棘轮）。判断逻辑不在这里，这里只负责「告诉模型怎么做」。
`rewrite.py` 会把 `build_messages` 等名字再导出，所以对外的用法没变。
"""

from __future__ import annotations

import re

from .voice_text import (
    corpus_path,
    profile_path,
    split_paragraphs,
    uses_blank_lines,
    voice_core_path,
)


def load_discipline() -> str:
    """voice-core.md 的「改稿纪律」一节，原文取（不复制到代码里）。"""
    path = voice_core_path()
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^## 改稿纪律.*?(?=^## )", text, re.S | re.M)
    if not match:
        raise SystemExit(f"[FAIL] {path} 里找不到「改稿纪律」一节")
    return match.group(0).strip()


def load_core_hints() -> list[str]:
    """6 条底色里的「改稿时看」+ 标题，给改稿人当镜头。"""
    text = voice_core_path().read_text(encoding="utf-8")
    hints = []
    for block in re.findall(r"^## (\d+\..*?)(?=^## )", text, re.S | re.M):
        title = block.splitlines()[0].strip()
        look = re.search(r"- \*\*改稿时看\*\*：(.+)", block)
        if look:
            hints.append(f"{title} → {look.group(1).strip()}")
    return hints


def failure_lines(diag: dict, limit: int = 8) -> list[str]:
    """把出界的属性翻成一句句能听懂的话（最多 limit 条）。"""
    rows = [row for row in diag["style"]["rows"] if not row["ok"]]
    rows.sort(key=lambda row: row.get("deviation") or 0, reverse=True)
    lines = []
    for row in rows[:limit]:
        detail = row.get("detail", "")
        pairs = re.findall(r"([^\s:;]+): ([\d.]+) vs ([\d.]+)", detail)
        if pairs:
            shown = "；".join(
                f"{key} {mine}（作者 {base}，{'偏多' if float(mine) > float(base) else '偏少'}）"
                for key, mine, base in pairs[:3]
            )
            lines.append(f"- {row['attribute_name']}：{shown}")
        else:
            lines.append(f"- {row['attribute_name']}（{detail[:60]}）")
    return lines


def load_protected_rules() -> str:
    """只取「一个字都不许动」那五条 + 段落要求。

    改别人起草的稿（voice 模式）时不能把整节纪律都塞进去：那节里
    「整篇重写会露馅」「能不动就不动」是给「改他自己的成稿」写的，
    照抄会让模型不敢动笔（实测：改 6–11%，守门过不了）。
    """
    discipline = load_discipline()
    block = re.search(r"\*\*一个字都不许动：\*\*(.+?)\*\*只改这些：", discipline, re.S)
    rules = block.group(1).strip() if block else ""
    if not rules:
        raise SystemExit(
            f"[FAIL] {voice_core_path()} 里「一个字都不许动」那节没解析出来（是不是改了标题措辞？）"
        )
    return (
        "**一个字都不许动：**\n\n"
        f"{rules}\n\n"
        "**账本：** 原稿里的数字、单位、书名、引语一个都不许删；"
        "「一定/必须/肯定/绝对」只许减不许加。\n\n"
        "**结构：** 段落顺序照原稿；长段可以拆短；同一件事的短段可以并，"
        "也可以删掉纯议论的空段（段数比例只报数、不当门）；"
        "不加标题、小标题、分点清单、结尾总结。"
    )


def style_digest() -> str:
    """从 profile.json 读几个能当靶子的数字（不抄 generation_guidance：FIX-367 说它有的维方向反）。"""
    import json

    data = json.loads(profile_path().read_text(encoding="utf-8"))
    by_id = {attr["attribute_id"]: attr for attr in data["attributes"]}
    sent = by_id["L2-01"]["observed_value"]
    classic = by_id["L1-12"]["observed_value"]
    idiom = by_id["L1-13"]["observed_value"]
    return (
        f"- 句子平均 {sent['mean']:.0f} 字（中位 {sent['median']:.0f}），"
        f"每句约 {sent['clauses_per_sentence']:.1f} 个分句，长句靠逗号串\n"
        f"- 文白夹杂 {classic:.2f}；四字格/成语 {idiom:.2f}（基本不用）\n"
        "- 判断留余地（怕是 / 想来 / 毕竟 / 倒也）；少喊口号、少排比、少分点清单；"
        "结尾多半补一句道理收，不总结、不升华"
    )


def voice_rules() -> str:
    """改**别人**起草的稿：事实照抄，用词随便换。

    为什么不能沿用 `load_protected_rules()`：那五条里「他反复用的原词别规范化」是给**他自己的稿**
    写的；套到别人的稿上，模型会把「原词别改」读成「一个词都别动」。2026-11-10 拿鲁迅
    《阿长与山海经》试，只改了 13%、8 字片段保留 92%——等于只做了校对。
    """
    return (
        "**这次改的是别人的文字，不是他的原稿：**\n\n"
        "- **事实照抄**：数字、金额、日期、人名/地名、书名、别人的话（引语）一个都不许少或改。\n"
        "- **人名地名照原样**：不许换成「一个朋友」「某人」这类模糊说法。\n"
        "- **其余用词、语序、句长，全部换成他的**：这份稿的句子是别人写的，按他的说法重写。\n"
        "- 他的腔：长句靠逗号推进；判断留余地（怕是 / 想来 / 毕竟 / 倒也）；自嘲先于讲理；\n"
        "  抽象判断后面挂一件能看见的小东西；结尾落回动作或留半句，不总结不升华。\n"
        "- **不许只加标点、不许只做校对**：把别人的句子原样搬过来不算改完。\n"
        "- **结构**：段落顺序照原稿；长段可以拆短；**同一件事的短段可以并**，也可以删掉纯议论的空段\n"
        "  （段数比例只报数、不当门：作者 2026-11-11 亲手改稿就删了一整段空议论，段数 11→8）。\n"
        "- 不加标题、小标题、分点清单、结尾总结。\n"
        "- 盲读评审 2026-11-10 点出来的三个病，逐条避开：\n"
        "  - **名词化 → 动作**：「对她产生过空前的敬意」这种不许留，要写成脸红、推辞、\n"
        "    手上多做了什么事这类能看见的动作。\n"
        "  - **程度词不许叠**：「总而言之」「到极点」「非常」不许叠着上；判断用语气词轻轻收\n"
        "    （「倒也是不错」），不靠加重副词。\n"
        "  - **「我」先出场**：写自己的事，主语先给「我」再出动作；不许把人从主语里抽掉，\n"
        "    变成旁观者替人叙述。\n"
        "  - 一句话里不许补白式的第二个「说」；要加码就用「不但……似乎连……」。\n"
        "  - **抽象词：把「机制怎么运作」改写成「人会怎么做」**（作者 2026-11-10 定的分寸，\n"
        "    比「换手感词」高一层）：他写抽象词时 77% 的句子都有人在做一件事（185 句实测）。\n"
        "    - 解释层：用人的行为代替制度语言——「人心不认这个」「人心得一回一回攒」，\n"
        "      不是「信任需要一再赢得」。\n"
        "    - 收束层：下判断时可以抽象，但要短、要留余地（「这些焦虑都不可避免」）。\n"
        "    - 具体物是给「人能碰的东西」用：选票、白宫、账单、冰箱里的菜。\n"
        "    - **反例**：换市井/官场词（位子、交椅、饭碗）= 矫枉过正，比原文更假；\n"
        "      换另一个抽象词（权力格局）= 等于没换。\n"
        "    - 自检：这句话里**有没有一个人在做一件事**？没有就不像他。\n"
    )


def modernize_rules() -> str:
    """翻新老稿的规矩：事实不动，其余都动。跟改别人稿的「一个字都不许动」不是一回事。

    为什么单独写：2026-11-10 实测，沿用 voice 模式那套五条纪律时，模型只补标点，
    去掉标点后 93% 的 2 字片段原样未动——「原词别改」被读成了「什么都别改」。
    """
    return (
        "**这次是换腔，不是校对：**\n\n"
        "- **事实不许动**：数字、金额、日期、地名、店名、英文词、别人的话，一个都不许少或改。\n"
        "- **其余都可以动**：语序、句长、用词、重复、语气词、网络语、颜文字，全部重写。\n"
        "- 现在的腔：长句靠逗号推进；判断留余地（怕是 / 想来 / 毕竟 / 倒也）；"
        "自嘲先于讲理；抽象判断后面挂一件能看见的小东西；结尾落回动作或留半句，不总结不升华。\n"
        "- **语气、感叹、加重，一个都不许删**（作者 2026-11-10 原话：「哟西 我就是生生一个抹茶控啊」\n"
        "  这边把我的情绪也抹除了）。这是他的情绪，不是过时的标记：\n"
        "  - 「哟西」「嘿」「哈哈」「好囧啊」「呃」「嗯」「咳」这类感叹和自语，原样保留。\n"
        "  - 「我就是……」「生生一个……」「真是」这类加重说法，保留加重，不许削平成「一个……」。\n"
        "  - 句尾「啊/呢/吧/呀/么」尽量留着，不是错字。\n"
        "  - 只许去掉纯符号的颜文字（^_^、=_=），换成标点或直接不留；别顺手把整句情绪一起删。\n"
        "  - **吐槽和狠话不许删**（作者 2026-11-10 第二次点名）：\n"
        "    「坑爹死了」「黑，真黑」「郁闷」「烦躁」「真黑啊」这类抱怨是情绪的高点，\n"
        "    不许当成冗余删掉、也不许改写成平淡的陈述（「坑爹死了」不许变成直接说下一句）。\n"
        "  - 参考密度：他现在的稿子每千字约 1.6 个语气词、1.1 个插入语；删到 0 就是抹平了情绪。\n"
        "- **判断的方向和谓语不许改**：这是他的品味，不是语言问题。留余地只许把话说轻（一定→大概、\n"
        "  肯定→怕是），**不许改褒贬方向**，也不许升级强度。实例（2026-11-10 真踩过）：\n"
        "  「就算让心能够尝到苦涩也是幸福的吧」**不许**改成「也认了」——那就从「算幸福」变成「认命」；\n"
        "  「会改变过去的看法」**不许**改成「把过去的看法改掉/推翻」；\n"
        "  「归为哲学问题」**不许**改成「归到哲学里去」这种换谓语的写法。\n"
        "- **不许只加标点**：原稿没写句号，你当然要补；但补完必须把句子也重写过。\n"
        "- **不许把念头缝平**（作者 2026-11-10 原话：最像他的是「离散的念头」「碎碎念般的低语」）：\n"
        "  - 允许话题跳、允许说着说着岔开、允许突然想起另一件事，不要每段都收成一个论点。\n"
        "  - 插话/自言自语保留到他现在的密度：每千字约 1 个插入语（其实 / 倒是 / 说来也是好笑 / 对了 / 说起来）、\n"
        "    约 2 个语气词；不要抽干净。\n"
        "  - 句子长短要不齐（句长方差约 190）：有十几字的短句，也有五六十字的长句，别都匀成三十来字。\n"
        "  - 段长忽长忽短（段长方差约 120）：可以一句一段，也可以一大段；别把每段都修成一样长。\n"
    )


def modern_voice_examples(limit: int = 3, chars: int = 220) -> list[str]:
    """他现在的腔：从他近年的成稿里各取开头一小段当锚（翻新老稿时用）。

    只取开头，是因为开头最能看出「一开口怎么说话」；不取全文，控制提示词体积。
    """
    corpus = corpus_path()
    if corpus is None:
        return []
    examples = []
    for path in sorted(corpus.glob("*.md")):
        doc = path.name.split("__")[0]
        if doc in {"034", "037"} or doc == "064":
            continue
        lines = [x.strip() for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]
        if not lines:
            continue
        body = "".join(lines[1:])[:chars]
        if len(body) < 80:
            continue
        examples.append(f"（{doc}）{body}")
        if len(examples) >= limit:
            break
    return examples


def build_messages(source: str, diag: dict, mode: str = "voice", push: bool = False,
                   examples: int = 3) -> list[dict]:
    hints = load_core_hints()
    failures = failure_lines(diag)
    sep = "空一行" if uses_blank_lines(source) else "换行（一段一行，跟原稿一样，不要加空行）"
    if mode == "polish":
        discipline = load_discipline()
        task = (
            "这是他自己的稿子。只修拗口、重复、读着不像他说话的句子，能不动就不动；"
            "改动大约三成以内，其余原样保留。"
        )
    elif mode == "modernize":
        discipline = modernize_rules()
        task = (
            "这是他本人**十五年前**写的稿子。事情、数字、名字、顺序一件都不许动，"
            "但**文字要换成他现在的腔**：长句靠逗号推进，少用感叹号；"
            "当年那些「好囧啊」「哈哈哈」「嘿嘿」「哟西」「坑爹」和颜文字要收掉大半；"
            "重复啰嗦的句子合并或删掉，判断改成留余地的说法；整句整句地重写。"
        )
    else:
        # 改别人起草的稿时只给「一个字都不许动」那五条：
        # 整节纪律里「整篇重写会露馅」「能不动就不动」是给「改他自己的成稿」写的，
        # 照抄会吓得模型不敢动笔（实测 6–11%）。
        discipline = voice_rules()
        task = (
            "下面这份稿子不是他写的（别人或机器起草的）。请把语言重写成他的腔调："
            "按他的句子长度和节奏重组句子，该口语的地方口语，去掉通稿腔、总结腔、口号；"
            "整句整句地改，改完读起来要像他写的。"
        )
    if push:
        task += (
            "\n这份稿子离他的腔差得更多（本地诊断只到 "
            f"{diag['style']['score']} 分，他自己的稿子通常 75 以上）。"
            "\n要求：每三句里至少有一句换一种说法（换语序、拆句或并句、换虚词），"
            "不许只删一两个字。"
        )
    long_paras = [para for para in split_paragraphs(source) if len("".join(para.split())) > 250]
    split_task = (
        f"**拆段是这次任务的一部分**：原稿有 {len(long_paras)} 个超过 250 字的长段，"
        f"每一段都要拆成 2 段以上（在句号处断开就行），否则这版不算完成；不许并段。"
        if long_paras
        else "长段可以拆短；同一件事的短段可以并；纯议论的空段可以删（段数比例只报数、不当门）。"
    )
    system = (
        "你在替一位中文随笔作者改稿。你不是作者，只做语言层的修改。\n\n"
        f"任务：{task}\n\n"
        f"{discipline}\n\n"
        "四条硬要求：①原稿里的数字、单位、书名、引语一个都不许少；"
        "②原稿里「一定/必须」这类判断，保留原来的数量，不要主动删（他成稿保留 2 处，删光就不是他）；"
        "③只有原稿里真有一个他想不通的问题时才保留困惑，不要自己把稿子改成「他每篇都在困惑」；"
        "④**指控类限定语一个字都不许删**——原稿写「被指为」「涉嫌」「据称」「疑似」的地方，"
        "改完还得是限定说法，不许改成「是」「就是」；稿子里说的人是真的，"
        "删掉限定语等于替他加罪。\n\n"
        f"输出格式：只输出改好的正文，段落之间{sep}；不要解释、不要代码块、"
        f"不要 diff，不要列出改了什么。段落顺序必须和原稿一致；{split_task}"
        "原稿第一行如果是标题，就保留这一行（标题的字可以按他的腔改，但整行不许删）；"
        "不要自己新加标题，也不要加 Markdown 的 #。"

    )
    failures_intro = (
        "这篇本地诊断出界的项（这一版**必须**靠过去，是任务不是建议）："
        if push
        else "这篇本地诊断出界的项（拿他的 59 篇语料当基线；方向靠一靠就行，不要为凑数字硬改）："
    )
    user_lines = [
        "他的句子长什么样（实测数字，改句时对着走）：",
        style_digest(),
        "",
        "他的读感底色（作者勾选过的 6 条，改句时当镜头）：",
        *[f"{index + 1}. {hint}" for index, hint in enumerate(hints)],
    ]
    if mode in ("modernize", "voice"):
        user_lines += [
            "",
            "他现在的腔长这样（近年成稿的开头，改完后要跟这个口气一致）：",
            *[f"- {item}" for item in modern_voice_examples(limit=examples)],
        ]
    user_lines += [
        "",
        failures_intro,
        *failures,
        "",
        "原稿如下（事实不许动，文字全部重写成现在的腔）："
        if mode == "modernize" else "原稿如下（只改语言，内容一个字都不许动）：",
        "",
        source.strip(),
    ]
    user = "\n".join(user_lines)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
