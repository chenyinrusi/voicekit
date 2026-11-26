"""改稿器：拿作者自己写的稿子，只改语言，改成他的腔调。

为什么是这个形态（不是「给个题目写一篇」）：五轮盲测的结论是——
- 冷启动代笔（只给题目）：两个无记忆盲读者 10/10 认得出；
- 给同一份内容、只改语言、并守住改稿纪律：盲读者 3/10、5/9，等于瞎猜。

所以产品的主循环是「他的稿子 → 他的腔调」，不是从零编。

用法（repo 根目录）：
    py -3.10 scripts/voice/rewrite.py --file 草稿.md                 # 写到 草稿.v2.md
    py -3.10 scripts/voice/rewrite.py --file 草稿.md --dry-run       # 0 元：只出诊断和提示词
    py -3.10 scripts/voice/rewrite.py --file 草稿.md --json out.json # 附机器可读报告

改稿纪律不在本文件里复制一份，而是从 `voices/panchen-voice/references/voice-core.md`
的「改稿纪律」一节与 6 条底色的「改稿时看」现读（单一来源，改纪律不用改代码）。

守门（任一不过就重试一次；还不过就报错，不静默交付）：
- 数字 / 百分比 / 英文词 / 《书名》 / “引语” 多重集合必须一字不差；
- 段数只许拆不许并；字数差 ≤10%；
- 句子改动率 20–60%（太少=没改，太多=重写）；
- 8 字片段保留 ≥40%（保留原腔）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
from .rewrite_prompt import (  # noqa: E402 - 依赖下面的 sys.path 设置
    build_messages,
    load_protected_rules,
    modern_voice_examples,
    modernize_rules,
    voice_rules,
)
from .voice_text import (  # noqa: E402
    corpus_path,
    countable_mentions,
    join_paragraphs,
    ledger_tokens,
    looks_like_title,
    material,
    split_lost_ledger,
    split_paragraphs,
    title_line,
    uses_blank_lines,
)
from .rules import REVIEW_BY, review_due  # noqa: E402

DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TEMPERATURE = 0.7
#: 要它实打实改的时候得升温：实测 0.7 → 改 6–11%、1.25 → 改 33%、1.45 开始跑飞（改引语、加段、加英文词）
PUSH_TEMPERATURE = 1.25
DEFAULT_TIMEOUT = 240.0

MIN_SENTENCE_CHANGE = 0.0
#: 低于这个算「改得太保守」：只提示，不拦（输入如果已经很像他，本来就不该动）。
SOFT_MIN_SENTENCE_CHANGE = 0.20
#: 段数下限：他改发表稿 61 篇实测，段比中位 1.00、最低 0.78 —— 允许把同一件事的短段并起来，
#: 但不许把段落砍掉四分之一以上（2026-11-10 用户指出「该合并的时候也可以合并」）。
PARAGRAPH_KEEP_FLOOR = 0.75

MAX_SENTENCE_CHANGE = 0.65
MIN_SHINGLE_KEEP = 0.40
MAX_LENGTH_DRIFT = 0.10

_DIGITS = re.compile(r"[0-9]+")
#: 汉字数字也是他的原词（`二〇一六年二月十四日`），改成阿拉伯数字同样是编辑痕迹。
_CN_NUM = re.compile(r"[一二三四五六七八九十〇零百千万两]{2,}")
_ASCII = re.compile(r"[A-Za-z]{2,}")
_TITLE = re.compile(r"《[^》]{1,40}》")
#: 引语：弯引号、直引号、全角引号、直角引号都算。
#: 原稿里有 ＂撒尿＂ 这种全角写法；只认弯引号时，模型把全角换成正常引号就会被判「引语多出来」
#: （2026-11-10 真踩过）。这里用 \u 转义写，免得源码里混着四种引号看不清。
_QUOTE = re.compile(
    "(?:\u201c[^\u201d]{1,80}\u201d"
    "|\"[^\"]{1,80}\""
    "|\uff02[^\uff02]{1,80}\uff02"
    "|\u300c[^\u300d]{1,80}\u300d)"
)
#: 说死的话。改稿只许减少，不许比他更武断（2026-11-09 探针：他 0.34/千字）。
_ASSERT = re.compile(r"(?:一定|必然|肯定|绝对|无疑|毫无疑问|必须|务必|全都|永远)")
#: 指控类限定语。这些词删掉 = 把「被指」变成「是」，对真人就是加罪，只许留不许删。
#: 2026-11-10 出生：黄仁勋那篇 AI 稿「被指为 Alice Huang」被改成「是 Alice Huang」。
_ALLEGE = re.compile(r"(?:被指为|被指|涉嫌|据称|据报|有消息称|疑与|疑似|据传)")
_SENT = re.compile(r"[^。！？!?…]+[。！？!?…]*")


#: 第一人称困惑（与 probe_dialog.CONFUSION 同源）：第 4 格「他在不在场」。
_CONFUSION = re.compile(
    r"(?:该不该|要不要|为什么|怎么才能|到底|是不是|有没有|想不通|没想明白|一直想"
    r"|不知道该怎么办|拿不准|说不好|搞不懂)"
)
#: 留门（与 probe_dialog.DOOR 同源）：末两句有缓和、未知、自我让步、语气词。
_DOOR = re.compile(
    r"(?:大概|也许|或许|似乎|好像|算是|怕是|说不定|未必|倒也|还算|也说不准"
    r"|说不准|说不清|谁知道|看来|也可能|罢|罢了|[呢吧嘛啊哈])"
)
_TAIL_SENTENCES = 2


# ---------------------------------------------------------------- 原稿与诊断


def judge_verdict(path: Path) -> str:
    """拿判官当「这份稿离他多远」的信号（一次调用，几分钱）。

    judge.py 的退出码 0=like / 1=not-like 是结论不是故障，所以不看 rc，只看 JSON。
    """
    import subprocess  # noqa: PLC0415

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "judge.py"), "--file", str(path), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return str(json.loads(proc.stdout).get("verdict") or "unknown")
    except json.JSONDecodeError:
        return "unknown"


def _quotes(text: list[str]) -> Counter:
    """引语按**内容**比，不按引号样式比：＂撒尿＂ 与 “撒尿” 是同一句引语。

    引号样式统一属于校对（他真做过：全角→半角），把内容弄丢才是事故。
    """
    return Counter(m[1:-1] for m in text)


def protected_tokens(text: str) -> dict[str, Counter]:
    """必须一字不差的五类：阿拉伯数字、汉字数字、英文词、书名、引号里的引语（按内容比）。"""
    return {
        "数字": Counter(_DIGITS.findall(text)),
        "汉字数字": Counter(_CN_NUM.findall(text)),
        "英文词": Counter(_ASCII.findall(text)),
        "书名": Counter(_TITLE.findall(text)),
        "引语": _quotes(_QUOTE.findall(text)),
    }


_PROVENANCE: dict[str, list[tuple[str, str]]] = {}


#: 小标题/清单的样子（偷加标题、分点清单时用；只报警示，不拦）。
_HEADING = re.compile(r"^(?:#{1,6}\s|[-*•]\s|\d+[、.．]\s?|第[一二三四五六七八九十]+[章节])")


def _para_signature(paragraph: str) -> str:
    return "".join(paragraph.split())[:6]


def order_kept(source: str, out: str) -> bool:
    """原稿段落的先后有没有被打乱：只比较「改后还能认出」的段落，认不出的跳过。"""
    body = "".join("".join(par.split()) for par in split_paragraphs(out))
    positions = []
    for paragraph in split_paragraphs(source):
        at = body.find(_para_signature(paragraph))
        if at >= 0:
            positions.append(at)
    return positions == sorted(positions)


def added_heading_lines(source: str, out: str) -> list[str]:
    """改稿里新冒出来的标题/清单行（原稿没有的）。"""
    original = set(split_paragraphs(source))
    return [
        paragraph for paragraph in split_paragraphs(out)
        if _HEADING.match(paragraph) and paragraph not in original
    ]


def title_dropped(source: str, out: str) -> str:
    """原稿的标题行在改稿里整行没了，返回那句提示；没这回事返回空串。

    2026-11-10 出生：改黄仁勋那篇 AI 稿，标题「黄仁勋的另一面：AI教父也有自己的软肋」
    被整行删掉，于是事实灯报「英文词被改掉：AI」——根因是提示词里写着「不要标题」。
    """
    title = title_line(source)
    if not title or len(split_paragraphs(source)) < 3:
        # 两段以内的稿子不当成「有标题的文章」：短句开头多半就是正文。
        return ""
    got = next(iter(split_paragraphs(out)), "")
    if looks_like_title(got):
        return ""
    return (
        f"标题被删了：原稿第一行是「{title[:20]}」，改后这一行没了"
        "（标题也是原文，字可以改，整行不许删）"
    )


def _provenance_index() -> dict[str, list[tuple[str, str]]]:
    """token → [(篇号, 那一篇的正文)]。只建一次；语料缺了就返回空（不拦）。"""
    corpus = corpus_path()
    if not _PROVENANCE and corpus is not None and corpus.is_dir():
        for path in sorted(corpus.glob("*.md")):
            doc = path.name.split("__")[0]
            text = path.read_text(encoding="utf-8")
            seen: set[str] = set()
            for counter in protected_tokens(text).values():
                for token in counter:
                    if token not in seen:
                        seen.add(token)
                        _PROVENANCE.setdefault(token, []).append((doc, text))
    return _PROVENANCE


def _meaningful(token: str) -> bool:
    """太短的 token 不算出处：一个「2」在哪儿都能找到，不构成来路。"""
    if token.startswith("《") or token.startswith("“"):
        return len(token) >= 4
    if token.isdigit():
        return len(token) >= 3
    return len(token) >= 2


def provenance(token: str, out: str = "") -> str | None:
    """新增的 token 能不能指到出处：返回篇号；成稿自己那一篇不算（避免自证）。"""
    if not _meaningful(token):
        return None
    for doc, text in _provenance_index().get(token, []):
        if out and text.strip() == out.strip():
            continue
        return doc
    return None


#: 去标点用的（翻新模式的「只加了标点」检查）：只留汉字、字母、数字。
_WORD_STRIP = re.compile(r"[\s，。！？、；：…“”‘’（）()《》\"'.\-—^・·_—～~]")


def word_keep(source: str, out: str, n: int = 2) -> float:
    """去掉标点后的 n 字片段保留率。翻新老稿时，>90% 说明「只加了标点，没改文字」。"""
    a = _WORD_STRIP.sub("", source)
    b = _WORD_STRIP.sub("", out)
    sa = {a[i:i + n] for i in range(len(a) - n + 1)}
    sb = {b[i:i + n] for i in range(len(b) - n + 1)}
    return len(sa & sb) / max(len(sb), 1)


def presence(source: str) -> dict:
    """第 4 格：他自己在不在场（有没有第一人称的困惑）。"""
    sentences = [s for s in _SENT.findall(source) if s.strip()]
    hits = [s for s in sentences if "我" in s and _CONFUSION.search(s)]
    return {
        "question": bool(hits),
        "hits": len(hits),
        "sentences": len(sentences),
        "rate": round(len(hits) / max(len(sentences), 1), 3),
    }


def door(text: str) -> bool:
    """第七格「说到哪」：末两句有没有留门（把话留一半，不封口）。"""
    sents = [s for s in _SENT.findall(text) if s.strip()]
    return bool(sents) and bool(_DOOR.search("".join(sents[-_TAIL_SENTENCES:])))


def sentence_change_rate(source: str, out: str) -> float:
    """原稿里有多少句子在改稿里找不到了（按整句精确比对，忽略空白）。"""
    def norm(s: str) -> str:
        return re.sub(r"\s+", "", s)

    src = [norm(s) for s in _SENT.findall(source) if norm(s)]
    out_set = {norm(s) for s in _SENT.findall(out) if norm(s)}
    if not src:
        return 0.0
    missing = sum(1 for s in src if s not in out_set)
    return missing / len(src)


def shingle_keep(source: str, out: str, n: int = 8) -> float:
    """改稿里 8 字片段有多大比例在原稿里出现过（保留原腔的程度）。"""
    def shingles(t: str) -> set[str]:
        t = "".join(ch for ch in t if ch.strip())
        return {t[i : i + n] for i in range(len(t) - n + 1)}

    out_s = shingles(out)
    if not out_s:
        return 0.0
    return len(out_s & shingles(source)) / len(out_s)


def length_ratio(source: str, out: str) -> float:
    a = len("".join(source.split()))
    b = len("".join(out.split()))
    return b / max(a, 1)


def guard(source: str, out: str, strict_counts: bool = True) -> list[str]:
    """返回问题清单；空清单 = 过门。

    2026-11-09 起门只管「账、来路、结构」三样：字数、改动率、8 字保留全部降级成提示
    （见 `warnings_of`）。理由：他改自己的稿子会改 67%、字数 +18%，那些「距离」指标
    量不出「是不是他」，只量「离原稿多近」。

    `strict_counts=False`：只查「这个事实还在不在」，不查「出现了几次」——起稿（`draft.py`）用。
    """
    problems: list[str] = []
    src_tokens, out_tokens = protected_tokens(source), protected_tokens(out)
    for kind, want in src_tokens.items():
        got = out_tokens[kind]
        lost = (want - got) if strict_counts else {
            token: count for token, count in want.items() if got[token] == 0
        }
        added = {token: count for token, count in got.items() if want[token] == 0}
        if lost:
            problems.append(f"{kind}被改掉：{', '.join(sorted(lost)[:5])}")
        if added:
            unsourced = [t for t in added if not provenance(t, out)]
            if unsourced:
                problems.append(f"{kind}多出来：{', '.join(sorted(unsourced)[:5])}")
    lost_ledger, _tightened = split_lost_ledger(source, out, strict_counts=strict_counts)
    if lost_ledger:
        problems.append(
            f"具体账被删：{', '.join(sorted(lost_ledger)[:5])}"
            "（数字、单位、书名、引语一个都不许少）"
        )
    src_hard, out_hard = len(_ASSERT.findall(source)), len(_ASSERT.findall(out))
    if out_hard > src_hard:
        problems.append(
            f"话说得更死了：「一定/必须」类从 {src_hard} 个变成 {out_hard} 个"
            "（他下判断很留余地，只许减不许加）"
        )
    lost_hedges = [token for token in _ALLEGE.findall(source) if token not in out]
    if lost_hedges:
        problems.append(
            f"指控被说死了：{', '.join(sorted(set(lost_hedges))[:3])} 被删掉"
            "（「被指为」不能改成「是」——对真人是加罪，限定语只许留不许删）"
        )
    # 段数不再是门（2026-11-11 用户拍板「段落其实没那么重要，升维看」）：
    # 机器有资格数段数，没资格判「该不该并」——他删空议论段、合并同类，都是判断层的事。
    # 这盏灯已经误伤三次：064「只许拆」、61 篇「可以并」、作者亲手改到 73%。降到读数，见 warnings_of。
    dropped = title_dropped(source, out)
    if dropped:
        problems.append(dropped)
    return problems


#: 结尾类型：只按最后两句的字面认。机器判不了「留白好不好」，只报类型。
_ENDING_KINDS = (
    ("自嘲", re.compile(r"(?:我这个人|我算是|我这种|啧|哈哈|呵呵|运气也|倒也)")),
    ("留余地", re.compile(r"(?:大概|也许|或许|怕是|未必|好在|不知道|谁知道|说不准|再说|先不|也可能)")),
    ("意象", re.compile(r"(?:就像|像|仿佛|好比|似的)")),
    ("总结/道理", re.compile(r"(?:说到底|无非|道理|人生|一辈子|才叫|本质上|终究|不等人|回不去|才算|本来就是)")),
    ("动作/轻话", re.compile(r"(?:吧|了|呢|嘛|算了|就好)[。！？!?…]?$")),
)


def ending_kind(text: str) -> str:
    """结尾是什么类型：自嘲 / 留余地 / 意象 / 总结/道理 / 动作/轻话 / 其他。

    2026-11-11 实测 61 篇最后两句：动作/轻话 22、留余地 13、意象 4、自嘲 3、总结/道理 **1**。
    所以「总结式收尾」是他几乎不做、机器最常做的事。但这只是读数，不拦：
    n=1 的语料统计不构成绝对禁止，判好不好仍是人的事。
    """
    sentences = [part.strip() for part in _SENT.findall(text.replace("\n", "")) if part.strip()]
    if not sentences:
        return "其他"
    # 只看末句；末句太短（「就这样吧。」）才把上一句带上一起看。
    tail = sentences[-1]
    if len(tail) < 6 and len(sentences) > 1:
        tail = sentences[-2] + tail
    for name, pattern in _ENDING_KINDS:
        if pattern.search(tail):
            return name
    return "其他"


def warnings_of(
    source: str, out: str, min_change: float | None = None, max_change: float | None = None
) -> list[str]:
    """不拦但要写进报告的事：没改 / 太保守 / 改太狠 / 字数跑偏 / 加的料有出处 / 困惑加太多。"""
    low = SOFT_MIN_SENTENCE_CHANGE if min_change is None else min_change
    high = MAX_SENTENCE_CHANGE if max_change is None else max_change
    warnings: list[str] = []
    change = sentence_change_rate(source, out)
    if change == 0:
        warnings.append("一字未改：要么这份稿已经很像他，要么提示词没打动模型，看一眼再决定")
    elif change < low:
        warnings.append(f"句子只改了 {change:.0%}（低于 {low:.0%}，偏保守）")
    elif change > high:
        warnings.append(f"句子改了 {change:.0%}（上限 {high:.0%}，等于重写；他不怕远，但要人看一眼）")
    ratio = length_ratio(source, out)
    if abs(ratio - 1) > MAX_LENGTH_DRIFT:
        warnings.append(f"字数变化 {ratio - 1:+.0%}（±{MAX_LENGTH_DRIFT:.0%} 之外，只提示）")
    keep = shingle_keep(source, out)
    if keep < MIN_SHINGLE_KEEP:
        warnings.append(f"8 字片段只保留 {keep:.0%}（低于 {MIN_SHINGLE_KEEP:.0%}，只提示）")
    out_tokens = protected_tokens(out)
    for kind, want in protected_tokens(source).items():
        sourced = [
            f"{token}（{doc}）"
            for token in (out_tokens[kind] - want)
            if (doc := provenance(token, out))
        ]
        if sourced:
            warnings.append(f"{kind}是从他语料搬来的，有出处：{', '.join(sorted(sourced)[:5])}")
    _lost, tightened = split_lost_ledger(source, out)
    if tightened:
        warnings.append(
            f"只丢了写法，没丢信息：{', '.join(sorted(tightened)[:5])}"
            "（数「一」是默认量，量词和后面的词都在，更口语）"
        )
    before, after = presence(source), presence(out)
    if after["hits"] >= 2 and before["hits"] == 0:
        warnings.append(
            f"加出了 {after['hits']} 处第一人称困惑，原稿没有：他是那种人，不等于每篇都要困惑"
        )
    if door(source) and not door(out):
        warnings.append("留门没了：原稿末两句有缓和/让步，改后把话说封了（只提示，人看）")
    if ending_kind(out) == "总结/道理":
        warnings.append(
            "结尾是总结/道理型：他 61 篇里只有 1 篇这样收尾（只提示——改成具体观察或一句轻话）"
        )
    if not order_kept(source, out):
        warnings.append("段落顺序变了：原稿段落的先后被打乱（只提示）")
    src_paras = split_paragraphs(source)
    if len(split_paragraphs(out)) < len(src_paras) * PARAGRAPH_KEEP_FLOOR:
        warnings.append(
            f"段数 {len(src_paras)} → {len(split_paragraphs(out))}（不到原稿 {PARAGRAPH_KEEP_FLOOR:.0%}）："
            "他改稿会并段、也会删掉纯议论的空段——只提示，不当门"
        )
    for heading in added_heading_lines(source, out)[:3]:
        warnings.append(f"偷加了标题/清单：{heading[:20]}（只提示）")
    things = material(source, out)
    if things["src"] and things["out"] * 2 < things["src"]:
        warnings.append(
            f"具体的东西说模糊了：数得出来的从 {things['src']} 处减到 {things['out']} 处（只提示，你定）"
        )
    longest = max((len("".join(p.split())) for p in src_paras), default=0)
    if len(split_paragraphs(out)) == len(src_paras) and longest > 250:
        warnings.append("没拆段：他常把长段拆开（064 从 5 段拆到 20 段）")
    return warnings


def metrics(source: str, out: str) -> dict:
    things = material(source, out)
    return {
        "chars_src": len("".join(source.split())),
        "chars_out": len("".join(out.split())),
        "length_ratio": round(length_ratio(source, out), 3),
        "sentence_change": round(sentence_change_rate(source, out), 3),
        "shingle_keep_8": round(shingle_keep(source, out), 3),
        "paragraphs_src": len(split_paragraphs(source)),
        "paragraphs_out": len(split_paragraphs(out)),
        "ledger_src": sum(ledger_tokens(source).values()),
        "ledger_out": sum(ledger_tokens(out).values()),
        "assert_src": len(_ASSERT.findall(source)),
        "assert_out": len(_ASSERT.findall(out)),
        "things_src": things["src"],
        "things_out": things["out"],
        "material_missing": things["missing"],
    }


# ---------------------------------------------------------------- 提示词


def diagnose(path: Path) -> dict:
    """跑本地确定性层（0 元），拿失败项。"""
    import subprocess  # noqa: PLC0415 - 只在这里用

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "voice_review.py"), "--file", str(path),
         "--no-llm", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"[FAIL] voice_review 跑不动：{proc.stderr.strip()[:300]}")
    return json.loads(proc.stdout)


def profile_score(text: str) -> float | None:
    """落点分（0 元，本地）：这一版离他的 42 条实测分布多近。太短的稿子量不出，返回 None。

    用**加权分**（`score_weighted`）：改标点、断句这类不动内容的属性减重（0.5），
    要动内容（材料/引语/时空/他的词/真问题）的加重（2.0）——否则把句子用逗号连起来就能涨分。
    比「改了多少」有用：他自己草稿 → 成稿在加权分上也是往上的；改得多不等于更像他。
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.md"
        probe.write_text(text, encoding="utf-8")
        style = diagnose(probe).get("style") or {}
    score = style.get("score_weighted", style.get("score"))
    return float(score) if score is not None else None


