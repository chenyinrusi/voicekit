"""判断库：把语料里「他的判断 / 联想 / 不确定」抽成一库，供起稿时真复用。

为什么是 JSONL 而不是向量库（2026-11-11 实测）：59 篇 + 2 份手稿只抽出 **274 条**候选句
（约 17K 字）。这么小的库，字面检索 <1ms，而且**能给出原句与篇号**——复用错立场是我们最怕的错
（作者刚否掉过一句「认错是容易的」），向量相似恰恰最容易返回「像但立场不同」的句子。
先用字面检索量 precision@3；只有漏召严重时才考虑 embedding，并且要先量增益。

两个入口：
- `find` 给关键词找相似句（人拿着问）；
- `for_material` 给一篇材料挑可复用的旧句（`draft.py` 起稿时用）：tf-idf 余弦 + 绝对/相对双门槛，
  门槛不过就返回空——空着才能「全部推断」，硬给三条不相关的旧句会引来立场硬贴。

用法：
    py -3.10 scripts/voice/judgments.py --write          # 重建 judgments.jsonl
    py -3.10 scripts/voice/judgments.py --find "道歉 信任" --limit 3
    py -3.10 scripts/voice/judgments.py --for 材料.md
    py -3.10 scripts/voice/judgments.py --selftest
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from .voice_text import corpus_path  # noqa: E402

#: 判断库跟着包走：`voices/panchen-voice/judgments.jsonl` 这种位置只在开发树成立，
#: 装成 wheel 后包由 `--pack` 给，所以改成懒路径。
def out_path() -> Path:
    from ..paths import active_pack

    return active_pack() / "judgments.jsonl"


#: 开发期重建判断库时额外收的两篇手稿，**只给 `--write` 用**，运行时路径不碰它们。
#: 语料之外的文件用短篇号：提示词里要求标 `〔复用·篇号〕`，篇号得能塞进标记里。
#: 这里的 parents[1] 指本仓库根（E:\voicekit），是开发目录不是作者包路径。
_REPO_ROOT = SCRIPT_DIR.parents[1]
EXTRA = (
    ("H1", _REPO_ROOT / "tmp/voice-live/huang-author.md"),
    ("H2", _REPO_ROOT / "dev/reports/voice/huang-author-final-20261111.md"),
)

#: 类型 → 规则。一句可命中多类，按 KINDS 顺序记主类型。
KINDS: list[tuple[str, re.Pattern]] = [
    ("自我关联", re.compile(r"(?:我这个人|我这辈子|我从小|我也会|我是不是|我总觉得|我这个状态)")),
    ("联想", re.compile(r"(?:想起|想到|让我想到|忽然想|好像|像是|类似的|跟[^。！？]{1,8}一样)")),
    ("后果", re.compile(r"(?:最坏|到头来|将来|会变成|一旦|到时候|早晚)")),
    ("不确定", re.compile(r"(?:我不知道|不知道|说不清|拿不准|未必|也许|怕是)")),
    ("为什么", re.compile(r"(?:为什么|怎么会|大约因为|怕是因为|大概因为|想来)")),
    ("判断", re.compile(r"(?:我觉得|我认为|其实|本来|根本|不过是|无非)")),
]
SENT = re.compile(r"[^。！？!?…]+[。！？!?…]*")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.strip().split("\n") if p.strip()]


def extract() -> list[dict]:
    rows: list[dict] = []
    corpus = corpus_path()
    if corpus is None:
        raise SystemExit("[FAIL] 这个包没有 corpus：重建判断库要读作者原文（--pack 指向带语料的包）")
    sources = [(path.stem[:3], path.read_text(encoding="utf-8")) for path in sorted(corpus.glob("*.md"))]
    sources += [(doc_id, path.read_text(encoding="utf-8")) for doc_id, path in EXTRA if path.exists()]
    for doc_id, text in sources:
        for para_index, paragraph in enumerate(_paragraphs(text), 1):
            for sent in SENT.findall(paragraph):
                body = "".join(sent.split())
                if "我" not in body or len(body) < 12:
                    continue
                kinds = [kind for kind, pattern in KINDS if pattern.search(body)]
                if not kinds:
                    continue
                rows.append(
                    {"doc": doc_id, "para": para_index, "kind": kinds[0],
                     "kinds": kinds, "text": body}
                )
    # 同句只留一条（同一篇里重复出现的引语）
    seen: set[str] = set()
    unique: list[dict] = []
    for row in rows:
        key = row["text"][:40]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write(rows: list[dict] | None = None) -> int:
    rows = rows if rows is not None else extract()
    path = out_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def load() -> list[dict]:
    path = out_path()
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _grams(text: str, n: int = 2) -> Counter:
    body = re.sub(r"[\s，。！？、；：…“”‘’（）()《》\"'.\-—]", "", text)
    return Counter(body[i:i + n] for i in range(max(len(body) - n + 1, 0)))


def find(query: str, kind: str | None = None, limit: int = 3,
         rows: list[dict] | None = None) -> list[dict]:
    """字面检索：2 字片段重合度打分；返回带出处与原句的候选（给人核，不自动用）。"""
    rows = rows if rows is not None else load()
    want = _grams(query)
    if not want:
        return []
    scored = []
    for row in rows:
        if kind and row["kind"] != kind and kind not in row.get("kinds", []):
            continue
        have = _grams(row["text"])
        overlap = sum(min(count, have[gram]) for gram, count in want.items())
        if not overlap:
            continue
        scored.append((overlap / max(sum(want.values()), 1), row))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1]["text"])))
    return [dict(row, score=round(score, 3)) for score, row in scored[:limit]]


def _idf(rows: list[dict]) -> dict[str, float]:
    """2 字片段的逆文档频率：库内到处都有的片段（「我不」「不知」）降权。"""
    df: Counter = Counter()
    for row in rows:
        df.update(set(_grams(row["text"])))
    total = len(rows)
    return {gram: math.log((total + 1) / (count + 0.5)) for gram, count in df.items()}


#: 余弦低于这个值就不推荐。宁可返回空：他的规矩是「没有过去的例子就推断」，
#: 硬给三条不相关的旧句，模型会把立场硬贴上去——那比没得复用时更糟。
MATERIAL_FLOOR = 0.03
#: 还要过「相对门槛」：低于最高分的这一比例就不要。绝对分数随材料长度漂，
#: 相对门槛才是真正压掉陪跑项的那一刀（2026-11-11 用黄仁勋那篇校准：0.087 是正主，
#: 0.067 同题，0.026 是「多年/信任」凑上的噪声 → 0.35 刚好切掉）。
MATERIAL_RATIO = 0.35


def for_material(text: str, limit: int = 3, rows: list[dict] | None = None,
                 exclude: set[str] | None = None, ratio: float | None = None) -> list[dict]:
    """起稿用：拿一篇材料当查询，找「他以前对同类事说过的判断」。

    打分是 2 字片段的 tf-idf 余弦（`_grams` + `_idf`），同一篇只留一条。
    `exclude`：不许出现的篇号——**重写他某一篇时，必须排除这篇自己**，否则等于把答案递过去
    （2026-11-11 实测：032 的材料检索第一条就是篇 032 自己的原句，把实验污染了）。
    `ratio`：相对门槛，默认 `MATERIAL_RATIO`；取 0 表示只要过绝对门槛就要，用于「多给几条素材」。
    shortcut: 只算字面相似，不判立场是否一致；候选**必须**由人核，
    报告里给出原句与篇号就是为了这一步。若实测 precision@3 长期低于 1/3，再考虑换检索方式。
    """
    rows = rows if rows is not None else load()
    exclude = exclude or set()
    rows = [row for row in rows if row["doc"] not in exclude]
    if not rows or not text.strip():
        return []
    idf = _idf(rows)
    default = math.log(len(rows) + 1)

    def vec(body: str) -> dict[str, float]:
        return {gram: (1 + math.log(count)) * idf.get(gram, default)
                for gram, count in _grams(body).items()}

    query = vec(text)
    query_norm = math.sqrt(sum(weight * weight for weight in query.values()))
    if not query_norm:
        return []
    scored: list[tuple[float, dict]] = []
    for row in rows:
        have = vec(row["text"])
        norm = math.sqrt(sum(weight * weight for weight in have.values()))
        if not norm:
            continue
        dot = sum(weight * have[gram] for gram, weight in query.items() if gram in have)
        if dot:
            scored.append((dot / (query_norm * norm), row))
    scored.sort(key=lambda pair: -pair[0])
    if not scored:
        return []
    cutoff = MATERIAL_FLOOR if ratio is None else max(MATERIAL_FLOOR, ratio * scored[0][0])
    if ratio is not None and ratio <= 0:
        cutoff = 0.0
    picked: list[dict] = []
    seen_docs: set[str] = set()
    for score, row in scored:
        if score < cutoff or row["doc"] in seen_docs:
            continue
        seen_docs.add(row["doc"])
        picked.append(dict(row, score=round(score, 3)))
        if len(picked) >= limit:
            break
    return picked


def _selftest() -> int:
    rows = [
        {"doc": "001", "para": 1, "kind": "联想", "kinds": ["联想"],
         "text": "倒是想起之前一个朋友发给我看的一篇文章。"},
        {"doc": "002", "para": 1, "kind": "不确定", "kinds": ["不确定"],
         "text": "我不知道以后会不会遇到这样的问题。"},
    ]
    hit = find("想起朋友的文章", rows=rows)
    assert hit and hit[0]["doc"] == "001", hit
    assert find("完全不相干的词", rows=rows) == []
    assert find("我不知道", kind="不确定", rows=rows)[0]["doc"] == "002"
    assert all(row["kinds"] for row in extract()[:20])
    # 材料级检索：只挑对题的，同一篇只留一条，差太远就空着
    pool = rows + [
        {"doc": "003", "para": 1, "kind": "判断", "kinds": ["判断"],
         "text": "我觉得，不管她是不是间谍，美国政治都会把她定义成间谍。"},
        {"doc": "003", "para": 2, "kind": "联想", "kinds": ["联想"],
         "text": "我觉得，不管她是不是间谍，美国政治早晚会这样。"},
        {"doc": "004", "para": 1, "kind": "联想", "kinds": ["联想"],
         "text": "倒是想起之前一个朋友发给我看的一篇文章。"},
    ]
    hits = for_material("据媒体报道，她被指为间谍，美国政治会怎样定义她？", rows=pool)
    assert hits and hits[0]["doc"] == "003", hits
    assert sum(1 for hit in hits if hit["doc"] == "003") == 1, hits
    assert for_material("完全不相干的词", rows=pool) == []
    print("[OK] selftest: 字面检索、类型过滤、材料级检索、抽取都过")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="判断库：抽取 + 字面检索")
    ap.add_argument("--write", action="store_true", help="重建 judgments.jsonl")
    ap.add_argument("--find", help="检索一句相似判断")
    ap.add_argument("--for", dest="for_material", type=Path, help="起稿用：给一篇材料找可复用的旧句")
    ap.add_argument("--kind", help="只看某一类（联想/为什么/后果/不确定/判断/自我关联）")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--pack", type=Path, help="作者包目录（默认找 ./voices/）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    from ..paths import use_pack

    use_pack(args.pack)
    if args.selftest:
        return _selftest()
    if args.write:
        rows = extract()
        count = write(rows)
        kinds = Counter(row["kind"] for row in rows)
        print(f"写入 {out_path()}：{count} 条")
        for kind, number in kinds.most_common():
            print(f"  {kind}: {number}")
        return 0
    if args.for_material:
        body = args.for_material.read_text(encoding="utf-8")
        hits = for_material(body, args.limit)
        if not hits:
            print("（这篇材料没有够近的旧句——按他的规矩，全部推断）")
            return 0
        for hit in hits:
            print(f"[{hit['score']:.3f}] 篇{hit['doc']} · {hit['kind']}：{hit['text']}")
        return 0
    if args.find:
        hits = find(args.find, args.kind, args.limit)
        if not hits:
            print("（没有相似句）")
            return 0
        for hit in hits:
            print(f"[{hit['score']:.2f}] 篇{hit['doc']} · {hit['kind']}：{hit['text']}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
