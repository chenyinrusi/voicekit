"""生成「哪几段不像我」的单文件 HTML（离线、无外部依赖）+ 密钥。

用途：只花作者 10 次判断，指认自己书里最可疑的 10 段。答案只有两种——像我 / 不像我；
选「不像我」再点一个原因。这是本项目唯一别人没有的标注（DD-VOICE-002 §3.3）。

选段（规则，可复算）：
1. 只用作者的稿，034/037 排除（与出题侧、校准侧同口径）。
2. 每段按「可疑句」计分：教程口气 4 / 对读者喊话 3 / 喊口号 2 / 格言当道理 2；
   整段角色是「喊话」再 +1。一段里同一类只算一次。
3. 按分数降序挑，每篇最多 1 段，凑够 10 段；不足 60 汉字的段不进候选。

作者私稿正文落在 tmp/rejection/（gitignored），只提交生成器。

用法（repo 根目录）：
    python scripts/voice/make_rejection_annotation.py            # 写 tmp/rejection/{annotate.html,key.json}
    python scripts/voice/make_rejection_annotation.py --selftest # 自检：可复算、无 034/037、10 段互不重复
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "voice"
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPT_DIR))

from . import build_profile as bp  # noqa: E402
from . import voice_lib as lib  # noqa: E402
from . import voice_review as vr  # noqa: E402

CORPUS = REPO_ROOT / "voices/panchen-voice/corpus"
OUT = REPO_ROOT / "tmp" / "rejection"

#: 可疑句权重：优先级从高到低，数字只是相对重要性，不是概率。
SUSPECT_WEIGHT = {"教程口气": 4, "对读者喊话": 3, "喊口号": 2, "格言当道理": 2}
#: 作者一次只做 10 个判断（用户拍板：先做 10 次，不做 30 次）。
ITEM_COUNT = 10
#: 混装模式的固定乱序种子：同一份素材每次出同样的顺序，便于复算。
MIX_SEED = 20261108
#: 低于这个长度的段落不作候选：太短，判断起来像猜。
MIN_PARA_CJK = 60
#: 超过这个长度的段落也不作候选：读起来像做阅读理解，10 段会变成体力活。
MAX_PARA_CJK = 400

#: 理由选项：收窄到 6 个，作者点一个就够；不逼他写句子。
REASONS = [
    ("preachy", "太说教"),
    ("literary", "太文艺"),
    ("chicken_soup", "太像鸡汤"),
    ("not_my_words", "不是我说话的样子"),
    ("context", "前后文接不上"),
    ("other", "其它（写一句）"),
]


def candidates() -> list[dict]:
    """全量候选段：分数降序，附出处与命中的可疑类。"""
    rows: list[dict] = []
    for path in sorted(CORPUS.glob("*.md")):
        if path.name.startswith(bp.EXCLUDED_DOC_PREFIXES):
            continue
        body, _ = lib.clean_platform_noise(path.read_text(encoding="utf-8"))
        if lib.cjk_count(body) < bp.MIN_CJK or body.count("，") == 0:
            continue
        for index, para in enumerate(vr._paragraphs(body), 1):
            cjk = lib.cjk_count(para)
            if not (MIN_PARA_CJK <= cjk <= MAX_PARA_CJK):
                continue
            kinds = sorted(
                {vr._suspect_kind(s) for s in vr._sentences(para)} - {""},
                key=lambda k: -SUSPECT_WEIGHT.get(k, 0),
            )
            score = sum(SUSPECT_WEIGHT.get(k, 0) for k in kinds)
            role = vr._role(para)
            if role == "喊话":
                score += 1
            if score <= 0:
                continue
            rows.append(
                {
                    "doc": path.name,
                    "para": index,
                    "cjk": cjk,
                    "role": role,
                    "kinds": kinds,
                    "score": score,
                    "text": para,
                }
            )
    rows.sort(key=lambda r: (-r["score"], r["doc"], r["para"]))
    return rows


def pick(rows: list[dict], count: int = ITEM_COUNT) -> list[dict]:
    """每篇最多 1 段，按分数从高到低取满。"""
    seen: set[str] = set()
    picked: list[dict] = []
    for row in rows:
        if row["doc"] in seen:
            continue
        seen.add(row["doc"])
        picked.append(row)
        if len(picked) == count:
            break
    return picked


def build() -> list[dict]:
    picked = pick(candidates())
    if len(picked) < ITEM_COUNT:
        raise SystemExit(f"not enough candidates: {len(picked)} < {ITEM_COUNT}")
    return [
        {
            "id": f"p{i:02d}",
            "text": row["text"],
            "_key": {
                "doc": row["doc"],
                "para": row["para"],
                "cjk": row["cjk"],
                "role": row["role"],
                "kinds": row["kinds"],
                "score": row["score"],
            },
        }
        for i, row in enumerate(picked, 1)
    ]


def build_mix(path: Path) -> list[dict]:
    """混装模式：作者原文 + 模型仿写，固定种子打乱；真值只写进密钥，页面看不到。"""
    rows = json.loads(path.read_text(encoding="utf-8"))["items"]
    if len(rows) != ITEM_COUNT:
        raise SystemExit(f"mix file needs {ITEM_COUNT} items, got {len(rows)}")
    rng = random.Random(MIX_SEED)
    rng.shuffle(rows)
    return [
        {
            "id": f"m{i:02d}",
            "text": row["text"],
            "_key": {
                "truth": row["truth"],
                "topic": row["topic"],
                "model": row.get("model"),
                "doc": row.get("doc"),
                "para": row.get("para"),
                "cjk": lib.cjk_count(row["text"]),
            },
        }
        for i, row in enumerate(rows, 1)
    ]


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    ap = argparse.ArgumentParser(description="paragraph rejection annotation (single-file html)")
    ap.add_argument("--selftest", action="store_true", help="只自检，不写文件")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--mix", type=Path, default=None,
                    help="混装模式：读一个 10 段的 JSON（{\"items\":[{\"text\",\"truth\":\"author\"/"
                         "\"generated\",\"topic\",\"model\"?}]}），真 5 假 5，打乱后出页；"
                         "这个文件目前由出题方手写，没有生成脚本")
    args = ap.parse_args(argv)

    items = build_mix(args.mix) if args.mix else build()
    intro = MIX_INTRO if args.mix else AUTHOR_INTRO
    if args.selftest and args.mix:
        truth = [item["_key"]["truth"] for item in items]
        problems = []
        if truth.count("author") != ITEM_COUNT // 2 or truth.count("generated") != ITEM_COUNT // 2:
            problems.append(f"truth split {truth.count('author')}/{truth.count('generated')}")
        if _html_with(build_mix(args.mix)) != _html_with(items):
            problems.append("not reproducible")
        leaked = [i["id"] for i in items if i["_key"]["truth"] in i["text"]]
        if leaked:
            problems.append(f"truth leaked into text: {leaked}")
        if problems:
            print(f"[FAIL] mix selftest: {problems}", file=sys.stderr)
            return 1
        print(f"[OK] mix selftest: {len(items)} 段（真 {truth.count('author')} / 假 {truth.count('generated')}），可复算")
        return 0

    if args.selftest and not args.mix:
        docs = [item["_key"]["doc"] for item in items]
        problems = []
        if len(items) != ITEM_COUNT:
            problems.append(f"item count {len(items)}")
        if len(set(docs)) != len(docs):
            problems.append("duplicate docs")
        if any(d.startswith(bp.EXCLUDED_DOC_PREFIXES) for d in docs):
            problems.append("034/037 leaked")
        if _html_with(build()) != _html_with(items):
            problems.append("not reproducible")
        page = _html_with(items)
        if "const FP = '" not in page:
            problems.append("missing batch fingerprint")
        tampered = [dict(items[0], text=items[0]["text"] + "多一句。")] + items[1:]
        if _html_with(tampered) == page:
            problems.append("fingerprint does not change when the items change")
        if problems:
            print(f"[FAIL] selftest: {problems}", file=sys.stderr)
            return 1
        print(f"[OK] selftest: {len(items)} 段 / {len(set(docs))} 篇，可复算，无人为挑选")
        return 0

    html = _html_with(items, intro)
    digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:16]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "annotate.html").write_text(html, encoding="utf-8", newline="\n")
    key = {
        "kind": "paragraph-rejection",
        "mode": "mix" if args.mix else "author-only",
        "built_sha256": digest,
        "items": {item["id"]: item["_key"] for item in items},
    }
    (args.out / "key.json").write_text(
        json.dumps(key, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"[OK] wrote {args.out / 'annotate.html'} — {len(items)} 段；密钥 {args.out / 'key.json'}")
    for item in items:
        k = item["_key"]
        if "truth" in k:
            print(f"  {item['id']} {k['truth']:9s} {k['topic']:5s} {k['cjk']:>3}字 {item['text'][:26]}")
        else:
            print(f"  {item['id']} {k['doc'][:14]:16s} p{k['para']:>2} {k['cjk']:>3}字 score={k['score']} {'/'.join(k['kinds'])}")
    return 0


def _utf8_stdio() -> None:
    """Windows 管道默认 cp1252：把中文写进管道会崩，统一转 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