def _text_of(response: object) -> str:
    if isinstance(response, str):
        return response
    for attr in ("content", "text"):
        value = getattr(response, attr, None)
        if isinstance(value, str):
            return value
    return str(response)


def _strip_fence(text: str) -> str:
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", text, re.S)
    return fence.group(1).strip() if fence else text


# ---------------------------------------------------------------- 主循环


def candidate_score(source: str, out: str) -> tuple:
    """机械分（越大越好）：只量他会在意的四件事，不评文笔。

    judge 会误放（把 AI 鸡汤夸成「他的私人标记」），所以挑版不听它：
    ① 过门问题越少越好；② 具体账丢得越少越好；③ 说死的话跟原稿越贴越好（他成稿保持原数量）；
    ④ 拆段越多越好（他把长段拆开写），封顶原稿段数的 4 倍（064 是 5→20）。
    """
    problems = guard(source, out)
    ledger_lost = sum((ledger_tokens(source) - ledger_tokens(out)).values())
    assert_gap = abs(len(_ASSERT.findall(out)) - len(_ASSERT.findall(source)))
    src_paras = len(split_paragraphs(source))
    split_bonus = min(len(split_paragraphs(out)) / max(src_paras, 1), 4.0)
    return (-len(problems), -ledger_lost, -assert_gap, split_bonus)


