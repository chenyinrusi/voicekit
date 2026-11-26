"""小样本裁判：给 10 篇作者样例，判待测稿是不是同一个人（FIX-348）。

判据不是 21 维 rubric，而是「和样例比，像不像这个人的笔性」：
同一个人换体裁（随笔、对话段子、短篇、技术评论）不算换人。

用法（repo 根目录）：
    python scripts/voice/exemplar_judge.py --selftest              # 无网络自检（check 入口）
    python scripts/voice/exemplar_judge.py --eval --repeat 2       # 真模型评测（烧 token）
    python scripts/voice/exemplar_judge.py --eval --write          # 评测通过后刷新基线数字
    python scripts/voice/judge.py --file 稿.md                      # 正式判定（默认走本模块）

退出码：0 达标 / 1 未达标或自检失败 / 2 环境、数据或守卫失败。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from . import build_profile as bp  # noqa: E402
from . import judge_lib as jl  # noqa: E402
from . import pack_lib as pk  # noqa: E402
from .exemplar_measured import (  # noqa: E402
    negatives_digest,
    set_sha256,
    sha256 as _sha256,
    verify_measured,
)
from .exemplar_inputs import (  # noqa: E402,F401 - 再导出给 scripts/tests 用
    _JSON_INSTRUCTION,
    clean_body,
    exemplar_texts,
    render_user_prompt,
)

lib = jl.lib
REPO_ROOT = jl.REPO_ROOT

DEFAULT_SET = REPO_ROOT / "voices/panchen-voice/exemplar_baseline.json"
DEFAULT_CORPUS = jl.DEFAULT_CORPUS
DEFAULT_PROFILE = jl.DEFAULT_PROFILE

#: 盲评留出稿（FIX-350 起为三审书稿里的 12 篇，含 FIX-347 probe-set 里 7 篇的成书版）。
#: 名单钉在代码里而不是运行时读别的证据包：换掉留出集（哪怕是另外 12 篇作者稿）
#: 必须改这里并重跑评测。
HOLDOUT_NAMES = (
    "005__浅喜似苍狗 深爱如长风.md",
    "009__爱情有二十二画 等于十一加上十一.md",
    "012__相亲圈里的乱象.md",
    "013__那些可以婚前讨论的话题.md",
    "018__思人如四月 情深似谎言.md",
    "024__林花谢了春红 太匆匆.md",
    "027__修炼多年 我终于把我自己修成了一个胖叔精.md",
    "028__经济适用男好难.md",
    "039__流水账.md",
    "049__听说矮胖丑穷终会遇见高瘦美富.md",
    "050__过期儿童的自艾自怜.md",
    "054__念如深渊.md",
)

PROTOCOL = "exemplar-same-author"
PROMPT_VERSION = "exemplar-v2"
JUDGE_LOGIC_VERSION = 1
EXEMPLAR_COUNT = 10
HOLDOUT_COUNT = 12
MIN_CJK = 200
VERDICTS = ("same", "different", "unsure")

#: 评测样本量下限：缺一类就是评测没跑全，不许静默通过。
#: **作者池不写死**：它是「语料 − rubric 基线样本」。2026-11-07 剔掉 034/037 之后
#: 写死的 49 反而把测量本身堵死了（2026-11-10：eval 直接报「need author 49」，跑不动）。
MEASURED_SIZES = {
    "author_holdout": HOLDOUT_COUNT,
    "other_author": 46,
    "generic_ai": 2,
}


def expected_sizes(corpus: Path = DEFAULT_CORPUS, baseline: Path | None = None) -> dict:
    """本次评测应有的样本量：作者池从语料现算，别的两类是外部目录的固定数。

    `baseline` 给了就读包里的基线；不给仍读开发树，仓库内脚本不变。
    """
    accepted = bp.load_corpus(corpus)[0]
    found = jl.load_baseline(baseline) if baseline is not None else jl.load_baseline()
    sample = {str(name) for name in (found or {}).get("sample", {}).get("docs", [])}
    return dict(MEASURED_SIZES, author_pool=len(accepted) - len(sample))

GENRE_HINT = (
    "同一个人会写不同体裁：随笔、对话段子、短篇小说、技术评论都可能出现。"
    "体裁不同不等于不是同一个人。"
)

#: v2 追加的负向判据：没有它，模型会把「年代语体」当笔性（FIX-354 实测 2/23 误收）。
NEGATIVE_RULE = (
    "同一位作者换体裁不算换人。但同一时代、同一路数的其他作者不算同一个人："
    "文白夹杂、民国腔、时代感、文体像，都不够。"
    "判 same 之前，要能说出这位作者独有的标记（固定用语、个人化比喻、句法习惯、收尾套路）；"
    "只凭「像那个年代」判 same 是错的。"
    "反过来，若待测文有这位作者反复出现的私人标记，即使题材不同也可以判 same。"
)

SYSTEM_PROMPT = (
    "你是中文文学编辑。下面给你同一位作者（下称「这位作者」）的 10 篇随笔样例。"
    "然后你会读到一篇待测文章，判断它是否也出自这位作者。"
    "待测文章可能出自他，也可能不是，甚至可能是机器生成的。"
    + GENRE_HINT
    + "只看笔性：用词、句法、语气、观察角度、收尾习惯。"
    "不要按「写得好不好」判，标准是「像不像这个人」。"
    + NEGATIVE_RULE
    + "拿不准就判 unsure。只输出 JSON。"
)

def prompt_sha256() -> str:
    """提示正文（含输出格式）的指纹：prompt 一变，实测数字就不能比（FIX-355）。"""
    return _sha256(SYSTEM_PROMPT + "\x00" + _JSON_INSTRUCTION)


def exemplar_names(corpus: Path = DEFAULT_CORPUS, baseline: Path | None = None) -> list[str]:
    """样例 = 裁判基线样本（12 篇）里按文件名排序的前 10 篇，和 pilot 完全一致。

    这条推导是确定的：换一份「另外 10 篇 + 重算 sha」的样例集，名单对不上就拒。
    `baseline` 给了就读那个文件：装成 wheel 时没有 `voices/` 目录，只能读包里的基线。
    """
    found = jl.load_baseline(baseline) if baseline is not None else jl.load_baseline()
    if found is None:
        raise jl.JudgeError("no judge baseline; cannot pick exemplars")
    sample = {str(name) for name in found.get("sample", {}).get("docs", [])}

    accepted = {doc["file"] for doc in bp.load_corpus(corpus)[0]}
    names = [name for name in sorted(accepted) if name in sample]
    if len(names) < EXEMPLAR_COUNT:
        raise jl.JudgeError(
            f"only {len(names)} baseline sample doc(s) in corpus; need {EXEMPLAR_COUNT}"
        )
    return names[:EXEMPLAR_COUNT]


def holdout_docs(corpus: Path = DEFAULT_CORPUS) -> list[dict]:
    """留出 12 篇钉进 baseline：名单来自 `HOLDOUT_NAMES`，运行时不依赖别的证据包。"""
    docs = []
    for name in HOLDOUT_NAMES:
        path = corpus / name
        if not path.is_file():
            raise jl.JudgeError(f"holdout doc missing from corpus: {name!r}")
        body = clean_body(path.read_text(encoding="utf-8"))
        docs.append({"name": name, "sha256": _sha256(body)})
    return docs


def holdout_names(set_obj: dict) -> list[str]:
    docs = set_obj.get("holdout")
    if not isinstance(docs, list) or len(docs) != HOLDOUT_COUNT:
        raise jl.JudgeError(
            f"exemplar baseline must pin {HOLDOUT_COUNT} holdout docs; "
            "re-run exemplar_judge.py --eval --write"
        )
    names = [str(doc.get("name", "")) for doc in docs if isinstance(doc, dict)]
    if names != list(HOLDOUT_NAMES):
        raise jl.JudgeError(
            "exemplar holdout list differs from the pinned 12 docs; "
            "changing it requires a code change and a fresh --eval --write"
        )
    return names


def _entry(corpus: Path, name: str) -> dict:
    body = clean_body((corpus / name).read_text(encoding="utf-8"))
    return {"name": name, "sha256": _sha256(body), "cjk": lib.cjk_count(body)}


def build_generic_set(
    corpus: Path,
    *,
    slug: str,
    author: str = "",
    exemplar_count: int = EXEMPLAR_COUNT,
    holdout_count: int = HOLDOUT_COUNT,
) -> dict:
    """给新作者建样例集：排序前 10 篇当样例，紧接的 12 篇当留出。

    与 panchen 的区别：没有实测数字（没有这个作者的负样本），所以 `measured={}`、
    `calibration="unmeasured"`。不伪造 MEASURED_SIZES。
    """
    if exemplar_count != EXEMPLAR_COUNT:
        raise jl.JudgeError(
            f"generic exemplar set must use {EXEMPLAR_COUNT} docs (prompt says 10)"
        )
    accepted = sorted(doc["file"] for doc in bp.load_corpus(corpus)[0])
    if len(accepted) < EXEMPLAR_COUNT:
        raise jl.JudgeError(
            f"corpus has only {len(accepted)} usable doc(s); need {EXEMPLAR_COUNT}"
        )
    names = accepted[:EXEMPLAR_COUNT]
    held = accepted[EXEMPLAR_COUNT : EXEMPLAR_COUNT + holdout_count]
    docs = [_entry(corpus, name) for name in names]
    try:
        corpus_rel = corpus.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        corpus_rel = corpus.resolve().as_posix()
    return {
        "schema": 1,
        "protocol": PROTOCOL,
        "pack": slug,
        "author": author,
        "prompt_version": PROMPT_VERSION,
        "judge_logic_version": JUDGE_LOGIC_VERSION,
        "exemplar_count": len(names),
        "holdout_count": len(held),
        "corpus": corpus_rel,
        "docs": docs,
        "holdout": [_entry(corpus, name) for name in held],
        "set_sha256": set_sha256(docs),
        "measured": {},
        "calibration": "unmeasured",
        "note": "没有该作者的负样本，样例集未做盲评验收；误收率未知。",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_set(corpus: Path = DEFAULT_CORPUS, model: str = jl.DEFAULT_MODEL) -> dict:
    docs = []
    for name in exemplar_names(corpus):
        path = corpus / name
        body = clean_body(path.read_text(encoding="utf-8"))
        docs.append({"name": name, "sha256": _sha256(body), "cjk": lib.cjk_count(body)})
    try:
        corpus_rel = str(corpus.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        corpus_rel = str(corpus)
    return {
        "schema": 1,
        "protocol": PROTOCOL,
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "judge_logic_version": JUDGE_LOGIC_VERSION,
        "corpus": corpus_rel,
        "docs": docs,
        "holdout": holdout_docs(corpus),
        "set_sha256": set_sha256(docs),
        "measured": {},
    }


def load_set(path: Path | None = None) -> dict | None:
    path = path or DEFAULT_SET
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _verify_generic(set_obj: dict, corpus: Path, pack: dict | None = None) -> list[str]:
    """通用样例集：验结构、文件、哈希、留出集、实测块；不比对 panchen 的钉死名单。"""
    problems: list[str] = []
    if set_obj.get("protocol") != PROTOCOL:
        problems.append(f"protocol {set_obj.get('protocol')!r} != {PROTOCOL!r}")
    if not str(set_obj.get("pack") or "").strip():
        problems.append("pack slug missing")
    calibration = set_obj.get("calibration")
    if calibration not in ("unmeasured", "measured"):
        problems.append(f"calibration {calibration!r} not in ('unmeasured', 'measured')")
    if int(set_obj.get("exemplar_count") or 0) != EXEMPLAR_COUNT:
        problems.append(f"exemplar_count must be {EXEMPLAR_COUNT}")
    docs = set_obj.get("docs")
    if not isinstance(docs, list) or len(docs) != EXEMPLAR_COUNT:
        return problems + [f"exemplar set must hold {EXEMPLAR_COUNT} docs, got {len(docs or [])}"]
    names: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get("sha256"), str) or not doc.get("name"):
            return problems + ["exemplar docs schema: name and sha256 are required strings"]
        name = str(doc["name"])
        names.append(name)
        path = corpus / name
        if not path.is_file():
            problems.append(f"exemplar missing from corpus: {name!r}")
            continue
        body = clean_body(path.read_text(encoding="utf-8"))
        if _sha256(body) != doc.get("sha256"):
            problems.append(f"exemplar text changed: {name}")
        if lib.cjk_count(body) < MIN_CJK:
            problems.append(f"exemplar too short (<{MIN_CJK} CJK): {name}")
    if len(set(names)) != len(names):
        problems.append("duplicate exemplar names")
    if set_obj.get("set_sha256") != set_sha256(docs):
        problems.append("set_sha256 mismatch")
    holdout = set_obj.get("holdout")
    if not isinstance(holdout, list):
        return problems + ["holdout must be a list of {name, sha256}"]
    if int(set_obj.get("holdout_count") or 0) != len(holdout):
        problems.append("holdout_count does not match holdout list")
    if len(holdout) > HOLDOUT_COUNT:
        problems.append(f"holdout holds {len(holdout)} docs (> {HOLDOUT_COUNT})")
    held_names: list[str] = []
    for doc in holdout:
        if not isinstance(doc, dict) or not isinstance(doc.get("sha256"), str) or not doc.get("name"):
            problems.append("holdout schema: name and sha256 are required strings")
            continue
        name = str(doc["name"])
        held_names.append(name)
        path = corpus / name
        if not path.is_file():
            problems.append(f"holdout missing from corpus: {name!r}")
            continue
        body = clean_body(path.read_text(encoding="utf-8"))
        if _sha256(body) != doc.get("sha256"):
            problems.append(f"holdout text changed: {name}")
    if set(names) & set(held_names):
        problems.append("holdout overlaps exemplars")
    if calibration == "measured":
        problems.extend(
            verify_measured(
                set_obj,
                protocol=PROTOCOL,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_sha256(),
                judge_logic_version=JUDGE_LOGIC_VERSION,
                holdout_sha256=set_sha256(holdout),
            )
        )
        measured_block = set_obj.get("measured")
        if pack is not None and isinstance(measured_block, dict):
            neg_meta = (pack.get("data") or {}).get("negatives") or {}
            names = [str(name) for name in (neg_meta.get("files") or [])]
            root = pack.get("negatives")
            if root is not None and names:
                digest = negatives_digest(Path(root), names)
                if measured_block.get("negatives_sha256") != digest:
                    problems.append("measured negatives_sha256 != current negatives")
    elif set_obj.get("measured"):
        problems.append("calibration is unmeasured but measured numbers are present")
    return problems


def verify_set(
    set_obj: dict,
    corpus: Path = DEFAULT_CORPUS,
    *,
    pack: dict | None = None,
    baseline: Path | None = None,
) -> list[str]:
    """样例集是不是当初验收的那一份：名单、篇数、文件、清洗后字节、总指纹、留出集。

    分支由调用方传入的 `pack` 决定（N-04）：带包的是通用集（unmeasured 或 measured），
    不带包的是 panchen 钉死集。钉死集要靠 `baseline`（包里的 judge_baseline.json）推名单；
    不给就去开发者树里找，装成 wheel 时那里没有东西，必然失败。
    """
    generic = pack is not None or "pack" in set_obj
    if generic:
        if pack is None:
            return ["generic exemplar set must be verified with its pack (pass pack=...)"]
        if pack.get("slug") != set_obj.get("pack"):
            return [
                f"exemplar set belongs to pack {set_obj.get('pack')!r}, "
                f"not {pack.get('slug')!r}"
            ]
        return _verify_generic(set_obj, corpus, pack)
    if set_obj.get("calibration") in ("unmeasured", "measured"):
        return ["pinned (panchen) exemplar set must not declare a generic calibration"]
    problems: list[str] = []
    docs = set_obj.get("docs")
    if not isinstance(docs, list) or len(docs) != EXEMPLAR_COUNT:
        return [f"exemplar set must hold {EXEMPLAR_COUNT} docs, got {len(docs or [])}"]
    if any(not isinstance(doc, dict) for doc in docs):
        return ["exemplar docs schema: every entry must be an object with name/sha256"]
    names = [str(doc.get("name", "")) for doc in docs]
    if any(not name or not isinstance(doc.get("sha256"), str) for name, doc in zip(names, docs, strict=True)):
        return ["exemplar docs schema: name and sha256 are required strings"]
    if len(set(names)) != len(names):
        problems.append("duplicate exemplar names")
    try:
        if names != exemplar_names(corpus, baseline):
            problems.append("exemplar list does not match the pinned 10 docs")
    except jl.JudgeError as exc:
        problems.append(f"cannot derive pinned exemplars: {exc}")
    for name, doc in zip(names, docs, strict=True):
        path = corpus / name
        if not path.is_file():
            problems.append(f"exemplar missing from corpus: {name!r}")
            continue
        body = clean_body(path.read_text(encoding="utf-8"))
        if _sha256(body) != doc.get("sha256"):
            problems.append(f"exemplar text changed: {name}")
        if lib.cjk_count(body) < MIN_CJK:
            problems.append(f"exemplar too short (<{MIN_CJK} CJK): {name}")
    if set_obj.get("set_sha256") != set_sha256(docs):
        problems.append("set_sha256 mismatch")
    holdout = set_obj.get("holdout")
    if not isinstance(holdout, list) or len(holdout) != HOLDOUT_COUNT:
        problems.append(f"holdout must hold {HOLDOUT_COUNT} docs")
    else:
        held = [str(doc.get("name", "")) if isinstance(doc, dict) else "" for doc in holdout]
        if held != list(HOLDOUT_NAMES):
            problems.append("holdout list does not match the pinned 12 docs")
        for doc in holdout:
            name = str(doc.get("name", "")) if isinstance(doc, dict) else ""
            path = corpus / name
            if not name or not path.is_file():
                problems.append(f"holdout missing from corpus: {name!r}")
                continue
            if _sha256(clean_body(path.read_text(encoding="utf-8"))) != doc.get("sha256"):
                problems.append(f"holdout text changed: {name}")
    return problems


def check_set_compatible(
    set_obj: dict,
    model: str | None,
    require_measured: bool = True,
    *,
    corpus: Path | None = None,
    baseline: Path | None = None,
) -> str | None:
    """基线是「模型 + prompt + 逻辑版本 + 样例 + 实测」的产物；不一致就不许判。

    钉死集要拿 `corpus` + 包里 `baseline` 现算评测样本量；不给就走开发树默认（仓库内脚本）。
    """
    if set_obj.get("protocol") != PROTOCOL:
        return f"exemplar protocol {set_obj.get('protocol')!r} != {PROTOCOL!r}"
    if set_obj.get("prompt_version") != PROMPT_VERSION:
        return (
            f"exemplar prompt_version {set_obj.get('prompt_version')!r} != {PROMPT_VERSION!r}; "
            "re-run exemplar_judge.py --eval --write"
        )
    if set_obj.get("judge_logic_version") != JUDGE_LOGIC_VERSION:
        return (
            f"exemplar judge_logic_version {set_obj.get('judge_logic_version')!r} != "
            f"{JUDGE_LOGIC_VERSION}; re-run exemplar_judge.py --eval --write"
        )
    if set_obj.get("pack"):
        # 通用包：未实测不绑模型；实测必须同模型，数字块要完整（D3）。
        # 评测（require_measured=False）是在重产 measured，允许换模型重测。
        calibration = set_obj.get("calibration")
        if calibration == "unmeasured":
            return None
        if calibration != "measured":
            return f"exemplar calibration {calibration!r} not in ('unmeasured', 'measured')"
        measured = set_obj.get("measured") or {}
        if require_measured:
            if model is not None and measured.get("model") != model:
                return (
                    f"exemplar measured on {measured.get('model')!r} != current {model!r}; "
                    "re-run author_pack.py eval --pack ... --write on this model"
                )
            problems = verify_measured(
                set_obj,
                protocol=PROTOCOL,
                prompt_version=PROMPT_VERSION,
                prompt_sha256=prompt_sha256(),
                judge_logic_version=JUDGE_LOGIC_VERSION,
                holdout_sha256=set_sha256(set_obj.get("holdout") or []),
            )
            if problems:
                return "exemplar measured block invalid: " + "; ".join(problems[:3])
        return None
    unmeasured = set_obj.get("calibration") == "unmeasured"
    if model is not None and not unmeasured and set_obj.get("model") != model:
        return (
            f"exemplar baseline model {set_obj.get('model')!r} != current {model!r}; "
            "re-run exemplar_judge.py --eval --write on this model"
        )
    if require_measured and not unmeasured:
        measured = set_obj.get("measured") or {}
        for key, expected in expected_sizes(corpus or DEFAULT_CORPUS, baseline).items():
            got = (measured.get(key) or {}).get("n")
            if got != expected:
                return (
                    f"exemplar measured {key} n {got!r} != {expected}; "
                    "re-run exemplar_judge.py --eval --write"
                )
        if int(measured.get("runs") or 0) < 1:
            return "exemplar measured runs missing; re-run exemplar_judge.py --eval --write"
    return None


def check_set_bound_pack(set_obj: dict, pack: dict | None) -> str | None:
    """样例集必须和命令行给的包对得上；通用集离开 --pack 不许用（F-03）。"""
    calibration = set_obj.get("calibration")
    if pack is None:
        if calibration in ("unmeasured", "measured"):
            return (
                "generic exemplar set requires --pack <dir>; "
                "generic packs are judged with judge.py --pack"
            )
        return None
    if set_obj.get("pack") != pack["slug"]:
        return (
            f"exemplar set belongs to pack {set_obj.get('pack')!r}, "
            f"not {pack['slug']!r}"
        )
    pack_calibration = pack["data"].get("calibration")
    if calibration != pack_calibration:
        return f"exemplar calibration {calibration!r} != pack.json {pack_calibration!r}"
    return None


def build_messages(exemplars: list[str], draft: str) -> list:
    from ..compat import Message

    return [Message.system(SYSTEM_PROMPT), Message.user(render_user_prompt(exemplars, draft))]


def parse_verdict(raw: str) -> dict:
    text = jl._strip_fence(raw or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise jl.JudgeError(f"no JSON object in model output: {(raw or '')[:120]!r}")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        payload = jl._lenient_json(text[start : end + 1])
    if not isinstance(payload, dict):
        raise jl.JudgeError("model output is not a JSON object")
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        raise jl.JudgeError(f"bad verdict {verdict!r}; want one of {VERDICTS}")
    cues = [
        str(cue).strip()[:120]
        for cue in (payload.get("cues") or [])
        if str(cue).strip()
    ]
    return {
        "verdict": verdict,
        "reason": str(payload.get("reason") or "").strip()[:200],
        "cues": cues[:3],
    }


def decide(payload: dict, model: str, cached: bool = False, usage: dict | None = None) -> dict:
    """same → like；different / unsure → not-like。unsure 按不像，保守方向。"""
    answer = payload["verdict"]
    same = answer == "same"
    reason = payload.get("reason") or {
        "same": "和样例是同一个人的笔性",
        "different": "和样例不是同一个人的笔性",
        "unsure": "拿不准，按不像处理",
    }[answer]
    return {
        "protocol": PROTOCOL,
        "verdict": "like" if same else "not-like",
        "same": same,
        "answer": answer,
        "reason": reason,
        "cues": list(payload.get("cues") or []),
        "cached": cached,
        "model": model,
        "usage": dict(usage or {}),
    }


def cache_key(draft: str, model: str, pinned_sha: str) -> str:
    payload = "\x00".join(
        [
            model,
            PROMPT_VERSION,
            prompt_sha256(),
            str(JUDGE_LOGIC_VERSION),
            pinned_sha,
            draft,
        ]
    )
    return _sha256(payload)


def read_cache(key: str) -> dict | None:
    path = jl.cache_dir() / f"exemplar-{key}.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("verdict") not in VERDICTS:
        return None
    return payload


def write_cache(key: str, payload: dict) -> None:
    path = jl.cache_dir() / f"exemplar-{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


async def _call_llm(llm, messages, timeout: float) -> tuple[str, dict]:
    # temperature=0：判定要可复现，不要模型的随机发挥。
    try:
        response = await asyncio.wait_for(llm.chat(messages, temperature=0.0), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise jl.JudgeError(f"LLM timeout after {timeout:.0f}s") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise jl.JudgeError(f"LLM call failed: {type(exc).__name__}") from exc
    text = getattr(response, "content", None) or ""
    usage = getattr(response, "usage", None) or {}
    return str(text), usage


async def judge_text(
    text: str,
    set_obj: dict,
    llm,
    model: str,
    corpus: Path = DEFAULT_CORPUS,
    use_cache: bool = True,
    timeout: float = 120.0,
    retries: int = 1,
) -> dict:
    """判一篇稿。返回 {verdict, reason, cues, model, usage, cached}。"""
    draft = clean_body(text)
    if lib.cjk_count(draft) == 0:
        raise jl.JudgeError("draft has no CJK text")
    key = cache_key(draft, model, str(set_obj.get("set_sha256")))
    if use_cache:
        cached = read_cache(key)
        if cached is not None:
            return {**cached, "cached": True, "model": model}
    exemplars = exemplar_texts(set_obj, corpus)
    usage_total: dict = {}
    last_raw = ""
    last_error: Exception | None = None
    parsed: dict | None = None
    for attempt in range(retries + 1):
        messages = build_messages(exemplars, draft)
        if attempt:
            from ..compat import Message

            messages = messages + [
                Message.assistant(last_raw[:1000]),
                Message.user(
                    f"上一轮输出不能用（{last_error}）。只输出一个 JSON 对象，"
                    "verdict 只能是 same / different / unsure。"
                ),
            ]
        try:
            raw, usage = await _call_llm(llm, messages, timeout)
        except jl.JudgeError as exc:
            last_error = exc
            continue
        last_raw = raw
        for key_name, value in usage.items():
            if isinstance(value, int):
                usage_total[key_name] = usage_total.get(key_name, 0) + value
        try:
            parsed = parse_verdict(raw)
        except jl.JudgeError as exc:
            last_error = exc
            continue
        last_error = None
        break
    if parsed is None:
        raise jl.JudgeError(
            f"model output unusable: {last_error} | raw: {last_raw[:300]!r}"
        )
    payload = {**parsed, "model": model, "usage": usage_total, "cached": False}
    if use_cache:
        # usage 一起缓存：命中时下游看到的不是 0 token，而是首判的真实用量。
        write_cache(key, {k: payload[k] for k in ("verdict", "reason", "cues", "usage")})
    return payload


class _FakeLLM:
    """假裁判：按顺序返回预置输出，记录调用次数。"""

    def __init__(self, outputs: list[str]):
        self.outputs = outputs
        self.calls = 0

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        output = self.outputs[min(self.calls - 1, len(self.outputs) - 1)]

        class _Resp:
            content = output
            usage = {"prompt_tokens": 10, "completion_tokens": 2}

        return _Resp()


# ---------------------------------------------------------------- self-check


def selftest() -> int:
    jl._utf8_stdio()
    fails: list[str] = []

    def check(ok: bool, label: str) -> None:
        if not ok:
            fails.append(label)

    try:
        set_obj = load_set()
        if set_obj is None:
            print(f"[FAIL] exemplar baseline missing: {DEFAULT_SET}", file=sys.stderr)
            return 1
        problems = verify_set(set_obj)
        check(not problems, "set verify: " + "; ".join(problems))
        incompatible = check_set_compatible(set_obj, set_obj.get("model"))
        check(incompatible is None, f"set compatibility: {incompatible}")
        texts = exemplar_texts(set_obj)
        check(len(texts) == EXEMPLAR_COUNT, "exemplar count")
        check(all("图片发自简书" not in text for text in texts), "no platform watermark")
        check(all("URL Source" not in text for text in texts), "no export header")

        messages = build_messages(texts, "待测稿正文，只有一句。")
        system = getattr(messages[0], "content", "")
        user = getattr(messages[1], "content", "")
        check(GENRE_HINT in system, "genre hint in system prompt")
        check("不要按「写得好不好」判" in system, "quality warning in system prompt")
        check(user.count("### 样例 ") == EXEMPLAR_COUNT, "prompt carries all exemplars")
        check(user.rstrip().endswith("}")
              and "## 待测文章" in user and "same|different|unsure" in user,
              "prompt carries draft and JSON instruction")
        check("band" not in system.lower() and "rubric" not in system.lower(),
              "prompt does not leak rubric vocabulary")

        parse_cases = {
            '{"verdict": "same", "reason": "x", "cues": ["a"]}': "same",
            '```json\n{"verdict": "different", "reason": "y"}\n```': "different",
            '判断如下：{"verdict": "unsure", "reason": "z"} 以上。': "unsure",
        }
        for raw, want in parse_cases.items():
            check(parse_verdict(raw)["verdict"] == want, f"parse {want}")
        for bad in ("", "没有 JSON", '{"verdict": "maybe"}'):
            try:
                parse_verdict(bad)
            except jl.JudgeError:
                pass
            else:
                fails.append(f"bad output accepted: {bad!r}")

        for answer, verdict in (("same", "like"), ("different", "not-like"), ("unsure", "not-like")):
            check(
                decide({"verdict": answer, "reason": "", "cues": []}, "m")["verdict"] == verdict,
                f"decision {answer}",
            )

        # 坏输出重试一次：第 1 轮没有 JSON，第 2 轮正常；第 2 次判同一稿必须命中缓存。
        fake = _FakeLLM(["这不是 JSON", '{"verdict": "same", "reason": "ok"}'])
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("DSH_VOICE_JUDGE_CACHE")
            os.environ["DSH_VOICE_JUDGE_CACHE"] = tmp
            try:
                judged = asyncio.run(
                    judge_text("这是一篇待测稿，写的是租房和吃饭。", set_obj, fake, "fake-model",
                               use_cache=True)
                )
                check(fake.calls == 2, "retry once after bad output")
                check(judged["verdict"] == "same", "retry keeps good answer")
                check(judged["usage"].get("prompt_tokens") == 20, "usage summed over attempts")

                no_call = _FakeLLM(["x"])
                cached_judge = asyncio.run(
                    judge_text("这是一篇待测稿，写的是租房和吃饭。", set_obj, no_call,
                               "fake-model", use_cache=True)
                )
                check(cached_judge["cached"] is True, "cache hit after first judge")
                check(no_call.calls == 0, "cache hit makes no LLM call")
            finally:
                if old is None:
                    os.environ.pop("DSH_VOICE_JUDGE_CACHE", None)
                else:
                    os.environ["DSH_VOICE_JUDGE_CACHE"] = old

        check(
            cache_key("稿", "m", "a") != cache_key("稿", "m", "b"),
            "cache key covers exemplar set",
        )
        check(
            cache_key("稿", "m", "a") != cache_key("另一稿", "m", "a"),
            "cache key covers draft",
        )

        tampered = json.loads(json.dumps(set_obj))
        tampered["docs"][0]["sha256"] = "0" * 64
        check(bool(verify_set(tampered)), "tampered exemplar is rejected")
        check(
            bool(verify_set(dict(set_obj, calibration="unmeasured"))),
            "unmeasured set without its pack is rejected",
        )
        check(
            check_set_compatible(set_obj, "other-model") is not None,
            "model mismatch is rejected",
        )
        check(check_set_bound_pack(set_obj, None) is None, "pinned set needs no --pack")
        check(
            check_set_bound_pack(dict(set_obj, calibration="unmeasured"), None) is not None,
            "unmeasured set requires --pack",
        )
    except Exception as exc:  # noqa: BLE001 - 自检要把异常变成 FAIL，不冒充通过
        print(f"[FAIL] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if fails:
        for fail in fails:
            print(f"[FAIL] {fail}", file=sys.stderr)
        return 1
    print(
        f"exemplar judge ok: {EXEMPLAR_COUNT} exemplars, prompt {len(SYSTEM_PROMPT)} chars, "
        f"set {str(set_obj['set_sha256'])[:12]}"
    )
    return 0


def write_set(set_obj: dict, path: Path = DEFAULT_SET) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(set_obj, ensure_ascii=False, indent=1) + "\n")
    os.replace(tmp, path)


def format_report(result: dict) -> str:
    """`judge.py --file` 的人读报告。"""
    judge = result["judge"]
    lines = jl.format_deterministic(result.get("deterministic"))
    if result.get("deterministic") is not None:
        lines.append("  （诊断层：分项偏差只供改稿参考，不参与放行）")
    lines.append("")
    lines.append(
        "裁判层 · {}（{}）  模型 {}".format(
            judge["verdict"], judge["reason"], result.get("model") or "?"
        )
    )
    lines.append(
        "  判定 {}；样例 {} 篇{}".format(
            judge.get("answer", "?"),
            result.get("exemplars", 0),
            "（命中缓存）" if result.get("cached") else "",
        )
    )
    if result.get("calibration") == "unmeasured":
        docs = int(result.get("negatives") or 0)
        suffix = f"（有 {docs} 篇负样本，但还没做盲评）" if docs else "（没有该作者的负样本）"
        lines.append(f"  样例集未实测{suffix}：误收率未知。")
    else:
        measured = result.get("measured") or {}
        if measured:
            h, o = measured.get("author_holdout") or {}, measured.get("other_author") or {}
            lines.append(
                f"  样例集已实测：留出 {h.get('n')} 篇判同人 {h.get('same')}/{h.get('n')}；"
                f"别人 {o.get('n')} 篇误收 {o.get('same')} 篇"
            )
    cues = judge.get("cues") or []
    if cues:
        lines.append("  线索：" + " / ".join(cues))
    lines.append("")
    lines.append(f"总判定：{result['verdict']}（{result['verdict_reason']}）")
    return "\n".join(lines)


def judge_file(args) -> int:
    """`judge.py --protocol exemplar` 的实现：判一个文件，返回进程退出码。"""
    text = Path(args.file).read_text(encoding="utf-8")
    profile = jl.load_profile(Path(args.profile)) if args.profile else None
    # 先算确定性层：不花钱，坏输入在这里就拦下。
    try:
        deterministic = jl.run_deterministic(text, profile)
    except (ValueError, KeyError) as exc:
        print(f"[FAIL] deterministic layer failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    async def _run() -> tuple[dict, dict, dict | None]:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        try:
            llm = create_llm(args.provider, args.model)
        except (ValueError, RuntimeError) as exc:
            raise jl.JudgeError(f"setup error: {exc}") from exc
        model = getattr(llm, "model", args.model or args.provider)
        try:
            set_obj = load_set(Path(args.exemplar_baseline))
            if set_obj is None:
                raise jl.JudgeError(
                    "no exemplar baseline; run "
                    "python scripts/voice/author_pack.py exemplars --pack <dir> (generic) "
                    "or exemplar_judge.py --eval --write (panchen)"
                )
            pack = pk.load_pack(Path(args.pack)) if getattr(args, "pack", None) else None
            problems = verify_set(set_obj, Path(args.corpus), pack=pack)
            if problems:
                raise jl.JudgeError("exemplar set unusable: " + "; ".join(problems))
            bound = check_set_bound_pack(set_obj, pack)
            if bound:
                raise jl.JudgeError(bound)
            incompatible = check_set_compatible(set_obj, model)
            if incompatible:
                raise jl.JudgeError(incompatible)
            payload = await judge_text(
                text, set_obj, llm, model, Path(args.corpus),
                use_cache=not args.no_cache, timeout=args.timeout,
            )
            return payload, set_obj, pack
        finally:
            await release_llm_client(llm)

    try:
        payload, set_obj, pack = asyncio.run(_run())
    except jl.JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - 环境错误不冒充判定
        print(f"[FAIL] unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    judge = decide(
        payload,
        payload.get("model") or args.model,
        cached=bool(payload.get("cached")),
        usage=payload.get("usage"),
    )
    combined = jl.gate(judge, deterministic)
    out = {
        "file": args.file,
        "protocol": PROTOCOL,
        "model": payload.get("model") or args.model,
        "cached": bool(payload.get("cached")),
        "usage": payload.get("usage", {}),
        "exemplars": len(set_obj.get("docs", [])),
        "calibration": set_obj.get("calibration") or "measured",
        "negatives": int((((pack or {}).get("data") or {}).get("negatives") or {}).get("docs") or 0),
        "measured": set_obj.get("measured") if set_obj.get("calibration") == "measured" else None,
        "gate_rule": "judge-only",
        "deterministic": deterministic,
        "judge": judge,
        "verdict": combined["verdict"],
        "verdict_reason": combined["reason"],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(format_report(out))
    return {"like": 0, "not-like": 1}.get(combined["verdict"], 2)


def main(argv: list[str] | None = None) -> int:
    jl._utf8_stdio()
    parser = argparse.ArgumentParser(description="小样本裁判：10 篇样例 + 同人判定")
    parser.add_argument("--selftest", action="store_true", help="无网络自检")
    parser.add_argument("--eval", action="store_true", help="在作者全量 + 别人 46 + AI 2 上评测（真模型；篇数见 MEASURED_SIZES）")
    parser.add_argument("--write", action="store_true", help="评测达标后刷新 exemplar_baseline.json")
    parser.add_argument("--out", type=Path, default=None, help="评测结果 JSON 路径")
    parser.add_argument("--provider", default=jl.DEFAULT_PROVIDER)
    parser.add_argument("--model", default=jl.DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=1, help="整批评测跑几遍（查翻转）")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    if args.selftest:
        return selftest()
    if not args.eval:
        parser.print_help()
        return 2
    if args.repeat < 1:
        print("[FAIL] --repeat must be >= 1", file=sys.stderr)
        return 2
    if args.concurrency < 1:
        print("[FAIL] --concurrency must be >= 1", file=sys.stderr)
        return 2
    try:
        set_obj = load_set()
        if set_obj is None or args.write:
            set_obj = build_set(model=args.model)
        problems = verify_set(set_obj)
    except jl.JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print(f"[FAIL] exemplar set: {problem}", file=sys.stderr)
        return 2
    try:
        import exemplar_eval as ee  # 延迟导入：评测依赖判定侧，反向不依赖，避免环

        return asyncio.run(
            ee.run_eval(
                set_obj,
                provider=args.provider,
                model=args.model,
                out=args.out,
                write=args.write,
                repeat=args.repeat,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
    except jl.JudgeError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
