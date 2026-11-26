"""从三审 docx 生成作者书稿语料。

只做三件事：接受修订取最终文本、按二级标题切篇、跳过 AI 代笔的后记。
生成的文件名与标题一一对应，供 profile/样例/留出集钉住（FIX-350）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
REPO_ROOT = Path(__file__).resolve().parents[2]
#: 切篇失败的兜底：本书 64 篇正文，正常至少 50 篇；低于它多半是 docx 样式变了。
MIN_ARTICLES = 50
#: 后记1 是 DeepSeek 写的书评，不属于作者语料。
SKIP_TITLES = {"后记1——DeepSeek书评"}
#: 小说两篇不进语料：本 skill 只管中文随笔，体裁判据见 DESIGN §2。
SKIP_PARTS = {"小说"}
MIN_CJK = 200


def para_text(paragraph) -> str:
    """最终文本：插入的修订算数，删除的修订不算。"""
    parts = []
    for node in paragraph._p.iter(W + "t"):
        parent = node.getparent()
        deleted = False
        while parent is not None:
            if parent.tag == W + "del":
                deleted = True
                break
            parent = parent.getparent()
        if not deleted:
            parts.append(node.text or "")
    return "".join(parts).strip()


def split_articles(paragraphs) -> list[dict]:
    """二级标题切篇；一级标题只记录所属辑名。"""
    articles: list[dict] = []
    current: dict | None = None
    part = ""
    for paragraph in paragraphs:
        style = paragraph.style.name
        if style == "Heading 1":
            part = paragraph.text.strip()
            continue
        if style == "Heading 2":
            current = {"part": part, "title": paragraph.text.strip(), "paras": []}
            articles.append(current)
            continue
        if current is None:
            continue
        text = para_text(paragraph)
        if text:
            current["paras"].append(text)
    return articles


def slug(title: str) -> str:
    """文件名用：标点换空格，压缩空白；保留中文、字母、数字。"""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", title)
    return re.sub(r"\s+", " ", cleaned).strip()


def build(
    source: Path,
    *,
    min_cjk: int = MIN_CJK,
    skip_titles: set[str] = SKIP_TITLES,
    skip_parts: set[str] = SKIP_PARTS,
) -> dict:
    """切篇。skip_titles / skip_parts 默认是本书的排除项，别的作者建包时传空集。"""
    from docx import Document

    doc = Document(str(source))
    articles = split_articles(doc.paragraphs)
    docs, skipped = [], []
    for index, article in enumerate(articles, 1):
        title = article["title"]
        body = "\n".join(article["paras"])
        cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
        if title in skip_titles:
            skipped.append({"title": title, "reason": "ai-authored-afterword"})
            continue
        if re.sub(r"\s+", "", article["part"]) in skip_parts:
            skipped.append({"title": title, "reason": "out-of-scope-genre", "part": article["part"]})
            continue
        if cjk < min_cjk:
            skipped.append({"title": title, "reason": f"cjk<{min_cjk}", "cjk": cjk})
            continue
        name = f"{index:03d}__{slug(title)}.md"
        docs.append({"file": name, "title": title, "part": article["part"], "cjk": cjk, "raw": f"# {title}\n\n{body}\n"})
    duplicates = {doc["file"] for doc in docs if [d["file"] for d in docs].count(doc["file"]) > 1}
    if duplicates:
        raise ValueError(f"duplicate corpus file names: {sorted(duplicates)}")
    return {
        "source": source.name,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "articles": docs,
        "skipped": skipped,
    }


def write(manifest: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    keep = {doc["file"] for doc in manifest["articles"]}
    # 只清本生成器命名过的旧篇（NNN__*.md），不碰目录里其它文件。
    for stale in out.glob("*.md"):
        if stale.name not in keep and re.match(r"^\d{3}__", stale.name):
            stale.unlink()
    for doc in manifest["articles"]:
        (out / doc["file"]).write_text(doc["raw"], encoding="utf-8", newline="\n")
    payload = {
        key: manifest[key] for key in ("source", "source_sha256", "generated_at", "skipped")
    }
    try:
        payload["corpus"] = out.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        payload["corpus"] = out.as_posix()
    payload["docs"] = [
        {"file": doc["file"], "title": doc["title"], "part": doc["part"], "cjk": doc["cjk"]}
        for doc in manifest["articles"]
    ]
    (out.parent / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从三审 docx 生成作者书稿语料")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--force",
        action="store_true",
        help=f"源稿切出少于 {MIN_ARTICLES} 篇时也写盘（默认拒绝，防止切篇失败清空语料）",
    )
    args = parser.parse_args(argv)
    if not args.source.is_file():
        print(f"[FAIL] source docx not found: {args.source}", file=sys.stderr)
        return 2
    manifest = build(args.source)
    if len(manifest["articles"]) < MIN_ARTICLES and not args.force:
        print(
            f"[FAIL] only {len(manifest['articles'])} article(s) parsed "
            f"(< {MIN_ARTICLES}); docx styles changed? refusing to overwrite corpus "
            f"(use --force only after checking the split)",
            file=sys.stderr,
        )
        return 2
    write(manifest, args.out)
    print(f"corpus written: {args.out}  docs={len(manifest['articles'])}  skipped={len(manifest['skipped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
