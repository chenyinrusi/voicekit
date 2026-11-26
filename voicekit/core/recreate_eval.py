"""再创作验收（FIX-356）：按包里的风格数字写稿，再判「像不像」+ 命中/抄袭/长度检查。

为什么单独一个脚本：`author_pack.py eval` 考的是「判官分得清真假作者」，考不到
「照这个包写出来的稿像不像」。本脚本补这一层，并且带一个**无文风对照**：同一骨架、
同一长度要求，不给风格数字。只有「有文风 > 无文风」才算判官在测风格，否则数字无效。

生成输入只用 profile.json 的数字 + 2 段**不在判官样例里**的语料片段（v2 起，段前标题已删）：
不告诉作者名字，也不给判官用的那 10 篇样例；抄超过 40 字会被抄袭检查拦住。
证据里存每篇的生成提示指纹与渲染后的 style/anchors，避免「中心结论不可自证」。

用法：
    python scripts/voice/recreate_eval.py --pack tmp/panchen-pack --pack tmp/luxun-pack \
        --out dev/fixes/FIX-356-recreation-acceptance/evidence/recreate-v1.json
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 自己的目录不够：src/backend import 需要仓库根（父子两级）；不依赖兄弟模块的 sys.path 副作用
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import exemplar_judge as ej  # noqa: E402
from . import judge_lib as jl  # noqa: E402
from . import pack_lib as pk  # noqa: E402
from . import style_distill as sd  # noqa: E402

from .recreate_prompt import (  # noqa: F401 - 拆分后保持旧导入名（测试/调用方用 re.xxx）
    ANCHOR_MODES,
    COPY_VIOLATION_LCS,
    _anchor_paths,
    _format_anchors,
    build_generation_prompt,
    build_revision_prompt,
    cjk_count,
    render_anchors,
    render_style,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BRIEFS = REPO_ROOT / "scripts/voice/fixtures/recreate/briefs.json"

#: 验收线（写进 DESIGN，跑之前定死）。
STYLED_SAME_MIN = 7
CONTROL_GAP_MIN = 3
FIDELITY_MIN = 9
CJK_MIN, CJK_MAX = 300, 1500


#: 生成参数（FIX-358 r1 P2-3：写进证据，不只写在代码里）。
GEN_TEMPERATURE = 0.8
#: 与喂给模型的全文样例逐字重合的上限（r1 P2-4：比全语料 40 字更严，抄样例 = 内容混淆）。
ANCHOR_COPY_VIOLATION = 20

#: 蒸馏规则卡的缓存（tmp 不入库；同一批样例只蒸馏一次）。
DISTILL_CACHE = REPO_ROOT / "tmp/style_distill"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_brief(row) -> dict:
    if not isinstance(row, dict) or not row.get("id") or not row.get("brief") or not row.get("must_include"):
        raise ValueError(f"brief needs id/brief/must_include: {row!r}")
    return row


def load_briefs(path: Path = BRIEFS) -> list[dict] | dict[str, list[dict]]:
    """默认 10 条共用骨架；`briefs-author.json` 是按作者 slug 分的典型题材骨架（FIX-358 R2）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        grouped = {str(slug): [_validate_brief(row) for row in rows] for slug, rows in data.items()}
        if not grouped or any(not rows for rows in grouped.values()):
            raise ValueError(f"briefs must not be empty: {path}")
        return grouped
    if not isinstance(data, list) or not data:
        raise ValueError("briefs.json must be a non-empty list")
    return [_validate_brief(row) for row in data]




def content_hits(text: str, must_include: list[str]) -> dict:
    hits = {token: token in text for token in must_include}
    return {"hits": hits, "passed": all(hits.values())}


def max_copy_length(text: str, corpus: Path) -> tuple[int, str]:
    """与该作者语料的最长公共片段：抄了原句会明显变长（阈值 40 字）。"""
    best, best_name = 0, ""
    for path in sorted(corpus.glob("*.md")):
        body = ej.clean_body(path.read_text(encoding="utf-8"))
        match = difflib.SequenceMatcher(None, text, body, autojunk=False).find_longest_match()
        if match.size > best:
            best, best_name = match.size, path.name
    return best, best_name


