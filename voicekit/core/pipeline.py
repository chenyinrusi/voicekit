"""流水线 v0：材料 → 起稿 → 改稿 → 体检。一条命令跑完，每步留产物。

为什么是这个形状（证据在各脚本自己的 docstring 里）：
- 冷启动代笔（只给题目）：盲读者 10/10 认得出。
- 给同一份内容、只改语言：3/10，等于瞎猜。
  所以主循环是「材料 → 稿」，不是「题目 → 稿」。
- 四轮盲测说穿的原因都是「没有他的私人材料」，所以第 0 步先查材料够不够。

本文件只做串联与记录，不复制任何判据：四步全是已有脚本。
第 0 步 intake 不够就直接停，不给模型钱。

用法（repo 根）：
    py -3.10 scripts/voice/pipeline.py --material tmp/in.md --slug 2026-11-14-a
    py -3.10 scripts/voice/pipeline.py --material tmp/in.md --slug x --dry-run   # 0 元
    py -3.10 scripts/voice/pipeline.py --material tmp/in.md --no-rewrite         # 只起稿
    py -3.10 scripts/voice/pipeline.py --selftest

产物（默认 tmp/voice-runs/<slug>/）：
    01-draft.md          起稿（去掉标记的正文）
    01-draft.marked.md   起稿（带〔推断〕〔复用·篇号〕，给他逐句核）
    01-draft.json        红/灰/要填的私人经历
    02-final.md          改稿后的成稿
    02-rewrite.json      改稿报告（守卫是否过）
    03-lint.json         42 条体检输出
    run.json             每步 rc、耗时、路径
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
PY = sys.executable


def _run(name: str, argv: list[str], log_dir: Path) -> dict:
    """跑一步。stdout/stderr 全存文件，只在结尾摘要里给一行。"""
    started = time.time()
    proc = subprocess.run(
        argv, cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    seconds = round(time.time() - started, 1)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"{name}.log").write_text(
        (proc.stdout or "") + "\n--- stderr ---\n" + (proc.stderr or ""),
        encoding="utf-8", newline="\n",
    )
    return {"name": name, "argv": argv, "rc": proc.returncode, "seconds": seconds,
            "stdout": proc.stdout or "", "log": str(log_dir / f"{name}.log")}


def plan(material: Path, run_dir: Path, rewrite_on: bool = True,
         dry_run: bool = False) -> list[tuple[str, list[str]]]:
    """四步的命令清单。dry_run 只排到起稿（0 元）。"""
    steps: list[tuple[str, list[str]]] = [
        ("intake", [PY, str(SCRIPT_DIR / "intake.py"), "--check", str(material)]),
        ("draft", [PY, str(SCRIPT_DIR / "draft.py"), "--file", str(material),
                   "--out", str(run_dir / "01-draft.md"),
                   "--json", str(run_dir / "01-draft.json")]),
    ]
    if dry_run:
        steps[-1][1].append("--dry-run")
        return steps
    if rewrite_on:
        steps.append(("rewrite", [PY, str(SCRIPT_DIR / "rewrite.py"),
                                  "--file", str(run_dir / "01-draft.md"),
                                  "--out", str(run_dir / "02-final.md"),
                                  "--json", str(run_dir / "02-rewrite.json")]))
    steps.append(("lint", [PY, str(SCRIPT_DIR / "style_lint.py"),
                           "--file", str(run_dir / ("02-final.md" if rewrite_on
                                                    else "01-draft.md")),
                           "--json"]))
    return steps


def _selftest() -> int:
    material = Path("tmp/in.md")
    run_dir = Path("tmp/voice-runs/x")
    full = plan(material, run_dir, True)
    assert [name for name, _ in full] == ["intake", "draft", "rewrite", "lint"], full
    no_rewrite = plan(material, run_dir, False)
    assert [name for name, _ in no_rewrite] == ["intake", "draft", "lint"], no_rewrite
    assert "01-draft.md" in " ".join(no_rewrite[-1][1]), no_rewrite[-1]
    assert "02-final.md" in " ".join(full[-1][1]), full[-1]
    dry = plan(material, run_dir, True, dry_run=True)
    assert [name for name, _ in dry] == ["intake", "draft"], dry
    assert "--dry-run" in dry[-1][1]
    assert "01-draft.md" in " ".join(full[1][1])
    print("[OK] selftest: 四步顺序、--no-rewrite、--dry-run、路径都过")
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="材料 → 起稿 → 改稿 → 体检")
    ap.add_argument("--material", type=Path, help="材料（intake 九类问答格式）")
    ap.add_argument("--slug", help="这轮叫什么（默认取材料文件名）")
    ap.add_argument("--outdir", type=Path, help="产物目录（默认 tmp/voice-runs/<slug>）")
    ap.add_argument("--no-rewrite", action="store_true", help="只起稿，不过改稿器")
    ap.add_argument("--allow-thin", action="store_true", help="材料不够也继续（不推荐）")
    ap.add_argument("--dry-run", action="store_true", help="0 元：只查材料 + 出提示词")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    if not args.material:
        ap.error("要 --material 材料")
    if not args.material.is_file():
        ap.error(f"材料不存在：{args.material}")
    slug = args.slug or args.material.stem
    run_dir = args.outdir or (REPO_ROOT / "tmp" / "voice-runs" / slug)
    run_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    for name, argv in plan(args.material, run_dir, not args.no_rewrite, args.dry_run):
        record = _run(name, argv, run_dir / "logs")
        records.append(record)
        print(f"  · {name}: rc={record['rc']} {record['seconds']}s")
        if name == "intake" and record["rc"] != 0 and not args.allow_thin:
            print("    材料不够，停。看 logs/intake.log 缺哪几类。")
            break
        if name == "draft" and record["rc"] not in (0, 1):
            print("    起稿挂了，停。看 logs/draft.log。")
            break

    summary = {"slug": slug, "material": str(args.material), "run_dir": str(run_dir),
               "dry_run": args.dry_run, "no_rewrite": args.no_rewrite,
               "steps": [{k: v for k, v in r.items() if k != "stdout"} for r in records]}
    (run_dir / "run.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8", newline="\n",
    )
    final = run_dir / ("01-draft.md" if args.no_rewrite else "02-final.md")
    print(f"\n产物目录：{run_dir}")
    if final.is_file():
        print(f"成稿：{final}")
    print(f"每步日志：{run_dir / 'logs'}")
    failed = [r["name"] for r in records if r["rc"] not in (0, 1)]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
