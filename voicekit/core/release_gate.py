"""放行参考：一条命令判「像不像这个人」（`voicekit judge`）。

只做参考层：判定要花钱，LLM 判官也不是作者，放行由作者签字拍板。
判定逻辑全在 `exemplar_judge` / `judge_lib`；这里只做解析、守卫、排版。
设计见 `E:\\customized_agentic_system\\dev\\fixes\\voice-judge-gate\\DESIGN.md`。
"""

from __future__ import annotations

import asyncio
import json
import math
import sys
from pathlib import Path

from . import exemplar_judge as ej
from . import judge_lib as jl
from . import pack_lib as pk
from .exemplar_inputs import clean_body

#: USD per 1M tokens (input, output)。源：CAS `src/config/llm_models.toml`
#: `[pricing_v2."deepseek-chat"]`（0.14 / 0.28，2026-08-03，状态 deprecated）。
#: 不认识模型就不算钱（cost_usd = null），不猜 0。
PRICE_PER_M: dict[str, tuple[float, float]] = {"deepseek-chat": (0.14, 0.28)}

#: 中文粗估：1 token ≈ 1.5 字符。只给 --dry-run 说规模，真跑以返回 usage 为准。
CHARS_PER_TOKEN = 1.5

#: 对外 verdict 词汇 → rc。same = 0 / not-same = 1 / inconclusive = 2。
_RC = {"same": 0, "not-same": 1}
_OUT = {"like": "same", "not-like": "not-same"}


def _short(value: object) -> str:
    return str(value or "")[:12] or "（无）"


def _corpus(pack: dict, set_obj: dict) -> tuple[Path | None, str]:
    """样例正文目录：包里声明优先，否则用样例集记的（相对当前目录）。返回 (目录, 说明)。"""
    if pack.get("corpus"):
        return Path(pack["corpus"]), "pack.json corpus"
    recorded = set_obj.get("corpus")
    if recorded:
        path = Path(str(recorded))
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.is_dir():
            return path, f"样例集 corpus={recorded}"
        return None, f"样例集记的 corpus 目录不存在：{path}"
    return None, "包和样例集都没写 corpus（样例正文只在基线里留了名字与 sha）"


def _generic(set_obj: dict) -> bool:
    """泛型集有 pack slug / calibration；钉死集两个都没有。"""
    return "pack" in set_obj or set_obj.get("calibration") in ("unmeasured", "measured")


