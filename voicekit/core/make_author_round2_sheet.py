"""作者复核页（第二轮）：场景→笔调 + 全书 54 篇格调抽检。

为什么单独一页：这两件事只能作者答（创作场景偏好、格调标签的直觉），
机器答不了；做成离线 HTML，手机也能点。

用法：
    py -3.10 scripts/voice/make_author_round2_sheet.py --out tmp/choice/round2.html
答案串形如：``s1=甲 s2=都 … m002=混 m005=严 …``，底部「看答案串 / 复制」可一键取出。

不改任何数据文件；本页只读 catalog.md 与语料，产物是临时 HTML + 题目指纹 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
OUT_JSON = REPO_ROOT / "tmp/choice/round2-questions.json"

MOODS = ("严肃", "混合", "轻松")
MOOD_CODE = {"严肃": "严", "混合": "混", "轻松": "轻"}
# 第一轮已抽检过的 5 篇；034/037 是调参污染，不抽。
CHECKED = {"001", "003", "008", "012", "013"}
POLLUTED = {"034", "037"}

SCENES = [
    ("s1", "开头第一段", "一篇随笔刚起来，先给读者一个画面或一件事"),
    ("s2", "记一件事的经过", "讲清发生了什么，时间、地点、人物、动作"),
    ("s3", "讲道理 / 下判断", "对一件事说出自己的看法"),
    ("s4", "自嘲 / 吐槽", "拿自己开玩笑，或吐槽生活里的小毛病"),
    ("s5", "结尾", "收尾那几句"),
    ("s6", "书影音评", "读完一本、看完一部之后的感想"),
    ("s7", "清单 / 问答体", "分条回答别人问的问题"),
    ("s8", "对读者喊话 / 劝慰", "直接对读者说「你」"),
]

CJK = re.compile(r"[\u4e00-\u9fff]")


def _read_catalog() -> list[dict]:
    text = CATALOG.read_text(encoding="utf-8")
    rows = []
    for line in text.splitlines():
        m = re.match(
            r"\|\s*(\d{3})\*?\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*(严肃|混合|轻松)\s*\|",
            line,
        )
        if not m:
            continue
        rows.append(
            {"num": m.group(1), "title": m.group(2).strip(), "cjk": int(m.group(3)), "mood": m.group(5)}
        )
    return rows


def _excerpts(num: str) -> tuple[str, str]:
    src = next(CORPUS.glob(f"{num}__*.md"))
    lines = [x.strip() for x in src.read_text(encoding="utf-8").split("\n")[1:] if x.strip()]
    body = "".join(lines)
    first = re.split(r"(?<=[。！？])", body)[0][:80]
    tail = body[-120:]
    last = re.split(r"(?<=[。！？])", tail)[-2:] if "。" in tail else [tail]
    last_txt = "".join(last)[-80:]
    return first, last_txt


def build_payload() -> dict:
    rows = _read_catalog()
    chapters = []
    for r in rows:
        if r["num"] in CHECKED or r["num"] in POLLUTED:
            continue
        opening, closing = _excerpts(r["num"])
        chapters.append(
            {
                "num": r["num"],
                "title": r["title"],
                "cjk": r["cjk"],
                "current": r["mood"],
                "opening": opening,
                "closing": closing,
            }
        )
    data = {
        "kind": "author-round2",
        "moods": list(MOODS),
        "scenes": [{"id": i, "name": n, "hint": h} for i, n, h in SCENES],
        "chapters": chapters,
        "skipped_checked": sorted(CHECKED),
        "skipped_polluted": sorted(POLLUTED),
    }
    data["fingerprint"] = hashlib.sha256(
        json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return data


def render_html(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    tpl = _TEMPLATE
    return (
        tpl.replace("__PAYLOAD__", payload)
        .replace("__FP__", data["fingerprint"])
        .replace("__N_SCENES__", str(len(data["scenes"])))
        .replace("__N_CH__", str(len(data["chapters"])))
    )


def write(data: dict, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(data), encoding="utf-8", newline="\n")
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _selftest() -> int:
    data = build_payload()
    problems = []
    if len(data["chapters"]) != 54:
        problems.append(f"chapters={len(data['chapters'])}, want 54")
    if len(data["scenes"]) != 8:
        problems.append(f"scenes={len(data['scenes'])}, want 8")
    if {c["num"] for c in data["chapters"]} & CHECKED:
        problems.append("已抽检的篇又被放进来了")
    if {c["num"] for c in data["chapters"]} & POLLUTED:
        problems.append("034/037 被放进来了")
    for c in data["chapters"]:
        if c["current"] not in MOODS or not c["opening"] or not c["closing"]:
            problems.append(f"bad chapter {c['num']}")
    html = render_html(data)
    if "__PAYLOAD__" in html or "__FP__" in html:
        problems.append("模板占位符没替换")
    if "http" in html.split("</head>")[0]:
        problems.append("head 里有外链（应自包含）")
    # 指纹要能区分数据
    changed = json.loads(json.dumps(data))
    changed["chapters"][0]["mood_hint"] = "x"
    fp2 = hashlib.sha256(
        json.dumps(changed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    if fp2 == data["fingerprint"]:
        problems.append("指纹不随数据变")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print(f"[OK] selftest: {len(data['scenes'])} 场景 + {len(data['chapters'])} 篇，指纹 {data['fingerprint']}")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>作者复核（第二轮）</title>
<style>
:root { --fg:#1b1b1b; --muted:#6b6b6b; --line:#e2e2e2; --bg:#faf9f7; --card:#fff;
        --pick:#1f6f4a; --pickbg:#e8f4ee; --warn:#8a5a00; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
main { padding: 16px 16px 120px; max-width: 760px; margin: 0 auto; }
h1 { font-size: 20px; margin: 8px 0 4px; }
.lead { color: var(--muted); font-size: 14px; margin: 0 0 14px; }
h2 { font-size: 17px; margin: 26px 0 6px; padding-top: 14px; border-top: 1px solid var(--line); }
.tip { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
        padding: 12px 14px; margin: 10px 0; }
.card.done { border-color: var(--pick); background: var(--pickbg); }
.head { display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; }
.num { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 13px; }
.title { font-weight: 600; }
.meta { color: var(--muted); font-size: 12px; }
.quote { margin: 8px 0 10px; color: #333; font-size: 14px; }
.quote .k { color: var(--muted); }
.opts { display: flex; gap: 8px; flex-wrap: wrap; }
button { font: inherit; border: 1px solid var(--line); background: #fff; color: var(--fg);
         border-radius: 999px; padding: 7px 14px; min-height: 40px; cursor: pointer; }
button:hover { border-color: #bbb; }
button[aria-pressed="true"] { background: var(--pick); border-color: var(--pick); color: #fff; }
button.cur { box-shadow: inset 0 0 0 2px #d8d8d8; }
.bar { position: fixed; left: 0; right: 0; bottom: 0; background: #fffffff2; backdrop-filter: blur(6px);
       border-top: 1px solid var(--line); padding: 8px 12px calc(8px + env(safe-area-inset-bottom));
       display: flex; gap: 8px; align-items: center; }
.bar .grow { flex: 1; font-size: 14px; color: var(--muted); }
#sheet { position: fixed; inset: 0; background: #fff; display: none; padding: 16px; overflow: auto; z-index: 20; }
#sheet.on { display: block; }
#sheet textarea { width: 100%; min-height: 180px; font: 14px/1.6 ui-monospace, Consolas, monospace;
                  border: 1px solid var(--line); border-radius: 10px; padding: 10px; }
#sheet .row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
.fp { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>
<main>
  <h1>作者复核（第二轮）</h1>
  <p class="lead">两件事只能你定：<b>哪些场景用哪种笔调</b>、<b>全书格调的标签对不对</b>。
  答完点底部「看答案串 / 复制」，把那一串发我就行。答过的会自动存在本机。</p>

  <h2>A. 场景配笔调</h2>
  <p class="tip">甲 = 长句、像你嘴上说出来的那种；乙 = 书里常见的那种分布。选「都行」也可以。</p>
  <div id="scenes"></div>

  <h2>B. 格调抽检（__N_CH__ 篇）</h2>
  <p class="tip">每篇给开头和结尾两句。现在机器标成的那档我标了「当前」，你只需要点你认的那档；
  跳过就留空。都不像可以点「说不清」。</p>
  <div id="chapters"></div>
</main>

<div class="bar">
  <span class="grow" id="progress">已答 0 / __N_SCENES__ 场景，0 / __N_CH__ 篇</span>
  <button id="open">看答案串</button>
</div>

<div id="sheet">
  <div class="row">
    <button id="close">关掉</button>
    <button id="copy">复制</button>
    <button id="reset">清空</button>
  </div>
  <p class="fp">指纹 __FP__（换题后旧答案自动作废）</p>
  <textarea id="out" readonly></textarea>
</div>

<script>
const DATA = __PAYLOAD__;
const FP = "__FP__";
const KEY = "author-round2-" + FP;
const SCENE_OPTS = ["甲", "乙", "都行"];
const st = { s: {}, m: {} };
DATA.scenes.forEach(x => st.s[x.id] = "");
DATA.chapters.forEach(x => st.m[x.num] = "");

function load() {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) { const j = JSON.parse(raw); if (j.s && j.m) { Object.assign(st.s, j.s); Object.assign(st.m, j.m); } }
  } catch (e) {}
}
function save() { try { localStorage.setItem(KEY, JSON.stringify(st)); } catch (e) {} }

function answerString() {
  const parts = [];
  DATA.scenes.forEach(x => { if (st.s[x.id]) parts.push(x.id + "=" + st.s[x.id]); });
  DATA.chapters.forEach(x => {
    if (!st.m[x.num]) return;
    parts.push("m" + x.num + "=" + (MOOD_CODE[st.m[x.num]] || "说不清"));
  });
  return parts.join(" ");
}
function progress() {
  const a = DATA.scenes.filter(x => st.s[x.id]).length;
  const b = DATA.chapters.filter(x => st.m[x.num]).length;
  document.getElementById("progress").textContent =
    "已答 " + a + " / " + DATA.scenes.length + " 场景，" + b + " / " + DATA.chapters.length + " 篇";
}
function paint() {
  document.getElementById("out").value = answerString();
  progress();
  document.querySelectorAll("[data-kind]").forEach(btn => {
    const kind = btn.dataset.kind, id = btn.dataset.id, val = btn.dataset.val;
    const picked = (kind === "s" ? st.s[id] : st.m[id]);
    btn.setAttribute("aria-pressed", picked === val ? "true" : "false");
  });
  document.querySelectorAll(".card[data-card]").forEach(card => {
    const kind = card.dataset.card, id = card.dataset.id;
    card.classList.toggle("done", !!(kind === "s" ? st.s[id] : st.m[id]));
  });
}
const MOODS = ["严肃","混合","轻松"];
const MOOD_CODE = {"严肃":"严","混合":"混","轻松":"轻"};

function build() {
  const sc = document.getElementById("scenes");
  DATA.scenes.forEach(x => {
    const d = document.createElement("div");
    d.className = "card"; d.dataset.card = "s"; d.dataset.id = x.id;
    d.innerHTML = '<div class="head"><span class="num">' + x.id + '</span>' +
      '<span class="title">' + x.name + '</span></div><p class="quote">' + x.hint + '</p>';
    const opts = document.createElement("div"); opts.className = "opts";
    SCENE_OPTS.forEach(v => {
      const b = document.createElement("button");
      b.textContent = v; b.dataset.kind = "s"; b.dataset.id = x.id; b.dataset.val = v;
      b.onclick = () => { st.s[x.id] = (st.s[x.id] === v ? "" : v); save(); paint(); };
      opts.appendChild(b);
    });
    d.appendChild(opts); sc.appendChild(d);
  });
  const ch = document.getElementById("chapters");
  DATA.chapters.forEach(x => {
    const d = document.createElement("div");
    d.className = "card"; d.dataset.card = "m"; d.dataset.id = x.num;
    d.innerHTML = '<div class="head"><span class="num">' + x.num + '</span>' +
      '<span class="title">' + x.title + '</span>' +
      '<span class="meta">' + x.cjk + ' 字 · 当前：' + x.current + '</span></div>' +
      '<p class="quote"><span class="k">开头</span> ' + x.opening + '<br>' +
      '<span class="k">结尾</span> ' + x.closing + '</p>';
    const opts = document.createElement("div"); opts.className = "opts";
    MOODS.forEach(v => {
      const b = document.createElement("button");
      b.textContent = v; b.dataset.kind = "m"; b.dataset.id = x.num; b.dataset.val = v;
      if (v === x.current) b.classList.add("cur");
      b.onclick = () => { st.m[x.num] = (st.m[x.num] === v ? "" : v); save(); paint(); };
      opts.appendChild(b);
    });
    const u = document.createElement("button");
    u.textContent = "说不清";
    u.dataset.kind = "m"; u.dataset.id = x.num; u.dataset.val = "说不清";
    u.onclick = () => { st.m[x.num] = (st.m[x.num] === "说不清" ? "" : "说不清"); save(); paint(); };
    opts.appendChild(u);
    d.appendChild(opts); ch.appendChild(d);
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
  DATA.scenes.forEach(x => st.s[x.id] = "");
  DATA.chapters.forEach(x => st.m[x.num] = "");
  save(); paint();
};

load(); build(); paint();
</script>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="作者复核页：场景配笔调 + 54 篇格调抽检")
    ap.add_argument("--out", default=str(REPO_ROOT / "tmp/choice/round2.html"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    data = build_payload()
    out = Path(args.out)
    write(data, out)
    print(f"[OK] wrote {out}（{len(data['scenes'])} 场景 + {len(data['chapters'])} 篇，指纹 {data['fingerprint']}）")
    print(f"[OK] questions json: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
