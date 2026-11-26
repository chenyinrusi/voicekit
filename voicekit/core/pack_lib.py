"""作者包（一个作者一个目录）的读取与校验。

包布局见 `dev/fixes/FIX-352-author-pack-generic/DESIGN.md` §D1：
`pack.json` 是唯一配置，里面的路径都相对包目录。
必需项只有 `schema` / `slug` / `profile`；`corpus` 可选（只有 build 需要，且 build 走自己的
`--corpus` 参数），见 `dev/fixes/voice-pack-manifest/DESIGN.md` D1。
本模块只做路径解析与结构校验，不读写语料、不调模型。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PACK_FILE = "pack.json"
SCHEMA = 1
#: 包内固定文件名（pack.json 里可覆盖，但 init 生成的就是这些）。
CORPUS_DIR = "corpus"
PROFILE_FILE = "profile.json"
EXEMPLAR_FILE = "exemplar_baseline.json"
BASELINE_FILE = "judge_baseline.json"
SKILL_FILE = "SKILL.md"
#: slug 同时是 skill 目录名：限制字符与长度，install 用它拼路径（F-07）。
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_SLUG = 40


class PackError(ValueError):
    """包缺失、schema 不认、路径或字段类型不合法。CLI 捕获后返回 2。"""


def _section(data: dict, key: str) -> dict:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PackError(f"pack.json {key}: must be an object, got {type(value).__name__}")
    return value


def _path_value(section: dict, label: str, default: str | None) -> str | None:
    value = section.get("path", default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"pack.json {label}.path must be a non-empty string")
    return value


def _resolve(root: Path, value: str | None, default: str | None) -> Path | None:
    """相对路径按包目录解析；统一 resolve()，`..` 归一化（F-11）。"""
    value = value or default
    if value is None:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def load_pack(path: Path) -> dict:
    """读包目录（或直接给 pack.json），返回解析好的绝对路径。"""
    path = Path(path)
    if path.is_dir():
        path = path / PACK_FILE
    if not path.is_file():
        raise PackError(f"pack.json not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackError(f"pack.json unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError("pack.json must be a JSON object")
    if data.get("schema") != SCHEMA:
        raise PackError(f"pack.json schema {data.get('schema')!r} != {SCHEMA}")
    slug = str(data.get("slug") or "").strip()
    if not slug:
        raise PackError("pack.json has no slug")
    if not SLUG_RE.fullmatch(slug) or len(slug) > MAX_SLUG:
        raise PackError(f"pack.json slug {slug!r} is not lowercase kebab-case (<= {MAX_SLUG})")
    author = str(data.get("author") or "").strip()
    root = path.parent
    corpus_meta = _section(data, "corpus")
    profile_meta = _section(data, "profile")
    exemplar_meta = _section(data, "exemplar")
    negatives_meta = _section(data, "negatives")
    corpus = _resolve(root, _path_value(corpus_meta, "corpus", None), None)
    #: corpus 只有提炼（build）才用，且 build 自己收 --corpus；体检/报告/起稿不需要。
    #: 声明了才校验，没声明就是 None —— 否则 CAS 这类「只发基线、不发语料」的包读不进来。
    if corpus is not None and not corpus.is_dir():
        raise PackError(f"pack corpus directory missing: {corpus}")
    profile = _resolve(root, _path_value(profile_meta, "profile", PROFILE_FILE), None)
    exemplar = _resolve(root, _path_value(exemplar_meta, "exemplar", EXEMPLAR_FILE), None)
    negatives_dir = _path_value(negatives_meta, "negatives", None)
    return {
        "path": path,
        "dir": root.resolve(),
        "slug": slug,
        "author": author,
        "data": data,
        "corpus": corpus,
        "profile": profile,
        "exemplar": exemplar,
        "baseline": (root / BASELINE_FILE).resolve(),
        "skill": (root / SKILL_FILE).resolve(),
        "negatives": _resolve(root, negatives_dir, None),
    }