def max_copy_vs_docs(text: str, paths: list[Path]) -> int:
    """与喂进去的样例逐字重合的最长片段（r1 P2-4：抄样例不等于学笔性）。"""
    best = 0
    for path in paths:
        body = ej.clean_body(path.read_text(encoding="utf-8"))
        match = difflib.SequenceMatcher(None, text, body, autojunk=False).find_longest_match()
        best = max(best, match.size)
    return best


async def _generate(llm, prompt: str, timeout: float) -> tuple[str, dict]:
    from ..compat import Message

    response = await asyncio.wait_for(
        llm.chat([Message.user(prompt)], temperature=GEN_TEMPERATURE), timeout=timeout
    )
    return str(getattr(response, "content", "") or ""), dict(getattr(response, "usage", None) or {})


async def _generate_sized(
    llm,
    prompt: str,
    timeout: float,
    *,
    min_cjk: int | None = None,
    max_cjk: int | None = None,
    retries: int = 0,
) -> tuple[str, dict, list[int]]:
    """按汉字数要求重试生成（r1 P1-1：两条件长度要配对，否则 gap 可能只是长度差）。"""
    total: dict = {}
    trace: list[int] = []
    body = ""
    for _ in range(max(0, retries) + 1):
        text, usage = await _generate(llm, prompt, timeout)
        body = ej.clean_body(text)
        trace.append(cjk_count(body))
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
        if min_cjk is None and max_cjk is None:
            break
        if (min_cjk is None or trace[-1] >= min_cjk) and (max_cjk is None or trace[-1] <= max_cjk):
            break
    return body, total, trace


def _verify_judge_set(pack: dict, set_obj: dict, judge_model: str) -> None:
    """验收路径先过 judge.py 同款三道 guard（r1 P2-2：冻结判官不能只写在文档里）。"""
    bound_pack = pack if set_obj.get("pack") else None
    problems = ej.verify_set(set_obj, pack["corpus"], pack=bound_pack)
    if problems:
        raise jl.JudgeError("exemplar set unusable: " + "; ".join(problems[:3]))
    if bound_pack is not None:
        bound = ej.check_set_bound_pack(set_obj, bound_pack)
        if bound:
            raise jl.JudgeError(bound)
    incompatible = ej.check_set_compatible(set_obj, judge_model, require_measured=True)
    if incompatible:
        raise jl.JudgeError(incompatible)


async def _judge_twice(
    text: str, set_obj: dict, llm, model: str, corpus: Path, repeat: int
) -> tuple[list[str], str]:
    answers = []
    reason = ""
    for _ in range(repeat):
        payload = await ej.judge_text(text, set_obj, llm, model, corpus, use_cache=False)
        answers.append(str(payload.get("verdict") or "error"))
        if not reason:
            reason = str(payload.get("reason") or "")
    return answers, reason