def decisions_of(source: str, out: str) -> dict:
    """两版之间「本该一样」的东西：五个决定，不是五个数字。

    人是做决定的方式，不是写出来的字。措辞允许掷骰子，这五项不该：
    账丢没丢、话有没有说得更死、拆不拆段（拆成几段是措辞，拆不拆是决定）、
    要不要让他在场（困惑）、末两句留不留门。第 4 格是原稿属性，各版恒定，不参与比。
    """
    lost = sum((ledger_tokens(source) - ledger_tokens(out)).values())
    src_paras = len(split_paragraphs(source))
    return {
        "账丢了": lost,
        "说死的话": len(_ASSERT.findall(out)),
        "拆段": "拆了" if len(split_paragraphs(out)) > src_paras else "没拆",
        "困惑": presence(out)["hits"],
        "留门": door(out),
    }


def best_of(source: str, results: list[dict]) -> dict:
    """跑了几版，挑机械分最高的一版；报告里记下选的是第几版。"""
    best = max(results, key=lambda result: candidate_score(source, result["text"]))
    best["chosen"] = results.index(best) + 1
    best["candidates"] = [candidate_score(source, result["text"]) for result in results]
    return best


def consensus(source: str, results: list[dict]) -> dict:
    """跑了几版先比决定，再挑一版。

    决定都一样 → 差异只在措辞，挑哪版都行（机械分挑）。
    有决定在漂 → 不拼、也不假装稳定：报出漂了哪几项，两版都留着给人拍板。
    拼两版会把两个不同的决定序列接到一起，读起来像换了个作者——所以先比决定，再谈拼。
    """
    best = best_of(source, results)
    vectors = [decisions_of(source, result["text"]) for result in results]
    drift = [key for key in vectors[0] if len({str(v[key]) for v in vectors}) > 1]
    best["decisions"] = vectors
    # 原稿那一行是尺子：两版都稳定、但都偏离原稿的决定，只有它能看出来。
    best["source_decisions"] = decisions_of(source, source)
    best["drift"] = drift
    best["stable"] = not drift
    return best