def run(
    file: Path | str,
    pack_path: Path | str,
    *,
    as_json: bool = False,
    dry_run: bool = False,
    timeout: float = 120.0,
    llm_factory=None,
    echo=print,
) -> int:
    """判一篇稿。返回进程退出码：0 same / 1 not-same / 2 inconclusive 或环境错。"""

    def say(line: str) -> None:
        # --json 时人看的进度走 stderr，stdout 只留一行 JSON，管道才拆得开。
        if as_json:
            print(line, file=sys.stderr)
        else:
            echo(line)

    def emit(line: str) -> None:
        echo(line)

    def fail(message: str) -> int:
        say(f"[FAIL] {message}")
        if as_json:
            emit(json.dumps({"error": message}, ensure_ascii=False))
        return 2

    path = Path(file)
    if not path.is_file():
        return fail(f"稿件不存在：{path}")
    text = path.read_text(encoding="utf-8")

    try:
        pack = pk.load_pack(Path(pack_path))
    except pk.PackError as exc:
        return fail(f"作者包读不进来：{exc}")

    set_path = Path(pack["exemplar"])
    set_obj = ej.load_set(set_path)
    if set_obj is None:
        return fail(
            f"包里没有 exemplar_baseline.json：{set_path}。"
            "先建基线（泛型包 build --pack / panchen 用 exemplar_judge --eval --write），"
            "不能拿别的判定顶替"
        )

    corpus, corpus_how = _corpus(pack, set_obj)
    if corpus is None:
        return fail(f"找不到 10 篇样例正文：{corpus_how}；没有正文就没法走 exemplar 协议")
    say(f"样例正文：{corpus_how} → {corpus}")

    if _generic(set_obj):
        if set_obj.get("pack") != pack["slug"]:
            return fail(
                f"包与样例集不匹配：样例集属于 {set_obj.get('pack')!r}，"
                f"这个包是 {pack['slug']!r}"
            )
        problems = ej.verify_set(set_obj, corpus, pack=pack)
        bound = ej.check_set_bound_pack(set_obj, pack)
        if bound:
            problems = problems + [bound]
    else:
        problems = ej.verify_set(set_obj, corpus, pack=None, baseline=Path(pack["baseline"]))
    if problems:
        return fail("样例集不可用：" + "；".join(str(p) for p in problems[:5]))

    profile_path = Path(pack["profile"])
    if not profile_path.is_file():
        return fail(f"包里的 profile.json 找不到：{profile_path}；判定要它做同人参照")
    baseline_path = Path(pack["baseline"])
    if baseline_path.is_file():
        baseline = jl.load_baseline(baseline_path)
        stale = jl.check_profile_match(baseline, profile_path)
        if stale:
            # 换成中文人话，并把两个短指纹摆出来：旧指纹时只看到一句英文 rc=2 最难受。
            return fail(
                f"基线过期：基线记录的 profile_sha256 {_short(baseline.get('profile_sha256'))} "
                f"≠ 当前 {_short(jl.profile_sha256(profile_path))}；"
                "重建基线：--build-baseline（改过 profile 就得重跑，旧基线不能再用）"
            )

    model = jl.DEFAULT_MODEL
    exemplar_count = len(set_obj.get("docs") or [])

    if dry_run:
        draft = clean_body(text)
        if jl.lib.cjk_count(draft) == 0:
            return fail("稿子里没有中文正文，判不了")
        messages = ej.build_messages(ej.exemplar_texts(set_obj, corpus), draft)
        chars = sum(len(m.content) for m in messages)
        estimate = math.ceil(chars / CHARS_PER_TOKEN)
        say(f"dry-run：提示词 {chars} 字符，约 {estimate} token（模型 {model}）")
        say("dry-run 不调模型，0 元；要真跑才调模型（去掉 --dry-run）")
        if as_json:
            emit(
                json.dumps(
                    {
                        "dry_run": True,
                        "model": model,
                        "pack": str(pack_path),
                        "exemplars": exemplar_count,
                        "baseline": _short(set_obj.get("set_sha256")),
                        "prompt_chars": chars,
                        "prompt_tokens_estimate": estimate,
                        "cost_usd": 0.0,
                    },
                    ensure_ascii=False,
                )
            )
        return 0

    factory = llm_factory
    if factory is None:

        def factory(provider: str, model_name: str):
            from ..compat import create_llm

            return create_llm(provider, model_name)

    say(f"将调用模型 {model}（{jl.DEFAULT_PROVIDER}）判同人；会花 token（同一稿同一基线命中缓存则 0 元）")

    async def _judge():
        from ..compat import release_llm_client

        llm = factory(jl.DEFAULT_PROVIDER, model)
        actual = str(getattr(llm, "model", "") or model)
        try:
            mismatch = ej.check_set_compatible(set_obj, actual)
            if mismatch:
                raise jl.JudgeError(mismatch)
            payload = await ej.judge_text(
                text, set_obj, llm, actual, corpus=corpus, timeout=timeout
            )
            return payload, actual
        finally:
            await release_llm_client(llm)

    try:
        payload, actual = asyncio.run(_judge())
    except jl.JudgeError as exc:
        return fail(str(exc))
    except SystemExit as exc:  # compat 缺 key / HTTP 错：抬成 rc=2，别让脚本当成 1
        return fail(str(exc) or "LLM 调用失败")
    except Exception as exc:  # noqa: BLE001 - CLI 边界，什么错都要变人话
        return fail(f"{type(exc).__name__}: {exc}")

    cached = bool(payload.get("cached"))
    answer = payload.get("verdict")
    judge = ej.decide(payload, actual, cached=cached, usage=payload.get("usage"))
    combined = jl.gate(judge)
    verdict = _OUT.get(combined["verdict"], "inconclusive")
    rc = _RC.get(verdict, 2)

    usage = judge.get("usage") or {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    price = PRICE_PER_M.get(actual)
    if cached:
        cost: float | None = 0.0
        cost_note = "命中缓存，本次 0 元（tokens 是首次判定的用量）"
    elif price is None:
        cost = None
        cost_note = "这个模型没有价目，未计费（cost_usd = null）"
    else:
        cost = (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
        cost_note = "上限估算，未按缓存命中价打折"

    say(f"判定：{verdict}（judge {combined['verdict']}；模型原话 {answer}）")
    say(f"样例 {exemplar_count} 篇；基线指纹 {_short(set_obj.get('set_sha256'))}；模型 {actual}")
    say(
        f"tokens {prompt_tokens + completion_tokens}"
        f"（输入 {prompt_tokens} / 输出 {completion_tokens}）；"
        f"cost_usd {cost}（{cost_note}）"
    )
    say(f"理由：{judge.get('reason')}")
    cues = judge.get("cues") or []
    if cues:
        say("线索：" + "；".join(str(cue) for cue in cues[:3]))
    say("这只是参考，放行仍由作者签字拍板。")

    if as_json:
        emit(
            json.dumps(
                {
                    "file": str(path),
                    "pack": str(pack_path),
                    "verdict": verdict,
                    "answer": answer,
                    "judge_verdict": combined["verdict"],
                    "reason": judge.get("reason"),
                    "cues": cues,
                    "model": actual,
                    "exemplars": exemplar_count,
                    "baseline": _short(set_obj.get("set_sha256")),
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cost_usd": cost,
                    "cached": cached,
                    "cost_note": cost_note,
                },
                ensure_ascii=False,
            )
        )
    return rc