async def write_draft(
    pack_dir: Path | str,
    brief_text: str,
    *,
    must_include: list[str] | tuple[str, ...] = (),
    gen_llm,
    gen_model: str,
    judge_llm=None,
    judge_model: str = "",
    out: Path | str | None = None,
    judge: bool = True,
    force: bool = False,
    timeout: float = 180.0,
    anchor_mode: str = "short",
    revision: bool = False,
    distill: bool = False,
    distill_model: str | None = None,
    distill_cache: Path | str | None = None,
    min_cjk: int | None = None,
    max_cjk: int | None = None,
    gen_retries: int = 1,
) -> dict:
    """一键入口的实现：读包 → 按风格数字写 → 落盘 → 自判（判官与 judge.py --pack 同一层）。

    判前跑和 `judge.py --pack` 同样的三道 guard（r1 P2-7），换模型/未实测的包不会被静默放行。
    """
    pack = pk.load_pack(Path(pack_dir))
    profile = json.loads(pack["profile"].read_text(encoding="utf-8"))
    set_obj = json.loads(pack["exemplar"].read_text(encoding="utf-8"))
    _verify_judge_set(pack, set_obj, judge_model)
    exemplar_names = {str(doc.get("name")) for doc in (set_obj.get("docs") or []) if isinstance(doc, dict)}
    holdout_names = {str(doc.get("name")) for doc in (set_obj.get("holdout") or []) if isinstance(doc, dict)}
    anchor_paths = _anchor_paths(
        pack["corpus"], exemplar_names, mode=anchor_mode, exclude=holdout_names
    )
    anchors = _format_anchors(anchor_paths, mode=anchor_mode)
    marker_card = ""
    distill_info: dict | None = None
    if distill:
        cache = Path(distill_cache or DISTILL_CACHE) / (
            f"{pack['slug']}-{sd.prompt_sha256(anchors)[:12]}.json"
        )
        distill_info = await sd.load_or_distill(
            anchors, cache, gen_llm, distill_model or gen_model, timeout=timeout
        )
        marker_card = sd.render_marker_card(distill_info["markers"])
    style = render_style(profile)
    prompt = build_generation_prompt(
        brief_text, style, list(must_include), anchors=anchors, markers=marker_card
    )
    body, usage, cjk_trace = await _generate_sized(
        gen_llm,
        prompt,
        timeout,
        min_cjk=min_cjk,
        max_cjk=max_cjk,
        retries=gen_retries,
    )
    if not cjk_count(body):
        raise ValueError("模型没有返回中文正文（检查 provider/model 配置）")
    result: dict = {
        "pack": pack["slug"],
        "author": pack["author"],
        "gen_model": gen_model,
        "cjk": cjk_count(body),
        "cjk_trace": cjk_trace,
        "usage": usage,
        "file": None,
        "anchor_mode": anchor_mode,
        "anchor_docs": [path.name for path in anchor_paths],
        "revision": revision,
        "markers": marker_card,
        "distill": (
            {
                key: distill_info[key]
                for key in ("model", "prompt_sha256", "prompt_chars", "cache_hit", "markers")
                if key in distill_info
            }
            if distill_info
            else None
        ),
        "generation_prompt_sha256": _sha256(prompt),
        "anchor_copy_lcs": max_copy_vs_docs(body, anchor_paths) if anchor_paths else 0,
    }
    result["anchor_copy_violation"] = result["anchor_copy_lcs"] >= ANCHOR_COPY_VIOLATION
    if revision:
        draft_sha = _sha256(body)
        revision_prompt = build_revision_prompt(body, anchors)
        body, revision_usage, revision_trace = await _generate_sized(
            gen_llm,
            revision_prompt,
            timeout,
            min_cjk=min_cjk,
            max_cjk=max_cjk,
            retries=gen_retries,
        )
        if not cjk_count(body):
            raise ValueError("改写遍没有返回中文正文（检查 provider/model 配置）")
        result.update(
            {
                "cjk": cjk_count(body),
                "revision_cjk_trace": revision_trace,
                "draft_sha256": draft_sha,
                "revision_prompt_sha256": _sha256(revision_prompt),
                "revision_usage": revision_usage,
                "anchor_copy_lcs": max_copy_vs_docs(body, anchor_paths) if anchor_paths else 0,
            }
        )
        result["anchor_copy_violation"] = result["anchor_copy_lcs"] >= ANCHOR_COPY_VIOLATION
    if out is not None:
        target = Path(out)
        if target.exists() and not force:
            raise jl.JudgeError(f"refusing to overwrite {target} (pass --force to replace)")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8", newline="\n")
        result["file"] = str(target)
    if judge:
        incompatible = ej.check_set_compatible(set_obj, judge_model, require_measured=True)
        if incompatible:
            raise jl.JudgeError(incompatible)
        payload = await ej.judge_text(
            body, set_obj, judge_llm, judge_model, pack["corpus"], use_cache=True, timeout=timeout
        )
        decision = ej.decide(payload, judge_model)
        try:
            deterministic = jl.run_deterministic(body, profile)
        except (KeyError, ValueError, TypeError):
            deterministic = None
        combined = jl.gate(decision, deterministic)
        result.update(
            {
                "judge": decision,
                "judge_model": judge_model,
                "deterministic": deterministic,
                "verdict": combined["verdict"],
                "verdict_reason": combined["reason"],
            }
        )
    return result


