"""一张纸问三件事：混装 10 段、5 条标签抽检、A/B 目标。

为什么合在一页：三件事都是「作者二选一」型判断（DD-VOICE-002 §15.1），分三次问就要三次
开页面、三次回答案串；合成一页只花他 8 分钟，答案也只有一个串。

三段：
1. 混装（m01–m10）：5 段他的真稿件 + 5 段按他口吻仿写，打乱。判「像我 / 不像我」+ 原因。
   真值只写进 key.json，页面里看不到。
   **注意（2026-11-08 实测）**：这一段的「像我/不像我」**不能给作者本人答**——他认得自己的稿，
   作者当自己稿子的盲判员不成立（他全判「像我」）。要测区分力，把同样的题面交给没有语料的
   判官（`judge.py` 或 fresh subagent），或者改成整章 + 强制二选一。给作者答的只有第 3 段的偏好题。
2. 标签抽检（l1–l5）：FIX-371 的类型/格调标签，每个类型取第一篇，给他看开头与结尾，
   判「都对 / 类型不对 / 格调不对 / 都不对」。
3. 目标（t）：改稿/仿写以后往哪个方向调——长句版 / 短句版 / 分场景。两版原文见
   `dev/fixes/FIX-369-preference-elicitation/evidence/ab/`。

用法（repo 根目录）：
    python scripts/voice/make_choice_sheet.py                 # 写到 tmp/choice/
    python scripts/voice/make_choice_sheet.py --selftest      # 只自检，不写文件

答案串形如：m01=1 m02=2c … l1=Y l3=M~格调该是轻松 … t=1
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402
from . import voice_review as vr  # noqa: E402

CORPUS = bp.DEFAULT_CORPUS
CATALOG = REPO_ROOT / "dev/fixes/FIX-371-style-triage/catalog.md"
EVIDENCE = REPO_ROOT / "dev/fixes/FIX-369-preference-elicitation/evidence"
MIX = EVIDENCE / "mix-10.json"
AB_DIR = EVIDENCE / "ab"
OUT = REPO_ROOT / "tmp/choice"

ITEM_COUNT = 10
#: 固定种子：证据文件按「同一话题 真/仿」成对存，直接上页面会露馅（成对出现）。
#: 出页面时再洗一次，顺序固定、可复算，但看不出配对。
SHEET_SEED = 20261108
LABEL_TYPES = ("随笔议论", "叙事记事", "观察吐槽", "书影音评", "清单问答")
EXCERPT_CHARS = 110
AB_EXCERPT_CHARS = 120

REASONS = [
    ["a", "太顺了，像范文"],
    ["b", "书面词太多"],
    ["c", "像鸡汤，在讲道理"],
    ["d", "不是我的说法"],
    ["e", "接不上前文"],
    ["f", "别的（下面补一句）"],
]


def parse_catalog(path: Path = CATALOG) -> list[dict]:
    """catalog.md → [{num, title, mood, type, ...}]，顺序保持文件顺序。"""
    rows: list[dict] = []
    current = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        heading = re.match(r"^##\s+(.+?)（\d+\s*篇）", line)
        if heading:
            current = heading.group(1).strip()
            continue
        row = re.match(r"^\|\s*(\d{3})\*?\s*\|", line)
        if not row or current not in LABEL_TYPES:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        rows.append(
            {
                "num": row.group(1),
                "title": cells[1],
                "cjk": cells[2],
                "mood": cells[4],
                "counts": cells[5],
                "note": cells[6],
                "type": current,
            }
        )
    return rows


def corpus_path(num: str) -> Path | None:
    for path in sorted(CORPUS.glob(f"{num}__*.md")):
        return path
    return None


def _clip(text: str, chars: int) -> str:
    """截到句读处，别把半句话甩给他看。"""
    text = re.sub(r"\s+", "", text)
    if len(text) <= chars:
        return text
    head = text[:chars]
    cut = max(head.rfind("。"), head.rfind("，"), head.rfind("；"), head.rfind("？"))
    return (head[: cut + 1] if cut > chars * 0.5 else head) + "…"


def chapter_excerpts(num: str) -> tuple[str, str]:
    path = corpus_path(num)
    if path is None:
        raise SystemExit(f"corpus file for {num} not found")
    paras = vr._paragraphs(path.read_text(encoding="utf-8"))
    if not paras:
        raise SystemExit(f"no paragraphs in {num}")
    return _clip(paras[0], EXCERPT_CHARS), _clip(paras[-1], EXCERPT_CHARS)


def label_items(catalog: list[dict]) -> list[dict]:
    """每个类型取第一篇（034/037 不参与），给开头和结尾让他判标签。"""
    picked: dict[str, dict] = {}
    for row in catalog:
        if row["num"].startswith(bp.EXCLUDED_DOC_PREFIXES):
            continue
        picked.setdefault(row["type"], row)
    items: list[dict] = []
    for order, typ in enumerate(LABEL_TYPES, 1):
        row = picked.get(typ)
        if row is None:
            continue
        opening, closing = chapter_excerpts(row["num"])
        items.append(
            {
                "id": f"l{order}",
                "kind": "label",
                "title": row["title"],
                "type": row["type"],
                "mood": row["mood"],
                "opening": opening,
                "closing": closing,
                "_key": {"num": row["num"], "type": typ, "mood": row["mood"]},
            }
        )
    return items


def _spread_by_topic(rows: list[dict]) -> list[dict]:
    """相邻两题不能同话题：证据文件是成对存的，连着出现等于把答案告诉他。"""
    for i in range(1, len(rows)):
        if rows[i].get("topic") == rows[i - 1].get("topic"):
            for j in range(i + 1, len(rows)):
                if rows[j].get("topic") != rows[i - 1].get("topic"):
                    rows[i], rows[j] = rows[j], rows[i]
                    break
    return rows


def mix_items(path: Path = MIX) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))["items"]
    if len(rows) != ITEM_COUNT:
        raise SystemExit(f"mix file needs {ITEM_COUNT} items, got {len(rows)}")
    random.Random(SHEET_SEED).shuffle(rows)
    rows = _spread_by_topic(rows)
    items = []
    for i, row in enumerate(rows, 1):
        items.append(
            {
                "id": f"m{i:02d}",
                "kind": "mix",
                "text": row["text"],
                "_key": {
                    "truth": row["truth"],
                    "topic": row.get("topic", ""),
                    "doc": row.get("doc"),
                    "model": row.get("model"),
                },
            }
        )
    truths = [item["_key"]["truth"] for item in items]
    if truths.count("author") != ITEM_COUNT // 2:
        raise SystemExit(f"mix truth split is {truths.count('author')}/{truths.count('generated')}")
    return items


def first_paragraph(path: Path) -> str:
    paras = vr._paragraphs(path.read_text(encoding="utf-8"))
    if not paras:
        raise SystemExit(f"no paragraphs in {path}")
    return _clip(paras[0], AB_EXCERPT_CHARS)


def ab_items(ab_dir: Path = AB_DIR) -> list[dict]:
    """目标是二选一（外加「分场景」）：给他两版开头 + 三个数字，问往哪边调。"""
    return [
        {
            "id": "t",
            "kind": "ab",
            "long": first_paragraph(ab_dir / "A.md"),
            "short": first_paragraph(ab_dir / "B.md"),
        }
    ]


def build() -> list[dict]:
    return mix_items() + label_items(parse_catalog()) + ab_items()


def _payload(items: list[dict]) -> list[dict]:
    """页面能看到的字段；`_key` 一律剔除。"""
    out = []
    for item in items:
        row = {k: v for k, v in item.items() if not k.startswith("_")}
        out.append(row)
    return out


def _fingerprint(items: list[dict]) -> str:
    blob = json.dumps(_payload(items), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def html_with(items: list[dict]) -> str:
    page = HTML.replace("__ITEMS__", json.dumps(_payload(items), ensure_ascii=False, separators=(",", ":")))
    page = page.replace("__REASONS__", json.dumps(REASONS, ensure_ascii=False, separators=(",", ":")))
    return page.replace("__FP__", _fingerprint(items))


def _selftest() -> int:
    items = build()
    problems: list[str] = []
    if len(items) != ITEM_COUNT + len(LABEL_TYPES) + 1:
        problems.append(f"item count {len(items)}")
    ids = [item["id"] for item in items]
    if len(set(ids)) != len(ids):
        problems.append("duplicate ids")
    mix = [item for item in items if item["kind"] == "mix"]
    labels = [item for item in items if item["kind"] == "label"]
    ab = [item for item in items if item["kind"] == "ab"]
    if len(mix) != ITEM_COUNT or len(labels) != len(LABEL_TYPES) or len(ab) != 1:
        problems.append(f"section sizes {len(mix)}/{len(labels)}/{len(ab)}")
    if len({item["type"] for item in labels}) != len(labels):
        problems.append("label items repeat a type")
    if html_with(build()) != html_with(items):
        problems.append("not reproducible")
    page = html_with(items)
    if "const FP = '" not in page:
        problems.append("missing batch fingerprint")
    if '"truth"' in page or '"author"' in page:
        problems.append("truth leaked into the page")
    if any(not (item.get("text") or item.get("opening") or item.get("long")) for item in items):
        problems.append("empty item")
    changed = [dict(item) for item in items]
    changed[0]["text"] = changed[0]["text"] + "多一句。"
    if html_with(changed) == page:
        problems.append("fingerprint does not change when items change")
    if problems:
        print(f"[FAIL] selftest: {problems}", file=sys.stderr)
        return 1
    print(
        f"[OK] selftest: {len(items)} 题（混装 {len(mix)} / 标签 {len(labels)} / 目标 {len(ab)}），"
        "可复算，真值不上页面"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="choice sheet: mix / labels / target")
    ap.add_argument("--selftest", action="store_true", help="只自检，不写文件")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    if args.selftest:
        return _selftest()

    items = build()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "annotate.html").write_text(html_with(items), encoding="utf-8", newline="\n")
    key = {
        "kind": "panchen-choice-sheet",
        "items": {item["id"]: item["_key"] for item in items if "_key" in item},
    }
    (args.out / "key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"[OK] wrote {args.out / 'annotate.html'} — {len(items)} 题；密钥 {args.out / 'key.json'}")
    for item in items:
        if item["kind"] == "mix":
            k = item["_key"]
            print(f"  {item['id']} {k['truth']:9s} {k.get('topic', ''):6s} {item['text'][:22]}")
        elif item["kind"] == "label":
            k = item["_key"]
            print(f"  {item['id']} {k['num']} {k['type']}·{k['mood']} {item['title']}")
        else:
            print("  t  A（长句版）vs B（短句版）")
    return 0


def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三件事，点几下就行</title>
<style>
  :root { --ink:#1c1c1e; --muted:#6b6b70; --line:#e3e3e6; --bg:#faf9f7; --card:#fff;
          --ok:#1f7a4d; --no:#a33a2a; --accent:#2b5f8a; }
  * { box-sizing: border-box; }
  html, body { max-width: 100%; overflow-x: hidden; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font: 17px/1.9 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", serif; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 22px 18px 110px; }
  h1 { font-size: 21px; margin: 0 0 6px; line-height: 1.5; }
  .sub { color: var(--muted); font-size: 14px; line-height: 1.7; }
  .card { background: var(--card); border:1px solid var(--line); border-radius: 12px;
          padding: 22px 20px; margin: 14px 0; overflow-wrap: anywhere; }
  .text p { margin: 0 0 12px; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.72; }
  .label-badge { display:inline-block; background:#eef4fa; color:var(--accent); border-radius: 8px;
                 padding: 4px 10px; font-size: 15px; margin: 8px 0 4px; }
  .excerpt { border-left: 3px solid var(--line); padding-left: 12px; margin: 10px 0; color:#333; }
  .excerpt .cap { color: var(--muted); font-size: 13px; }
  .ab { border:1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 10px 0;
        line-height: 1.72; }
  .ab .name { font-weight: 700; margin-bottom: 4px; }
  .bar { height: 6px; background:#ececef; border-radius: 99px; overflow:hidden; }
  .bar i { display:block; height:100%; width:0; background:var(--accent); transition: width .2s; }
  .row { display:flex; justify-content:space-between; align-items:center; gap:12px;
         margin: 12px 0 4px; font-size: 14px; color: var(--muted); }
  .btns { display:flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; }
  .btns button { flex: 1 1 42%; min-height: 52px; font-size: 17px; border-radius: 10px;
                 border:1px solid var(--line); background:#fff; color:var(--ink); cursor:pointer; }
  .btns button.like { border-color:#bcdcc9; background:#f2faf5; }
  .btns button.no { border-color:#e8c3bb; background:#fdf5f3; }
  .btns button.primary { background: var(--accent); color:#fff; border-color: var(--accent); }
  .btns button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .chips { display:flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .chips button { min-height: 44px; padding: 0 14px; border-radius: 999px; font-size: 15px;
                  border:1px solid var(--line); background:#fff; cursor:pointer; }
  .chips button[aria-pressed="true"] { background: var(--accent); color:#fff; border-color: var(--accent); }
  .hide { display: none !important; }
  .note textarea, #result { width: 100%; font: inherit; font-size: 16px; border:1px solid var(--line);
                            border-radius: 10px; padding: 10px 12px; background:#fff; }
  .note textarea { min-height: 76px; margin-top: 12px; }
  #result { min-height: 76px; margin-top: 8px; color: #333; }
  .strip { position: fixed; left:0; right:0; bottom:0; background: rgba(250,249,247,.97);
           border-top:1px solid var(--line); padding: 8px 16px; }
  .strip .inner { max-width: 760px; margin: 0 auto; display: flex; gap: 10px; align-items: center; }
  .strip button { flex: 1 1 auto; min-height: 46px; font-size: 16px; border-radius: 10px;
                  border:1px solid var(--accent); background: var(--accent); color:#fff; cursor:pointer; }
  .sheet { position: fixed; left:0; right:0; bottom:0; z-index: 20; background:#fff;
           border-top:1px solid var(--line); border-radius: 16px 16px 0 0;
           box-shadow: 0 -8px 30px rgba(0,0,0,.12); padding: 16px 18px 22px;
           max-height: 82vh; overflow: auto; }
  .sheet .inner { max-width: 760px; margin: 0 auto; }
  .warn { background:#fff8e6; border:1px solid #f0dfae; border-radius: 10px; padding: 10px 14px;
          font-size: 14px; color:#6b5a1e; margin-top: 12px; }
  .hint { font-size: 13px; color: var(--muted); min-height: 18px; margin-top: 6px; }
  @media (max-width: 560px) { .btns button { flex: 1 1 40%; } .wrap { padding: 18px 14px 96px; } }
</style>
</head>
<body>
<div class="wrap">
  <h1>三件事，点几下就行</h1>
  <div class="sub" id="intro">
    一共 16 道，大约 8 分钟。分三段：先看 10 段文字像不像你写的，再看 5 篇的类型标签对不对，
    最后定一个方向。<br>
    每道点一下就好，答案自动存在这台设备上。做完点下面的「看答案串 / 复制」，长按那行字发我。
  </div>

  <div id="quiz">
    <div class="bar"><i id="prog"></i></div>
    <div class="row"><span id="counter">1 / 16</span><span id="saved">自动保存</span></div>
    <div class="card" id="card">
      <div id="phase" class="sub"></div>
      <div id="body"></div>
      <div class="btns" id="mainBtns"></div>
      <div class="chips hide" id="chips"></div>
      <div class="note hide" id="noteBox"><textarea id="note" placeholder="可以补一句：哪里不对、像谁的口吻"></textarea></div>
      <div class="btns hide" id="nextBox"><button class="primary" id="next">下一题 →</button></div>
      <div class="btns"><button id="back">← 上一题</button></div>
    </div>
  </div>

  <div id="end" class="card hide">
    <h1 style="font-size:19px">三件事都做完了</h1>
    <div class="sub" id="endSub"></div>
    <div class="warn">点下面「看答案串 / 复制」，长按那行字 → 全选 → 复制，直接粘到聊天里发我。
      手机上复制不了也没关系，照着念或截图给我。</div>
  </div>
</div>

<div class="strip">
  <div class="inner"><button id="jumpResult">看答案串 / 复制</button></div>
</div>

<div class="sheet hide" id="sheet">
  <div class="inner">
    <div class="row" style="margin-top:0"><b>答案串</b><button id="closeSheet"
      style="min-height:36px;padding:0 12px;border-radius:8px;border:1px solid var(--line);background:#fff">关闭</button></div>
    <div class="sub">做完发我这一行；没答完会显示 =?。长按下面的字 → 全选 → 复制。</div>
    <textarea id="result" readonly aria-label="答案串"></textarea>
    <div class="btns">
      <button class="primary" id="copy">复制答案串</button>
      <button id="share">分享 / 发送</button>
    </div>
    <div class="hint" id="hint"></div>
  </div>
</div>

<script>
const ITEMS = __ITEMS__;
const FP = '__FP__';
const REASONS = __REASONS__;
const LS = 'panchen-choice-sheet-v1';
const PHASE = { mix: '第一段 · 这 10 段像不像你写的', label: '第二段 · 这 5 篇的类型标签对不对', ab: '第三段 · 以后往哪边调' };
let state = { fp: FP, index: 0, answers: {}, started: null };

const $ = (id) => document.getElementById(id);
const esc = (t) => t.replace(/[&<>]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function storeGet() { try { return JSON.parse(localStorage.getItem(LS) || 'null'); } catch (e) { return null; } }
function storeSet(s) { try { localStorage.setItem(LS, JSON.stringify(s)); return true; } catch (e) { return false; } }

function answerCode() {
  return ITEMS.map((it) => {
    const a = state.answers[it.id];
    if (!a) return it.id + '=?';
    let v = a.choice;
    if (it.kind === 'mix' && a.choice === '2') v = '2' + (a.reason || '?');
    if (a.note) v += '~' + a.note.replace(/\s+/g, ' ').slice(0, 20);
    return it.id + '=' + v;
  }).join(' ');
}
function doneCount() { return ITEMS.filter((it) => state.answers[it.id]).length; }

function sectionCounts() {
  const of = (kind) => ITEMS.filter((it) => it.kind === kind);
  const got = (kind) => of(kind).filter((it) => state.answers[it.id]).length;
  return '混装 ' + got('mix') + '/' + of('mix').length +
         ' · 标签 ' + got('label') + '/' + of('label').length +
         ' · 目标 ' + got('ab') + '/' + of('ab').length;
}

function renderResult() { $('result').value = answerCode(); }

function save() {
  state.fp = FP;
  const ok = storeSet(state);
  $('saved').textContent = sectionCounts() + (ok ? '' : '（本浏览器不能存进度，请一次做完）');
  // 已经开始了就把开头的说明收起来，手机上多给正文留一屏。
  if (doneCount() > 0) $('intro').classList.add('hide');
  renderResult();
}

function buttons(spec) {
  $('mainBtns').innerHTML = spec.map(([cls, label, value]) =>
    '<button class="' + cls + '" data-choice="' + value + '">' + esc(label) + '</button>').join('');
  $('mainBtns').classList.remove('hide');
}

function resetCard() {
  $('chips').classList.add('hide');
  $('chips').innerHTML = '';
  $('noteBox').classList.add('hide');
  $('note').value = '';
  $('nextBox').classList.add('hide');
  $('mainBtns').classList.remove('hide');
}

function showNext() { $('nextBox').classList.remove('hide'); }

function render() {
  const item = ITEMS[state.index];
  window.scrollTo({ top: 0, behavior: 'instant' });
  $('prog').style.width = (doneCount() / ITEMS.length * 100) + '%';
  $('counter').textContent = (state.index + 1) + ' / ' + ITEMS.length;
  $('phase').textContent = PHASE[item.kind];
  $('back').disabled = state.index === 0;
  resetCard();
  const prev = state.answers[item.id];

  if (item.kind === 'mix') {
    $('body').innerHTML = '<div class="text">' +
      item.text.split(/\n+/).filter(Boolean).map((t) => '<p>' + esc(t.trim()) + '</p>').join('') + '</div>';
    buttons([['like', '像我', '1'], ['no', '不像我', '2']]);
    if (prev) { if (prev.choice === '2') showReasons(prev); else showNext(); }
  } else if (item.kind === 'label') {
    $('body').innerHTML =
      '<div class="text"><p><b>' + esc(item.title) + '</b></p></div>' +
      '<div class="label-badge">标成了：' + esc(item.type) + ' · ' + esc(item.mood) + '</div>' +
      '<div class="excerpt"><div class="cap">开头</div>' + esc(item.opening) + '</div>' +
      '<div class="excerpt"><div class="cap">结尾</div>' + esc(item.closing) + '</div>';
    buttons([['like', '都对', 'Y'], ['no', '类型不对', 'T'], ['no', '格调不对', 'M'], ['no', '都不对', 'B']]);
    if (prev) {
      if (prev.choice !== 'Y') { showNote(prev); $('mainBtns').classList.add('hide'); }
      showNext();
    }
  } else {
    const metrics = '<div class="sub">你的书：均句长 32 字、每句 3.5 个分句。下面两版是同一个题目写出来的。</div>';
    $('body').innerHTML = metrics +
      '<div class="ab"><div class="name">甲 · 均句长 210 字，每句 17.8 个分句</div>' + esc(item.long) + '</div>' +
      '<div class="ab"><div class="name">乙 · 均句长 73 字，每句 6.7 个分句</div>' + esc(item.short) + '</div>' +
      '<div class="sub">以后改稿 / 仿写，往哪边调？</div>';
    buttons([['like', '往甲：句子更长的方向', '1'], ['no', '往乙：回到书里的长度', '2'], ['', '分场景（下面说）', '3']]);
    if (prev) {
      if (prev.choice === '3') { showNote(prev); $('mainBtns').classList.add('hide'); }
      showNext();
    }
  }
}

function showReasons(prev) {
  const chosen = prev ? prev.reason : null;
  $('chips').innerHTML = REASONS.map(([id, label]) =>
    '<button data-reason="' + id + '" aria-pressed="' + (chosen === id) + '">' + esc(label) + '</button>').join('');
  $('chips').classList.remove('hide');
  $('mainBtns').classList.add('hide');
  if (chosen === 'f' || (prev && prev.note)) { showNote(prev); showNext(); }
}

function showNote(prev) {
  $('noteBox').classList.remove('hide');
  if (prev && prev.note) $('note').value = prev.note;
}

function store(choice, reason) {
  const item = ITEMS[state.index];
  const prev = state.answers[item.id] || {};
  state.answers[item.id] = { choice: choice, reason: reason || null, note: prev.note || '', at: new Date().toISOString() };
  save();
}

function advance() {
  if (state.index + 1 >= ITEMS.length) { finish(); return; }
  state.index += 1;
  render(); save();
}

function choose(choice, reason) { store(choice, reason); advance(); }

function needsNote(item, choice) {
  return (item.kind === 'label' && choice !== 'Y') || (item.kind === 'ab' && choice === '3');
}

function finish() {
  $('quiz').classList.add('hide');
  $('end').classList.remove('hide');
  $('endSub').textContent = '16 道都点了：' + sectionCounts() + '。';
  save();
}

function hint(msg) { $('hint').textContent = msg || ''; }

async function copyCode() {
  const text = answerCode();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      hint('已复制。直接粘到聊天里发我。');
      return;
    }
  } catch (e) { /* 落到下面 */ }
  const ta = $('result');
  if (ta) { ta.focus(); ta.select(); ta.setSelectionRange(0, ta.value.length); }
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  hint(ok ? '已复制。' : '这个浏览器不给复制：长按上面的文字 → 全选 → 复制。');
}

async function shareCode() {
  const text = answerCode();
  try {
    if (navigator.share) { await navigator.share({ title: '文风三件事答案', text: text }); return; }
  } catch (e) { /* 用户取消或不可用 */ }
  hint('这个浏览器不支持分享：长按上面的文字复制，或直接截图发我。');
}

$('mainBtns').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-choice]');
  if (!b) return;
  const item = ITEMS[state.index];
  const choice = b.dataset.choice;
  if (item.kind === 'mix' && choice === '2') {
    store('2', null);
    showReasons(state.answers[item.id]);
    showNext();
    return;
  }
  if (needsNote(item, choice)) {
    store(choice);
    showNote(state.answers[item.id]);
    showNext();
    $('mainBtns').classList.add('hide');
    return;
  }
  choose(choice);
});

$('chips').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-reason]');
  if (!b) return;
  Array.from($('chips').children).forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
  if (b.dataset.reason === 'f') {
    store('2', 'f');
    showNote(state.answers[ITEMS[state.index].id]);
    showNext();
    return;
  }
  choose('2', b.dataset.reason);
});

$('note').addEventListener('input', () => {
  const item = ITEMS[state.index];
  const prev = state.answers[item.id];
  if (!prev) return;
  prev.note = $('note').value.trim();
  state.answers[item.id] = prev;
  save();
});

$('next').addEventListener('click', advance);

$('back').addEventListener('click', () => {
  if (state.index > 0) { state.index -= 1; render(); save(); }
});

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'TEXTAREA') return;
  if (!$('end').classList.contains('hide')) return;
  const item = ITEMS[state.index];
  const choice = item.kind === 'label'
    ? { '1': 'Y', '2': 'T', '3': 'M', '4': 'B' }[e.key]
    : { '1': '1', '2': '2', '3': '3' }[e.key];
  if (!choice) return;
  if (item.kind === 'mix' && choice === '2') { store('2', null); showReasons(state.answers[item.id]); showNext(); return; }
  if (needsNote(item, choice)) {
    store(choice); showNote(state.answers[item.id]); showNext(); $('mainBtns').classList.add('hide'); return;
  }
  choose(choice);
});

$('copy').addEventListener('click', copyCode);
$('share').addEventListener('click', shareCode);
$('jumpResult').addEventListener('click', () => { renderResult(); $('sheet').classList.remove('hide'); });
$('closeSheet').addEventListener('click', () => $('sheet').classList.add('hide'));

(function boot() {
  const saved = storeGet();
  if (saved && saved.answers && saved.fp === FP) {
    state = saved;
    if (Object.keys(state.answers).length >= ITEMS.length) { finish(); renderResult(); return; }
    const firstOpen = ITEMS.findIndex((it) => !state.answers[it.id]);
    state.index = firstOpen >= 0 ? firstOpen : 0;
  } else {
    // 换了一批题（文本指纹不同）就丢掉上一轮的答案，否则会误判成「已做完」。
    state.started = new Date().toISOString();
  }
  render(); save();
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
