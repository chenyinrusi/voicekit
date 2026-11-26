"""生成盲标 HTML（单文件、离线、无外部依赖）+ 密钥；输出到 tmp/annotation/（不入库）。

正文含作者私稿与他人文章，产物只能留在 tmp/；这里只提交生成器代码。

作者稿排除裁判基线用过的 12 篇（避免拿基线样本当标注集），别人稿 46 篇做分层抽样：
先放 6 篇「确定性层误放」的硬样本，再随机补足，两类混合打乱。
"""
from __future__ import annotations

import importlib.util
import json
import random
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
VOICE = HERE
CORPUS = ROOT / "voices/panchen-voice/corpus"
NEG = HERE / "fixtures" / "negatives"
OUT = ROOT / "tmp" / "annotation"
SEED = 20261104


def load(name: str):
    return importlib.import_module(f"{__package__}.{name}")


BP = load("build_profile")
LIB = load("voice_lib")

PROFILE = json.loads((ROOT / "voices/panchen-voice/profile.json").read_text(encoding="utf-8"))
BASELINE = json.loads((ROOT / "voices/panchen-voice/judge_baseline.json").read_text(encoding="utf-8"))
BASELINE_DOCS = {Path(name).name for name in BASELINE.get("sample", {}).get("docs", [])}
FALSE_ACCEPTS = set(
    PROFILE["meta"]["calibration"]["negatives"]["other_author"].get("accepted_files", [])
)


def body_of(path: Path) -> str:
    body = path.read_text(encoding="utf-8")
    cleaned, _ = LIB.clean_platform_noise(body)
    return cleaned.strip()


