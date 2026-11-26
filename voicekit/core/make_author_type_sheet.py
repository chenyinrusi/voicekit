"""作者复核页（第三轮）：全书 56 篇的类型标签对不对。

为什么单独一页：格调 2026-11-08 已降级成参考，策略卡与参照集改成**按类型**分桶
（`build_strategy_card.py` / `build_catalog.py`），类型成了唯一的键；但类型只抽检过 5 篇
（001/003/008/012/013，5/5 对）。这页把剩下 56 篇交给作者点一遍。

用法：
    py -3.10 scripts/voice/make_author_type_sheet.py --out tmp/choice/type.html
答案串形如：``t002=叙 t005=随 t034=随``，底部「看答案串 / 复制」一键取出。

不改任何数据文件；只读 catalog.md 与语料，产物是临时 HTML + 题目指纹 JSON。
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
OUT_JSON = REPO_ROOT / "tmp/choice/type-questions.json"

TYPES = ("随笔议论", "叙事记事", "观察吐槽", "书影音评", "清单问答")
TYPE_CODE = {"随笔议论": "随", "叙事记事": "叙", "观察吐槽": "观", "书影音评": "书", "清单问答": "清"}
TYPE_HINT = {
    "随笔议论": "一件事起头，主要说自己的看法",
    "叙事记事": "主要讲发生了什么",
    "观察吐槽": "看别人、看现象，边看边吐槽",
    "书影音评": "读完一本、看完一部之后的感想",
    "清单问答": "分条回答别人问的问题",
}

#: 2026-11-08 第一轮标签抽检过的 5 篇（类型 5/5 对），不重复问。
CHECKED = {"001", "003", "008", "012", "013"}
WANT = 56  # 61 - 5

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _read_catalog() -> list[dict]:
    """按 catalog.md 的二级标题取类型，表格行取篇号/标题/字数/格调。"""
    rows: list[dict] = []
    genre = ""
    for line in CATALOG.read_text(encoding="utf-8").splitlines():
        head = re.match(r"^##\s+(\S+?)（(\d+) 篇）", line)
        if head:
            genre = head.group(1)
            continue
        m = re.match(
            r"\|\s*(\d{3})\*?\s*\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*([\d.]+)\s*\|\s*(严肃|混合|轻松)\s*\|",
            line,
        )
        if m and genre in TYPES:
            rows.append(
                {"num": m.group(1), "title": m.group(2).strip(), "cjk": int(m.group(3)),
                 "tone": m.group(5), "genre": genre}
            )
    return rows


def _paragraphs(num: str) -> list[str]:
    src = next(CORPUS.glob(f"{num}__*.md"))
    text = src.read_text(encoding="utf-8")
    # 语料段间只有一个换行（少数文件另有空行）；按行切，空行丢掉，第一行是标题。
    blocks = [b.strip() for b in text.split("\n") if b.strip()]
    return blocks[1:] or blocks


def _clip(text: str, limit: int = 90) -> str:
    """截到 limit 个汉字左右，断在句号上。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind("。"), cut.rfind("！"), cut.rfind("？"))
    return cut[: end + 1] if end >= limit // 2 else cut + "…"


def _take(paras: list[str], start: int, step: int, want: int = 90, limit: int = 110) -> str:
    """从 start 往 step 方向攒段，攒够 want 个汉字就停（短段太短时往后并几段）。"""
    out: list[str] = []
    got = 0
    idx = start
    while 0 <= idx < len(paras) and got < want:
        out.append(paras[idx])
        got += len(_CJK.findall(paras[idx]))
        idx += step
    text = "".join(out if step > 0 else reversed(out))
    return _clip(text, limit)


