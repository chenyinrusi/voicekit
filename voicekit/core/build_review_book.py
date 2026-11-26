"""把 `voice_review.py --dir` 出的整本报告摊成一个能翻、能搜的单文件网页。

为什么要有：61 篇的 markdown 报告散在一个目录里，人翻起来要开 61 个文件；
作者只需要看「哪几篇最差、每段在干什么、哪几句可疑」。网页一页按分数从低到高排，
能搜标题/正文、能按类型筛，点开看全文。

用法（repo 根目录）：
    py -3.10 scripts/voice/build_review_book.py --dir tmp/voice-review-full-v2 --out tmp/review/book.html

只读报告目录与 `catalog.md`，不改任何数据文件；产物是单文件 HTML（无外链）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
DEFAULT_OUT = REPO_ROOT / "tmp/review/book.html"
TYPE_ORDER = ("随笔议论", "叙事记事", "观察吐槽", "书影音评", "清单问答")

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
_TABLE_SEP = re.compile(r"^\|[\s:\-|]+\|$")
_NUM = re.compile(r"^(\d{3})__(.+)\.md$")
_SCORE_BULLET = re.compile(r"接近度：([\d.]+) / 100")
#: 每篇都有这句说明；搜「喊话」会被 61 篇全部命中，显得搜索没用，所以搜索串里去掉。
_BOILERPLATE = re.compile(r"讲事＝讲了一件具体的事.*?可以直接改。", re.S)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _inline(text: str) -> str:
    out = _escape(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _CODE.sub(r"<code>\1</code>", out)
    return out


def md_to_html(text: str) -> str:
    """报告用的 markdown 子集：标题 / 表格 / 列表 / 段落 / 粗体 / 行内代码。"""
    lines = text.splitlines()
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("#"):
            level = min(len(s) - len(s.lstrip("#")), 4)
            out.append(f"<h{level}>{_inline(s.lstrip('#').strip())}</h{level}>")
            i += 1
            continue
        if s.startswith("|") and i + 1 < n and _TABLE_SEP.match(lines[i + 1].strip()):
            head = [c.strip() for c in s.strip("|").split("|")]
            i += 2
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            out.append(
                '<div class="tw"><table><thead><tr>'
                + "".join(f"<th>{_inline(c)}</th>" for c in head)
                + "</tr></thead><tbody>"
                + "".join(
                    "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>"
                    for r in rows
                )
                + "</tbody></table></div>"
            )
            continue
        if re.match(r"^[-*] ", s):
            items = []
            while i < n and re.match(r"^[-*] ", lines[i].strip()):
                items.append(_inline(lines[i].strip()[2:]))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue
        if re.match(r"^\d+\. ", s):
            items = []
            while i < n and re.match(r"^\d+\. ", lines[i].strip()):
                items.append(_inline(re.sub(r"^\d+\.\s*", "", lines[i].strip())))
                i += 1
            out.append("<ol>" + "".join(f"<li>{x}</li>" for x in items) + "</ol>")
            continue
        para = []
        while i < n:
            cur = lines[i].strip()
            if not cur or cur.startswith(("#", "|", "- ", "* ")) or re.match(r"^\d+\. ", cur):
                break
            para.append(cur)
            i += 1
        if not para:
            # 落单的 |xxx / --- / >xxx：当成普通段落吃掉一行。
            # 不这样处理，这一轮 i 不前进，while 会原地死循环（2026-11-09 review 发现）。
            para = [lines[i].strip()]
            i += 1
        out.append("<p>" + _inline(" ".join(para)) + "</p>")
    return "\n".join(out)


def parse_catalog(path: Path) -> dict[str, dict]:
    """catalog.md → {篇号: {"type", "mood", "title"}}；文件不在就返回空表。"""
    if not path.is_file():
        return {}
    out: dict[str, dict] = {}
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)（\d+\s*篇）", line)
        if heading:
            current = heading.group(1).strip()
            continue
        row = re.match(r"^\|\s*(\d{3})\*?\s*\|", line)
        if not row or not current:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 6:
            out[row.group(1)] = {"type": current, "mood": cells[4], "title": cells[1]}
    return out


def _number(path: Path, text: str) -> tuple[float, int, int]:
    """分 / 过线数 / 总线数：优先读同名 JSON，没有就从正文那行抓。"""
    js = path.with_suffix(".json")
    if js.is_file():
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
            style = data.get("style") or {}
            return (
                float(style.get("score", 0)),
                int(style.get("passed", 0)),
                int(style.get("total", 42)),
            )
        except (ValueError, TypeError):
            pass
    match = _SCORE_BULLET.search(text)
    passed = re.search(r"过 (\d+)/(\d+)", text)
    return (
        float(match.group(1)) if match else 0.0,
        int(passed.group(1)) if passed else 0,
        int(passed.group(2)) if passed else 42,
    )


def build_payload(dir_path: Path, catalog: Path = CATALOG) -> dict:
    tags = parse_catalog(catalog)
    cards = []
    for path in sorted(dir_path.glob("*.md")):
        if path.name == "index.md":
            continue
        match = _NUM.match(path.name)
        if not match:
            continue
        num, title = match.group(1), match.group(2)
        text = path.read_text(encoding="utf-8")
        score, passed, total = _number(path, text)
        body = text.splitlines()
        # 首行是 h1 标题，summary 里已经有了
        if body and body[0].startswith("# "):
            body = body[1:]
        html = md_to_html("\n".join(body))
        tag = tags.get(num, {})
        cards.append(
            {
                "num": num,
                "title": title,
                "score": score,
                "passed": passed,
                "total": total,
                "type": tag.get("type", "未分类"),
                "mood": tag.get("mood", ""),
                "html": html,
                "search": re.sub(
                    r"\s+", " ", f"{num} {title} {_BOILERPLATE.sub('', text)}"
                ).lower(),
            }
        )
    cards.sort(key=lambda c: (c["score"], c["num"]))
    scores = [c["score"] for c in cards]
    types = []
    for card in cards:
        if card["type"] not in types and card["type"] != "未分类":
            types.append(card["type"])
    types.sort(key=lambda t: (TYPE_ORDER.index(t) if t in TYPE_ORDER else len(TYPE_ORDER), t))
    payload = {
        "kind": "voice-review-book",
        "cards": cards,
        "types": types,
        "stats": {
            "n": len(cards),
            "avg": round(sum(scores) / len(scores), 1) if scores else 0,
            "min": min(scores) if scores else 0,
            "max": max(scores) if scores else 0,
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            [{k: c[k] for k in ("num", "score", "html")} for c in cards],
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:12]
    payload["fingerprint"] = fingerprint
    return payload


def render_html(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    stats = payload["stats"]
    return (
        _TEMPLATE.replace("__DATA__", data)
        .replace("__FP__", payload["fingerprint"])
        .replace("__N__", str(stats["n"]))
        .replace("__AVG__", str(stats["avg"]))
        .replace("__MIN__", str(stats["min"]))
        .replace("__MAX__", str(stats["max"]))
    )


def _selftest() -> int:
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "001__甲篇.md").write_text(
            "# 文风审稿报告：001__甲篇.md\n\n"
            "- 与作者语料接近度：80.0 / 100，42 条里过 34/42\n\n"
            "## 一、每段在干什么\n\n- 讲事 3 段\n\n"
            "| 段 | 字 | 在干什么 |\n|---|---|---|\n| 1 | 120 | 讲事 |\n",
            encoding="utf-8",
        )
        (tmp_path / "002__乙篇.md").write_text(
            "# 文风审稿报告：002__乙篇.md\n\n"
            "- 与作者语料接近度：60.0 / 100，42 条里过 25/42\n\n"
            "## 二、要你看一眼的句子\n\n1. **喊口号**：你就该努力。\n",
            encoding="utf-8",
        )
        payload = build_payload(tmp_path, catalog=tmp_path / "nope.md")
        if len(payload["cards"]) != 2:
            problems.append(f"cards={len(payload['cards'])}")
        if [c["num"] for c in payload["cards"]] != ["002", "001"]:
            problems.append("没按分数从低到高排")
        if payload["cards"][1]["score"] != 80.0:
            problems.append("分数没从正文抓到")
        if "<table>" not in payload["cards"][1]["html"]:
            problems.append("表格没转成 HTML")
        if "<ol>" not in payload["cards"][0]["html"]:
            problems.append("编号列表没转成 HTML")
        html = render_html(payload)
        if "http" in html.split("</head>")[0]:
            problems.append("head 里有外链（应自包含）")
        if "__DATA__" in html or "__FP__" in html:
            problems.append("占位符没替换")
        changed = build_payload(tmp_path, catalog=tmp_path / "nope.md")
        changed["cards"][0]["score"] = 1.0
        fp2 = hashlib.sha256(
            json.dumps(
                [{k: c[k] for k in ("num", "score", "html")} for c in changed["cards"]],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:12]
        if fp2 == payload["fingerprint"]:
            problems.append("指纹不随数据变")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print("[OK] selftest: 报告转换、排序、自包含、指纹都过")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>全书文风审稿报告（__N__ 篇）</title>
<style>
:root { --fg:#1b1b1b; --muted:#6b6b6b; --line:#e2e2e2; --bg:#faf9f7; --card:#fff;
        --bad:#b3261e; --mid:#8a5a00; --good:#1f6f4a; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
header { position: sticky; top:0; z-index:5; background:#fffffff2; backdrop-filter: blur(6px);
         border-bottom:1px solid var(--line); padding:12px 16px; }
.inner { max-width: 900px; margin:0 auto; }
h1 { font-size: 19px; margin:0 0 4px; }
.lead { color:var(--muted); font-size:13px; margin:0 0 8px; }
.stats { display:flex; gap:14px; flex-wrap:wrap; font-size:13px; color:var(--muted); margin-bottom:8px; }
.stats b { color:var(--fg); font-variant-numeric: tabular-nums; }
.bar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
input[type=search] { font:inherit; padding:8px 12px; border:1px solid var(--line); border-radius:999px;
                     min-height:40px; flex:1 1 200px; min-width:0; }
button { font:inherit; border:1px solid var(--line); background:#fff; color:var(--fg);
         border-radius:999px; padding:6px 12px; min-height:36px; cursor:pointer; }
button[aria-pressed="true"] { background:#1f6f4a; border-color:#1f6f4a; color:#fff; }
main { max-width: 900px; margin:0 auto; padding:14px 16px 60px; }
details.ch { background:var(--card); border:1px solid var(--line); border-radius:12px; margin:10px 0; }
details.ch[hidden] { display:none; }
summary { list-style:none; cursor:pointer; padding:12px 14px; display:flex; gap:10px;
          align-items:baseline; flex-wrap:wrap; }
summary::-webkit-details-marker { display:none; }
.score { font-variant-numeric: tabular-nums; font-weight:700; min-width:52px; }
.s-bad { color:var(--bad); } .s-mid { color:var(--mid); } .s-good { color:var(--good); }
.num { color:var(--muted); font-size:13px; font-variant-numeric: tabular-nums; }
.title { font-weight:600; }
.tags { color:var(--muted); font-size:12px; }
.body { padding:0 14px 14px; border-top:1px solid var(--line); }
.body h2 { font-size:16px; margin:16px 0 6px; }
.body h3 { font-size:14px; margin:14px 0 4px; color:var(--muted); }
.body p, .body li { font-size:14px; }
.body ul, .body ol { padding-left:20px; }
.tw { overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { border-collapse:collapse; font-size:13px; width:100%; margin:8px 0; }
th, td { border:1px solid var(--line); padding:5px 7px; text-align:left; vertical-align:top; }
th { background:#f3f2ef; font-weight:600; white-space:nowrap; }
code { background:#f0efec; padding:1px 4px; border-radius:4px; font-size:13px; }
.empty { color:var(--muted); text-align:center; padding:30px 0; }
</style>
</head>
<body>
<header>
  <div class="inner">
    <h1>全书文风审稿报告（__N__ 篇）</h1>
    <p class="lead">本地规则跑，0 元；判官层没跑。按分数<b>从低到高</b>排，最差的在最上面。
    点一条展开全文；能搜标题和正文，也能按类型筛。指纹 __FP__。</p>
    <div class="stats">
      <span>平均 <b>__AVG__</b></span>
      <span>最低 <b>__MIN__</b></span>
      <span>最高 <b>__MAX__</b></span>
      <span>显示 <b id="shown">__N__</b> 篇</span>
    </div>
    <div class="bar">
      <input id="q" type="search" placeholder="搜标题或正文里的词，比如「问句」「喊话」">
      <button id="all" aria-pressed="true">全部</button>
      <span id="types"></span>
    </div>
  </div>
</header>
<main id="list"></main>
<p class="empty" id="empty" hidden>没有匹配的篇。</p>

<script>
const DATA = __DATA__;
const list = document.getElementById("list");
let typeFilter = "全部";
let query = "";

function scoreClass(v) { return v < 60 ? "s-bad" : (v < 75 ? "s-mid" : "s-good"); }

DATA.cards.forEach(c => {
  const d = document.createElement("details");
  d.className = "ch";
  d.dataset.type = c.type;
  d.dataset.text = c.search;
  d.innerHTML =
    '<summary><span class="score ' + scoreClass(c.score) + '">' + c.score.toFixed(1) +
    '</span><span class="num">' + c.num + '</span><span class="title">' + c.title +
    '</span><span class="tags">' + c.type + (c.mood ? "·" + c.mood : "") +
    " · 过 " + c.passed + "/" + c.total + "</span></summary>" +
    '<div class="body">' + c.html + "</div>";
  list.appendChild(d);
});

const types = document.getElementById("types");
DATA.types.forEach(t => {
  const b = document.createElement("button");
  b.textContent = t;
  b.onclick = () => setType(t);
  b.dataset.type = t;
  types.appendChild(b);
});
function setType(t) {
  typeFilter = t;
  document.getElementById("all").setAttribute("aria-pressed", t === "全部" ? "true" : "false");
  types.querySelectorAll("button").forEach(b =>
    b.setAttribute("aria-pressed", b.dataset.type === t ? "true" : "false"));
  apply();
}
document.getElementById("all").onclick = () => setType("全部");
document.getElementById("q").oninput = e => { query = e.target.value.trim().toLowerCase(); apply(); };

function apply() {
  let shown = 0;
  list.querySelectorAll("details.ch").forEach(d => {
    const hitType = typeFilter === "全部" || d.dataset.type === typeFilter;
    const hitText = !query || d.dataset.text.includes(query);
    const hit = hitType && hitText;
    d.hidden = !hit;
    if (hit) shown++;
  });
  document.getElementById("shown").textContent = shown;
  document.getElementById("empty").hidden = shown > 0;
}
apply();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="整本文风审稿报告 → 单文件网页")
    ap.add_argument("--dir", type=Path, required=False,
                    help="voice_review.py --dir 的输出目录")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--catalog", type=Path, default=CATALOG)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.dir:
        ap.error("要 --dir（报告目录）")
    payload = build_payload(args.dir, args.catalog)
    if not payload["cards"]:
        print(f"[FAIL] {args.dir} 里没找到 <篇号>__<标题>.md", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_html(payload), encoding="utf-8", newline="\n")
    stats = payload["stats"]
    print(
        f"[OK] wrote {args.out} — {stats['n']} 篇，平均 {stats['avg']}，"
        f"最低 {stats['min']}，指纹 {payload['fingerprint']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
