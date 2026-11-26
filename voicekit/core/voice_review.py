"""把两层诊断合成一份能照着改的审稿报告。

输入一篇稿；输出 markdown：42 条确定性属性逐条打分 + 21 维 rubric 逐维 band 与证据，
再按影响排一份整改清单。只诊断，不放行——放行仍由 `judge.py` 的 10 篇样例判定。

用法（repo 根目录）：
    python scripts/voice/voice_review.py --file 稿.md                    # 报告打到 stdout
    python scripts/voice/voice_review.py --file 稿.md --out tmp/r.md      # 写文件
    python scripts/voice/voice_review.py --file 稿.md --no-llm            # 只跑本地层，0 元
    python scripts/voice/voice_review.py --file 稿.md --json              # 机器读
    python scripts/voice/voice_review.py --dir 书稿目录 --out-dir out/    # 整本逐篇 + index.md
    python scripts/voice/voice_review.py --dir 书稿目录 --out-dir out/ --limit 5   # 先跑 5 篇试水

确定性层调 `style_lint.py`，rubric 层调 `judge.py --protocol rubric`；两层各只有一份实现。
`--dir` 模式按篇写 `<篇名>.md` + `<篇名>.json`，末尾写 `index.md`；已存在的报告默认跳过，
可断点续跑，`--force` 重跑。
退出码：0 报告已出 / 2 输入或环境错。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_PROFILE = REPO_ROOT / "voices" / "panchen-voice" / "profile.json"
DEFAULT_BASELINE = REPO_ROOT / "voices" / "panchen-voice" / "judge_baseline.json"
DEFAULT_RUBRICS = SCRIPT_DIR / "rubrics.yaml"

#: 整改清单最多列几条：多到十条就不是清单，是第二张表。
MAX_FIXES = 8
#: 两层各占几条：只列维度会看不见确定性层的出界项，只列属性会漏掉结构问题。
FIXES_PER_LAYER = 4
#: 表格里的证据句/评语截断长度（原话完整保留在 --json 里）。
QUOTE_CHARS = 26
WHY_CHARS = 46
ANCHOR_CHARS = 50
#: rubric 层在短篇上 band 波动大：报告要写明别把单篇 verdict 当放行结论。
SHORT_DOC_CJK = 2000

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SCALAR_RE = re.compile(r"obs=([-\d.eE+]+) base=([-\d.eE+]+) tol=±?([-\d.eE+]+)")
_PAIR_RE = re.compile(r"([^\s=]+)=([-\d.eE+]+)/([-\d.eE+]+)")
#: 多子项出界时的 detail：`1 subkey(s) out: praise_per_k: 2.83 vs 0.44 (±0.44)`。
_OUT_RE = re.compile(r"([\w\u4e00-\u9fff]+):\s*([-\d.eE+]+)\s+vs\s+([-\d.eE+]+)")


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：中文写进管道会崩。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _num(value: float) -> str:
    """按数量级取 3 位有效数字：76.88 与 0.007 都能读。"""
    return f"{value:.3g}"


def _run_json(script: str, args: list[str]) -> tuple[dict | None, str]:
    """跑子脚本拿 JSON。非 0 退出码也可能是正常判定（not-like=1），只认 stdout。"""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    out = (proc.stdout or "").strip()
    if not out.startswith("{"):
        err = (proc.stderr or "").strip().splitlines()
        return None, err[0] if err else f"exit {proc.returncode}"
    try:
        return json.loads(out), ""
    except json.JSONDecodeError:
        return None, "unparsable JSON from " + script


def _pairs(detail: str) -> list[tuple[str, float, float]]:
    pairs = [(k, float(o), float(b)) for k, o, b in _PAIR_RE.findall(detail)]
    pairs += [(k, float(o), float(b)) for k, o, b in _OUT_RE.findall(detail)]
    return pairs


def _direction(row: dict) -> str:
    """偏差方向：偏高/偏低。整改建议要带方向，否则「别堆」配「偏低」会自相矛盾。"""
    scalar = _SCALAR_RE.search(row.get("detail", ""))
    if scalar:
        obs, base = float(scalar.group(1)), float(scalar.group(2))
    else:
        pairs = _worst(_pairs(row.get("detail", "")))
        if not pairs:
            return ""
        _, obs, base = pairs[0]
    if abs(obs - base) < 1e-12:
        return ""
    return "偏高" if obs > base else "偏低"


def _worst(
    pairs: list[tuple[str, float, float]], count: int = 2
) -> list[tuple[str, float, float]]:
    """按相对偏差排前几项；基线为 0 的子项用绝对差排。"""

    def score(item: tuple[str, float, float]) -> float:
        _, obs, base = item
        return abs(obs - base) / base if base else abs(obs - base)

    return sorted(pairs, key=score, reverse=True)[:count]


def _read(row: dict) -> tuple[str, str]:
    """从 detail 里抽「你的值 / 作者值」，表格用。"""
    scalar = _SCALAR_RE.search(row.get("detail", ""))
    if scalar:
        return _num(float(scalar.group(1))), _num(float(scalar.group(2)))
    pairs = _worst(_pairs(row.get("detail", "")))
    if pairs:
        return _num(pairs[0][1]), _num(pairs[0][2])
    return "—", "—"


def _comment(row: dict) -> str:
    """每条属性一句评语：差在哪一项、差多少。过线的也说，过线不等于没偏差。"""
    detail = row.get("detail", "")
    scalar = _SCALAR_RE.search(detail)
    if scalar:
        obs, base, tol = (float(scalar.group(i)) for i in (1, 2, 3))
        delta = obs - base
        if row.get("ok"):
            return f"接近（差 {delta:+.3g}，容差 ±{_num(tol)}）"
        side = "偏高" if delta > 0 else "偏低"
        return f"{side}：你 {_num(obs)}，作者 {_num(base)}"
    pairs = _worst(_pairs(detail))
    if not pairs:
        return detail[:WHY_CHARS] or "—"
    shown = "、".join(f"「{k}」{_num(o)}/{_num(b)}" for k, o, b in pairs)
    return ("差最多 " if not row.get("ok") else "稳，差最多 ") + shown


def _rubric_rows(rubric: dict | None) -> list[dict]:
    if not rubric:
        return []
    return list((rubric.get("judge") or {}).get("dims") or [])


def _band_anchor(rubric: dict | None, dim_id: str, band: int) -> str:
    for dim in (rubric or {}).get("_rubrics", {}).get("dims", []):
        if str(dim.get("id")) == str(dim_id):
            return str((dim.get("anchors") or {}).get(str(band), "")).strip()
    return ""


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


#: 段落功能编码册：规则初判，混合段按主导记一条（报告里也印这句）。
ROLE_STORY = "讲事"
ROLE_MORAL = "讲理"
ROLE_ADDRESS = "喊话"
ROLE_ORDER = (ROLE_STORY, ROLE_MORAL, ROLE_ADDRESS)

_SECOND_PERSON = ("你", "你们", "读者", "各位")
_ADDRESS_VERBS = ("希望", "不妨", "加油", "请", "可以看看")  # 「记得」不作祈使：他常写「我记得」
_STORY_MARKERS = (
    "前些天",
    "最近",
    "今天",
    "昨天",
    "早晨",
    "早上",
    "那时",
    "现在",
    "第一次",
    "几个月前",
    "有一天",
    "后来",
    "当时",
    "去年",
    "今年",
    "夏天",
    "冬天",
)
_STORY_ACTIONS = (
    "买",
    "站",
    "走",
    "看",
    "拉",
    "练",
    "排",
    "滑",
    "学",
    "弄",
    "涂",
    "听",
    "做",
    "吃",
    "喝",
    "画",
    "说",
)
_MORAL_TOKENS = (
    "便是",
    "就是",
    "所谓",
    "道理",
    "人生",
    "成长",
    "意思",
    "值得",
    "应该",
    "或许",
    "也许",
    "如果",
    "所以",
    "因为",
    "而且",
    "其实",
    "不过",
    "但是",
    "并非",
    "结果",
    "借口",
    "挑战",
    "稳定",
    "责任",
    "真实",
    "成功",
    "变化",
    "不仅",
    "而是",
)

#: 可疑句四类：编码册同源，报告里逐条附原句，只提示不给分。
#: 2026-11-08：把「总之 / 所谓 / 正是 / 总会」移出触发词——它们是作者自己的常用连接词，
#: 不是口号或教程腔；「同学」也不算二称（叙述里「老同学请我吃饭」不是对读者说话）。
#: 二称与口号只在引号外匹配：书名、引文里的「你」不算（`_outside_quotes`）。
SUSPECT_KINDS = ("喊口号", "教程口气", "对读者喊话", "格言当道理")
_SLOGAN_WORDS = ("一定", "必须", "绝对", "毫无疑问", "必然", "永远")
_TUTORIAL_WORDS = (
    "可以看看",
    "不妨看看",
    "也就是说",
    "换句话说",
    "首先",
    "其次",
    "需要知道",
)
_APHORISM_WORDS = ("者得", "以至", "穷极", "必有", "便是如此", "积跬步")
_QUOTE_RE = re.compile(r"[「“\"]([^」”\"]{2,24})[」”\"]")
_CLASSICAL_IN_QUOTE = ("之", "者", "也", "矣", "乎", "焉")
#: 二称 / 口号只在引号外匹配：书名里的「你」（《从你的全世界路过》）不是对读者说话。
_QUOTED_BIT_RE = re.compile(r"[「“\"]([^」”\"]*)[」”\"]|《([^》]*)》")


def _outside_quotes(sentence: str) -> str:
    """去掉引号与书名号里的内容，只留作者自己说的那部分。"""
    return _QUOTED_BIT_RE.sub("", sentence)
#: 一条稿最多列多少条可疑句：再多就不叫「要你看一眼」了。
MAX_SUSPECTS = 20
_SENT_SPLIT = re.compile(r"(?<=[。！？…])")
#: 段末标点：单块按行切段时，上一行没到句末就继续并到同一段（折行）。
#: 收尾的引号/括号也算句末，否则 `他说：“好。”` 会和下一行并成一段。
_SENT_END = re.compile(r"[。！？…」”’）]")


def _paragraphs(text: str) -> list[str]:
    """切段：优先按空行；整篇不空行、靠换行分段的稿（如 034）按行切。

    行切时，短于 30 汉字的行当作折行续到上一段，避免把硬折行当成段。
    只丢 markdown 标题那几行，标题与正文同块时正文要留下。
    """
    def without_headings(raw: str) -> str:
        kept = [ln for ln in raw.splitlines() if not ln.strip().startswith("#")]
        return "\n".join(kept).strip()

    blocks = [cleaned for raw in re.split(r"\n\s*\n", text or "")
              if (cleaned := without_headings(raw))]
    if len(blocks) != 1:
        return [re.sub(r"\s+", "", block) for block in blocks]
    paras: list[str] = []
    for line in (line.strip() for line in blocks[0].splitlines()):
        line = re.sub(r"\s+", "", line)
        if not line:
            continue
        if paras and not _SENT_END.match(paras[-1][-1]):
            paras[-1] += line
        else:
            paras.append(line)
    return paras


def _role(para: str) -> str:
    """段落功能：喊话优先，其次按讲事的动作量对讲理的判断词量比大小。"""
    plain = _outside_quotes(para)
    addressed = any(w in plain for w in _SECOND_PERSON) and any(
        v in plain for v in _ADDRESS_VERBS
    )
    if addressed:
        return ROLE_ADDRESS
    story = (
        2 * sum(1 for m in _STORY_MARKERS if m in para)
        + sum(1 for v in _STORY_ACTIONS if v in para)
        + ("我" in para)
    )
    moral = sum(1 for t in _MORAL_TOKENS if t in para)
    return ROLE_STORY if story > moral else ROLE_MORAL


def _sentences(text: str) -> list[str]:
    return [
        re.sub(r"\s+", "", s)
        for s in _SENT_SPLIT.split(text or "")
        if s.strip()
    ]


def _suspect_kind(sentence: str) -> str:
    """四类可疑句，顺序即优先级：教程 > 喊话 > 口号 > 格言。"""
    plain = _outside_quotes(sentence)
    if any(w in plain for w in _TUTORIAL_WORDS):
        return "教程口气"
    if any(w in plain for w in _SECOND_PERSON) and any(
        v in plain for v in _ADDRESS_VERBS
    ):
        return "对读者喊话"
    if any(w in plain for w in _SLOGAN_WORDS):
        return "喊口号"
    if any(w in sentence for w in _APHORISM_WORDS):
        return "格言当道理"
    for quoted in _QUOTE_RE.findall(sentence):
        if any(w in quoted for w in _CLASSICAL_IN_QUOTE):
            return "格言当道理"
    return ""


def _suspects(text: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        if len(sentence) < 8 or sentence in seen:
            continue
        kind = _suspect_kind(sentence)
        if not kind:
            continue
        seen.add(sentence)
        out.append({"kind": kind, "sentence": sentence})
        if len(out) >= MAX_SUSPECTS:
            break
    return out


def text_shape(text: str) -> dict:
    """报告前两节的原料：每段在干什么 + 要你看一眼的句子。全规则，可重算。"""
    paras = _paragraphs(text)
    roles = [_role(para) for para in paras]
    suspects = _suspects(text)
    return {
        "paragraphs": [
            {
                "index": i,
                "chars": len(_CJK_RE.findall(para)),
                "role": role,
                "head": para[:16],
            }
            for i, (para, role) in enumerate(zip(paras, roles, strict=False), 1)
        ],
        "counts": {role: roles.count(role) for role in ROLE_ORDER},
        "suspects": suspects,
        "suspect_counts": {
            kind: sum(1 for s in suspects if s["kind"] == kind)
            for kind in SUSPECT_KINDS
        },
    }


def _fixes(review: dict) -> list[dict]:
    """整改清单：两层各取几条。核心轴与关键维先，再按偏差大小。"""
    attr_items: list[dict] = []
    style = review.get("style")
    core = set((style or {}).get("core_failed") or [])

    if style:
        rows = [r for r in (style.get("rows") or []) if not r.get("ok")]
        rows.sort(
            key=lambda r: (
                r["attribute_id"] not in core,
                -float(r.get("deviation") or 0),
            )
        )
        for row in rows:
            you, base = _read(row)
            direction = _direction(row)
            evidence = f"你 {you}，作者 {base}"
            attr_items.append(
                {
                    "layer": "属性",
                    "id": row["attribute_id"],
                    "name": row["attribute_name"],
                    "evidence": f"{evidence}（{direction}）" if direction else evidence,
                    # 不印 generation_guidance：它是写作口径（作者怎么做），
                    # 方向可能与本篇偏差相反（如 L8-02 作者 0，口径却写「写出来」）。
                    "advice": "",
                }
            )

    rubric = review.get("rubric") or {}
    keys = {str(k) for k in (rubric.get("_rubrics") or {}).get("key_dims") or []}
    dim_items: list[dict] = []
    for dim in _rubric_rows(review.get("rubric")):
        if dim.get("passed"):
            continue
        dim_items.append(
            {
                "layer": "维度",
                "id": str(dim["id"]),
                "name": dim["name"],
                "evidence": f"band {dim['band']}，基线 {dim['baseline']}，线 {dim['floor']}；"
                + _trim(str(dim.get("quote", "")).rstrip("。！？…"), QUOTE_CHARS),
                "advice": f"目标是：{_trim(_band_anchor(review.get('rubric'), dim['id'], 4), ANCHOR_CHARS)}",
                "key": str(dim["id"]) in keys,
            }
        )
    dim_items.sort(key=lambda item: not item["key"])

    return (dim_items[:FIXES_PER_LAYER] + attr_items[:FIXES_PER_LAYER])[:MAX_FIXES]


def render(review: dict) -> str:
    style = review.get("style") or {}
    rubric = review.get("rubric") or {}
    judge = rubric.get("judge") or {}
    profile = review.get("profile") or {}
    attrs = {a["attribute_id"]: a for a in profile.get("attributes", [])}
    lines: list[str] = []

    lines.append(f"# 文风审稿报告：{Path(review['file']).name}")
    lines.append("")
    lines.append(f"- 字数：{review['chars']} 个汉字（{review['file']}）")
    if style:
        lines.append(
            f"- 与作者语料接近度：{style['score']} / 100（相对量，不是文风分），"
            f"42 条里过 {style['passed']}/{style['total']}，"
            f"核心轴偏差 {style['core_dev']}（上限 {style['core_limit']}）"
        )
    else:
        lines.append(
            "- 与作者语料接近度：未跑（" + (review.get("style_error") or "no profile") + "）"
        )
    if judge:
        lines.append(
            f"- 判官层（只当线索，不给结论）：{judge.get('verdict')}，过 {judge.get('passed_count')}/"
            f"{len(judge.get('dims') or [])} 维（要求 {judge.get('required')}）"
            + (
                f"；关键维失败 {','.join(judge['key_failed'])}"
                if judge.get("key_failed")
                else ""
            )
        )
        if rubric.get("cached"):
            usage = rubric.get("usage") or {}
            lines.append(
                "- 判官调用：命中缓存；token 入 {} / 出 {}（缓存命中 {}）".format(
                    usage.get("prompt", "?"),
                    usage.get("completion", "?"),
                    usage.get("cache_read", 0),
                )
            )
        elif rubric.get("usage"):
            usage = rubric["usage"]
            lines.append(
                "- 判官调用：token 入 {} / 出 {}".format(
                    usage.get("prompt", "?"), usage.get("completion", "?")
                )
            )
        samples = rubric.get("samples") or {}
        if samples:
            lines.append(
                "- 多次采样：{} 次，过线数 {}，band 取逐维中位".format(
                    samples.get("count"),
                    "/".join(str(n) for n in samples.get("passed") or []),
                )
            )
    else:
        reason = review.get("rubric_error")
        lines.append("- 判官层：未跑" + (f"（{reason}）" if reason else ""))

    fixes = _fixes(review)
    failed_attrs = [r for r in (style.get("rows") or []) if not r.get("ok")]
    headline = f"{len(failed_attrs)} 条属性出界"
    if judge:
        headline += f"，{len((judge.get('failed') or []))} 个维度没过线（只当线索）"
    one_line = "先看前两节：段落在干什么、要你看一眼的句子"
    if fixes:
        one_line += "；属性层先改 " + "、".join(item["name"] for item in fixes[:3])
    lines.append(f"- 一句话：{one_line}（{headline}）")
    if judge and review["chars"] < SHORT_DOC_CJK:
        lines.append(
            f"- 注意：本篇 {review['chars']} 字，偏短；rubric band 在短篇上波动大（实测可漂 3 维），"
            "模型判的「像不像」只当线索，不当结论。"
        )
    lines.append("")

    shape = review.get("shape") or {}
    counts = shape.get("counts") or {}
    paras = shape.get("paragraphs") or []
    suspects = shape.get("suspects") or []
    suspect_counts = shape.get("suspect_counts") or {}
    lines.append("## 一、每段在干什么（规则初判，混合段按主导记一条）")
    lines.append("")
    lines.append(
        "讲事＝讲了一件具体的事；讲理＝从事情里总结道理；喊话＝转头对读者说话。"
        "这段标签是规则给的，你不同意可以直接改。"
    )
    lines.append("")
    lines.append(
        f"- 讲事 {counts.get(ROLE_STORY, 0)} 段 / 讲理 {counts.get(ROLE_MORAL, 0)} 段 / "
        f"喊话 {counts.get(ROLE_ADDRESS, 0)} 段（共 {len(paras)} 段）"
    )
    if paras:
        lines.append("")
        lines.append("| 段 | 字 | 在干什么 | 开头 |")
        lines.append("|---|---|---|---|")
        for item in paras:
            lines.append(
                f"| {item['index']} | {item['chars']} | {item['role']} | "
                f"{_trim(item['head'], 20)} |"
            )
    lines.append("")
    lines.append("## 二、要你看一眼的句子（规则初判，不打分）")
    lines.append("")
    lines.append(
        "四类：喊口号（说死了、替人下结论）／教程口气（把读者当学生）／"
        "对读者喊话（对「你」下指令）／格言当道理（引古话撑场面）。"
    )
    lines.append("")
    lines.append(
        "- 判定句 "
        + " / ".join(f"{kind} {suspect_counts.get(kind, 0)} 条" for kind in SUSPECT_KINDS)
        + f"（共 {len(suspects)} 条）"
    )
    if suspects:
        lines.append("")
        for index, item in enumerate(suspects, 1):
            lines.append(f"{index}. 〔{item['kind']}〕{item['sentence']}")
    else:
        lines.append("")
        lines.append("（规则没找到这四类句子。）")
    lines.append("")

    if style:
        rows_by_id = {r["attribute_id"]: r for r in style.get("rows") or []}
        categories: list[str] = []
        for attr in profile.get("attributes", []):
            category = str(attr.get("category") or "其他")
            if category not in categories:
                categories.append(category)
        lines.append("## 三、42 条属性逐条打分（与作者语料的接近度，相对量）")
        lines.append("")
        for category in categories:
            members = [
                a
                for a in profile.get("attributes", [])
                if str(a.get("category") or "其他") == category
                and a["attribute_id"] in rows_by_id
            ]
            if not members:
                continue
            lines.append(f"### {category}")
            lines.append("")
            lines.append("| 属性 | 你 | 作者 | 判定 | 评语 |")
            lines.append("|---|---|---|---|---|")
            for attr in members:
                row = rows_by_id[attr["attribute_id"]]
                you, base = _read(row)
                mark = "✅" if row.get("ok") else "❌"
                lines.append(
                    f"| {row['attribute_id']} {row['attribute_name']} | {you} | {base} | {mark} | {_comment(row)} |"
                )
            lines.append("")

    if judge:
        samples = rubric.get("samples") or {}
        bands_by_dim = samples.get("bands") or {}
        lines.append("## 四、21 维 rubric（只当线索）")
        lines.append("")
        head = "| 维度 | band / 基线 | 过线阈值 | 证据句 | 评语 |"
        rule = "|---|---|---|---|---|"
        if bands_by_dim:
            head = "| 维度 | band / 基线 | 过线阈值 | 几次 band | 证据句 | 评语 |"
            rule = "|---|---|---|---|---|---|"
        lines.append(head)
        lines.append(rule)
        for dim in judge.get("dims") or []:
            mark = "✅" if dim.get("passed") else "❌"
            why = _trim(dim.get("why") or "", WHY_CHARS) or "—"
            spread = ""
            bands = bands_by_dim.get(str(dim["id"]))
            if bands:
                spread = "| " + "/".join(str(b) for b in bands) + " "
            lines.append(
                f"| {mark} {dim['id']} {dim['name']} | {dim['band']} / {dim['baseline']} | "
                f"{dim['floor']} {spread}| {_trim(dim.get('quote', ''), QUOTE_CHARS)} | {why} |"
            )
        excluded = judge.get("excluded") or []
        if excluded:
            lines.append("")
            lines.append(
                "未计入："
                + "、".join(f"{d['id']} {d['name']}（{d['reason']}）" for d in excluded)
            )
        clues = sorted(
            (dim for dim in judge.get("dims") or [] if not dim.get("passed")),
            key=lambda dim: int(dim.get("band") or 0),
        )[:3]
        if clues:
            lines.append("")
            lines.append("模型标的可疑位置（原句，不打分）：")
            for dim in clues:
                lines.append(
                    f"- 〔{dim['id']} {dim['name']}〕"
                    f"{_trim(str(dim.get('quote', '')), QUOTE_CHARS)}"
                )
        lines.append("")

    if fixes:
        lines.append("## 五、整改清单")
        lines.append("")
        for index, item in enumerate(fixes, 1):
            advice = item["advice"]
            lines.append(
                f"{index}. **{item['id']} {item['name']}**（{item['layer']}）："
                f"{item['evidence']}。" + advice
            )
        lines.append("")

    lines.append("---")
    lines.append(
        "方法：确定性层比对作者语料基线（42 条）；rubric 层由大模型给 band 并抄原文证据（21 维）。"
        "两层都只诊断，不放行；放行看 `python scripts/voice/judge.py` 的 10 篇样例判定。"
    )
    return "\n".join(lines) + "\n"


def _merge_sample_rows(runs: list[dict]) -> tuple[list[dict], dict[str, list[int]]]:
    """k 次判官：每维取 band 中位数。同一篇稿单次能漂 ±3 维，中位才稳。

    证据句/评语取「band 等于中位」的那一次，保证评语和 band 出自同一遍。
    """
    by_dim: dict[str, list[dict]] = {}
    for run in runs:
        for row in run.get("rows") or []:
            by_dim.setdefault(str(row["id"]), []).append(row)
    merged: list[dict] = []
    bands_by_dim: dict[str, list[int]] = {}
    for dim_id, rows in by_dim.items():
        valid = [r for r in rows if r.get("evidence_ok") and r.get("band") is not None]
        if not valid:
            merged.append(dict(rows[0]))
            continue
        bands = sorted(int(r["band"]) for r in valid)
        median = bands[len(bands) // 2]
        bands_by_dim[dim_id] = bands
        pick = next((r for r in valid if int(r["band"]) == median), valid[0])
        row = dict(pick)
        row["band"] = median
        row["note"] = ""
        merged.append(row)
    merged.sort(key=lambda r: int(str(r["id"])) if str(r["id"]).isdigit() else 0)
    return merged, bands_by_dim


def _judge_sampled(args) -> tuple[dict | None, str]:
    """跑 k 次 judge.py，合并成一份（每维 band 中位）。"""
    sys.path.insert(0, str(SCRIPT_DIR))
    import judge_lib as jl  # noqa: PLC0415 - 只有多次采样模式要重算判定

    runs: list[dict] = []
    for _ in range(max(2, args.samples)):
        out, err = _run_json(
            "judge.py",
            [
                "--file",
                str(args.file),
                "--protocol",
                "rubric",
                "--json",
                "--no-cache",
                "--profile",
                str(args.profile),
                "--baseline",
                str(args.baseline),
                "--rubrics",
                str(args.rubrics),
                "--provider",
                args.provider,
                "--model",
                args.model,
            ],
        )
        if out is None:
            return None, err
        runs.append(out)
    rubrics = jl.load_rubrics(Path(args.rubrics))
    baseline = jl.load_baseline(Path(args.baseline))
    rows, bands_by_dim = _merge_sample_rows(runs)
    judge = jl.evaluate(rows, baseline, rubrics)
    usage: dict[str, int] = {}
    for run in runs:
        for key, value in (run.get("usage") or {}).items():
            usage[key] = usage.get(key, 0) + int(value or 0)
    return {
        "judge": judge,
        "rows": rows,
        "usage": usage,
        "cached": False,
        "samples": {
            "count": len(runs),
            "passed": [run["judge"]["passed_count"] for run in runs],
            "bands": bands_by_dim,
            "verdicts": [run["judge"]["verdict"] for run in runs],
        },
    }, ""


def build_review(args) -> dict:
    text = Path(args.file).read_text(encoding="utf-8")
    review: dict = {
        "file": str(args.file),
        "chars": len(_CJK_RE.findall(text)),
        "shape": text_shape(text),
        "profile": json.loads(Path(args.profile).read_text(encoding="utf-8")),
    }
    style, err = _run_json(
        "style_lint.py",
        ["--file", str(args.file), "--profile", str(args.profile), "--json"],
    )
    if style is None:
        review["style_error"] = err
    else:
        review["style"] = style
    if args.no_llm:
        return review

    if getattr(args, "samples", 1) and int(args.samples) > 1:
        rubric, err = _judge_sampled(args)
    else:
        rubric, err = _run_json(
            "judge.py",
            [
                "--file",
                str(args.file),
                "--protocol",
                "rubric",
                "--json",
                "--profile",
                str(args.profile),
                "--baseline",
                str(args.baseline),
                "--rubrics",
                str(args.rubrics),
                "--provider",
                args.provider,
                "--model",
                args.model,
            ],
        )
    if rubric is None:
        review["rubric_error"] = err
    else:
        review["rubric"] = rubric
        # evaluate() 的 dims 不带模型的 why，raw rows 带；按 id 合回去，报告才有评语。
        whys = {str(r.get("id")): r.get("why", "") for r in rubric.get("rows") or []}
        for dim in (rubric.get("judge") or {}).get("dims") or []:
            dim.setdefault("why", whys.get(str(dim.get("id")), ""))
        try:
            import yaml  # noqa: PLC0415 - 只有要 anchors 时才需要

            review["rubric"]["_rubrics"] = yaml.safe_load(
                Path(args.rubrics).read_text(encoding="utf-8")
            )
        except Exception:  # noqa: BLE001 - 读不到 anchors 只少了整改建议，不拦报告
            pass
    return review


def _payload(review: dict) -> dict:
    """JSON 落盘：去掉 profile 与 rubrics 原文，只留结果。"""
    payload = dict(review)
    payload.pop("profile", None)
    if payload.get("rubric"):
        payload["rubric"] = {
            k: v for k, v in payload["rubric"].items() if k != "_rubrics"
        }
    return payload


def _summary(payload: dict) -> dict:
    """索引行要的最小字段集。"""
    style = payload.get("style") or {}
    rubric = payload.get("rubric") or {}
    judge = rubric.get("judge") or {}
    failed = [r for r in (style.get("rows") or []) if not r.get("ok")]
    return {
        "name": Path(payload["file"]).name,
        "chars": payload.get("chars", 0),
        "score": style.get("score"),
        "passed": style.get("passed"),
        "total": style.get("total"),
        "like": style.get("like"),
        "verdict": judge.get("verdict") or ("?" if rubric else "未跑"),
        "dims_passed": judge.get("passed_count"),
        "dims_total": len(judge.get("dims") or []) or None,
        "failed_names": [r["attribute_name"] for r in failed],
        "usage": rubric.get("usage") or {},
        "skipped": bool(payload.get("skipped")),
    }


_RANK = {"not-like": 0, "?": 1, "未跑": 1, "like": 2}


def build_index(summaries: list[dict], *, source: str) -> str:
    """全书索引：不像的、分低的排前面。一页能看出先改哪篇。"""
    lines = [f"# 全书文风审稿索引：{source}", ""]
    done = [s for s in summaries if not s["skipped"]]
    not_like = [s for s in done if s["verdict"] == "not-like"]
    scored = [s["score"] for s in done if s.get("score") is not None]
    tokens = sum(int(s["usage"].get("prompt") or 0) for s in done)
    lines.append(
        f"- 篇数：{len(summaries)}（本次跑 {len(done)}，跳过已存在 {len(summaries) - len(done)}）"
    )
    if scored:
        lines.append(
            f"- 属性层：平均 {sum(scored) / len(scored):.1f} 分，最低 {min(scored):.1f}"
        )
    if done:
        lines.append(f"- 判官层：{len(not_like)} 篇 not-like / {len(done)} 篇")
    if tokens:
        lines.append(f"- 判官 token 入：{tokens}（缓存命中另计）")
    lines.append("")
    lines.append("| 篇 | 字 | 属性分 | 过线 | 判官 | 维度过线 | 主要出界 |")
    lines.append("|---|---|---|---|---|---|---|")
    for item in sorted(
        summaries,
        key=lambda s: (
            _RANK.get(s["verdict"], 1),
            s["score"] if s["score"] is not None else 0,
        ),
    ):
        score = "—" if item["score"] is None else f"{item['score']:.1f}"
        passed = f"{item['passed']}/{item['total']}" if item["total"] else "—"
        dims = (
            f"{item['dims_passed']}/{item['dims_total']}"
            if item["dims_passed"] is not None and item["dims_total"]
            else "—"
        )
        failed = "、".join(item["failed_names"][:3]) or "—"
        if item["skipped"]:
            failed = "跳过（报告已存在）"
        lines.append(
            f"| {item['name']} | {item['chars']} | {score} | {passed} | {item['verdict']} | {dims} | {failed} |"
        )
    lines.append("")
    lines.append("逐篇报告与 JSON 在同目录；报告里的整改清单按影响排好，可直接照改。")
    lines.append(
        "判官 verdict 只作诊断（短篇波动大）；放行看 `judge.py` 的 10 篇样例判定。"
    )
    return "\n".join(lines) + "\n"


def run_batch(args) -> int:
    """按目录逐篇出报告：写 `<篇名>.md` + `<篇名>.json`，末尾出 `index.md`。"""
    files = sorted(p for p in args.dir.iterdir() if p.suffix.lower() in {".md", ".txt"})
    if not files:
        print(f"[FAIL] no .md/.txt in {args.dir}", file=sys.stderr)
        return 2
    if args.limit:
        files = files[: args.limit]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict] = []
    for index, path in enumerate(files, 1):
        target = args.out_dir / f"{path.stem}.md"
        cached = target.with_suffix(".json")
        if target.exists() and cached.exists() and not args.force:
            payload = json.loads(cached.read_text(encoding="utf-8"))
            payload["skipped"] = True
            summaries.append(_summary(payload))
            print(f"[{index}/{len(files)}] 跳过 {path.name}（报告已存在）")
            continue
        single = argparse.Namespace(**{**vars(args), "file": path})
        try:
            review = build_review(single)
        except Exception as exc:  # noqa: BLE001 - 单篇坏不拦全书
            print(
                f"[{index}/{len(files)}] [FAIL] {path.name}: {type(exc).__name__}: {exc}"
            )
            continue
        payload = _payload(review)
        target.write_text(render(review), encoding="utf-8")
        cached.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        summary = _summary(payload)
        summaries.append(summary)
        score = "—" if summary["score"] is None else f"{summary['score']:.1f}"
        print(
            f"[{index}/{len(files)}] {path.name}: {score} 分，判官 {summary['verdict']}"
        )
    index_path = args.out_dir / "index.md"
    index_path.write_text(
        build_index(summaries, source=str(args.dir)), encoding="utf-8"
    )
    print(f"索引：{index_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="出文风审稿报告：逐条打分 + 整改清单")
    ap.add_argument("--file", type=Path, help="待审稿")
    ap.add_argument("--dir", type=Path, help="整本书稿目录：逐篇出报告 + index.md")
    ap.add_argument("--out-dir", type=Path, default=None, help="--dir 模式的输出目录")
    ap.add_argument(
        "--limit", type=int, default=0, help="--dir 模式只跑前 N 篇（0=全跑）"
    )
    ap.add_argument("--force", action="store_true", help="--dir 模式重跑已存在的报告")
    ap.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--rubrics", type=Path, default=DEFAULT_RUBRICS)
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--no-llm", action="store_true", help="只跑本地确定性层（0 元）")
    ap.add_argument(
        "--samples",
        type=int,
        default=1,
        help="判官连判几次取逐维 band 中位（>=2 才生效）；单次会漂 ±3 维",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--out", type=Path, default=None, help="--file 模式 markdown 写到文件"
    )
    args = ap.parse_args(argv)

    if not args.profile.is_file():
        print(f"[FAIL] profile not found: {args.profile}", file=sys.stderr)
        return 2
    if args.dir:
        if not args.dir.is_dir():
            print(f"[FAIL] dir not found: {args.dir}", file=sys.stderr)
            return 2
        if not args.out_dir:
            print("[FAIL] --dir 需要 --out-dir", file=sys.stderr)
            return 2
        return run_batch(args)
    if not args.file:
        ap.error("need --file or --dir")
    if not args.file.is_file():
        print(f"[FAIL] file not found: {args.file}", file=sys.stderr)
        return 2

    review = build_review(args)
    if args.json:
        print(json.dumps(_payload(review), ensure_ascii=False, indent=1))
        return 0
    report = render(review)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"报告：{args.out}")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

