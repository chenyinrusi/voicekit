"""起稿：按作者的路径写初稿，并把「判断 / 情绪」分成复用与推断两类标出来。

为什么要有这个入口（2026-11-11 作者拍板）：
- 仿写（机器全写、他直接发）已被证否：三轮 0/10，读者一眼认出。
- 提问器（只问不写）不省力。
- 起稿（机器写、判断与情绪标出来、他改）没试过——这是缺的那格。
  验收也从「读者认不认出」换成「他改多少 / 删几条我编的 / 几轮定稿」。

判断与情绪的处理（作者原话「可以推断也可以复用，有过去例子的情况下复用，没有的话推断」）：
- 起稿前先查判断库（`judgments.for_material`），把够近的旧句当候选塞进提示词；
- 语料里有过的意思 → 复用，句末标 `〔复用·篇号〕`；篇号必须是这次真给过它的，否则报红；
- 没有 → 允许推断，但整句包在 `〔推断〕…〔/推断〕` 里；
- 事实（数字、名字、引语）只能来自材料，一个新字都不许编。

用法：
    py -3.10 scripts/voice/draft.py --file 材料.md
    py -3.10 scripts/voice/draft.py --file 材料.md --facts-only   # 0 元：只出事实骨架
    py -3.10 scripts/voice/draft.py --file 材料.md --json out.json --examples 6
    py -3.10 scripts/voice/draft.py --selftest
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import judgments  # noqa: E402
from . import rewrite  # noqa: E402
from .rewrite_prompt import modern_voice_examples, style_digest  # noqa: E402
from .voice_text import split_paragraphs  # noqa: E402

INFER = re.compile(r"〔推断〕(.*?)〔/推断〕", re.S)
#: 标记本身（含没闭合的）：正文核对前要清掉，否则标记会被当成「事实句」。
MARK = re.compile(r"〔(?:/)?推断〕")
#: 容错：模型 2026-11-11 实测会写成 `〔复用·篇H2〕`（把示例里的「篇号」当成字面），
#: 所以篇号前允许一个「篇」。机器不该因为格式小滑就漏掉一次真复用——漏了就等于没核出处。
REUSE = re.compile(r"〔复用·(?:篇)?([0-9A-Za-z]{2,4})〕")
#: 机器不许编的私人经历，只留占位让作者填。
PLACEHOLDER = re.compile(r"〔此处缺你的真实经历：([^〕]*)〕")
#: 占位行后面紧跟的一段，多半是机器又替他回忆了。
AFTER_PLACEHOLDER = re.compile(r"〔此处缺你的真实经历：[^〕]*〕[ \t]*\n+(\S[^\n]*)")

PATH_STEPS = (
    "1. 先摆事实（谁、什么时候、发生了什么）。\n"
    "2. 写作者的第一反应——不是评论，是他看到这件事的第一个念头。\n"
    "3. 问一句「为什么」，给出他可能接受的那个解释（不是通用解释）。\n"
    "4. 把后果往前推一步：最坏的具体那一步是什么。\n"
    "5. 承认他不确定的地方（用「怕是 / 也可能 / 我不知道」这类说法）。\n"
    "6. 收在他自己身上：一件小事、一句轻话；不总结、不升华、不劝人。\n"
)


def _reuse_block(reuse: list[dict], mark: bool = True) -> str:
    """把检索到的旧句放进提示词。检索只是候选，立场对不对得模型挑、人来核。

    `mark=False`（直接模仿模式）：照着他的说法写就行，不用标篇号。
    """
    if not reuse:
        return "（这篇材料没检索到够近的旧句。全部写成推断，不要硬凑篇号。）\n"
    lines = "\n".join(f"- 篇{row['doc']}（{row['kind']}）：{row['text']}" for row in reuse)
    if not mark:
        return f"{lines}\n- 意思对得上就照着他的说法写，**不用标任何标记**。\n"
    return (
        f"{lines}\n"
        "- 只有**意思对得上**才复用，句末标 `〔复用·篇号〕`；用不上就都写成推断。\n"
        "- **不许为了用上篇号而硬贴**：安立场比安事实更冒犯。\n"
    )


def draft_system_prompt(examples: int = 6, reuse: list[dict] | None = None) -> str:
    """起稿的系统提示：路径 + 判断/情绪两类处理 + 可复用的旧句 + 输出格式。"""
    samples = modern_voice_examples(limit=examples)
    return (
        "你要替一位中文随笔作者起一篇初稿。你不是作者，你是替他起草。\n\n"
        "## 他处理一件事的顺序（不知道怎么走时按这个来）\n"
        f"{PATH_STEPS}\n"
        "## 判断和情绪怎么处理（作者 2026-11-11 拍板）\n"
        "- 语料里有过类似的意思 → **复用**他的说法，并在句末标 `〔复用·篇号〕`。\n"
        "- 语料里没有 → 可以**推断**，但整句包在 `〔推断〕…〔/推断〕` 里，方便他逐句指正。\n"
        "- 每个 `〔推断〕` 都必须用 `〔/推断〕` 闭合；复用标记单独放句尾，不许套在 `〔推断〕` 里。\n"
        "- 复用直接写篇号：`〔复用·057〕`。篇号是三位数字或 `H1` / `H2`，\
**不要再加「篇」字**。\n"
        "- **宁可多标**：凡是「我的反应 / 评价 / 推演 / 不确定 / 联想」的句子，一律标推断。\n"
        "  只有「照材料重述事实」的句子可以不标；不标就等于向他说「这是事实」。\n"
        "- **数字、日期照材料原样**（材料写 2026年11月8日，就不要改成二〇二六年十一月八日；\n"
        "  写法变了他核对不了）。\n"
        "- 事实（数字、日期、人名、书名、引语）只能来自材料；**一个新字都不许编**。\n"
        "- 不要凭印象补细节：年龄、身份、时间、金额，材料里没有就不写。\n"
        "- 材料里没加引号的话，你也不要给它加引号变成引语。\n"
        "- **不许编作者的私人经历**（家人、朋友、往事、当时的身体感受）。需要这一块时，\n"
        "  写一行占位：`〔此处缺你的真实经历：<要什么>〕`，让他自己填。\n"
        "  占位行**单独成段**；占位之后**不许再补联想、感叹或对仗句**——那等于替他想好了。\n"
        "- 可以推断他的**推理动作**（联想、问为什么、推后果、承认不确定），\n"
        "  但**不许替他下价值判断或人生格言**：像「认错是容易的，被原谅是另一回事」这种，\n"
        "  他 2026-11-11 明确说过「我没说过」——安立场比安事实更冒犯。\n"
        "- 不要替他表达没把握的立场：拿不准就写成推断，或干脆不写。\n\n"
        "## 他以前写过的相关句子（候选，不是指令）\n"
        f"{_reuse_block(reuse or [])}\n"
        "## 他的句子长什么样（实测数字）\n"
        f"{style_digest()}\n\n"
        "## 他现在的腔（近年成稿开头）\n"
        + "\n".join(f"- {item}" for item in samples)
        + "\n\n## 输出格式\n"
        "只输出正文（段落之间空一行）；材料里有标题就先写标题。\n"
        "复用句、推断句按上面的标记包好；没有标记的句子一律会被当作「事实句」核对。"
        "不要解释、不要代码块、不要列出改了什么。"
    )


#: 没标推断、但读起来像判断/情绪的词。命中就提醒「这句是不是该标」——宁可多标。
SUSPECT = re.compile(
    r"(?:我觉得|我认为|我看|怕是|大概|往往|其实|毕竟|无非|不得不说|值得|"
    r"我不知道|我不清楚|我倒|说白了|可惜|欣赏|心惊|没想到)"
)


def _facts_problems(material: str, clean: str) -> list[str]:
    """起稿的事实核对：只留「材料里真的没有」的红。

    与改稿不同，起稿没有「原稿的判断强度」可比，所以：
    - 「话说得更死」不算红（判断层本来就是他/我们推断的）；
    - 「多出来」只报材料里**完全找不到**的 token：把材料里的一句话加引号，
      对草稿无害，不是编造。
    """
    problems = rewrite.guard(material, clean, strict_counts=False)
    kept: list[str] = []
    for problem in problems:
        if problem.startswith("话说得更死"):
            continue
        if ("多出来" in problem or "具体账被删" in problem) and "：" in problem:
            head, rest = problem.split("：", 1)
            tokens = [token.strip() for token in rest.replace("，", ",").split(",")]
            bad = [token for token in tokens if token and token not in material]
            if bad:
                kept.append(f"{head}：{', '.join(bad)}")
            continue
        kept.append(problem)
    return kept


#: 事实覆盖率的机器代理：一条事实的字面 4 字窗口有多少出现在成稿里。
#: 改写会拉低这个数，所以只当读数，不当灯。准确率由 `_facts_problems` 的红单看。
COVER_FLOOR = 0.3


def _shingles(text: str, size: int = 4) -> set[str]:
    body = re.sub("[\\s，。！？、；：「」『』“”‘’（）()《》—…·]", "", text)
    return {body[i:i + size] for i in range(len(body) - size + 1)}


def fact_coverage(material: str, clean: str) -> dict:
    """材料里每条事实，成稿用了没有。返回 {covered, total, ratio, missing}。"""
    rows = [line[2:].strip() for line in material.splitlines()
            if line.strip().startswith("- ")]
    body = _shingles(clean)
    missing = [row for row in rows
               if not _shingles(row) or
               len(_shingles(row) & body) / len(_shingles(row)) < COVER_FLOOR]
    covered = len(rows) - len(missing)
    return {
        "covered": covered,
        "total": len(rows),
        "ratio": round(covered / len(rows), 2) if rows else 1.0,
        "missing": missing,
    }


#: 像人生格言的句子：抽离具体事、下通用结论。命中就提醒「这句可能在替他下结论」。
#: 「就晚了」是 2026-11-11 真跑抓到的：草稿收尾写「有些话，说出口就晚了」——对偶式格言，
#: 作者会说「我没说过」。这类收尾句是机器最容易自己编的东西，宁可多报。
MAXIM = re.compile(
    r"(?:说到底|无非|不过是|天底下|世上|人生|人这一辈子|"
    r"就晚了|就来不及|就回不去了|"
    r"[^。！？]{1,12}是容易的|容易的[^。！？]{0,8}难的|"
    r"[^。！？]{2,10}是[^。！？]{2,10}，[^。！？]{2,10}是[^。！？]{2,10})"
)


#: 事实骨架的标签：只标机器能从字面认出来的性质，不判断、不评价。
FACT_TAGS = (
    ("指控", re.compile(r"(?:被指为|被指|涉嫌|疑与|疑似|据称|据报|有消息称|据传|怀疑)")),
    ("否认", re.compile(r"(?:否认|没有理由认为|未获证实|没有得到独立证实|没有公开证据)")),
    ("引语", rewrite._QUOTE),
    ("时间", re.compile(r"[\d一二三四五六七八九十百千]{1,4}\s*(?:年|月|日|星期)")),
    ("数字", re.compile(r"[\d一二三四五六七八九十百千两]{1,5}\s*(?:年|岁|块|元|家|人|亿|万)")),
)
#: 机器不写、留给作者的三样东西（他的判断与情绪机器写不过他）。
LEAVE_TO_AUTHOR = (
    "你看到这事的第一反应（不是评论，是你的第一个念头）",
    "你为什么这么看（不是通用解释）",
    "收在你自己身上的那一句：一件小事、一句轻话",
)


def facts_only(material: str) -> list[dict]:
    """只把材料里的事实句摆出来，一行一句，标上它属于哪一类。**一个字都不改、不评。**

    作者 2026-11-11 拍板：「如果起稿的话，只有 fact 会更好」。他的判断和情绪机器写不过他，
    混进稿子只增加他的修改量；事实是机器能保证的东西，那就只给事实。
    """
    rows: list[dict] = []
    for paragraph in split_paragraphs(material):
        for sentence in rewrite._SENT.findall(paragraph):
            body = sentence.strip(" -•·\t")
            if len(body) < 6:
                continue
            tags = [name for name, pattern in FACT_TAGS if pattern.search(body)]
            rows.append({"text": body, "tags": tags or ["事实"]})
    return rows


def direct_system_prompt(examples: int = 6, reuse: list[dict] | None = None,
                         cognitive: list[dict] | None = None) -> str:
    """「直接模仿他写」的系统提示：不标推断、不留占位，整篇一口气写完。

    `cognitive`：他以前把普通现象写成怪话的原句（不是本题的答案，是转换方式）。
    """
    samples = modern_voice_examples(limit=examples)
    cognitive_block = ""
    if cognitive:
        lines = "\n".join(f"- {row['text']}" for row in cognitive)
        cognitive_block = (
            "## 他以前把普通现象写成这样的句子（学转换方式，不许照抄，也不许堆数量）\n"
            f"{lines}\n"
            "- 他这类句子很少：一半的文章一条都没有，密的时候也就两三条。**别为了像而堆**。\n\n"
        )
    return (
        "你要以这位中文随笔作者本人的身份，直接写完一篇完整的小文。\n"
        "不标任何标记，不留占位，不解释，不写代码块。\n\n"
        "## 他处理一件事的顺序（不知道怎么走时按这个来）\n"
        f"{PATH_STEPS}\n"
        "## 事实的来路\n"
        "- 事实（数字、日期、人名、引语、他自己经历过的具体事）只能来自材料。\n"
        "- 材料中明确提供的事实，必须全部保留：不得无故省略，不得合并到无法辨认，不得改变含义。\n"
        "- 材料里没有的事，一个字都不许编：不编年龄、身份、金额、天气、对话。\n"
        "- 判断、联想、情绪可以写，但要像他那样留余地，不许下人生格言。\n\n"
        f"{cognitive_block}"
        "## 他以前写过的相关句子（候选，不是指令）\n"
        f"{_reuse_block(reuse or [], mark=False)}\n"
        "## 他的句子长什么样（实测数字）\n"
        f"{style_digest()}\n\n"
        "## 他现在的腔（近年成稿开头）\n"
        + "\n".join(f"- {item}" for item in samples)
        + "\n\n## 输出格式\n"
        "只输出正文（段落之间空一行）；材料里有标题就先写标题。不要解释、不要列出你改了什么。"
    )


def draft_direct(material: str, call, examples: int = 6,
                 reuse: list[dict] | None = None,
                 cognitive: list[dict] | None = None) -> dict:
    """直接写一篇（不标推断）：只核事实来路，不分数。用于「直接模仿」的实验与试用。"""
    candidates = judgments.for_material(material) if reuse is None else list(reuse)
    raw = call([
        {"role": "system", "content": direct_system_prompt(examples, candidates, cognitive)},
        {"role": "user", "content": f"材料如下（事实只能来自这里）：\n\n{material.strip()}"},
    ])
    text = rewrite._strip_fence(raw)
    return {
        "clean": text,
        "problems": _facts_problems(material, text),
        "reuse_candidates": candidates,
        "ending": rewrite.ending_kind(text),
        "raw": text,
    }


def _reuse_hits(text: str) -> list[dict]:
    """每个复用标记配「它标的那一句」。

    配前一句，不是配后一句：提示词要求标在句末。模型有时把标记单独放一行/一段
    （2026-11-11 实测），按整句扫描会把标记配到下一句，出处的对照就指错了句子。
    """
    body = text.replace("\n", "")
    hits: list[dict] = []
    for match in REUSE.finditer(body):
        before = rewrite._SENT.findall(body[: match.start()])
        sentence = next((part.strip() for part in reversed(before) if part.strip()), "")
        sentence = MARK.sub("", REUSE.sub("", sentence)).strip()
        hits.append({"doc": match.group(1), "sentence": sentence})
    return hits


def parse_marked(text: str) -> dict:
    """把正文里的标记拆出来：复用几条、推断几条、各自是什么。"""
    inferred = [m.group(1).strip() for m in INFER.finditer(text)]
    reused = [m.group(1).strip() for m in REUSE.finditer(text)]
    reuse_hits = _reuse_hits(text)
    placeholders = [m.group(1).strip() for m in PLACEHOLDER.finditer(text)]
    maxims = [sentence for sentence in inferred if MAXIM.search(sentence)]
    # 去掉标记后的干净正文（推断句保留内容，方便他直接读）
    clean = INFER.sub(lambda m: m.group(1), text)
    clean = REUSE.sub("", clean)
    clean = MARK.sub("", clean)  # 没闭合的标记也算标记，不能漏进事实句
    marked_bodies = {s.strip() for s in inferred}
    suspects = []
    for sent in rewrite._SENT.findall(clean.replace("\n", "")):
        body = sent.strip()
        if not body or any(body in marked for marked in marked_bodies):
            continue
        if SUSPECT.search(body):
            suspects.append(body)
    facts = [
        sent.strip()
        for sent in rewrite._SENT.findall(clean.replace("\n", ""))
        if sent.strip()
        and "〔" not in sent
        and sent.strip() not in suspects
        and not any(sent.strip() in marked for marked in marked_bodies)
    ]
    return {
        "clean": clean,
        "inferred": inferred,
        "reused": reused,
        "reuse_hits": reuse_hits,
        "placeholders": placeholders,
        "maxims": maxims,
        "facts": facts,
        "unmarked_suspects": suspects,
        "counts": {
            "facts": len(facts),
            "reused": len(reused),
            "inferred": len(inferred),
            "placeholders": len(placeholders),
            "maxims": len(maxims),
            "unmarked_suspects": len(suspects),
        },
    }


def _reuse_check(reuse_hits: list[dict], candidates: list[dict],
                 rows: list[dict] | None = None) -> dict:
    """核复用标记：出处必须是这次真给过他的旧句，并给出原话供人对照。

    只有机器能断定的事才报红：篇号不存在，或篇号存在但机器这次没看过它——
    这两种都是编出处。复用句与原话像不像，机器不判（他的复用本来就是改写），
    只把原话摆在报告里让他一秒扫完。
    """
    rows = rows if rows is not None else judgments.load()
    originals = {row["doc"]: row["text"] for row in rows}
    originals.update({row["doc"]: row["text"] for row in candidates})
    offered = {row["doc"] for row in candidates}
    problems: list[str] = []
    pairs: list[dict] = []
    for hit in reuse_hits:
        doc = hit["doc"]
        if doc not in originals:
            problems.append(f"复用篇号不存在：{doc}（机器编的引用）")
        elif doc not in offered:
            problems.append(f"复用篇号篇{doc} 不在这次给它的旧句里（多半是编的出处）")
        else:
            pairs.append({"doc": doc, "sentence": hit["sentence"],
                          "origin": originals[doc]})
    return {"problems": problems, "pairs": pairs}


def _mark_notes(text: str) -> list[str]:
    """标记本身的读数（灰）：没闭合的推断标记、占位后面又出现一段。

    这两件事说明「分层不可靠」——正文不一定错，但作者得知道哪几句该信。
    2026-11-11 实测：模型把两条复用套在一个没闭合的 `〔推断〕` 里，
    结果计数是「复用 0 句」，出处也没核到——机器漏掉真复用，比多报更危险。
    """
    notes: list[str] = []
    opens, closes = text.count("〔推断〕"), text.count("〔/推断〕")
    if opens != closes:
        notes.append(f"推断标记没闭合：开了 {opens} 个、闭了 {closes} 个（分层不可靠，按正文读）")
    for match in AFTER_PLACEHOLDER.finditer(text):
        notes.append(
            f"占位后面又跟了一段「{match.group(1).strip()[:36]}」（多半是替你回忆/感慨，你判）"
        )
    if text.count("〔复用") and not REUSE.search(text):
        notes.append("复用标记写坏了：机器没认出篇号，出处没核成")
    return notes


def draft(material: str, call, examples: int = 6, reuse: list[dict] | None = None) -> dict:
    candidates = judgments.for_material(material) if reuse is None else list(reuse)
    messages = [
        {"role": "system", "content": draft_system_prompt(examples, candidates)},
        {"role": "user", "content": f"材料如下（事实只能来自这里）：\n\n{material.strip()}"},
    ]
    raw = call(messages)
    text = rewrite._strip_fence(raw)
    parsed = parse_marked(text)
    checked = _reuse_check(parsed["reuse_hits"], candidates)
    # 占位句是给作者看的说明，不是正文：核对事实前先摘掉（里面常带引号，会被当成引语）
    parsed["problems"] = checked["problems"] + _facts_problems(
        material, PLACEHOLDER.sub("", parsed["clean"])
    )
    parsed["reuse_candidates"] = candidates
    parsed["reuse_pairs"] = checked["pairs"]
    parsed["notes"] = _mark_notes(text)
    parsed["ending"] = rewrite.ending_kind(parsed["clean"])
    parsed["raw"] = text
    return parsed


def _selftest() -> int:
    text = (
        "11月8日，他向员工道歉。\n\n"
        "〔推断〕我第一反应是这事不体面。〔/推断〕\n\n"
        "这点展示了权力在手的样子〔复用·057〕。"
    )
    parsed = parse_marked(text)
    assert parsed["counts"]["inferred"] == 1, parsed["counts"]
    assert parsed["counts"]["reused"] == 1, parsed["counts"]
    assert "〔推断〕" not in parsed["clean"] and "〔复用" not in parsed["clean"]
    assert "11月8日" in parsed["clean"]
    assert parsed["facts"], "没有标记的句子要能被拎出来当事实句"
    assert parsed["reuse_hits"] and parsed["reuse_hits"][0]["doc"] == "057", parsed["reuse_hits"]
    assert "〔复用" not in parsed["reuse_hits"][0]["sentence"]
    assert "他处理一件事的顺序" in draft_system_prompt(2)
    assert "〔复用·篇号〕" in draft_system_prompt(2)
    assert "一个新字都不许编" in draft_system_prompt(2)
    pool = [{"doc": "057", "kind": "判断", "text": "这点展示了权力在手的样子。"}]
    assert "篇057" in draft_system_prompt(2, pool)
    assert "全部写成推断" in draft_system_prompt(2, [])
    checked = _reuse_check(
        [{"doc": "999", "sentence": "这句引了不存在的篇号。"},
         {"doc": "042", "sentence": "这句引了机器没给过的篇号。"},
         {"doc": "057", "sentence": "权力在手的时候，人就不一样了。"}],
        pool, rows=pool + [{"doc": "042", "kind": "判断", "text": "他写过别的。"}],
    )
    assert any("篇号不存在" in item for item in checked["problems"]), checked
    assert any("不在这次给它的旧句里" in item for item in checked["problems"]), checked
    assert checked["pairs"] and checked["pairs"][0]["origin"].startswith("这点展示了"), checked
    # 2026-11-11 真跑抓到的两种格式事故：把「篇」写进篇号、推断没闭合、占位后面又补一句
    sloppy = (
        "〔推断〕我觉得这事不体面。〔复用·篇H2〕\n\n"
        "〔此处缺你的真实经历：你也遇到过吗〕\n\n"
        "〔推断〕我倒是想起一句轻的。〔/推断〕\n"
    )
    parsed = parse_marked(sloppy)
    assert parsed["counts"]["reused"] == 1, parsed["counts"]
    assert parsed["reuse_hits"][0]["doc"] == "H2", parsed["reuse_hits"]
    assert "〔推断〕" not in parsed["clean"] and "〔/推断〕" not in parsed["clean"]
    notes = _mark_notes(sloppy)
    assert any("没闭合" in note for note in notes), notes
    assert any("占位后面" in note for note in notes), notes
    # 作者 2026-11-11：「如果起稿的话，只有 fact 会更好」→ 事实骨架逐字来自材料，不评一句
    source = ("2026年11月，英伟达创始人黄仁勋陷入一场风波。\n\n"
              "- 据《纽约时报》等媒体报道，她被指为 Alice Huang。\n"
              "- 中国方面否认相关指控。")
    rows = facts_only(source)
    assert rows and all(row["text"] in source for row in rows), rows
    assert any("指控" in row["tags"] for row in rows), rows
    assert any("否认" in row["tags"] for row in rows), rows
    assert any("时间" in row["tags"] for row in rows), rows
    assert all(row["tags"] for row in rows)
    print("[OK] selftest: 标记拆分、事实句、复用出处核对、格式事故、事实骨架、提示词都过")
    return 0


def _print_ending(kind: str) -> None:
    """结尾类型当读数报出来：他 61 篇里总结式只有 1 篇（2026-11-11 实测）。"""
    print(f"  · 结尾类型：{kind}（他 61 篇里总结式收尾只有 1 篇）")
    if kind == "总结/道理":
        print("      ⚠ 你几乎从不这样收尾——要不要改成一句具体的动作或轻话")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="按作者的路径起一篇初稿（判断/情绪分复用与推断）")
    ap.add_argument("--file", type=Path, help="材料（事实清单）")
    ap.add_argument("--out", type=Path, help="初稿写哪（默认 <file>.draft.md）")
    ap.add_argument("--json", dest="json_out", type=Path, help="机器可读报告")
    ap.add_argument("--examples", type=int, default=6, help="提示词里放几段他的腔当锚")
    ap.add_argument("--reuse-limit", type=int, default=3, help="放几条他以前的旧句当复用候选")
    ap.add_argument("--no-reuse", action="store_true", help="不检索旧句（A/B 用）")
    ap.add_argument("--cognitive", type=int, default=0,
                    help="再放几条「他以前的怪话」当转换素材（实验用）")
    ap.add_argument("--exclude", action="append", metavar="篇号",
                    help="检索时排除这篇（重写谁就排除谁，否则等于把答案递过去）")
    ap.add_argument("--facts-only", action="store_true",
                    help="0 元：只出事实骨架（一行一事实 + 分类），判断与情绪留给你写")
    ap.add_argument("--direct", action="store_true",
                    help="直接写完一篇（不标推断/占位）：直接模仿，用来和作者原文比")
    ap.add_argument("--dry-run", action="store_true", help="0 元：只出提示词")
    ap.add_argument("--provider", default=rewrite.DEFAULT_PROVIDER)
    ap.add_argument("--model", default=rewrite.DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--timeout", type=float, default=rewrite.DEFAULT_TIMEOUT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.file:
        ap.error("要 --file 材料")
    material = args.file.read_text(encoding="utf-8")
    if args.facts_only:
        rows = facts_only(material)
        out_path = args.out or args.file.with_suffix(".facts.md")
        body = "\n".join(f"{index}. [{'/'.join(row['tags'])}] {row['text']}"
                         for index, row in enumerate(rows, 1))
        leave = "\n".join(f"- {item}" for item in LEAVE_TO_AUTHOR)
        out_path.write_text(
            f"# 事实骨架（只有事实，一个字不评）\n\n{body}\n\n## 机器不写，留给你\n\n{leave}\n",
            encoding="utf-8", newline="\n",
        )
        print(f"== 事实骨架：{len(rows)} 条（0 元，没有模型参与）==")
        for index, row in enumerate(rows, 1):
            print(f"  {index}. [{'/'.join(row['tags'])}] {row['text'][:60]}")
        print("\n== 机器不写，留给你 ==")
        for item in LEAVE_TO_AUTHOR:
            print(f"  · {item}")
        print(f"\n[OK] 事实骨架写到 {out_path}")
        return 0
    excluded = set(args.exclude or [])
    reuse: list[dict] = ([] if args.no_reuse
                         else judgments.for_material(material, args.reuse_limit, exclude=excluded))
    cognitive: list[dict] = (
        judgments.for_material(material, args.cognitive, exclude=excluded, ratio=0.0)
        if args.cognitive else []
    )
    if args.dry_run:
        print(direct_system_prompt(args.examples, reuse, cognitive) if args.direct
              else draft_system_prompt(args.examples, reuse))
        print("\n===== user =====\n" + material)
        return 0

    call = rewrite._llm_callable(
        args.provider, args.model, args.temperature, args.timeout
    )
    if args.direct:
        result = draft_direct(material, call, args.examples, reuse, cognitive)
        out_path = args.out or args.file.with_suffix(".direct.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result["clean"].strip() + "\n", encoding="utf-8", newline="\n")
        print(f"\n[OK] 直接写的稿子写到 {out_path}")
        _print_ending(result["ending"])
        if result["problems"]:
            print("\n== 红：事实（机器定）==")
            for problem in result["problems"]:
                print(f"  ✗ {problem}")
        return 1 if result["problems"] else 0
    result = draft(material, call, args.examples, reuse)
    out_path = args.out or args.file.with_suffix(".draft.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result["clean"].strip() + "\n", encoding="utf-8", newline="\n")
    # 带标记的版本：他一眼看出哪几句是推断、哪几处要他填真实经历
    marked_path = out_path.with_suffix(".marked.md")
    marked = result["raw"].strip() + "\n"
    if result["reuse_candidates"]:
        lines = "\n".join(
            f"篇{row['doc']}（{row['kind']}）：{row['text']}"
            for row in result["reuse_candidates"]
        )
        marked += f"\n---\n这次给它检索到的可复用旧句（供你核篇号用）：\n{lines}\n"
    marked_path.write_text(marked, encoding="utf-8", newline="\n")
    counts = result["counts"]
    print("\n== 可复用的旧句（检索候选，不是结论）==")
    if not result["reuse_candidates"]:
        print("  · 没有够近的旧句 → 按他的规矩，全部当成推断")
    for index, row in enumerate(result["reuse_candidates"], 1):
        print(f"  {index}. 篇{row['doc']}·{row['kind']} [{row['score']:.3f}] {row['text'][:50]}")
    print("\n== 起稿：三类句 ==")
    print(f"  · 事实句 {counts['facts']} 句（未标记，机器只保证来自材料）")
    print(f"  · 复用句 {counts['reused']} 句（标了篇号，可回查语料）")
    print(f"  · 推断句 {counts['inferred']} 句（**要他逐句指正**）")
    print(f"  · 要你填的真实经历 {counts['placeholders']} 处（机器不编，只留空）")
    _print_ending(result["ending"])
    for index, sentence in enumerate(result["inferred"], 1):
        print(f"      {index}. {sentence[:70]}")
    if result["unmarked_suspects"]:
        print(f"  · ⚠ 没标推断、但读起来像判断的 {len(result['unmarked_suspects'])} 句：")
        for index, sentence in enumerate(result["unmarked_suspects"], 1):
            print(f"      {index}. {sentence[:70]}")
    for index, need in enumerate(result["placeholders"], 1):
        print(f"  · 要你填：{index}. {need[:60]}")
    if result["maxims"]:
        print(f"  · ⚠ 像格言的 {len(result['maxims'])} 句（可能在替他下结论，你判）：")
        for index, sentence in enumerate(result["maxims"], 1):
            print(f"      {index}. {sentence[:70]}")
    if result["reuse_pairs"]:
        print("\n== 复用对照（你核一句就够）==")
        for pair in result["reuse_pairs"]:
            print(f"  · 篇{pair['doc']} 你写的「{pair['sentence'][:40]}」")
            print(f"      原话「{pair['origin'][:50]}」")
    if result["notes"]:
        print("\n== 灰：标记本身（你核）==")
        for note in result["notes"]:
            print(f"  · {note}")
    if result["problems"]:
        print("\n== 红：事实（机器定）==")
        for problem in result["problems"]:
            print(f"  ✗ {problem}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(
                {
                    "file": str(args.file),
                    "out": str(out_path),
                    "model": args.model,
                    "counts": counts,
                    "inferred": result["inferred"],
                    "reused": result["reused"],
                    "reuse_candidates": result["reuse_candidates"],
                    "reuse_pairs": result["reuse_pairs"],
                    "notes": result["notes"],
                    "unmarked_suspects": result["unmarked_suspects"],
                    "placeholders": result["placeholders"],
                    "maxims": result["maxims"],
                    "problems": result["problems"],
                    "raw": result["raw"],
                },
                ensure_ascii=False,
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"\n[OK] 起稿写到 {out_path}")
    return 1 if result["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