AUTHOR_INTRO = "10 段全部取自你自己的书，顺序打乱，不显示出处。"
MIX_INTRO = "10 段里，有的是你写的，有的是照着你的口吻仿写的，混在一起，顺序打乱。"


def _html_with(items: list[dict], intro: str = AUTHOR_INTRO) -> str:
    payload = [{"id": item["id"], "text": item["text"]} for item in items]
    # 批次指纹：题号是位置（p01..p10），换一批题号不变；只认指纹才知道旧答案还算不算。
    fingerprint = hashlib.sha256(
        "\n".join(item["text"] for item in items).encode("utf-8")
    ).hexdigest()[:10]
    html = HTML.replace("__ITEMS__", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    html = html.replace("__INTRO__", intro)
    html = html.replace("__FP__", fingerprint)
    return html.replace("__REASONS__", json.dumps(REASONS, ensure_ascii=False, separators=(",", ":")))


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>这 10 段，哪几段不像你</title>
<style>
  :root { --ink:#1c1c1e; --muted:#6b6b70; --line:#e3e3e6; --bg:#faf9f7; --card:#fff;
          --ok:#1f7a4d; --no:#a33a2a; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font: 17px/1.95 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", serif; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 24px 20px 96px; }
  h1 { font-size: 21px; margin: 0 0 6px; }
  .sub { color: var(--muted); font-size: 14px; line-height: 1.7; }
  .card { background: var(--card); border:1px solid var(--line); border-radius: 12px;
          padding: 24px 26px; margin-top: 16px; }
  .text { font-size: 17px; line-height: 1.95; }
  .text p { margin: 0 0 14px; text-align: justify; }
  .text p:last-child { margin-bottom: 0; }
  .bar { height: 6px; background: #ececea; border-radius: 999px; margin: 18px 0 8px; overflow:hidden; }
  .bar > i { display:block; height:100%; width:0; background:#1c1c1e; transition: width .2s; }
  .row { display:flex; gap:12px; align-items:center; justify-content:space-between;
         color:var(--muted); font-size:14px; }
  .btns { display:flex; gap:12px; margin-top: 18px; flex-wrap: wrap; }
  button { font: inherit; border-radius: 10px; border:1px solid var(--line); background:#fff;
           padding: 12px 22px; cursor:pointer; color:var(--ink); }
  button:hover { border-color:#b9b9bd; }
  button:focus-visible { outline: 2px solid #1c1c1e; outline-offset: 2px; }
  button.like { border-color:#9ccdb3; color:var(--ok); }
  button.no { border-color:#deb4aa; color:var(--no); }
  button.chip { padding: 9px 14px; font-size: 15px; border-radius: 999px; }
  button.chip[aria-pressed="true"] { background:#1c1c1e; color:#fff; border-color:#1c1c1e; }
  button.primary { background:#1c1c1e; color:#fff; border-color:#1c1c1e; }
  button:disabled { opacity:.45; cursor:not-allowed; }
  .kbd { font-size:12px; color:var(--muted); margin-left:6px; }
  .reasons { margin-top:14px; display:none; }
  .reasons.on { display:block; }
  .reasons .sub { margin-bottom: 8px; }
  .chips { display:flex; gap:8px; flex-wrap: wrap; }
  .note { margin-top:12px; display:none; }
  .note.on { display:block; }
  textarea { width:100%; font: inherit; font-size:15px; padding:9px 12px;
             border:1px solid var(--line); border-radius:10px; resize:vertical; }
  .note textarea { min-height:58px; }
  .strip { background:#fff; border:1px dashed #c9c9cc; border-radius:12px;
           padding:16px 18px; margin-top:22px; }
  .strip .sub { margin-bottom:8px; }
  #result { font: 14px/1.8 ui-monospace, Consolas, "Courier New", monospace;
            background:#fbfbfa; min-height:76px; }
  .hint { color:#8a5a12; font-size:13px; margin-top:8px; min-height:18px; }
  .foot { position:fixed; left:0; right:0; bottom:0; background:rgba(250,249,247,.96);
          border-top:1px solid var(--line); padding:10px 20px; }
  .foot .wrap { padding:0; display:flex; gap:10px; justify-content:space-between; align-items:center; }
  .hide { display:none !important; }
  .warn { color:#8a5a12; background:#fff6e5; border:1px solid #f0dfba; padding:10px 14px;
          border-radius:10px; font-size:14px; margin-top:16px; }
  @media (max-width:560px){ .card{padding:18px} .btns button{flex:1 1 40%} }
</style>
</head>
<body>
<div class="wrap">
  <h1>这 10 段，哪几段不像你写的</h1>
  <div class="sub">
    __INTRO__<br>
    每段只回答一次：<b>像我</b> 还是 <b>不像我</b>。选「不像我」再点一个原因。<br>
    大约 5 分钟。答案会实时写进页面最下面的「答案串」，做完把那行发我就行，不用下载。
  </div>

  <div id="quiz">
    <div class="bar"><i id="prog"></i></div>
    <div class="row"><span id="counter">1 / 10</span><span id="saved">自动保存</span></div>

    <div class="card"><div class="text" id="text"></div></div>

    <div class="btns" id="mainBtns">
      <button class="like" data-label="like">像我<span class="kbd">1</span></button>
      <button class="no" data-label="not_like">不像我<span class="kbd">2</span></button>
    </div>

    <div class="reasons" id="reasonBox">
      <div class="sub">哪里不像？（点一个）</div>
      <div class="chips" id="chips"></div>
      <div class="note" id="noteBox">
        <textarea id="note" placeholder="可以补一句：哪句最不像、像谁的口吻"></textarea>
      </div>
    </div>

    <div class="btns" style="margin-top:16px">
      <button id="back">← 上一段</button>
    </div>
  </div>

  <div id="end" class="card hide">
    <h1 style="font-size:19px">做完了</h1>
    <div class="sub" id="endSub"></div>
    <div class="warn">往下滑到「答案串」，长按那行字 → 全选 → 复制，直接粘到聊天里发我。
      手机上如果复制不了，也可以照着念或截图给我。</div>
  </div>

  <div class="strip" id="strip">
    <div class="sub"><b>答案串</b>（做完发我这一行；还没做完会显示 =?）</div>
    <textarea id="result" readonly rows="4" aria-label="答案串"></textarea>
    <div class="btns">
      <button class="primary" id="copy">复制答案串</button>
      <button id="share">分享 / 发送</button>
      <button id="download">下载 JSON（电脑上用）</button>
    </div>
    <div class="hint" id="hint"></div>
  </div>
</div>

<div class="foot hide" id="foot">
  <div class="wrap">
    <span class="sub" id="footState">自动保存</span>
    <button id="jumpResult">看答案串</button>
  </div>
</div>

<script>
const ITEMS = __ITEMS__;
const FP = '__FP__';  // 这批题的指纹：题号是位置，换题时题号不变，只能靠它认出旧答案
const REASONS = __REASONS__;
const LS = 'panchen-paragraph-rejection-v1';  // 沿用旧键：手机上已经答过的进度还能读回来
const LETTER = { preachy:'a', literary:'b', chicken_soup:'c', not_my_words:'d', context:'e', other:'f' };
let state = { fp: FP, index: 0, answers: {}, started: null };

const $ = (id) => document.getElementById(id);
const esc = (t) => t.replace(/[&<>]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));

function storeGet() { try { return JSON.parse(localStorage.getItem(LS) || 'null'); } catch (e) { return null; } }
function storeSet(s) { try { localStorage.setItem(LS, JSON.stringify(s)); return true; } catch (e) { return false; } }

function answerCode() {
  return ITEMS.map((it) => {
    const a = state.answers[it.id];
    if (!a) return it.id + '=?';
    const v = a.label === 'like' ? '1' : '2' + (LETTER[a.reason] || '?');
    return it.id + '=' + v + (a.note ? '~' + a.note.replace(/\s+/g, ' ').slice(0, 30) : '');
  }).join(' ');
}

function renderResult() {
  const ta = $('result');
  if (ta) ta.value = answerCode();
}

function save() {
  state.fp = FP;
  const ok = storeSet(state);
  const n = Object.keys(state.answers).length;
  const msg = '已答 ' + n + ' / ' + ITEMS.length + (ok ? '' : '（本浏览器不能存进度，请一次做完）');
  $('saved').textContent = msg;
  $('footState').textContent = msg;
  renderResult();
}

function render() {
  const item = ITEMS[state.index];
  window.scrollTo({ top: 0, behavior: 'instant' });
  $('prog').style.width = (state.index / ITEMS.length * 100) + '%';
  $('counter').textContent = (state.index + 1) + ' / ' + ITEMS.length;
  $('text').innerHTML = item.text.split(/\n+/).filter(Boolean).map((t) => '<p>' + esc(t.trim()) + '</p>').join('');
  const prev = state.answers[item.id];
  $('back').disabled = state.index === 0;
  showReasons(!!prev && prev.label === 'not_like', prev);
  $('mainBtns').classList.remove('hide');
}

function showReasons(on, prev) {
  const box = $('reasonBox');
  box.classList.toggle('on', on);
  if (!on) { $('note').value = ''; return; }
  const chosen = prev ? prev.reason : null;
  $('chips').innerHTML = REASONS.map(([id, label]) =>
    '<button class="chip" data-reason="' + id + '" aria-pressed="' + (chosen === id) + '">' + esc(label) + '</button>'
  ).join('');
  $('noteBox').classList.toggle('on', chosen === 'other' || (prev && prev.note));
  $('note').value = prev && prev.note ? prev.note : '';
}

function commit(label, reason) {
  const item = ITEMS[state.index];
  const note = $('note').value.trim();
  state.answers[item.id] = { label: label, reason: reason || null, note: note, at: new Date().toISOString() };
  if (state.index + 1 >= ITEMS.length) { finish(); return; }
  state.index += 1;
  render(); save();
}

function finish() {
  $('quiz').classList.add('hide');
  $('end').classList.remove('hide');
  $('prog').style.width = '100%';
  const n = Object.keys(state.answers).length;
  const no = ITEMS.filter((i) => state.answers[i.id] && state.answers[i.id].label === 'not_like').length;
  $('endSub').textContent = '10 段做完，' + n + ' 段有答案，其中 ' + no + ' 段你说不像自己。';
  save();
}

function payload() {
  return {
    version: 2,
    kind: 'paragraph-rejection',
    started: state.started,
    exported_at: new Date().toISOString(),
    code: answerCode(),
    answers: ITEMS.map((item) => {
      const a = state.answers[item.id];
      return a ? { id: item.id, label: a.label, reason: a.reason, note: a.note, at: a.at }
               : { id: item.id, label: null };
    })
  };
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
    if (navigator.share) { await navigator.share({ title: '文风标注答案', text: text }); return; }
  } catch (e) { /* 用户取消或不可用 */ }
  hint('这个浏览器不支持分享：长按上面的文字复制，或直接截图发我。');
}

function download() {
  try {
    const blob = new Blob([JSON.stringify(payload(), null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'paragraph-rejection.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    hint('已开始下载。手机上下载不了也没关系，上面那行字就够了。');
  } catch (e) {
    hint('这个浏览器不给下载：用上面那行答案串就行。');
  }
}

$('mainBtns').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-label]');
  if (!b) return;
  if (b.dataset.label === 'like') { commit('like', null); return; }
  showReasons(true, null);
  $('mainBtns').classList.add('hide');
});

$('chips').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-reason]');
  if (!b) return;
  Array.from($('chips').children).forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
  $('noteBox').classList.toggle('on', b.dataset.reason === 'other');
  if (b.dataset.reason === 'other') { $('note').focus(); return; }
  commit('not_like', b.dataset.reason);
});

$('note').addEventListener('input', () => {
  const sel = $('chips').querySelector('[aria-pressed="true"]');
  if (!sel) return;
  const item = ITEMS[state.index];
  state.answers[item.id] = { label: 'not_like', reason: sel.dataset.reason, note: $('note').value.trim(), at: new Date().toISOString() };
  save();
});

$('back').addEventListener('click', () => {
  if (state.index > 0) { state.index -= 1; render(); save(); }
});

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'TEXTAREA') return;
  if (!$('end').classList.contains('hide')) return;
  if (e.key === '1') commit('like', null);
  else if (e.key === '2') { showReasons(true, null); $('mainBtns').classList.add('hide'); }
  else if (e.key === 'ArrowLeft' && state.index > 0) { state.index -= 1; render(); save(); }
});

$('copy').addEventListener('click', copyCode);
$('share').addEventListener('click', shareCode);
$('download').addEventListener('click', download);
$('jumpResult').addEventListener('click', () => { $('strip').scrollIntoView({ behavior: 'smooth', block: 'center' }); });

(function boot() {
  const saved = storeGet();
  if (saved && saved.answers && saved.fp === FP) {
    state = saved;
    if (Object.keys(state.answers).length >= ITEMS.length) { finish(); renderResult(); return; }
  } else {
    // 换了一批题（文本指纹不同）就丢掉上一轮的答案，否则会误判成「已做完」。
    state.started = new Date().toISOString();
  }
  $('foot').classList.remove('hide');
  render(); save();
})();
</script>
</body>
</html>
"""



if __name__ == "__main__":
    raise SystemExit(main())

