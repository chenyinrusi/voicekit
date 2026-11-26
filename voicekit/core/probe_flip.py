"""探针：每条属性有多「表面可翻」？（决定它该算便宜还是贵）

做法：拿他自己语料里的篇，做 8 种**不改内容**的表面手术（只动标点、断句、引号、语气词、
塞一句通用元话语），看每条属性的判定翻了没有。翻得动的 = 便宜（改形式就能刷），
翻不动的 = 贵（至少要动内容才挪得动）。

`weights.json` 的分组用这个表校对；本探针只打印「实测 vs 现行分组」，不改文件——
表格是人维护的，探针负责指出错标。

用法：py -3.10 scripts/voice/probe_flip.py [--docs N]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))
from . import voice_lib as lib  # noqa: E402

PROFILE = REPO_ROOT / "voices/panchen-voice/profile.json"
WEIGHTS = REPO_ROOT / "voices/panchen-voice/weights.json"
CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
SKIP = {"034", "037"}
#: 判定阈值：可翻率 >= CHEAP_RATE 算便宜，< EXPENSIVE_RATE 算贵；中间是「说不准」，不动。
CHEAP_RATE = 0.10
EXPENSIVE_RATE = 0.02


def _cycle_particles(text: str) -> str:
    marks = ["吧", "呢", "啊", "嘛"]
    out, index = [], 0
    for piece in re.split(r"(。)", text):
        if piece == "。":
            out.append(piece)
            out.append(marks[index % len(marks)] + "。")
            index += 1
        else:
            out.append(piece)
    return "".join(out).replace("。。", "。")


def _quote_every_sentence(text: str) -> str:
    return re.sub(r"([^。！？\n]+)。", lambda m: "“" + m.group(1) + "”。", text)


#: 表面手术分两族：①只动标点/断句；②只塞词（四字格、文言、二称、情绪词、网络语、叠词、欧化句式）。
#: 两族都不加事实、不加材料——这正是「便宜」的定义：不动内容也能把判定挪过去。
#: 最后一条塞通用元话语，是以前真踩过的坑（补裁判点名的小词）。
TRANSFORMS = {
    "逗号→句号": lambda t: t.replace("，", "。"),
    "顿号→逗号": lambda t: t.replace("、", "，"),
    "句号→逗号": lambda t: t.replace("。", "，") + "。",
    "句号→问号": lambda t: t.replace("。", "？"),
    "句号→感叹号": lambda t: t.replace("。", "！"),
    "每句套引号": _quote_every_sentence,
    "句尾加语气词": _cycle_particles,
    "塞一句通用元话语": lambda t: t.replace("。", "。说来也是好笑，"),
    "塞四字格": lambda t: t.replace("，", "，总而言之，", 1).replace("\n\n", "\n\n不言而喻，"),
    "文言替换": lambda t: t.replace("的", "之"),
    "塞二称（你）": lambda t: t.replace("\n\n", "\n\n你知道吗，"),
    "塞情绪词": lambda t: t.replace("。", "。真是让人心里难受，"),
    "塞网络流行语": lambda t: t.replace("。", "。绝了。"),
    "塞叠词": lambda t: t.replace("我", "我慢慢"),
    "塞欧化句式": lambda t: t.replace("。", "。关于这一点，"),
}


def docs_for(limit: int | None) -> list[tuple[str, str]]:
    out = []
    for path in sorted(CORPUS.glob("*.md")):
        doc = path.name.split("__")[0]
        if doc in SKIP:
            continue
        lines = [x.strip() for x in path.read_text(encoding="utf-8").split("\n") if x.strip()]
        body = "\n".join(lines[1:])
        if len("".join(body.split())) < 200:
            continue
        out.append((doc, body))
    return out[:limit] if limit else out


def verdicts(text: str, profile: dict) -> dict[str, bool]:
    result = lib.compare(profile, lib.resolve_values(profile, lib.measure(text)))
    return {r["attribute_id"]: r["ok"] for r in result["rows"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=None, help="只跑前 N 篇（默认全部）")
    ap.add_argument("--write", action="store_true", help="按实测可翻率重写 weights.json 的分档")
    args = ap.parse_args(argv)
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    current = lib.load_weights()["table"]
    docs = docs_for(args.docs)
    ids = [a["attribute_id"] for a in profile["attributes"]]
    names = {a["attribute_id"]: a["attribute_name"] for a in profile["attributes"]}
    flips = {aid: 0 for aid in ids}
    for _doc, body in docs:
        base = verdicts(body, profile)
        for transform in TRANSFORMS.values():
            after = verdicts(transform(body), profile)
            for aid in ids:
                if base[aid] != after[aid]:
                    flips[aid] += 1
    attempts = len(docs) * len(TRANSFORMS)
    rates = {aid: flips[aid] / attempts for aid in ids}
    print(f"篇数 {len(docs)} × 手术 {len(TRANSFORMS)} = {attempts} 次判定；"
          f"便宜线 {CHEAP_RATE:.0%}、贵线 {EXPENSIVE_RATE:.0%}\n")
    print(f"{'属性':<22}{'可翻率':>8}{'现行':>6}  实测")
    rows = sorted(ids, key=lambda a: rates[a], reverse=True)
    mismatches = []
    for aid in rows:
        rate = rates[aid]
        weight = current.get(aid, 1.0)
        want = _tier_name(rate)
        have = {0.5: "便宜", 1.0: "中", 2.0: "贵"}.get(weight, str(weight))
        flag = ""
        if want != have:
            flag = f"  [MISMATCH 实测={want} 现={have}]"
            mismatches.append((aid, want, have))
        print(f"{aid + ' ' + names[aid]:<22}{rate:>7.0%}{have:>6}  {want}{flag}")
    print(f"\n没对上的 {len(mismatches)} 条："
          + ("、".join(f"{a}(实测{want}/现{have})" for a, want, have in mismatches) or "无"))
    if args.write:
        _write_weights(profile, ids, rates, len(docs), attempts)
    return 0


def _tier_name(rate: float) -> str:
    if rate >= CHEAP_RATE:
        return "便宜"
    return "贵" if rate < EXPENSIVE_RATE else "中"


_TIER_WEIGHT = {"便宜": 0.5, "中": 1.0, "贵": 2.0}
_TIER_WHY = {
    "便宜": "试过的表面手术（15 种：标点/断句/引号/语气词/四字格/文言/二称/情绪词/网络语/叠词/欧化/元话语）里，"
            "有 ≥10% 的判定会被翻动——不动内容就能挪，所以减重。",
    "中": "可翻率在 2%–10%：表面手段偶尔能挪，但不够稳。"
          "不高估也不低估（2026-11-10 起按实测分档，不再人工判断）。",
    "贵": "15 种表面手术一次都没翻动（可翻率 <2%）：要挪它，得换真东西（材料、引语、时空、"
          "他自己的词、真问题）。这只是「没被证明便宜」，不是证明它必须动内容。",
}


def _write_weights(profile: dict, ids: list[str], rates: dict[str, float],
                   n_docs: int, attempts: int) -> None:
    data = json.loads(WEIGHTS.read_text(encoding="utf-8"))
    groups: dict[str, list[str]] = {"便宜": [], "中": [], "贵": []}
    for aid in ids:
        groups[_tier_name(rates[aid])].append(aid)
    data["tiers"] = [
        {"name": name, "weight": _TIER_WEIGHT[name], "why": _TIER_WHY[name], "ids": groups[name]}
        for name in ("便宜", "中", "贵")
    ]
    data["measurement"] = {
        "probe": "scripts/voice/probe_flip.py",
        "transform_kinds": len(TRANSFORMS),
        "docs": n_docs,
        "attempts": attempts,
        "thresholds": {"cheap": CHEAP_RATE, "expensive": EXPENSIVE_RATE},
        "flip_rate": {aid: round(rates[aid], 4) for aid in ids},
        "updated": "2026-11-10",
    }
    data["tier_sizes"] = {name: len(groups[name]) for name in groups}
    total = sum(_TIER_WEIGHT[name] * len(groups[name]) for name in groups)
    data["tier_sizes"]["total_weight"] = round(total, 2)
    WEIGHTS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写回 {WEIGHTS.name}："
          + "、".join(f"{name} {len(groups[name])} 条" for name in groups)
          + f"；总权重 {total:.1f}")


if __name__ == "__main__":
    raise SystemExit(main())