def rewrite(source: str, diag: dict, call, retries: int = 1,
            min_change: float | None = None, max_change: float | None = None,
            push: bool = False, mode: str = "voice", examples: int = 3) -> dict:
    """call(messages) -> 模型输出。守门不过就带着问题清单重试。

    mode 显式传（voice / polish）：以前从 `min_change == 0.0` 反推，等于拿参数当开关，
    polish 以外传 0.0 就会误判成润色模式（2026-11-09 review 修）。
    """
    messages = build_messages(source, diag, mode, push, examples)
    before_raw = (diag.get("style") or {}).get("score")
    before = float(before_raw) if before_raw is not None else None
    attempts = []
    notes: list[str] = []
    last_out = ""
    last_score = before
    for attempt in range(retries + 1):
        raw = call(messages)
        out = _strip_fence(raw)
        problems = guard(source, out)
        if mode != "polish":
            # 抄写检查只有一条门：去掉标点后的 2 字片段（word_keep）。
            # 句子改动率只写进报告，不再当门（同一件事两把尺，且对空格断句的输入会失真）。
            if mode in ("modernize", "voice"):
                keep = word_keep(source, out)
                if keep > 0.90:
                    problems.append(
                        f"只加了标点，没换文字（去掉标点后 {keep:.0%} 的 2 字片段原样未动）："
                        "这是翻新老稿，句子要按他现在的腔重写，不许只补标点"
                    )
            if before is not None:
                after = profile_score(out)
                if after is not None:
                    last_score = after
                    if after < before:
                        # gauge 层：分数是读数，不是判决（鲁迅稿 +6.2 而插话密度为 0 就是反例）。
                        notes.append(
                            f"落点更远了（他的分布 {before:.1f} 分 → 这一版 {after:.1f} 分）："
                            "只提示，别拿它当放行依据"
                        )
        attempts.append({"attempt": attempt + 1, "problems": problems})
        last_out = out
        if not problems:
            return {
                "text": out,
                "attempts": attempts,
                "metrics": metrics(source, out),
                "warnings": notes + warnings_of(source, out, min_change, max_change),
                "presence": presence(source),
                "material": material(source, out),
                "score": last_score,
                "problems": [],
            }
        if attempt < retries:
            messages = messages + [
                {"role": "assistant", "content": out},
                {
                    "role": "user",
                    "content": (
                        "这一版没过机器检查。在**这一版**上逐条改对，再输出整篇正文：\n"
                        + "\n".join(f"- {p}" for p in problems)
                        + "\n不要退回原稿：改完的幅度要跟这一版一样（整句整句地改，账和结构照纪律）。"
                    ),
                },
            ]
    return {"text": last_out, "attempts": attempts, "metrics": metrics(source, last_out),
            "warnings": notes + warnings_of(source, last_out, min_change, max_change),
            "presence": presence(source),
            "material": material(source, last_out),
            "score": last_score,
            "problems": problems}