def format_write_report(result: dict) -> str:
    lines = [
        f"== 写稿（{result['author']} / {result['pack']}，生成 {result['gen_model']}）",
        f"稿：{result['file'] or '（未落盘）'}，{result['cjk']} 汉字",
    ]
    if result.get("anchor_mode") == "full" or result.get("revision") or result.get("distill"):
        lines.append(
            "样例：{}（{} 篇）{}".format(
                "全文" if result.get("anchor_mode") == "full" else "短样例",
                len(result.get("anchor_docs") or []),
                (" + 蒸馏卡" if result.get("distill") else "")
                + (" + 改写遍" if result.get("revision") else ""),
            )
        )
    if result.get("judge") is None:
        lines.append("判定：未自判（--no-judge）")
        return "\n".join(lines)
    judge = result["judge"]
    label = {"like": "像", "not-like": "不像"}.get(judge["verdict"], judge["verdict"])
    lines.append(f"判定：{label}（judge {judge['answer']}，{result['judge_model']}）")
    lines.append(f"理由：{judge['reason']}")
    det = result.get("deterministic") or {}
    if det:
        lines.append(
            "确定性层（只作诊断）：{}/100，通过线 {}，判定 {}（core_dev {} / 上限 {}）".format(
                det.get("score"), det.get("pass_score"),
                "通过" if det.get("passed") else "未过",
                det.get("core_dev"), det.get("core_limit"),
            )
        )
    return "\n".join(lines)


