"""把一次体检变成一页自包含 HTML（结论 3 行 + 统计块 + 先改这三条 + 全部 42 条）。

只排版，不新增判据：分数、通过数、偏差全部来自 `voicekit.core.style_lint --json`。
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import re
from pathlib import Path

from .core import style_lint
from .core.evidence import NO_EVIDENCE
from .core.quick import HAN_RE, share_non_prose

CSS = """
:root{--ink:#1c1a17;--dim:#6b6459;--line:#dfd8cc;--bg:#fbf9f4;--card:#fffdf8;--acc:#8a5a2b;--bad:#a4342a;--ok:#2f6b46}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.75 "Songti SC","SimSun",system-ui,-apple-system,sans-serif}
.wrap{max-width:920px;margin:0 auto;padding:clamp(1rem,4vw,2.5rem) clamp(1rem,4vw,2rem)}
h1{font-size:clamp(1.35rem,3.6vw,1.9rem);margin:0 0 .3rem}
.sub{color:var(--dim);font-size:.9rem;margin-bottom:1.6rem}
.score{display:flex;align-items:baseline;gap:.6rem;margin:0 0 1.2rem}
.score b{font-size:clamp(2.6rem,8vw,3.6rem);line-height:1;color:var(--acc);font-weight:600}
.score span{color:var(--dim);font-size:.95rem}
.lead{border-left:3px solid var(--acc);padding:.2rem 0 .2rem 1rem;margin:0 0 1.8rem}
.lead p{margin:.25rem 0}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.8rem;margin:0 0 2rem}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
.tile .k{color:var(--dim);font-size:.8rem;letter-spacing:.04em}
.tile .v{font-size:1.5rem;font-weight:600;margin-top:.2rem}
.tile .v.bad{color:var(--bad)} .tile .v.ok{color:var(--ok)}
h2{font-size:1.1rem;margin:2.2rem 0 .7rem;padding-bottom:.3rem;border-bottom:1px solid var(--line)}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:.55rem .5rem;border-bottom:1px solid var(--line)}
th{color:var(--dim);font-weight:500;font-size:.8rem}
code{font:.85em/1.5 ui-monospace,Consolas,monospace;background:#f4efe4;padding:.1rem .3rem;border-radius:4px;word-break:break-word}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.7rem 1rem;margin:.5rem 0}
summary{cursor:pointer;color:var(--acc)}
details p{margin:.5rem 0 0;color:var(--dim);font-size:.9rem}
.bar{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin:1.4rem 0 0}
button{font:inherit;font-size:.9rem;padding:.5rem 1rem;border:1px solid var(--acc);background:var(--acc);color:#fff;border-radius:8px;cursor:pointer}
#copy-decision-ok{color:var(--ok);font-size:.88rem;min-height:1.2em}
.note{margin-top:2rem;padding:.9rem 1rem;background:#f6f1e6;border-radius:10px;color:var(--dim);font-size:.88rem}
.fix3{display:grid;gap:.6rem;margin:.4rem 0 1rem}
.fix3 .it{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--bad);border-radius:10px;padding:.8rem 1rem}
.fix3 .t{font-weight:600}
.fix3 .d{margin-top:.35rem;color:var(--dim);font-size:.9rem}
.ev{margin-top:.35rem;font-size:.88rem;color:var(--acc);word-break:break-word}
.dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:.5rem}
.warn{margin:0 0 1.4rem;padding:.9rem 1rem;background:#fdf0ee;border:1px solid #e6bdb6;border-left:4px solid var(--bad);border-radius:10px;color:#7d2a22;font-size:.92rem}
@media print{body{background:#fff}.bar{display:none}details{break-inside:avoid}}
"""


def han_len(text: str) -> int:
    return len(HAN_RE.findall(text))


def humanize(detail: str) -> str:
    """把 lint 的内部口径翻译成人话；翻不动的原样保留，不编数字。"""
    s = (detail or "").strip()
    m = re.match(r"^(\d+) subkey\(s\) out: (.+)$", s)
    if m:
        parts = re.findall(r"(\S+): ([\d.]+) vs ([\d.]+) \(±([\d.]+)\)", m.group(2))
        if parts:
            body = "、".join(f"{k} {a}（他 {b}，允差 ±{c}）" for k, a, b, c in parts)
            return f"{m.group(1)} 项偏离：{body}"
    m = re.match(r"^obs=([\d.]+) base=([\d.]+) tol=±([\d.]+)$", s)
    if m:
        return f"实测 {m.group(1)}，他的基线 {m.group(2)}，允许差 ±{m.group(3)}"
    return s


def record_share(text: str) -> float:
    """这份文件里有多少行不是正文（标题/表格/引用/列表/加粗小标题）。"""
    return share_non_prose(text)


def run_lint(draft: Path, pack: Path | None = None) -> dict:
    buf = io.StringIO()
    argv = ["--file", str(draft), "--json", "--annotate"]
    if pack is not None:
        argv += ["--pack", str(pack)]
    with contextlib.redirect_stdout(buf):
        style_lint.main(argv)
    raw = buf.getvalue()
    start = raw.find("{")
    if start < 0:
        raise SystemExit(f"style_lint 没给 JSON：\n{raw[-1500:]}")
    data, _ = json.JSONDecoder().raw_decode(raw[start:])
    return data


def build(draft: Path, data: dict, title: str) -> str:
    rows = data.get("rows", [])
    fails = sorted([r for r in rows if not r.get("ok")], key=lambda r: -float(r.get("deviation") or 0))
    score = float(data.get("score") or 0)
    passed = int(data.get("passed") or 0)
    total = int(data.get("total") or len(rows))
    body = draft.read_text(encoding="utf-8", errors="replace") if draft.is_file() else ""
    prose = "\n".join(
        ln for ln in body.splitlines() if not ln.lstrip().startswith(("#", ">", "|", "标题", "作者"))
    )
    chars = han_len(prose)
    top3 = fails[:3]
    rest = fails[3:]

    e = html.escape
    lead = [
        f"分数 {score:g} / 100，{total} 条里过 {passed} 条。",
        f"最该先动的 {len(top3)} 条：{'、'.join(e(r['attribute_name']) for r in top3) or '无'}。",
        "分数不是及格线：签过字的老稿也常掉分，签不签字由人定。",
    ]

    def tile(k: str, v: str, cls: str = "") -> str:
        return f'<div class="tile"><div class="k">{e(k)}</div><div class="v {cls}">{e(v)}</div></div>'

    tiles = "".join(
        [
            tile("通过 / 总数", f"{passed} / {total}", "ok" if passed == total else ""),
            tile("不过", str(len(fails)), "bad" if fails else "ok"),
            tile("正文汉字", str(chars)),
            tile("最大偏差", f"{float(top3[0]['deviation']):.1f}" if top3 else "—", "bad" if top3 else ""),
        ]
    )
    def ev_line(r: dict) -> str:
        note = r.get("evidence_note") or ""
        if not note:
            return ""
        if note == NO_EVIDENCE:
            # 「句级证据：无句级证据」读起来像结巴，换个说法
            return '<div class="ev">这项是全篇统计，指不到具体句子</div>'
        return f'<div class="ev">句级证据（只标位置，不判好坏）：{e(note)}</div>'

    fix3_html = "".join(
        f'<div class="it"><div class="t">{i}. {e(r["attribute_name"])}'
        f'<span class="d"> 偏差 {float(r.get("deviation") or 0):.1f}</span></div>'
        f'<div class="d"><code>{e(humanize(r.get("detail") or ""))}</code></div>'
        f"{ev_line(r)}</div>"
        for i, r in enumerate(top3, 1)
    )
    detail_html = "".join(
        f"<details><summary>{e(r['attribute_id'])} · {e(r['attribute_name'])}"
        f"（偏差 {float(r.get('deviation') or 0):.1f}）</summary>"
        f"<p>{e(humanize(r.get('detail') or ''))}</p>"
        f"{ev_line(r)}"
        f"<p><code>原文口径：{e(r.get('detail') or '')}</code></p></details>"
        for r in rest
    )
    all_html = "".join(
        f"<tr><td><code>{e(r['attribute_id'])}</code> {e(r['attribute_name'])}</td>"
        f"<td>{'过' if r.get('ok') else '不过'}</td></tr>"
        for r in rows
    )
    copy_text = "改稿清单（来自 voicekit lint）：\n" + "\n".join(
        f"- {r['attribute_id']} {r['attribute_name']}：{humanize(r.get('detail') or '')}"
        + (f"（{r['evidence_note']}）" if r.get("evidence_note") else "")
        for r in fails
    )
    share = record_share(body)
    guard_html = (
        f'<div class="warn">这份文件 {share:.0%} 的行是标题/表格/记录，不是一篇正文。'
        f"分数只对正文有意义，请只贴正文再跑一次。</div>"
        if share >= 0.25
        else ""
    )
    copy_js = json.dumps(copy_text, ensure_ascii=False)

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} · 文风报告</title><style>{CSS}</style></head>
<body><div class="wrap">
<h1>文风报告 · {e(title)}</h1>
<div class="sub">{e(draft.name)} · 判据 = 42 条可数属性（voicekit lint）· 不新增判据</div>
<div class="score"><b>{score:g}</b><span>/ 100（加权 {float(data.get('score_weighted') or score):g}）</span></div>
<div class="lead">{''.join(f'<p>{p}</p>' for p in lead)}</div>
<div class="tiles">{tiles}</div>
{guard_html}
<h2>先改这三条</h2>
<div class="fix3">{fix3_html or '<p>没有不过的条目。</p>'}</div>
<div class="bar"><button id="copy-decision">复制改稿清单（{len(fails)} 条）</button><span id="copy-decision-ok"></span></div>
<h2>其余 {len(rest)} 条</h2>
<div class="dgrid">{detail_html or '<p>没有更多不通过的条目。</p>'}</div>
<h2>全部 {total} 条</h2>
<details><summary>展开看每条过没过</summary><table><tbody>{all_html}</tbody></table></details>
<div class="note">这份报告只数「像不像他」，不判写得好不好。体裁若是论文/说理/公号，语料里没有这种文体，分数偏低是预期的。放行标准始终是作者肯不肯签字。</div>
</div>
<script>
const COPY = {copy_js};
function done(msg){{ const el=document.getElementById('copy-decision-ok'); el.textContent=msg; }}
document.getElementById('copy-decision').addEventListener('click', async () => {{
  try {{
    if (navigator.clipboard && window.isSecureContext) {{ await navigator.clipboard.writeText(COPY); done('已复制'); return; }}
  }} catch (err) {{ /* 落到下面的兜底 */ }}
  const ta = document.createElement('textarea');
  ta.value = COPY; ta.style.position='fixed'; ta.style.opacity='0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  const ok = document.execCommand('copy'); document.body.removeChild(ta);
  done(ok ? '已复制' : '复制失败，请手动选择清单');
}});
</script>
</body></html>
"""


def render(draft: Path, pack: Path | None, out: Path | None, title: str | None) -> Path:
    out = out or draft.with_name(draft.stem + "-report.html")
    page = build(draft, run_lint(draft, pack), title or draft.stem)
    out.write_text(page, encoding="utf-8")
    return out
