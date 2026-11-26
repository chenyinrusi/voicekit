"""作者复核页（第四轮）：把类型改过的 26 篇给全文，让作者看全文再确认一次。

为什么要有这页：第三轮每篇只给开头/中间/结尾三段（约 270 字），作者据此改了 25 篇的类型，
但一篇 1200–3500 字，三段代表不了全篇。这页给全文，并把**上次给他看的三段高亮**，
他自己就能看出上次是不是被那三段带偏了。

用法：
    py -3.10 scripts/voice/make_author_type_full.py --out tmp/choice/type-full.html
答案串形如：``c004=书 c007=叙``（只列点过的），「看不出来」记 ``c002=说不清``。

只读 catalog.md、语料与证据表；不改任何数据文件。
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from . import make_author_type_sheet as base  # noqa: E402

REPO_ROOT = base.REPO_ROOT
ANSWERS = (
    REPO_ROOT
    / "dev/fixes/FIX-369-preference-elicitation/evidence/type-sheet-answers-20261108.md"
)
OUT_JSON = REPO_ROOT / "tmp/choice/type-full-questions.json"

TYPES = base.TYPES
TYPE_CODE = base.TYPE_CODE
TYPE_HINT = base.TYPE_HINT
WANT = 26  # 改过的 25 篇 + 1 篇「说不清」


def parse_answers() -> dict[str, dict]:
    """证据文档里的对照表 → {篇号: {"old": 机器标, "last": 作者上次判} }。

    表里的单元格可能带括注（如「说不清（保留机器标，标待定）」），括注去掉再认。
    """
    out: dict[str, dict] = {}
    for line in ANSWERS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(\d{3})\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*$", line)
        if m:
            out[m.group(1)] = {
                "old": re.sub(r"（.*?）", "", m.group(2)).strip(),
                "last": re.sub(r"（.*?）", "", m.group(3)).strip(),
            }
    return out


def _full_text(num: str, excerpts: dict[str, str]) -> str:
    """整篇正文；上次给他看的那几段包 <mark>，让他看见自己上次读的是哪几段。"""
    needles = [e.rstrip("…") for e in excerpts.values() if e]
    blocks = []
    for para in base._paragraphs(num):
        text = html.escape(para)
        if any(para in n or (len(para) > 20 and para[:20] in n) for n in needles):
            text = f'<mark class="seen">{text}</mark>'
        blocks.append(f"<p>{text}</p>")
    return "\n".join(blocks)


def build_payload() -> dict:
    """这页是第三轮的历史快照：old/last 都取自证据表，不跟当前 catalog 比（第四轮之后会不一致）。"""
    answers = parse_answers()
    catalog = {r["num"]: r for r in base._read_catalog()}
    chapters = []
    for num in sorted(answers):
        info = catalog.get(num)
        if not info:
            raise SystemExit(f"[FAIL] catalog 里没有 {num}")
        excerpts = base._excerpts(num)
        chapters.append(
            {
                "num": num,
                "title": info["title"],
                "cjk": info["cjk"],
                "tone": info["tone"],
                "old": answers[num]["old"],
                "last": answers[num]["last"],
                "html": _full_text(num, excerpts),
            }
        )
    data = {
        "kind": "author-type-full",
        "types": [{"name": t, "code": TYPE_CODE[t], "hint": TYPE_HINT[t]} for t in TYPES],
        "chapters": chapters,
    }
    data["fingerprint"] = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return data


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__PAYLOAD__", payload)
        .replace("__FP__", data["fingerprint"])
        .replace("__N_CH__", str(len(data["chapters"])))
    )


def write(data: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8", newline="\n")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(
        json.dumps(
            {k: v for k, v in data.items() if k != "chapters"} | {
                "chapters": [
                    {k: c[k] for k in ("num", "title", "cjk", "old", "last")}
                    for c in data["chapters"]
                ]
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n",
        encoding="utf-8",
    )


def _selftest() -> int:
    data = build_payload()
    problems: list[str] = []
    nums = [c["num"] for c in data["chapters"]]
    if len(nums) != WANT:
        problems.append(f"chapters={len(nums)}, want {WANT}")
    if len(set(nums)) != len(nums):
        problems.append("有重复篇号")
    for c in data["chapters"]:
        if c["old"] not in TYPES:
            problems.append(f"{c['num']} 旧标不认识：{c['old']}")
        if c["last"] not in TYPES and c["last"] != "说不清":
            problems.append(f"{c['num']} 上次判的不认识：{c['last']}")
        if c["last"] == c["old"]:
            problems.append(f"{c['num']} 没改过却被放进来了")
        if c["html"].count("<p>") < 2:
            problems.append(f"{c['num']} 全文太短，不像整篇")
        if '<mark class="seen">' not in c["html"]:
            problems.append(f"{c['num']} 上次给他看的段落没高亮")
    html_text = render_html(data)
    if "__PAYLOAD__" in html_text or "__FP__" in html_text or "__N_CH__" in html_text:
        problems.append("模板占位符没替换")
    if "http" in html_text.split("</head>")[0]:
        problems.append("head 里有外链（应自包含）")
    changed = json.loads(json.dumps(data))
    changed["chapters"][0]["last"] = "观察吐槽"
    fp2 = hashlib.sha256(
        json.dumps(changed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    if fp2 == data["fingerprint"]:
        problems.append("指纹不随数据变")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print(f"[OK] selftest: {len(nums)} 篇全文，指纹 {data['fingerprint']}")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>作者复核（第四轮）：看全文定类型</title>
<style>
:root { --fg:#1b1b1b; --muted:#6b6b6b; --line:#e2e2e2; --bg:#faf9f7; --card:#fff;
        --pick:#1f6f4a; --pickbg:#e8f4ee; --seen:#fff3c4; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.7 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
main { padding: 16px 16px 120px; max-width: 780px; margin: 0 auto; }
h1 { font-size: 20px; margin: 8px 0 4px; }
.lead { color: var(--muted); font-size: 14px; margin: 0 0 12px; }
.legend { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 10px 14px; margin: 0 0 14px; font-size: 14px; }
.legend div { margin: 3px 0; }
.legend .t { font-weight: 600; }
.legend .h { color: var(--muted); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 12px 14px; margin: 12px 0; }
.card.done { border-color: var(--pick); background: var(--pickbg); }
.head { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.num { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 13px; }
.title { font-weight: 600; }
.meta { color: var(--muted); font-size: 12px; }
.badges { display:flex; gap:8px; flex-wrap:wrap; margin: 6px 0 2px; font-size: 12px; color: var(--muted); }
.badge { border:1px solid var(--line); border-radius:999px; padding:2px 10px; }
.badge.last { border-color:#b9d8c7; color:#1f6f4a; }
details.full { margin: 8px 0; border:1px solid var(--line); border-radius:10px; }
details.full summary { cursor:pointer; padding:8px 12px; color:var(--muted); font-size:13px; }
details.full .text { padding: 4px 14px 10px; font-size: 15px; }
details.full p { margin: 10px 0; }
mark.seen { background: var(--seen); padding: 1px 2px; border-radius: 3px; }
.tip { color:var(--muted); font-size:12px; margin: 6px 0 4px; }
.opts { display:flex; gap:8px; flex-wrap:wrap; }
button { font:inherit; border:1px solid var(--line); background:#fff; color:var(--fg);
         border-radius:999px; padding:7px 14px; min-height:40px; cursor:pointer; }
button:hover { border-color:#bbb; }
button[aria-pressed="true"] { background:var(--pick); border-color:var(--pick); color:#fff; }
button.cur { box-shadow: inset 0 0 0 2px #d8d8d8; }
.bar { position:fixed; left:0; right:0; bottom:0; background:#fffffff2; backdrop-filter: blur(6px);
       border-top:1px solid var(--line); padding:8px 12px calc(8px + env(safe-area-inset-bottom));
       display:flex; gap:8px; align-items:center; }
.bar .grow { flex:1; font-size:14px; color:var(--muted); }
#sheet { position:fixed; inset:0; background:#fff; display:none; padding:16px; overflow:auto; z-index:20; }
#sheet.on { display:block; }
#sheet textarea { width:100%; min-height:180px; font:14px/1.6 ui-monospace, Consolas, monospace;
                  border:1px solid var(--line); border-radius:10px; padding:10px; }
#sheet .row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; }
.fp { color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<main>
  <h1>类型复核（第四轮）：__N_CH__ 篇看全文</h1>
  <p class="lead">上次每篇只给了三段（约 270 字），你据此改了 25 篇的类型。这次给<b>全文</b>：
  点开「看全文」，里面<b style="background:var(--seen);padding:0 4px;border-radius:3px">黄色高亮</b>的就是上次给你看的那三段。
  看完点一个类型；你上次的判断标了「上次」，点它等于不变。跳过就留空。</p>

  <div class="legend" id="legend"></div>
  <div id="chapters"></div>
</main>

<div class="bar">
  <span class="grow" id="progress">已答 0 / __N_CH__ 篇</span>
  <button id="open">看答案串</button>
</div>

<div id="sheet">
  <div class="row">
    <button id="close">关掉</button>
    <button id="copy">复制</button>
    <button id="reset">清空</button>
  </div>
  <p class="fp">指纹 __FP__（换题后旧答案自动作废）· 代码：随=随笔议论 叙=叙事记事 观=观察吐槽 书=书影音评 清=清单问答</p>
  <textarea id="out" readonly></textarea>
</div>

<script>
const DATA = __PAYLOAD__;
const FP = "__FP__";
const KEY = "author-type-full-" + FP;
const CODES = {};
DATA.types.forEach(t => CODES[t.name] = t.code);
const st = {};
DATA.chapters.forEach(x => st[x.num] = "");

function load() {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) Object.assign(st, JSON.parse(raw));
  } catch (e) {}
}
function save() { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} }

function answerString() {
  const parts = [];
  DATA.chapters.forEach(x => {
    if (!st[x.num]) return;
    parts.push("c" + x.num + "=" + (CODES[st[x.num]] || "说不清"));
  });
  return parts.join(" ");
}
function progress() {
  const a = DATA.chapters.filter(x => st[x.num]).length;
  document.getElementById("progress").textContent = "已答 " + a + " / " + DATA.chapters.length + " 篇";
}
function paint() {
  document.getElementById("out").value = answerString();
  progress();
  document.querySelectorAll("[data-num]").forEach(btn => {
    const same = st[btn.dataset.num] === btn.dataset.val;
    btn.setAttribute("aria-pressed", same ? "true" : "false");
    btn.classList.toggle("cur", !st[btn.dataset.num] && btn.dataset.val === btn.dataset.last);
  });
  document.querySelectorAll(".card[data-card]").forEach(card => {
    card.classList.toggle("done", !!st[card.dataset.id]);
  });
}

function pick(num, val) {
  st[num] = (st[num] === val ? "" : val);
  save(); paint();
}

function build() {
  const lg = document.getElementById("legend");
  DATA.types.forEach(t => {
    const d = document.createElement("div");
    d.innerHTML = '<span class="t">' + t.name + '（' + t.code + '）</span> ' +
      '<span class="h">' + t.hint + '</span>';
    lg.appendChild(d);
  });
  const box = document.getElementById("chapters");
  DATA.chapters.forEach(x => {
    const d = document.createElement("div");
    d.className = "card"; d.dataset.card = "t"; d.dataset.id = x.num;
    const head = document.createElement("div");
    head.className = "head";
    head.innerHTML = '<span class="num">' + x.num + '</span>' +
      '<span class="title">' + x.title + '</span>' +
      '<span class="meta">' + x.cjk + ' 字 · ' + x.tone + '</span>';
    const badges = document.createElement("div");
    badges.className = "badges";
    badges.innerHTML = '<span class="badge">机器标：' + x.old + '</span>' +
      '<span class="badge last">上次你判：' + x.last + '</span>';
    const det = document.createElement("details");
    det.className = "full";
    det.innerHTML = '<summary>看全文（' + x.cjk + ' 字；黄色＝上次给你看的三段）</summary>' +
      '<div class="text">' + x.html + '</div>';
    const tip = document.createElement("p");
    tip.className = "tip"; tip.textContent = "看完全文再点：";
    const opts = document.createElement("div");
    opts.className = "opts";
    DATA.types.forEach(t => {
      const b = document.createElement("button");
      b.textContent = t.name;
      b.dataset.num = x.num; b.dataset.val = t.name;
      b.dataset.last = x.last === t.name ? t.name : "";
      b.onclick = () => pick(x.num, t.name);
      opts.appendChild(b);
    });
    const u = document.createElement("button");
    u.textContent = "看不出来";
    u.dataset.num = x.num; u.dataset.val = "说不清";
    u.dataset.last = x.last === "说不清" ? "说不清" : "";
    u.onclick = () => pick(x.num, "说不清");
    opts.appendChild(u);
    d.appendChild(head); d.appendChild(badges); d.appendChild(det);
    d.appendChild(tip); d.appendChild(opts); box.appendChild(d);
  });
}

document.getElementById("open").onclick = () => { paint(); document.getElementById("sheet").classList.add("on"); };
document.getElementById("close").onclick = () => document.getElementById("sheet").classList.remove("on");
document.getElementById("copy").onclick = async () => {
  const t = document.getElementById("out");
  t.select();
  try { await navigator.clipboard.writeText(t.value); }
  catch (e) { document.execCommand("copy"); }
  document.getElementById("copy").textContent = "已复制";
  setTimeout(() => document.getElementById("copy").textContent = "复制", 1200);
};
document.getElementById("reset").onclick = () => {
  if (!confirm("清空本页所有答案？")) return;
  DATA.chapters.forEach(x => st[x.num] = "");
  save(); paint();
};

load(); build(); paint();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="作者复核页：26 篇看全文定类型")
    ap.add_argument("--out", default=str(REPO_ROOT / "tmp/choice/type-full.html"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    data = build_payload()
    out = Path(args.out)
    write(data, out)
    print(f"[OK] wrote {out}（{len(data['chapters'])} 篇全文，指纹 {data['fingerprint']}）")
    print(f"[OK] questions json: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