async def run(
    packs: list[Path],
    *,
    gen_llm,
    gen_model: str,
    judge_llm,
    judge_model: str,
    repeat: int = 2,
    concurrency: int = 3,
    timeout: float = 180.0,
    briefs: list[dict] | dict[str, list[dict]] | None = None,
    briefs_path: Path | None = None,
    anchor_mode: str = "short",
    anchor_count: int | None = None,
    anchor_chars: int | None = None,
    revision: bool = False,
    distill: bool = False,
    distill_model: str | None = None,
    distill_cache: Path | str | None = None,
    min_cjk: int | None = None,
    max_cjk: int | None = None,
    gen_retries: int = 1,
) -> tuple[dict, dict]:
    briefs_path = briefs_path or BRIEFS
    given_briefs = briefs is not None
    briefs = briefs or load_briefs(briefs_path)
    sem = asyncio.Semaphore(concurrency)
    authors: dict[str, dict] = {}
    total_usage: dict[str, int] = {}

    for pack_dir in packs:
        pack = pk.load_pack(pack_dir)
        profile = json.loads(pack["profile"].read_text(encoding="utf-8"))
        set_obj = json.loads(pack["exemplar"].read_text(encoding="utf-8"))
        _verify_judge_set(pack, set_obj, judge_model)
        pack_briefs = briefs.get(pack["slug"]) if isinstance(briefs, dict) else briefs
        if not pack_briefs:
            raise jl.JudgeError(f"no briefs for pack: {pack['slug']}")
        style = render_style(profile)
        exemplar_names = {
            str(doc.get("name")) for doc in (set_obj.get("docs") or []) if isinstance(doc, dict)
        }
        holdout_names = {
            str(doc.get("name")) for doc in (set_obj.get("holdout") or []) if isinstance(doc, dict)
        }
        anchor_paths = _anchor_paths(
            pack["corpus"],
            exemplar_names,
            mode=anchor_mode,
            count=anchor_count,
            exclude=holdout_names,
        )
        anchors = _format_anchors(anchor_paths, mode=anchor_mode, max_chars=anchor_chars)
        marker_card = ""
        distill_info: dict | None = None
        if distill:
            cache = Path(distill_cache or DISTILL_CACHE) / (
                f"{pack['slug']}-{sd.prompt_sha256(anchors)[:12]}.json"
            )
            distill_info = await sd.load_or_distill(
                anchors, cache, gen_llm, distill_model or gen_model, timeout=timeout
            )
            marker_card = sd.render_marker_card(distill_info["markers"])
        rows: list[dict] = []

        async def one(
            brief: dict,
            condition: str,
            *,
            _style=style,
            _anchors=anchors,
            _marker_card=marker_card,
            _set_obj=set_obj,
            _pack=pack,
            _anchor_paths=anchor_paths,
            _profile=profile,
        ) -> dict:
            async with sem:
                prompt = build_generation_prompt(
                    brief,
                    _style if condition == "styled" else None,
                    anchors=_anchors if condition == "styled" else None,
                    markers=_marker_card if condition == "styled" else None,
                )
                body, usage, cjk_trace = await _generate_sized(
                    gen_llm,
                    prompt,
                    timeout,
                    min_cjk=min_cjk,
                    max_cjk=max_cjk,
                    retries=gen_retries,
                )
                extra: dict = {"cjk_trace": cjk_trace}
                if revision and condition == "styled" and _anchors:
                    extra["draft_sha256"] = _sha256(body)
                    revision_prompt = build_revision_prompt(body, _anchors)
                    body, revision_usage, revision_trace = await _generate_sized(
                        gen_llm,
                        revision_prompt,
                        timeout,
                        min_cjk=min_cjk,
                        max_cjk=max_cjk,
                        retries=gen_retries,
                    )
                    extra["revision_prompt_sha256"] = _sha256(revision_prompt)
                    extra["revision_usage"] = revision_usage
                    extra["revision_cjk_trace"] = revision_trace
                answers, reason = await _judge_twice(
                    body, _set_obj, judge_llm, judge_model, _pack["corpus"], repeat
                )
                copy_len, copy_src = max_copy_length(body, _pack["corpus"])
                anchor_copy = max_copy_vs_docs(body, _anchor_paths) if condition == "styled" else 0
                # 确定性层只作诊断：profile 缺字段也不许把验收跑挂（真 profile 是齐的）。
                try:
                    det = jl.run_deterministic(body, _profile)
                except (KeyError, ValueError, TypeError):
                    det = None
                return {
                    "brief": brief["id"],
                    "condition": condition,
                    "generation_prompt_sha256": _sha256(prompt),
                    "generation_prompt_chars": len(prompt),
                    "cjk": cjk_count(body),
                    "sha256": _sha256(body),
                    "answers": answers,
                    "answer": answers[0],
                    "same": answers[0] == "same",
                    "reason": reason,
                    "content": content_hits(body, brief["must_include"]),
                    "copy_lcs": copy_len,
                    "copy_source": copy_src,
                    "copy_violation": copy_len >= COPY_VIOLATION_LCS,
                    "anchor_copy_lcs": anchor_copy,
                    "anchor_copy_violation": anchor_copy >= ANCHOR_COPY_VIOLATION,
                    "deterministic_score": (det or {}).get("score"),
                    "generation_usage": usage,
                    **extra,
                    "text": body,
                }

        tasks = [one(brief, cond) for brief in pack_briefs for cond in ("styled", "control")]
        for row in await asyncio.gather(*tasks):
            rows.append(row)
            for usage_key in ("generation_usage", "revision_usage"):
                for key, value in (row.get(usage_key) or {}).items():
                    if isinstance(value, int):
                        total_usage[key] = total_usage.get(key, 0) + value

        styled = [row for row in rows if row["condition"] == "styled"]
        control = [row for row in rows if row["condition"] == "control"]
        authors[pack["slug"]] = {
            "author": pack["author"],
            "pack": str(pack["dir"]),
            "profile_sha256": _sha256(pack["profile"].read_text(encoding="utf-8")),
            "exemplar_sha256": _sha256(pack["exemplar"].read_text(encoding="utf-8")),
            "judge_set": {
                "set_sha256": set_obj.get("set_sha256"),
                "prompt_version": set_obj.get("prompt_version"),
                "measured": set_obj.get("measured"),
                "docs": [
                    {"name": doc.get("name"), "sha256": doc.get("sha256")}
                    for doc in (set_obj.get("docs") or [])
                ],
                "holdout": [
                    {"name": doc.get("name"), "sha256": doc.get("sha256")}
                    for doc in (set_obj.get("holdout") or [])
                ],
            },
            "style": style,
            "style_sha256": _sha256(style),
            "anchors": anchors,
            "anchor_mode": anchor_mode,
            "anchor_docs": [path.name for path in anchor_paths],
            "revision": revision,
            "markers": marker_card,
            "distill": (
                {
                    key: distill_info[key]
                    for key in ("model", "prompt_sha256", "prompt_chars", "cache_hit", "markers")
                    if key in distill_info
                }
                if distill_info
                else None
            ),
            "briefs": [brief["id"] for brief in pack_briefs],
            "styled": {
                "n": len(styled),
                "same": sum(1 for row in styled if row["same"]),
                "errors": sum(1 for row in styled if "error" in row["answers"]),
                "flips": [row["brief"] for row in styled if len(set(row["answers"])) > 1],
                "fidelity": sum(1 for row in styled if row["content"]["passed"]),
                "copy_violations": sum(1 for row in styled if row["copy_violation"]),
                "max_copy_lcs": max((row["copy_lcs"] for row in styled), default=0),
                "anchor_copy_violations": sum(1 for row in styled if row["anchor_copy_violation"]),
                "max_anchor_copy_lcs": max((row["anchor_copy_lcs"] for row in styled), default=0),
                "mean_cjk": round(sum(row["cjk"] for row in styled) / max(len(styled), 1), 1),
                "mean_det": round(
                    sum((row["deterministic_score"] or 0) for row in styled) / max(len(styled), 1), 2
                ),
            },
            "control": {
                "n": len(control),
                "same": sum(1 for row in control if row["same"]),
                "errors": sum(1 for row in control if "error" in row["answers"]),
                "flips": [row["brief"] for row in control if len(set(row["answers"])) > 1],
                "fidelity": sum(1 for row in control if row["content"]["passed"]),
                "copy_violations": sum(1 for row in control if row["copy_violation"]),
                "max_copy_lcs": max((row["copy_lcs"] for row in control), default=0),
                "mean_cjk": round(sum(row["cjk"] for row in control) / max(len(control), 1), 1),
                "mean_det": round(
                    sum((row["deterministic_score"] or 0) for row in control) / max(len(control), 1), 2
                ),
            },
            "paired": [
                {
                    "brief": row["brief"],
                    "styled": next(r["answer"] for r in styled if r["brief"] == row["brief"]),
                    "control": next(r["answer"] for r in control if r["brief"] == row["brief"]),
                }
                for row in styled
            ],
        }
        authors[pack["slug"]]["docs"] = rows

    payload = {
        "protocol": "recreate-acceptance",
        "generator": "pack profile numbers -> 中文随笔",
        "gen_model": gen_model,
        "anchor_mode": anchor_mode,
        "anchor_count": anchor_count,
        "anchor_chars": anchor_chars,
        "revision": revision,
        "distill": distill,
        "distill_model": distill_model,
        "min_cjk": min_cjk,
        "max_cjk": max_cjk,
        "gen_retries": gen_retries,
        "gen_temperature": GEN_TEMPERATURE,
        "anchor_copy_limit": ANCHOR_COPY_VIOLATION,
        "judge_model": judge_model,
        "judge_protocol": ej.PROTOCOL,
        "prompt_version": ej.PROMPT_VERSION,
        "judge_prompt_sha256": ej.prompt_sha256(),
        "repeat": repeat,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "briefs_sha256": (
            _sha256(json.dumps(briefs, ensure_ascii=False, sort_keys=True))
            if given_briefs
            else _sha256(briefs_path.read_text(encoding="utf-8"))
        ),
        "authors": authors,
        "usage": total_usage,
    }
    failures = check_targets(payload)
    payload["failures"] = failures
    return payload, {"failures": failures}