def _excerpts(num: str) -> dict[str, str]:
    paras = _paragraphs(num)
    # 中间段：挑接近正中间、且本身够长的段（语料里有「但最终……」这类一句成段的）
    long_idx = [i for i, p in enumerate(paras) if len(_CJK.findall(p)) >= 60]
    mid = min(long_idx, key=lambda i: abs(i - len(paras) // 2)) if long_idx else len(paras) // 2
    return {
        "opening": _take(paras, 0, 1),
        "middle": _take(paras, mid, 1),
        "closing": _take(paras, len(paras) - 1, -1),
    }


def build_payload() -> dict:
    chapters = []
    for r in _read_catalog():
        if r["num"] in CHECKED:
            continue
        item = {**r, **_excerpts(r["num"])}
        item["current"] = item.pop("genre")
        chapters.append(item)
    data = {
        "kind": "author-type",
        "types": [{"name": t, "code": TYPE_CODE[t], "hint": TYPE_HINT[t]} for t in TYPES],
        "chapters": chapters,
        "skipped_checked": sorted(CHECKED),
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
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _selftest() -> int:
    data = build_payload()
    problems = []
    nums = {c["num"] for c in data["chapters"]}
    if len(nums) != WANT:
        problems.append(f"chapters={len(nums)}, want {WANT}")
    if nums & CHECKED:
        problems.append("已抽检的篇又被放进来了")
    if len(TYPE_CODE) != len(TYPES) or len(set(TYPE_CODE.values())) != len(TYPES):
        problems.append("类型代码有重复")
    for c in data["chapters"]:
        if c["current"] not in TYPES:
            problems.append(f"{c['num']} 当前类型不认识：{c['current']}")
        if not (c["opening"] and c["middle"] and c["closing"]):
            problems.append(f"{c['num']} 摘要有空")
        if any(len(c[k]) < 40 for k in ("opening", "middle", "closing")):
            problems.append(f"{c['num']} 摘要太短")
        if any(len(c[k]) > 140 for k in ("opening", "middle", "closing")):
            problems.append(f"{c['num']} 摘要太长")
    html = render_html(data)
    if "__PAYLOAD__" in html or "__FP__" in html or "__N_CH__" in html:
        problems.append("模板占位符没替换")
    if "http" in html.split("</head>")[0]:
        problems.append("head 里有外链（应自包含）")
    changed = json.loads(json.dumps(data))
    changed["chapters"][0]["current"] = "观察吐槽"
    fp2 = hashlib.sha256(
        json.dumps(changed, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    if fp2 == data["fingerprint"]:
        problems.append("指纹不随数据变")
    if problems:
        print("[FAIL] selftest: " + "; ".join(problems))
        return 1
    print(f"[OK] selftest: {len(nums)} 篇，指纹 {data['fingerprint']}")
    return 0


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>作者复核（第三轮）：类型标签</title>
<style>
:root { --fg:#1b1b1b; --muted:#6b6b6b; --line:#e2e2e2; --bg:#faf9f7; --card:#fff;
        --pick:#1f6f4a; --pickbg:#e8f4ee; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
       font: 16px/1.65 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
main { padding: 16px 16px 120px; max-width: 760px; margin: 0 auto; }
h1 { font-size: 20px; margin: 8px 0 4px; }
.lead { color: var(--muted); font-size: 14px; margin: 0 0 10px; }
.legend { background: var(--card); border: 1px solid var(--line); border-radius: 12px;
          padding: 10px 14px; margin: 0 0 14px; font-size: 14px; }
.legend div { margin: 3px 0; }
.legend .t { font-weight: 600; }
.legend .h { color: var(--muted); }
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
  <h1>作者复核（第三轮）：类型标签</h1>
  <p class="lead">格调已经从硬指标降成参考了，现在查骨架、找参照都只看<b>类型</b>。
  类型只抽检过 5 篇（5 篇都对），剩下 __N_CH__ 篇请你点一遍。
  每篇给开头、中间一段、结尾；认哪类就点哪类，跳过就留空。答过的存在本机。</p>

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
const KEY = "author-type-" + FP;
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
    parts.push("t" + x.num + "=" + (CODES[st[x.num]] || "说不清"));
  });
  return parts.join(" ");
}
function progress() {
  const a = DATA.chapters.filter(x => st[x.num]).length;
  document.getElementById("progress").textContent =
    "已答 " + a + " / " + DATA.chapters.length + " 篇";
}
function paint() {
  document.getElementById("out").value = answerString();
  progress();
  document.querySelectorAll("[data-num]").forEach(btn => {
    btn.setAttribute("aria-pressed", st[btn.dataset.num] === btn.dataset.val ? "true" : "false");
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
    d.innerHTML = '<div class="head"><span class="num">' + x.num + '</span>' +
      '<span class="title">' + x.title + '</span>' +
      '<span class="meta">' + x.cjk + ' 字 · 机器标：' + x.current + '</span></div>' +
      '<p class="quote"><span class="k">开头</span> ' + x.opening + '<br>' +
      '<span class="k">中间</span> ' + x.middle + '<br>' +
      '<span class="k">结尾</span> ' + x.closing + '</p>';
    const opts = document.createElement("div"); opts.className = "opts";
    DATA.types.forEach(t => {
      const b = document.createElement("button");
      b.textContent = t.name;
      b.dataset.num = x.num; b.dataset.val = t.name;
      if (t.name === x.current) b.classList.add("cur");
      b.onclick = () => pick(x.num, t.name);
      opts.appendChild(b);
    });
    const u = document.createElement("button");
    u.textContent = "说不清";
    u.dataset.num = x.num; u.dataset.val = "说不清";
    u.onclick = () => pick(x.num, "说不清");
    opts.appendChild(u);
    d.appendChild(opts); box.appendChild(d);
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
    ap = argparse.ArgumentParser(description="作者复核页：56 篇类型标签抽检")
    ap.add_argument("--out", default=str(REPO_ROOT / "tmp/choice/type.html"))
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    data = build_payload()
    out = Path(args.out)
    write(data, out)
    print(f"[OK] wrote {out}（{len(data['chapters'])} 篇，指纹 {data['fingerprint']}）")
    print(f"[OK] questions json: {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
