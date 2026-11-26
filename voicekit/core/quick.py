"""冷启动快检：手里还没有作者包时，也要能先量一遍。

只做两件事：① 把 `voice_lib.measure()` 已经算好的通用测量取出来排版；
② 写清楚「没有作者基线」。不打分、不给通过线、不判像不像谁——
分数只有在和某个作者的语料校准之后才有意义（见 `voice_lib.compare()` 的说明）。
"""

from __future__ import annotations

import re

from . import voice_lib as lib
from .build_profile import MIN_CJK

#: 汉字口径和 `voicekit.report.han_len()` 一致（含扩展 A），否则同一个词两个模块判出两套数。
HAN_RE = re.compile(r"[\u3400-\u9fff]")

#: 低于这个汉字数，数字还在抖。这是 `voicekit build` 的抽样下限（`MIN_CJK`），
#: 不是 `style_lint.LOW_CONFIDENCE_CJK`（那个是 500，管的是「分数先别信」）。
WARN_CJK = MIN_CJK

#: 非正文行占比超过它，就提醒「这份文件不全是正文」。
WARN_NON_PROSE = 0.25

NO_BASELINE = "无作者基线"


def share_non_prose(text: str) -> float:
    """这份文件里有多少行不是正文（标题/表格/引用/列表/加粗小标题）。

    和 `voicekit.report.record_share()` 同一口径；放这里是为了让 cold-start 也用它。
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return 0.0
    off = sum(
        1
        for ln in lines
        if ln.startswith(("#", "|", ">", "-", "*", "1.", "2.", "3."))
        or (ln.startswith("**") and ln.endswith("**"))
        or len(HAN_RE.findall(ln)) < 8
    )
    return off / len(lines)


def report(text: str, path: str = "<stdin>") -> dict:
    """量一遍，返回可直接打印/输出的字典；不写文件、不联网。"""
    raw = lib.measure(text)
    doc = raw["_doc"]
    long_share = raw["L2-01"]["share_31_50"] + raw["L2-01"]["share_gt50"]
    data = {
        "path": path,
        "no_author_baseline": True,
        "diagnostic_only": True,
        "measurements": {
            "cjk_chars": doc["cjk_chars"],
            "paragraphs": doc["paragraphs"],
            "sentences": doc["sentences"],
            "mean_sentence": raw["L2-01"]["mean"],
            "median_sentence": raw["L2-01"]["median"],
            "clause_mean": raw["L3-05"]["clause_mean"],
            "short_sentence_share": raw["L2-08"]["short_share"],
            "share_le10": raw["L2-01"]["share_le10"],
            "share_over30": round(long_share, 4),
            "function_per_k": raw["L1-09"],
        },
        "warnings": [],
    }
    if doc["cjk_chars"] < WARN_CJK:
        data["warnings"].append(f"样本偏小（不足 {WARN_CJK} 汉字），数字还在抖")
    non_prose = share_non_prose(text)
    if non_prose >= WARN_NON_PROSE:
        data["warnings"].append(f"这份文件 {non_prose:.0%} 的行是标题/表格/记录，不是一篇正文")
    return data


def render(data: dict) -> str:
    m = data["measurements"]
    fk = m["function_per_k"]
    lines = [
        f"== quick: {data['path']}（{NO_BASELINE}）",
        f"汉字 {m['cjk_chars']} · 段 {m['paragraphs']} · 句 {m['sentences']}",
        f"平均句长 {m['mean_sentence']:g} 字 · 中位 {m['median_sentence']:g} 字 · "
        f"逗号串均值 {m['clause_mean']:g}",
        f"极短句 {m['short_sentence_share']:.0%} · ≤10 字句 {m['share_le10']:.0%} · "
        f">30 字句 {m['share_over30']:.0%}",
        "虚词千字："
        + " · ".join(
            f"{label} {fk[key]:g}"
            for key, label in (("de", "的"), ("shi", "是"), ("le", "了"), ("bu", "不"), ("zai", "在"))
        ),
    ]
    lines += [f"[提示] {w}" for w in data["warnings"]]
    lines.append(
        "— 没有作者基线：这些只是量出来的特征，不判好坏，也不判像不像谁。"
        "要和某个作者比：voicekit lint <稿> --pack <作者包>"
    )
    return "\n".join(lines)