def authors() -> list[dict]:
    pool = []
    for path in sorted(CORPUS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        body, _ = LIB.clean_platform_noise(text)
        if LIB.cjk_count(body) < BP.MIN_CJK or body.count("，") == 0:
            continue  # 与 build_profile 的可用稿口径一致
        if path.name in BASELINE_DOCS:
            continue
        pool.append({"class": "author", "source": path.name, "text": body.strip()})
    return pool


def others() -> list[dict]:
    pool = []
    for path in sorted(NEG.glob("other-author-zh-*.md")):
        pool.append({"class": "other", "source": path.name, "text": body_of(path),
                     "hard": path.name in FALSE_ACCEPTS})
    return pool


def take(pool: list[dict], hard: list[dict], count: int, rng: random.Random) -> list[dict]:
    chosen = hard[: min(len(hard), count)]
    rest = [item for item in pool if item not in chosen]
    rng.shuffle(rest)
    chosen = chosen + rest[: max(0, count - len(chosen))]
    rng.shuffle(chosen)
    return chosen


def build(mode: int) -> tuple[dict, dict, list[str]]:
    rng = random.Random(SEED + mode)
    author_pool = authors()
    other_pool = others()
    hard = [item for item in other_pool if item["hard"]]
    normal = [item for item in other_pool if not item["hard"]]
    if mode == 30:
        author_n, other_n, hard_n = 15, 15, 6
    elif mode == 46:
        author_n, other_n, hard_n = 23, 23, 6
    else:
        author_n, other_n, hard_n = len(author_pool), len(other_pool), len(hard)
    picked = take(author_pool, [], author_n, rng) + take(normal, hard, other_n, rng)
    rng.shuffle(picked)
    prefix = {30: "q", 46: "s", 95: "f"}[mode]
    items: dict[str, dict] = {}
    meta: dict[str, dict] = {}
    ids: list[str] = []
    for index, owner in enumerate(picked, 1):
        item_id = f"{prefix}{index:03d}"
        paragraphs = [p.strip() for p in owner["text"].split("\n\n") if p.strip()]
        items[item_id] = {"paragraphs": paragraphs}
        meta[item_id] = {"class": owner["class"], "source": owner["source"]}
        ids.append(item_id)
    return items, meta, ids


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data: dict = {"items": {}, "sets": {}}
    key: dict = {"seed": SEED, "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "modes": {}}
    for mode in (30, 46, 95):
        items, meta, ids = build(mode)
        data["items"].update(items)
        data["sets"][str(mode)] = ids
        key["modes"][str(mode)] = meta
        classes = [meta[item_id]["class"] for item_id in ids]
        print(f"mode {mode}: {len(ids)} items, author={classes.count('author')}, other={classes.count('other')}")
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    (OUT / "annotate.html").write_text(html, encoding="utf-8", newline="\n")
    (OUT / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("html bytes", (OUT / "annotate.html").stat().st_size)
    return 0


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>文风盲标</title>
<style>
  :root { --ink:#1c1c1e; --muted:#6b6b70; --line:#e3e3e6; --bg:#faf9f7; --card:#fff;
          --ok:#1f7a4d; --no:#a33a2a; --idk:#6b6b70; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font: 17px/1.95 -apple-system, "Segoe UI", "Microsoft YaHei", "PingFang SC", serif; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 28px 20px 120px; }
  h1 { font-size: 22px; margin: 0 0 6px; }
  .sub { color: var(--muted); font-size: 14px; line-height: 1.7; }
  .card { background: var(--card); border:1px solid var(--line); border-radius: 12px;
          padding: 26px 28px; margin-top: 18px; }
  .text p { margin: 0 0 14px; text-align: justify; }
  .bar { height: 6px; background: #ececea; border-radius: 999px; margin: 20px 0 8px; overflow:hidden; }
  .bar > i { display:block; height:100%; width:0; background:#1c1c1e; transition: width .2s; }
  .row { display:flex; gap:12px; align-items:center; justify-content:space-between;
         color:var(--muted); font-size:14px; }
  .btns { display:flex; gap:12px; margin-top: 22px; flex-wrap: wrap; }
  button { font: inherit; border-radius: 10px; border:1px solid var(--line); background:#fff;
           padding: 12px 22px; cursor:pointer; }
  button:hover { border-color:#b9b9bd; }
  button.like { border-color:#9ccdb3; color:var(--ok); }
  button.no { border-color:#deb4aa; color:var(--no); }
  button.idk { color:var(--idk); }
  button.primary { background:#1c1c1e; color:#fff; border-color:#1c1c1e; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .kbd { font-size:12px; color:var(--muted); margin-left:6px; }
  .note { margin-top:16px; display:none; }
  textarea { width:100%; min-height:64px; font: inherit; font-size:15px; padding:10px 12px;
             border:1px solid var(--line); border-radius:10px; resize:vertical; }
  .foot { position:fixed; left:0; right:0; bottom:0; background:rgba(250,249,247,.95);
          border-top:1px solid var(--line); padding:10px 20px; }
  .foot .wrap { padding:0; display:flex; gap:10px; justify-content:space-between; align-items:center; }
  .hide { display:none !important; }
  .mode { display:flex; flex-direction:column; gap:10px; margin-top:16px; }
  .mode button { text-align:left; padding:14px 18px; }
  .mode b { display:block; font-size:16px; }
  .mode span { color:var(--muted); font-size:13px; }
  .warn { color:#8a5a12; background:#fff6e5; border:1px solid #f0dfba; padding:10px 14px;
          border-radius:10px; font-size:14px; margin-top:16px; }
  @media (max-width:560px){ .card{padding:18px} .btns button{flex:1 1 30%; padding:12px 8px} }
</style>
</head>
<body>
<div class="wrap">
  <h1>文风盲标</h1>
  <div class="sub">
    判断每篇文章「像不像同一个人写的」。有的同一作者，有的不是，顺序打乱的。<br>
    凭文字感觉判断，不查来源、不猜文件名。随时可停，进度存在本机浏览器。
  </div>

  <div id="start" class="card">
    <div class="sub">选一个范围开始：</div>
    <div class="mode" id="modes">
      <button data-mode="30"><b>快速 · 30 篇</b><span>约 20 分钟，两类各 15；先试一遍</span></button>
      <button data-mode="46"><b>标准 · 46 篇</b><span>约 35 分钟，两类各 23；建议做这个，够扫切点</span></button>
      <button data-mode="95"><b>全量 · 95 篇</b><span>约 60–80 分钟，作者 49 + 别人 46；最稳</span></button>
    </div>
    <div id="resume" class="warn hide"></div>
  </div>

  <div id="quiz" class="hide">
    <div class="bar"><i id="prog"></i></div>
    <div class="row"><span id="counter">1 / 30</span><span id="elapsed">0:00</span></div>
    <div class="card"><div class="text" id="text"></div></div>
    <div class="btns">
      <button class="like" data-label="like">像 <span class="kbd">1</span></button>
      <button class="no" data-label="not_like">不像 <span class="kbd">2</span></button>
      <button class="idk" data-label="unsure">说不清 <span class="kbd">3</span></button>
      <button id="toggleNote" style="margin-left:auto">加一句理由</button>
    </div>
    <div class="note" id="noteBox">
      <textarea id="note" placeholder="可留空；写「哪里不像」最有用（句尾太满、没有 PS、用词太新、太像公号…）"></textarea>
    </div>
    <div class="btns" style="margin-top:14px">
      <button id="back">← 上一篇</button>
      <button id="export2" style="margin-left:auto">导出进度</button>
    </div>
  </div>

  <div id="end" class="card hide">
    <h1 style="font-size:20px">标完了</h1>
    <div class="sub" id="endSub"></div>
    <div class="btns">
      <button class="primary" id="download">下载结果 JSON</button>
      <button id="copy">复制 JSON</button>
      <button id="again">重标</button>
    </div>
    <div class="warn">把下载的 JSON 交给 agent，说「标注结果在 &lt;路径&gt;」即可。</div>
  </div>
</div>

<div class="foot hide" id="foot">
  <div class="wrap">
    <span class="sub" id="footState">自动保存</span>
    <button id="export">导出进度</button>
  </div>
</div>

<script>
const DATA = __DATA__;
const LS = 'panchen-voice-blind-label-v1';
let state = { mode: null, index: 0, answers: {}, started: null, order: [] };

const $ = (id) => document.getElementById(id);
const T0 = Date.now();
let tick = setInterval(updateElapsed, 1000);

function updateElapsed() {
  const started = state.started ? Date.parse(state.started) : T0;
  const s = Math.max(0, Math.floor((Date.now() - started) / 1000));
  $('elapsed').textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}

function save() {
  localStorage.setItem(LS, JSON.stringify(state));
  $('footState').textContent = '已自动保存 ' + Object.keys(state.answers).length + ' / ' + state.order.length;
}

function load() {
  try { const raw = localStorage.getItem(LS); if (raw) return JSON.parse(raw); } catch (e) {}
  return null;
}

function start(mode) {
  const order = DATA.sets[String(mode)];
  state = { mode: mode, index: 0, answers: state.answers || {}, started: new Date().toISOString(), order: order };
  $('start').classList.add('hide');
  $('quiz').classList.remove('hide');
  $('foot').classList.remove('hide');
  render();
  save();
}

function current() { return DATA.items[state.order[state.index]]; }

function esc(t) { return t.replace(/[&<>]/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

function render() {
  const item = current();
  window.scrollTo({ top: 0, behavior: 'instant' });  // 换篇后回到页首，否则停在上一条的底部
  $('prog').style.width = (state.index / state.order.length * 100) + '%';
  $('counter').textContent = (state.index + 1) + ' / ' + state.order.length;
  $('text').innerHTML = item.paragraphs.map((t) => '<p>' + esc(t) + '</p>').join('');
  const prev = state.answers[state.order[state.index]];
  $('note').value = prev && prev.note ? prev.note : '';
  $('noteBox').style.display = prev && prev.note ? 'block' : 'none';
  $('toggleNote').textContent = prev && prev.note ? '收起理由' : '加一句理由';
  $('back').disabled = state.index === 0;
  updateElapsed();
}

function answer(label) {
  const id = state.order[state.index];
  const note = $('note').value.trim();
  state.answers[id] = { label: label, note: note, at: new Date().toISOString() };
  if (state.index + 1 >= state.order.length) { finish(); return; }
  state.index += 1;
  render(); save();
}

function finish() {
  $('quiz').classList.add('hide');
  $('end').classList.remove('hide');
  const n = state.order.filter((id) => state.answers[id]).length;
  $('endSub').textContent = '共 ' + state.order.length + ' 篇，标了 ' + n + ' 篇。下载后把文件名告诉 agent。';
  $('foot').classList.add('hide');
  save();
}

function payload() {
  const answers = state.order.map((id) => {
    const a = state.answers[id];
    return a ? { id: id, label: a.label, note: a.note, at: a.at } : { id: id, label: null };
  });
  return { version: 1, mode: state.mode, started: state.started,
           exported_at: new Date().toISOString(), answers: answers };
}

function download() {
  const blob = new Blob([JSON.stringify(payload(), null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'voice-labels-' + (state.mode || 'x') + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
}

function copy() {
  navigator.clipboard.writeText(JSON.stringify(payload(), null, 2)).then(() => {
    $('copy').textContent = '已复制';
    setTimeout(() => { $('copy').textContent = '复制 JSON'; }, 1500);
  });
}

$('modes').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-mode]');
  if (b) start(Number(b.dataset.mode));
});

// 三个判定按钮走事件委托（2026-11-04 修：之前只绑了键盘，鼠标点了没反应）
$('quiz').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-label]');
  if (b) answer(b.dataset.label);
});

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'TEXTAREA') return;
  if ($('quiz').classList.contains('hide')) return;
  if (e.key === '1') answer('like');
  else if (e.key === '2') answer('not_like');
  else if (e.key === '3') answer('unsure');
  else if (e.key === 'ArrowLeft' && state.index > 0) { state.index -= 1; render(); save(); }
});

$('toggleNote').addEventListener('click', () => {
  const box = $('noteBox');
  box.style.display = box.style.display === 'block' ? 'none' : 'block';
  $('toggleNote').textContent = box.style.display === 'block' ? '收起理由' : '加一句理由';
  if (box.style.display === 'block') $('note').focus();
});

$('back').addEventListener('click', () => { if (state.index > 0) { state.index -= 1; render(); save(); } });
['export', 'export2', 'download'].forEach((id) => $(id).addEventListener('click', download));
$('copy').addEventListener('click', copy);
$('again').addEventListener('click', () => {
  if (!confirm('清空本机进度，重标一遍？')) return;
  localStorage.removeItem(LS);
  location.reload();
});

(function boot() {
  const saved = load();
  if (saved && saved.order && saved.order.length) {
    state = saved;
    const done = Object.keys(state.answers || {}).length;
    const r = $('resume');
    r.classList.remove('hide');
    r.innerHTML = '发现上次进度：' + done + ' / ' + state.order.length +
      ' 篇（' + new Date(state.started).toLocaleString() + '）。' +
      '<button id="resumeBtn" class="primary" style="margin-left:10px">继续</button>';
    $('resumeBtn').addEventListener('click', () => {
      $('start').classList.add('hide');
      $('quiz').classList.remove('hide');
      $('foot').classList.remove('hide');
      if (state.index >= state.order.length) finish(); else render();
      save();
    });
  }
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