def _llm_callable(provider: str, model: str | None, temperature: float, timeout: float):
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from ..compat import release_llm_client  # noqa: PLC0415
    from ..compat import create_llm, ensure_provider_sdks_loaded  # noqa: PLC0415

    async def _run(messages: list[dict]) -> str:
        from ..compat import Message  # noqa: PLC0415

        await ensure_provider_sdks_loaded()
        llm = create_llm(provider, model)
        built = []
        for message in messages:
            role, content = message["role"], message["content"]
            if role == "system":
                built.append(Message.system(content))
            elif role == "assistant":
                built.append(Message.assistant(content))
            else:
                built.append(Message.user(content))
        try:
            response = await asyncio.wait_for(
                llm.chat(built, temperature=temperature), timeout=timeout
            )
            return _text_of(response)
        finally:
            await release_llm_client(llm)

    def call(messages: list[dict]) -> str:
        return asyncio.run(_run(messages))

    return call


def _selftest() -> int:
    problems: list[str] = []
    src = (
        "二〇一六年二月十四日，星期日。这个周日大概会是我一个人度过的第二十八个情人节了。"
        "但最终，我还是去排队了。45% 的人说那天要加班，我信了半天。\n\n"
        "《房思琪的初恋乐园》我读了三遍。她说：“我也想要快乐。”我把这句话抄在纸上。\n\n"
        "就这样吧，日子还是一天一天过。"
    )
    # 段落切分
    if len(split_paragraphs(src)) != 3:
        problems.append(f"split_paragraphs={len(split_paragraphs(src))}，want 3")
    single = "第一段。\n第二段。\n第三段。"
    if len(split_paragraphs(single)) != 3:
        problems.append("单换行分段没认出来")
    # 保护项
    tokens = protected_tokens(src)
    if tokens["数字"]["45"] != 1 or "《房思琪的初恋乐园》" not in tokens["书名"]:
        problems.append("保护项没抓全")
    # 改一个字就红
    broken = src.replace("45%", "百分之四十五").replace("《房思琪的初恋乐园》", "那本书")
    if not any("数字被改掉" in p for p in guard(src, broken)):
        problems.append("改掉数字没被发现")
    if not any("书名被改掉" in p for p in guard(src, broken)):
        problems.append("改掉书名没被发现")
    # 原样返回：不该被拦（输入可能已经很像），但要出提示
    if guard(src, src):
        problems.append(f"原样返回被拦了：{guard(src, src)}")
    if not warnings_of(src, src):
        problems.append("原样返回没出「一字未改」提示")
    # 整篇重写：不再按「距离」拦（他成稿自己就改了 67%），但要出提示；
    # 丢保护项/账本、并段这些仍会红——那是账与结构，不是距离。
    rewritten = "。".join(["今天发生了很多事情"] * 8) + "。"
    if not any("重写" in w for w in warnings_of(src, rewritten)):
        problems.append("整篇重写没出提示")
    distance = ("字数变化", "等于重写", "8 字片段", "句子只改了")
    if any(word in p for p in guard(src, rewritten) for word in distance):
        problems.append(f"整篇重写还在按距离拦：{guard(src, rewritten)}")
    # 提示词要带纪律原文与诊断项
    diag = {
        "style": {
            "rows": [
                {"attribute_name": "句长与分句密度", "ok": False, "deviation": 3.0,
                 "detail": "obs=40.0 base=32.0 tol=±3.0"},
                {"attribute_name": "标点谱", "ok": False, "deviation": 2.0,
                 "detail": "、: 2.21 vs 0.94 (±0.94)"},
                {"attribute_name": "没问题", "ok": True, "deviation": 0, "detail": ""},
            ]
        }
    }
    messages = build_messages(src, diag)
    joined = "\n".join(m["content"] for m in messages)
    for want in ("一个字都不许动", "句长与分句密度", "偏多", "原稿如下"):
        if want not in joined:
            problems.append(f"提示词缺「{want}」")
    polish_joined = "\n".join(m["content"] for m in build_messages(src, diag, "polish"))
    if "改稿纪律" not in polish_joined:
        problems.append("润色模式没带整节改稿纪律")
    if "没问题" in joined:
        problems.append("通过了属性也进了提示词")
    # 具体账被删：拦（「三遍」这种单字数词也算账本）
    if not any("具体账被删" in p for p in guard(src, src.replace("三遍", "几遍"))):
        problems.append("删掉「三遍」没被发现")
    # 把话说死：拦
    hardened = src.replace("大概会是我一个人度过的", "一定会是我一个人度过的")
    if not any("话说得更死了" in p for p in guard(src, hardened)):
        problems.append("把「大概」改成「一定」没被发现")
    # 守门的正常路径：改两成到六成的句子应通过（实测第四轮改 35–47%）
    ok = src.replace("但最终，我还是去排队了。", "但最后，我还是去排了那个队。").replace(
        "就这样吧，日子还是一天一天过。", "好吧，日子还是一天一天地过。"
    )
    if guard(src, ok):
        problems.append(f"正常小改被拦：{guard(src, ok)}")
    # 润色模式：只改一句应该放行（作者自己的稿子不该被逼着重写）
    small_edit = src.replace("我信了半天", "我当真了半天")
    if guard(src, small_edit):
        problems.append(f"润色模式的正常小改被拦：{guard(src, small_edit)}")
    # 留门：末两句有缓和/让步才算
    if not door(src) or door("今天天气很好。"):
        problems.append(f"留门判错：src={door(src)} 无留门={door('今天天气很好。')}")
    # 多跑比决定：措辞变了但五个决定一样 → 算稳；说死的话变多 → 算漂
    same = [{"text": src.replace("但最终", "但最后"), "problems": []},
            {"text": src.replace("我把这句话抄在纸上。", "我把这句话抄了下来。"), "problems": []}]
    got = consensus(src, same)
    if not got["stable"] or got["drift"]:
        problems.append(f"两版决定一样却判漂：{got['drift']}")
    if got["source_decisions"]["说死的话"] != len(_ASSERT.findall(src)):
        problems.append("原稿那一行没跟着原稿走")
    drift = [{"text": src, "problems": []},
             {"text": src.replace("大概会是我一个人度过的", "一定会是我一个人度过的"),
              "problems": []}]
    got = consensus(src, drift)
    if got["stable"] or "说死的话" not in got["drift"]:
        problems.append(f"「说死的话」变了却没判漂：{got['drift']}")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print("[OK] selftest: 分段、保护项、账本、说死的话、距离类只提示、提示词、正常小改、"
          "留门、多跑比决定都过")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="按作者腔调改稿（只改语言）")
    ap.add_argument("--file", type=Path, help="待改的稿子")
    ap.add_argument("--out", type=Path, help="改后写哪（默认 <file>.v2.md）")
    ap.add_argument("--json", dest="json_out", type=Path, help="机器可读报告")
    ap.add_argument("--dry-run", action="store_true", help="0 元：只出诊断与提示词")
    ap.add_argument("--polish", action="store_true",
                    help="润色模式：作者自己的稿子只修拗口，不要改动率下限（上限压到 35%）")
    ap.add_argument("--modernize", action="store_true",
                    help="翻新老稿：他本人若干年前的文字，保留事实、把腔换成他现在的"
                         "（温度与改动率下限按推一把走；只加标点会被打回）")
    ap.add_argument("--push", action="store_true",
                    help="推一把：稿子明显不熟时要求实打实改，改动率下限提到 35%")
    ap.add_argument("--push-min", type=float, default=0.30, help="--push 时的改动率下限")
    ap.add_argument("--examples", type=int, default=3,
                    help="提示词里放几段他现在的腔当锚（默认 3；0 = 不放）")
    ap.add_argument("--retries", type=int, default=None,
                    help="守门不过时重试几次（默认 1；--modernize 默认 2，因为翻新改动大、容易漏事实）")
    ap.add_argument("--auto", action="store_true",
                    help="先判一次（几分钱）：判官说 not-like 就自动 --push")
    ap.add_argument("--consensus", type=int, default=1,
                    help="跑几版先比决定（账 / 说死 / 拆段 / 困惑 / 留门）再挑一版；"
                         "决定在漂就把其他版也留下（每版约一分钱）")
    ap.add_argument("--provider", default=DEFAULT_PROVIDER)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--temperature", type=float, default=None,
                    help=f"默认 {DEFAULT_TEMPERATURE}；--push 时默认 {PUSH_TEMPERATURE}")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.file:
        ap.error("要 --file")
    source = args.file.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.md"
        probe.write_text(source, encoding="utf-8")
        diag = diagnose(probe)
    before = diag.get("style")
    if not before:
        # 稿子太短（<200 汉字）或 profile 缺失时，style_lint 不给分。以前这里直接 KeyError 崩掉。
        raise SystemExit(
            f"[FAIL] 诊断没出属性分，先别改：{diag.get('style_error') or '原因不明'}"
        )
    print(
        f"诊断：{before['score']} 分，过 {before['passed']}/{before['total']}，"
        f"出界 {len([r for r in before['rows'] if not r['ok']])} 条"
    )
    mode = "polish" if args.polish else ("modernize" if args.modernize else "voice")
    verdict = "skip"
    push = args.push
    # 翻新老稿也要真动手：温度与改动率下限按「推一把」走（但提示词用翻新那套，不用 push 那段）
    heated = push or args.modernize
    if args.auto and not args.polish:
        verdict = judge_verdict(args.file)
        push = push or verdict != "like"
        heated = heated or push
        print(f"判官：{verdict} → {'推一把' if push else '不推'}")
    if args.dry_run:
        messages = build_messages(source, diag, mode, push=push)
        for message in messages:
            print(f"\n===== {message['role']} =====\n{message['content']}")
        return 0

    call = _llm_callable(
        args.provider, args.model,
        args.temperature if args.temperature is not None
        else (PUSH_TEMPERATURE if heated else DEFAULT_TEMPERATURE),
        args.timeout,
    )
    min_change = 0.0 if args.polish else None
    max_change = 0.35 if args.polish else None
    if heated and not args.polish:
        min_change = args.push_min
    retries = args.retries if args.retries is not None else (2 if args.modernize else 1)
    runs = max(1, args.consensus)
    results = [
        rewrite(source, diag, call, retries=retries, min_change=min_change,
                max_change=max_change, push=push, mode=mode,
                examples=args.examples)
        for _ in range(runs)
    ]
    result = consensus(source, results) if len(results) > 1 else results[0]
    out_path = args.out or args.file.with_suffix(".v2.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result["text"].strip() + "\n", encoding="utf-8", newline="\n")
    # 决定在漂时不挑完就算：其他版也留下正文，人才能看出差在哪。
    kept: list[str] = []
    if len(results) > 1 and not result.get("stable", True):
        for index, other in enumerate(results, 1):
            if index == result.get("chosen"):
                continue
            extra = out_path.with_name(f"{out_path.stem}.c{index}{out_path.suffix}")
            extra.write_text(other["text"].strip() + "\n", encoding="utf-8", newline="\n")
            kept.append(str(extra))
    report = {
        "file": str(args.file),
        "out": str(out_path),
        "model": args.model,
        "mode": mode,
        "before": {"score": before["score"], "passed": before["passed"], "total": before["total"]},
        "metrics": result["metrics"],
        "score": {"before": before["score"], "after": result.get("score")},
        "attempts": result["attempts"],
        "warnings": result.get("warnings", []),
        "presence": result.get("presence", {}),
        "problems": result.get("problems", []),
        # 三栏同时进 JSON：机器读红，人读灰和黄。
        "facts": {"problems": result.get("problems", [])},
        "readings": {
            "metrics": result["metrics"],
            "score": {"before": before["score"], "after": result.get("score")},
            "warnings": result.get("warnings", []),
        },
        "human": {
            "presence_question": bool(result.get("presence", {}).get("question")),
            "material_missing": bool(result.get("material", {}).get("missing")),
            "judgments": ["判断的方向和谓语", "情绪与吐槽", "像不像他", "能不能发"],
        },
        "consensus": {
            "runs": runs,
            "chosen": result.get("chosen"),
            "candidates": result.get("candidates"),
            "source": result.get("source_decisions"),
            "decisions": result.get("decisions"),
            "drift": result.get("drift"),
            "stable": result.get("stable"),
            "kept": kept,
        },
    }
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
    m = result["metrics"]
    if runs > 1:
        print(f"跑了 {runs} 版，比 5 个决定（账 / 说死 / 拆段 / 困惑 / 留门）：")
        print("  原稿  " + "  ".join(f"{k}={v}" for k, v in result["source_decisions"].items()))
        for index, decisions in enumerate(result["decisions"], 1):
            picked = "（选）" if index == result["chosen"] else ""
            shown = "  ".join(f"{k}={v}" for k, v in decisions.items())
            print(f"  第 {index} 版{picked} {shown}")
        print(f"选第 {result['chosen']} 版：机械分（账 / 说死 / 拆段），不看文笔")
        if result["stable"]:
            print("[OK] 决定都稳定：两版差的只是措辞")
        else:
            print(f"[WARN] 决定在漂：{'、'.join(result['drift'])} —— 不自动拼，人拍板")
    # 报告按「谁负责」分三栏（2026-11-10 用户拍板）：
    # 红＝机器定的事实；灰＝只是读数；黄＝只有作者能判。
    print("\n== 红：事实（机器定，你确认）==")
    if result.get("problems"):
        for problem in result["problems"]:
            print(f"  ✗ {problem}")
    else:
        print("  ✓ 账、结构、有没有真改：都过")

    print("\n== 灰：读数（只当参考，不当判决）==")
    print(
        f"  · 句子改 {m['sentence_change']:.0%}，8 字片段保留 {m['shingle_keep_8']:.0%}，"
        f"字数 {m['length_ratio'] - 1:+.0%}，段数 {m['paragraphs_src']}→{m['paragraphs_out']}"
    )
    landed = result.get("score")
    if landed is not None:
        moved = landed - before["score"]
        print(
            f"  · 落点 {before['score']:.1f} → {landed:.1f}（{moved:+.1f}；加权分）"
        )
    for warning in result.get("warnings", []):
        print(f"  · {warning}")

    print("\n== 黄：待你定（只有你能判）==")
    if result.get("material", {}).get("missing"):
        print("  · 素材：原稿里一处数得出来的东西都没有——改语言补不了，得你补素材")
    if result.get("presence", {}).get("question"):
        print("  · 第 4 格：他在场（原稿里有第一人称困惑）——改后有没有加戏，你看")
    else:
        print("  · 第 4 格：他不在场（原稿没有一个他想不通的问题）——先补素材更靠谱")
    print("  · 判断的方向和谓语有没有被拧（幸福→认了这种）")
    print("  · 情绪、吐槽有没有被删（哟西、坑爹死了）")
    print("  · 像不像他、能不能发——放行由你拍板")
    if review_due():
        print(f"  · 规矩该复查了（{REVIEW_BY} 到期）：登记表逐条看出生案例还在不在，该退的退")

    if result.get("problems"):
        print(f"\n[FAIL] 守门没过：{'；'.join(result['problems'])}", file=sys.stderr)
        print(f"（稿子已写到 {out_path}，别直接用）", file=sys.stderr)
        return 1
    print(f"\n[OK] 写到 {out_path}")
    for path in kept:
        print(f"[OK] 另一版没混进来，单独留着：{path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
