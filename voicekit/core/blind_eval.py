"""盲评工具（FIX-358）：把「有文风稿 / 对照稿」做成不知道标签的 A/B 对，人评或跨模型评。

为什么单独一个模块：判官（deepseek-chat + exemplar-v2）已认证，但「判官说像」还有一层疑问——
换一个模型、换一个人，还能不能看出差别？本模块只做两件事：

1. `kit`：从 `recreate_eval` 证据里取 N 对，按种子打乱 A/B；公开包只留 A/B 正文，
   答案（哪边是有文风稿）单独放 key。公开包里没有人名、没有条件标签。
2. `rate`：让另一个模型（默认 gpt-4o，不是判官家族）只看 A/B 与作者参考稿，选更像的一篇；
   `repeat` 两遍不一致记「说不清」。

用法：
    python scripts/voice/blind_eval.py kit --evidence evidence/D.json --author panchen-legacy \
        --refs-dir <留出稿目录> --out-kit evidence/blind-kit-panchen.md --out-key evidence/blind-key-panchen.json
    python scripts/voice/blind_eval.py rate --kit evidence/blind-kit-panchen.json --key <key> \
        --refs-dir <留出稿目录> --out evidence/blind-rate-panchen.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 自己的目录不够：src/backend import 需要仓库根（父子两级）；不依赖兄弟模块的 sys.path 副作用
if str(Path(__file__).resolve().parents[2]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from . import judge_lib as jl  # noqa: E402

REF_CHARS = 1500


def parse_choice(text: str) -> str:
    """rater 回答 -> A / B / unsure。同时出现两篇或都没提都算说不清。"""
    upper = (text or "").strip().upper()
    has_a, has_b = "A" in upper, "B" in upper
    if has_a and has_b:
        return "unsure"
    if has_a:
        return "A"
    if has_b:
        return "B"
    return "unsure"


def build_kit(
    payload: dict, slug: str, *, seed: int = 358, n: int = 10
) -> tuple[dict, dict]:
    """返回 (公开包, 答案 key)：公开包给 rater/人看，key 才记哪边有文风。"""
    block = payload["authors"].get(slug)
    if not block:
        raise ValueError(f"unknown author slug: {slug}")
    styled = {row["brief"]: row for row in block["docs"] if row["condition"] == "styled"}
    control = {row["brief"]: row for row in block["docs"] if row["condition"] == "control"}
    briefs = sorted(set(styled) & set(control))[:n]
    if not briefs:
        raise ValueError(f"no styled/control pairs for {slug}")
    rng = random.Random(seed)
    # r1/R-01：A/B 必须各半（否则「恒答 A」的 rater 会白拿高分，8/10 可能只是位置偏好）。
    sides = (["A", "B"] * ((len(briefs) + 1) // 2))[: len(briefs)]
    rng.shuffle(sides)
    public_pairs: list[dict] = []
    key_pairs: list[dict] = []
    for index, (brief, styled_side) in enumerate(zip(briefs, sides, strict=True)):
        styled_row, control_row = styled[brief], control[brief]
        control_side = "B" if styled_side == "A" else "A"
        a_text, b_text = (
            (styled_row["text"], control_row["text"])
            if styled_side == "A"
            else (control_row["text"], styled_row["text"])
        )
        pair_id = f"p{index + 1:02d}"
        public_pairs.append({"id": pair_id, "brief": brief, "A": a_text, "B": b_text})
        key_pairs.append(
            {
                "id": pair_id,
                "brief": brief,
                "styled_side": styled_side,
                "control_side": control_side,
                "judge_same": bool(styled_row.get("same")),
            }
        )
    public = {
        "protocol": "blind-pairs-v1",
        "generated_by": "scripts/voice/blind_eval.py",
        "seed": seed,
        "n": len(public_pairs),
        "side_counts": {"A": sides.count("A"), "B": sides.count("B")},
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pairs": public_pairs,
    }
    key = {"protocol": "blind-key-v1", "slug": slug, "n": len(key_pairs), "pairs": key_pairs, "human": {}}
    return public, key


def format_kit_md(public: dict, refs: list[str], *, instructions: str | None = None) -> str:
    """给人和 rater 看的盲评包：只有参考稿 + A/B，没有作者名/条件标签。"""
    lines = [
        "# 盲评包（A/B 对）",
        "",
        instructions
        or (
            "下面每一对 A、B 都是同一道题目写的中文随笔。其中只有一篇是照同一位作者的笔法写的，"
            "另一篇没有。请读完参考稿后，只凭文字判断哪一篇更像同一位作者。\n"
            "每组选 A、B 或「说不清」。不要猜题目、不要查作者名。"
        ),
        "",
        "## 参考稿（同一位作者）",
        "",
    ]
    for index, ref in enumerate(refs):
        lines += [f"### 参考 {index + 1}", "", ref.strip(), ""]
    for pair in public["pairs"]:
        lines += [
            f"## 第 {pair['id']} 对",
            "",
            f"题目：{pair['brief']}",
            "",
            "**A**",
            "",
            pair["A"].strip(),
            "",
            "**B**",
            "",
            pair["B"].strip(),
            "",
            "作答（A / B / 说不清）：",
            "",
        ]
    return "\n".join(lines)


def _rater_template() -> str:
    return (
        "你会看到同一位作者的几篇参考稿，以及同一道题目写的两篇中文随笔 A、B。"
        "其中只有一篇是照这位作者的笔法写的，另一篇不是。\n"
        "只凭文字判断：哪一篇更像同一位作者写的？只回 A 或 B；真分不出回 unsure。\n\n"
        "## 参考稿\n\n{refs}\n\n## A\n\n{a}\n\n## B\n\n{b}"
    )


def _rate_prompt(refs: list[str], pair: dict) -> str:
    ref_text = "\n\n".join(f"### 参考 {i + 1}\n{text.strip()}" for i, text in enumerate(refs))
    return _rater_template().format(refs=ref_text, a=pair["A"].strip(), b=pair["B"].strip())


async def _ask(llm, prompt: str, timeout: float) -> str:
    from ..compat import Message

    response = await asyncio.wait_for(
        llm.chat([Message.user(prompt)], temperature=0.0), timeout=timeout
    )
    return str(getattr(response, "content", "") or "")


async def rate_pairs(
    public: dict,
    refs: list[str],
    *,
    llm,
    model: str,
    repeat: int = 2,
    key: dict | None = None,
    timeout: float = 180.0,
    concurrency: int = 3,
    refs_files: list[str] | None = None,
    kit_sha256: str = "",
    key_sha256: str = "",
) -> dict:
    """跨模型盲评：每对问 repeat 遍，不一致算说不清；给 key 时才算「选中有文风稿」的篇数。

    没有 key 时 `chose_styled/chose_control` 记 None（R-15：不能把非 unsure 都算成选对照）。
    证据记 refs 文件与指纹（R-08：照命令能复现同一个盲评包）。
    """
    if not refs:
        raise ValueError("refs required: 至少给 2 篇作者参考稿")
    if not public.get("side_counts") or len(set(public["side_counts"].values())) != 1:
        raise ValueError("kit is not A/B balanced; rebuild it with `kit` (R-01)")
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(pair: dict) -> dict:
        prompt = _rate_prompt(refs, pair)
        answers: list[str] = []
        async with sem:
            for _ in range(max(1, repeat)):
                answers.append(parse_choice(await _ask(llm, prompt, timeout)))
        choice = answers[0] if len(set(answers)) == 1 else "unsure"
        return {"id": pair["id"], "answers": answers, "choice": choice}

    results = await asyncio.gather(*[one(pair) for pair in public["pairs"]])
    key_pairs = (key or {}).get("pairs", [])
    key_side = {row["id"]: row["styled_side"] for row in key_pairs}
    for row in results:
        row["styled_side"] = key_side.get(row["id"])
    # 位置基线：一个「恒答 A」的 rater 在 balanced kit 上能选中多少篇（R-01）。
    always_a = sum(1 for row in key_pairs if row["styled_side"] == "A") if key_pairs else None
    if key_pairs:
        chose_styled: int | None = sum(1 for row in results if row["styled_side"] == row["choice"])
        chose_control: int | None = sum(
            1
            for row in results
            if row["choice"] not in {"unsure"} and row["choice"] != row["styled_side"]
        )
    else:
        chose_styled = None
        chose_control = None
    return {
        "protocol": "blind-rate-v1",
        "model": model,
        "repeat": repeat,
        "n": len(results),
        "refs_files": refs_files or [],
        "refs_sha256": hashlib.sha256("\n\n".join(refs).encode("utf-8")).hexdigest(),
        "rater_prompt_sha256": hashlib.sha256(_rater_template().encode("utf-8")).hexdigest(),
        "kit_sha256": kit_sha256,
        "key_sha256": key_sha256,
        "side_counts": public.get("side_counts"),
        "always_a_baseline": always_a,
        "chose_styled": chose_styled,
        "chose_control": chose_control,
        "unsure": sum(1 for row in results if row["choice"] == "unsure"),
        "pairs": results,
    }


def _ref_files(paths: list[str], refs_dir: str | None) -> list[Path]:
    files = [Path(path) for path in paths]
    if refs_dir:
        files += sorted(Path(refs_dir).glob("*.md"))
    return files


def _load_refs(paths: list[str], refs_dir: str | None, limit: int = REF_CHARS) -> list[str]:
    files = _ref_files(paths, refs_dir)
    refs: list[str] = []
    for path in files:
        body = path.read_text(encoding="utf-8")
        body = "\n".join(
            line for line in body.split("\n") if not line.lstrip().startswith(("#", "="))
        ).strip()
        if not body:
            continue
        cut = body[:limit]
        stop = cut.rfind("。")
        refs.append(cut[: stop + 1] if stop >= 100 else cut)
    return refs


def _write_json(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    jl._utf8_stdio()
    parser = argparse.ArgumentParser(description="盲评工具：A/B 对 + 跨模型评分")
    sub = parser.add_subparsers(dest="command", required=True)

    kit = sub.add_parser("kit", help="从再创作证据生成盲评包")
    kit.add_argument("--evidence", required=True)
    kit.add_argument("--author", required=True, help="证据 authors 里的 slug")
    kit.add_argument("--refs", action="append", default=[], help="作者参考稿路径（可多次）")
    kit.add_argument("--refs-dir", default=None, help="作者参考稿目录（取全部 .md）")
    kit.add_argument("--n", type=int, default=10)
    kit.add_argument("--seed", type=int, default=358)
    kit.add_argument("--out-kit", required=True, help="公开包 JSON 路径")
    kit.add_argument("--out-key", required=True, help="答案 key JSON 路径")

    rate = sub.add_parser("rate", help="跨模型盲评")
    rate.add_argument("--kit", required=True, help="公开包 JSON")
    rate.add_argument("--key", default=None, help="答案 key（给了才算选中有文风稿）")
    rate.add_argument("--refs", action="append", default=[])
    rate.add_argument("--refs-dir", default=None)
    rate.add_argument("--provider", default="openai")
    rate.add_argument("--model", default="gpt-4o")
    rate.add_argument("--repeat", type=int, default=2)
    rate.add_argument("--concurrency", type=int, default=3)
    rate.add_argument("--timeout", type=float, default=180.0)
    rate.add_argument("--out", default=None)

    args = parser.parse_args(argv)
    if args.command == "kit":
        payload = json.loads(Path(args.evidence).read_text(encoding="utf-8"))
        public, key = build_kit(payload, args.author, seed=args.seed, n=args.n)
        refs = _load_refs(args.refs, args.refs_dir)
        if not refs:
            print("[FAIL] --refs/--refs-dir 至少要给一篇作者参考稿", file=sys.stderr)
            return 2
        public["refs_files"] = [str(path) for path in _ref_files(args.refs, args.refs_dir)]
        _write_json(args.out_kit, public)
        _write_json(args.out_key, key)
        md = format_kit_md(public, refs)
        md_path = Path(args.out_kit).with_suffix(".md")
        md_path.write_text(md, encoding="utf-8", newline="\n")
        print(f"wrote {args.out_kit} / {args.out_key} / {md_path}（{public['n']} 对）")
        return 0

    public = json.loads(Path(args.kit).read_text(encoding="utf-8"))
    key = json.loads(Path(args.key).read_text(encoding="utf-8")) if args.key else None
    refs = _load_refs(args.refs, args.refs_dir)
    if not refs:
        print("[FAIL] --refs/--refs-dir 至少要给一篇作者参考稿", file=sys.stderr)
        return 2

    async def _run() -> dict:
        from ..compat import release_llm_client
        from ..compat import create_llm, ensure_provider_sdks_loaded

        await ensure_provider_sdks_loaded()
        llm = create_llm(args.provider, args.model)
        try:
            model = getattr(llm, "model", args.model)
            return await rate_pairs(
                public,
                refs,
                llm=llm,
                model=model,
                repeat=args.repeat,
                key=key,
                timeout=args.timeout,
                concurrency=args.concurrency,
                refs_files=[str(path) for path in _ref_files(args.refs, args.refs_dir)],
                kit_sha256=hashlib.sha256(Path(args.kit).read_bytes()).hexdigest(),
                key_sha256=(
                    hashlib.sha256(Path(args.key).read_bytes()).hexdigest() if args.key else ""
                ),
            )
        finally:
            await release_llm_client(llm)

    result = asyncio.run(_run())
    baseline = result.get("always_a_baseline")
    print(
        f"== 盲评（rater {result['model']}，{result['repeat']} 遍，A/B 各半）："
        f"选中有文风稿 {result['chose_styled']}/{result['n']}，"
        f"选中对照 {result['chose_control']}，说不清 {result['unsure']}，"
        f"恒答 A 基线 {baseline}"
    )
    if args.out:
        _write_json(args.out, result)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
