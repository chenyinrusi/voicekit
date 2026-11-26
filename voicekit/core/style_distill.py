"""文风蒸馏（FIX-358 生成侧第二轮）：把几篇原文总结成「独有笔法」规则卡。

为什么要这一步：FIX-356 的判词反复说「缺少这位作者反复出现的私人标记」。数字表和全文样例
都是**隐含**的示范，蒸馏是把它们写成**可照做**的规则（口头禅、句长、开头、收尾），
再喂回生成提示。规则卡由另一个模型读原文生成，不入判定；证据里存提示指纹与规则原文。

用法（由 `recreate_eval.run(distill=True)` 或 `author_pack.py write --distill` 调用；
单独调试：python scripts/voice/style_distill.py --anchors-file x.md --model gpt-4o）
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 自己的目录不够：src/backend import 需要仓库根（父子两级）；不依赖兄弟模块的 sys.path 副作用
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import judge_lib as jl  # noqa: E402

DISTILL_PROMPT = (
    "下面是同一位作者的几篇中文随笔原文。请只根据原文，总结这位作者**独有**的笔法标记"
    "（换一个普通写作者不会有的那些）。\n"
    "要覆盖：\n"
    "- 口头禅与转折词（原文里反复出现的词，例如「想来」「倒是」「姑且」；每条给原文例子）\n"
    "- 句长与分句习惯（逗号一路推进，还是一串短句）\n"
    "- 开头方式（时间/地点起兴、诗句起兴、议论起兴）\n"
    "- 段落与结尾套路（格言收束、自我调侃、PS、反问）\n"
    "- 标点、人称、文白夹杂、情绪浓度\n"
    "要求：8–12 条；每条 = 一句可以照着做的规则 + 原文里 10–20 字的例子；不要写作者名字。\n"
    '只输出 JSON：{"markers": [{"trait": "口头禅", "rule": "……", "example": "……"}]}\n\n'
    "## 原文\n\n{anchors}"
)

MIN_MARKERS = 8


def prompt_for(anchors: str) -> str:
    return DISTILL_PROMPT.replace("{anchors}", anchors)


def prompt_sha256(anchors: str) -> str:
    return hashlib.sha256(prompt_for(anchors).encode("utf-8")).hexdigest()


def parse_markers(text: str, *, min_markers: int = MIN_MARKERS) -> list[dict]:
    """从模型回答里取出 markers；容忍 ```json 围栏与前后废话。"""
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"distill answer has no JSON object: {text[:120]!r}")
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"distill answer is not valid JSON: {exc}") from exc
    markers = payload.get("markers") if isinstance(payload, dict) else None
    if not isinstance(markers, list) or len(markers) < min_markers:
        raise ValueError(f"distill needs >= {min_markers} markers, got {type(markers).__name__}")
    cleaned: list[dict] = []
    for marker in markers:
        if not isinstance(marker, dict):
            continue
        rule = str(marker.get("rule") or "").strip()
        if not rule:
            continue
        cleaned.append(
            {
                "trait": str(marker.get("trait") or "").strip(),
                "rule": rule,
                "example": str(marker.get("example") or "").strip(),
            }
        )
    if len(cleaned) < min_markers:
        raise ValueError(f"distill needs >= {min_markers} usable markers, got {len(cleaned)}")
    return cleaned


def render_marker_card(markers: list[dict]) -> str:
    """规则卡：给生成提示用的纯文本，不写作者名。"""
    lines = [f"{index}. {marker['trait']}：{marker['rule']}" for index, marker in enumerate(markers, 1)]
    examples = [marker for marker in markers if marker.get("example")]
    if examples:
        lines.append("原文例子：" + "；".join(f"「{marker['example']}」" for marker in examples[:6]))
    return "\n".join(lines)


async def _ask(llm, prompt: str, timeout: float) -> tuple[str, dict]:
    from ..compat import Message

    response = await asyncio.wait_for(
        llm.chat([Message.user(prompt)], temperature=0.0), timeout=timeout
    )
    return str(getattr(response, "content", "") or ""), dict(getattr(response, "usage", None) or {})


async def distill(anchors: str, llm, model: str, *, timeout: float = 180.0) -> dict:
    prompt = prompt_for(anchors)
    answer, usage = await _ask(llm, prompt, timeout)
    return {
        "model": model,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_chars": len(prompt),
        "markers": parse_markers(answer),
        "usage": usage,
    }


async def load_or_distill(
    anchors: str, cache_path: Path | str, llm, model: str, *, timeout: float = 180.0
) -> dict:
    """同一批样例只蒸馏一次：缓存命中直接读，指纹不符就重算。"""
    cache = Path(cache_path)
    sha = prompt_sha256(anchors)
    if cache.is_file():
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cached = {}
        if cached.get("prompt_sha256") == sha and cached.get("model") == model:
            cached["cache_hit"] = True
            return cached
    result = await distill(anchors, llm, model, timeout=timeout)
    result["cache_hit"] = False
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    jl._utf8_stdio()
    parser = argparse.ArgumentParser(description="把原文蒸馏成独有笔法规则卡")
    parser.add_argument("--anchors-file", required=True)
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--model", default="gpt-4o")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        llm = create_llm(args.provider, args.model)
        try:
            return await distill(
                Path(args.anchors_file).read_text(encoding="utf-8"),
                llm,
                getattr(llm, "model", args.model),
            )
        finally:
            await release_llm_client(llm)

    result = asyncio.run(_run())
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(render_marker_card(result["markers"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
