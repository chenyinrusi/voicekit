"""voicekit 命令行：lint（体检）/ report（一页报告）/ build（提炼）/ draft（起稿）。

设计原则：本层只做参数与路径解析，判据与算法全在 core/ 里。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, paths, report
from .core import style_lint


def _lint(args: argparse.Namespace) -> int:
    pack = paths.resolve_pack(args.pack)
    argv = ["--file", str(args.file)]
    argv += ["--pack", str(pack)]
    if args.json:
        argv += ["--json"]
    rc = style_lint.main(argv)
    if args.html is not None:
        out = None if args.html is True else Path(args.html)
        page = report.render(args.file, pack, out, args.title)
        print(f"一页报告：{page}")
    return rc


def _report(args: argparse.Namespace) -> int:
    pack = paths.resolve_pack(args.pack) if not args.no_pack else None
    page = report.render(args.file, pack, args.out, args.title)
    print(f"一页报告：{page}")
    return 0


def _build(args: argparse.Namespace) -> int:
    from .core import build_profile

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    argv = [
        "--corpus", str(Path(args.corpus)),
        "--out", str(out / "profile.json"),
        "--slug", args.slug,
        "--author", args.author,
    ]
    argv += ["--check"] if args.check else []
    rc = build_profile.main(argv)
    if rc == 0 and not args.check:
        _write_pack_json(out, args.slug, args.author, args.corpus)
    return rc


def _write_pack_json(pack_dir: Path, slug: str, author: str, corpus: Path) -> None:
    """提炼完补一个 pack.json，让这个目录马上能被 --pack 用。"""
    import json

    corpus_path = Path(corpus).resolve()
    try:
        corpus_rel = corpus_path.relative_to(pack_dir.resolve())
        corpus_value = corpus_rel.as_posix()
    except ValueError:
        corpus_value = str(corpus_path)
    data = {
        "schema": 1,
        "slug": slug,
        "author": author,
        "corpus": {"path": corpus_value},
        "profile": {"path": "profile.json"},
    }
    (pack_dir / "pack.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"作者包：{pack_dir}")


def _draft(args: argparse.Namespace) -> int:
    from .core import draft

    argv = ["--file", str(args.material)]
    argv += ["--out", str(args.out)] if args.out else []
    argv += ["--dry-run"] if args.dry_run else []
    argv += ["--provider", args.provider, "--model", args.model] if args.model else ["--provider", args.provider]
    return draft.main(argv)


def _judge(args: argparse.Namespace) -> int:
    from .core import release_gate

    pack = paths.resolve_pack(args.pack)
    return release_gate.run(
        args.file, pack, as_json=args.json, dry_run=args.dry_run
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="voicekit", description="提炼一个人的文风，体检一篇稿子，按他的腔起稿。")
    ap.add_argument("--version", action="version", version=f"voicekit {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    lint = sub.add_parser("lint", help="体检：42 条可数属性，逐条过/不过")
    lint.add_argument("file", type=Path, help="要体检的稿子（.md/.txt）")
    lint.add_argument("--pack", type=Path, help="作者包目录（默认找 ./voices/）")
    lint.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    lint.add_argument("--html", nargs="?", const=True, default=None, metavar="OUT",
                      help="同时生成一页 HTML 报告（可给输出路径）")
    lint.add_argument("--title", help="报告标题")
    lint.set_defaults(func=_lint)

    rep = sub.add_parser("report", help="一页 HTML 报告：结论 3 行 + 先改这三条 + 全部条目")
    rep.add_argument("file", type=Path)
    rep.add_argument("--pack", type=Path)
    rep.add_argument("--no-pack", action="store_true", help="不加载作者包（只跑通用检查）")
    rep.add_argument("-o", "--out", type=Path, help="写到哪（默认 <file>-report.html）")
    rep.add_argument("--title")
    rep.set_defaults(func=_report)

    build = sub.add_parser("build", help="提炼：从一个语料目录算出一份作者 profile")
    build.add_argument("--corpus", required=True, type=Path, help="语料目录（一篇一个 .md/.txt）")
    build.add_argument("--out", required=True, type=Path, help="作者包目录（写 profile.json + pack.json）")
    build.add_argument("--slug", default="myvoice", help="作者 slug（小写连字符）")
    build.add_argument("--author", default="我的作者", help="作者显示名")
    build.add_argument("--check", action="store_true", help="只比对，不写文件")
    build.set_defaults(func=_build)

    dr = sub.add_parser("draft", help="起稿：按作者的腔写一篇初稿（要 API key）")
    dr.add_argument("--material", required=True, type=Path, help="材料（事实清单）")
    dr.add_argument("--out", type=Path, help="初稿写到哪")
    dr.add_argument("--provider", default="deepseek")
    dr.add_argument("--model")
    dr.add_argument("--dry-run", action="store_true", help="0 元：只出提示词")
    dr.set_defaults(func=_draft)

    jd = sub.add_parser("judge", help="判同人：拿 10 篇样例判这篇是不是同一个人（要花 token，只作参考）")
    jd.add_argument("file", type=Path, help="要判的稿子（.md/.txt）")
    jd.add_argument("--pack", type=Path, help="作者包目录（默认找 ./voices/）")
    jd.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    jd.add_argument("--dry-run", action="store_true", help="0 元：只打印提示词规模，不调模型")
    jd.set_defaults(func=_judge)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