def _min_same(n: int) -> int:
    """验收线按比例算：10 篇要 ≥7；5 篇（R2 作者典型骨架）要 ≥4。"""
    return max(1, -(-STYLED_SAME_MIN * n // 10))


def _min_fidelity(n: int) -> int:
    """保真线按比例算：10 篇要 ≥9；5 篇（R2 作者典型骨架）要 ≥5。"""
    return max(1, -(-FIDELITY_MIN * n // 10))


def check_targets(payload: dict) -> list[str]:
    failures: list[str] = []
    for slug, block in payload["authors"].items():
        styled, control = block["styled"], block["control"]
        if styled["n"] != control["n"] or styled["n"] == 0:
            failures.append(f"{slug}: styled/control n mismatch")
        if styled["same"] < _min_same(styled["n"]):
            failures.append(
                f"{slug}: styled same {styled['same']}/{styled['n']} < {_min_same(styled['n'])}"
            )
        if styled["fidelity"] < _min_fidelity(styled["n"]):
            failures.append(
                f"{slug}: styled fidelity {styled['fidelity']}/{styled['n']} < {_min_fidelity(styled['n'])}"
            )
        if styled["copy_violations"]:
            failures.append(f"{slug}: {styled['copy_violations']} styled doc(s) copy corpus >= {COPY_VIOLATION_LCS} chars")
        if styled.get("anchor_copy_violations"):
            failures.append(
                f"{slug}: {styled['anchor_copy_violations']} styled doc(s) copy the exemplars "
                f">= {ANCHOR_COPY_VIOLATION} chars (content mimicry, not style)"
            )
        if styled["errors"] or control["errors"]:
            failures.append(f"{slug}: judgement parse errors")
        if styled["flips"] or control["flips"]:
            failures.append(
                f"{slug}: judgement flips (styled {styled['flips']}, control {control['flips']})"
            )
        gap = styled["same"] - control["same"]
        if gap < CONTROL_GAP_MIN:
            failures.append(
                f"{slug}: styled-vs-control gap {gap} < {CONTROL_GAP_MIN} "
                f"(styled {styled['same']}, control {control['same']})"
            )
        out_of_range = [
            row["brief"]
            for row in block["docs"]
            if not (CJK_MIN <= row["cjk"] <= CJK_MAX)
        ]
        if out_of_range:
            failures.append(f"{slug}: {len(out_of_range)} doc(s) outside {CJK_MIN}-{CJK_MAX} CJK: {out_of_range[:3]}")
    return failures


def _fmt(payload: dict) -> str:
    extra = ""
    if payload.get("anchor_mode") == "full":
        extra = "全文样例"
    if payload.get("revision"):
        extra = (extra + " + 改写遍").strip()
    lines = [
        f"== 再创作验收（生成 {payload['gen_model']}{'，' + extra if extra else ''}，"
        f"判官 {payload['judge_model']}，{payload['repeat']} 遍）",
    ]
    for slug, block in payload["authors"].items():
        styled, control = block["styled"], block["control"]
        lines.append(
            f"{block['author']}（{slug}）：有文风 same {styled['same']}/{styled['n']}，"
            f"无文风对照 {control['same']}/{control['n']}，保真 {styled['fidelity']}/{styled['n']}，"
            f"最长抄句 {styled['max_copy_lcs']} 字，平均 {styled['mean_cjk']} 字，"
            f"确定性层均分 {styled['mean_det']} vs 对照 {control['mean_det']}"
        )
        lines.append(f"  两遍抖动：有文风 {len(styled['flips'])}，对照 {len(control['flips'])}")
    return "\n".join(lines)


async def _run_cli(args) -> int:
    from ..compat import release_llm_client
    from ..compat import create_llm, ensure_provider_sdks_loaded

    await ensure_provider_sdks_loaded()
    gen = create_llm(args.gen_provider, args.gen_model)
    judge = create_llm(args.judge_provider, args.judge_model)
    try:
        payload, result = await run(
            [Path(pack) for pack in args.pack],
            gen_llm=gen,
            gen_model=getattr(gen, "model", args.gen_model),
            judge_llm=judge,
            judge_model=getattr(judge, "model", args.judge_model),
            repeat=args.repeat,
            concurrency=args.concurrency,
            timeout=args.timeout,
            anchor_mode=args.anchor_mode,
            anchor_count=args.anchor_count,
            anchor_chars=args.anchor_chars,
            revision=args.revision,
            distill=args.distill,
            distill_model=args.distill_model,
            distill_cache=args.distill_cache,
            min_cjk=args.min_cjk,
            max_cjk=args.max_cjk,
            gen_retries=args.gen_retries,
            briefs_path=Path(args.briefs) if args.briefs else None,
        )
    finally:
        await release_llm_client(gen)
        await release_llm_client(judge)
    print(_fmt(payload))
    for failure in result["failures"]:
        print(f"[FAIL] {failure}", file=sys.stderr)
    if not result["failures"]:
        print("== 达标")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0 if not result["failures"] else 1


def cli_write(args) -> int:
    """一键入口：骨架稿 → 按包写 → 落盘 → 自判。0 像 / 1 不像 / 2 错误。"""
    pack = pk.load_pack(Path(args.pack))
    brief_path = Path(args.brief)
    if not brief_path.is_file():
        print(f"[FAIL] brief not found: {brief_path}", file=sys.stderr)
        return 2
    try:
        brief = brief_path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        print(f"[FAIL] brief is not UTF-8: {brief_path}", file=sys.stderr)
        return 2
    if not brief:
        print(f"[FAIL] brief is empty: {brief_path}", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else None
    if out is not None and out.resolve() == brief_path.resolve():
        print("[FAIL] --out must differ from --brief (would overwrite the brief)", file=sys.stderr)
        return 2
    if out is not None and out.exists() and not getattr(args, "force", False):
        print(f"[FAIL] --out exists: {out} (pass --force to replace)", file=sys.stderr)
        return 2
    if not pack["exemplar"].is_file():
        print(f"[FAIL] exemplar set missing: {pack['exemplar']}", file=sys.stderr)
        return 2
    must = [token.strip() for token in (args.must_include or "").split(",") if token.strip()]

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        try:
            gen = create_llm(args.provider, args.model)
        except (ValueError, RuntimeError) as exc:
            raise jl.JudgeError(f"setup error: {exc}") from exc
        judge = None
        try:
            judge_model = ""
            if not args.no_judge:
                judge = create_llm(args.judge_provider, args.judge_model)
                judge_model = getattr(judge, "model", args.judge_model or args.judge_provider)
            return await write_draft(
                pack["dir"],
                brief,
                must_include=must,
                gen_llm=gen,
                gen_model=getattr(gen, "model", args.model or args.provider),
                judge_llm=judge,
                judge_model=judge_model,
                out=out,
                judge=not args.no_judge,
                force=getattr(args, "force", False),
                timeout=args.timeout,
                anchor_mode=getattr(args, "exemplars", "short"),
                revision=getattr(args, "revision", False),
                distill=getattr(args, "distill", False),
                distill_model=getattr(args, "distill_model", None),
                distill_cache=getattr(args, "distill_cache", None),
                min_cjk=getattr(args, "min_cjk", None),
                max_cjk=getattr(args, "max_cjk", None),
            )
        finally:
            await release_llm_client(gen)
            if judge is not None:
                await release_llm_client(judge)

    try:
        result = asyncio.run(_run())
    except jl.JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 环境错误不冒充判定
        print(f"[FAIL] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(format_write_report(result))
    if result.get("verdict") is None:
        return 0
    return 0 if result["verdict"] == "like" else 1


def main(argv: list[str] | None = None) -> int:
    jl._utf8_stdio()
    parser = argparse.ArgumentParser(description="再创作验收：照包里的风格数字写稿并判定")
    parser.add_argument("--pack", action="append", required=True, help="作者包目录（可多次）")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--gen-provider", default="openai")
    parser.add_argument("--gen-model", default="gpt-4o-mini")
    parser.add_argument("--judge-provider", default=jl.DEFAULT_PROVIDER)
    parser.add_argument("--judge-model", default=jl.DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=2, help="同一篇稿判几遍（查抖动）")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--anchor-mode",
        choices=list(ANCHOR_MODES),
        default="short",
        help="风格样例形式：short = 2×160 字；full = 4 篇整文（FIX-358）",
    )
    parser.add_argument("--anchor-count", type=int, default=None, help="样例篇数（默认按模式）")
    parser.add_argument(
        "--anchor-chars", type=int, default=None, help="每篇例文字数上限；0 = 整文（full 默认）"
    )
    parser.add_argument("--revision", action="store_true", help="有文风组加一遍改写（FIX-358）")
    parser.add_argument(
        "--distill",
        action="store_true",
        help="先用样例蒸馏「独有笔法」规则卡，再生成（FIX-358 第二轮）",
    )
    parser.add_argument("--distill-model", default=None, help="蒸馏用模型（默认同生成模型）")
    parser.add_argument("--distill-cache", default=None, help="规则卡缓存目录（默认 tmp/style_distill）")
    parser.add_argument("--min-cjk", type=int, default=None, help="生成稿汉字数下限（不足重试）")
    parser.add_argument("--max-cjk", type=int, default=None, help="生成稿汉字数上限（超出重试）")
    parser.add_argument("--gen-retries", type=int, default=1, help="长度不达标时重生成几次")
    parser.add_argument(
        "--briefs",
        default=None,
        help="骨架文件（默认 10 条共用；可传按作者分组的 briefs-author.json）",
    )
    args = parser.parse_args(argv)
    if args.repeat < 1 or args.concurrency < 1:
        print("[FAIL] --repeat/--concurrency must be >= 1", file=sys.stderr)
        return 2
    if args.anchor_count is not None and args.anchor_count < 1:
        print("[FAIL] --anchor-count must be >= 1", file=sys.stderr)
        return 2
    if args.anchor_chars is not None and args.anchor_chars < 0:
        print("[FAIL] --anchor-chars must be >= 0", file=sys.stderr)
        return 2
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
