"""最小冒烟测试：包能导入、CLI 能跑、报告排版函数不编数字。

带真作者包的集成测试默认跳过；设 `VOICEKIT_TEST_PACK=<包目录>` 才跑。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    return subprocess.run(
        [sys.executable, "-m", "voicekit", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd or ROOT),
        env=env,
        timeout=180,
    )


def write_pack(dir_path: Path, **extra) -> Path:
    """写一个最小包：只有 pack.json + profile.json。extra 用来加 corpus 等键。"""
    (dir_path / "profile.json").write_text("{}", encoding="utf-8")
    pack = {"schema": 1, "slug": "demo", "author": "示例作者", "profile": {"path": "profile.json"}}
    pack.update(extra)
    (dir_path / "pack.json").write_text(json.dumps(pack), encoding="utf-8")
    return dir_path


def test_all_core_modules_import():
    from voicekit.core import (  # noqa: F401
        build_profile,
        judge_lib,
        pack_lib,
        rewrite,
        style_lint,
        voice_lib,
    )
    assert callable(style_lint.main)


def test_version():
    done = run_cli("--version")
    assert done.returncode == 0
    assert "voicekit" in done.stdout


def test_lint_without_pack_says_what_to_do(tmp_path: Path):
    draft = tmp_path / "draft.md"
    draft.write_text("今天下午我去了一趟海边，风很大。\n", encoding="utf-8")
    done = run_cli("lint", str(draft), cwd=tmp_path)
    assert done.returncode != 0
    assert "no voice pack" in (done.stdout + done.stderr)


def test_pack_loader_rejects_bad_schema(tmp_path: Path):
    from voicekit.core.pack_lib import PackError, load_pack

    (tmp_path / "pack.json").write_text(json.dumps({"schema": 2, "slug": "x"}), encoding="utf-8")
    with pytest.raises(PackError):
        load_pack(tmp_path)


def test_pack_loader_reads_synthetic_pack(tmp_path: Path):
    from voicekit.core.pack_lib import load_pack

    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "a.md").write_text("他写的第一篇。\n", encoding="utf-8")
    pack = {
        "schema": 1,
        "slug": "demo",
        "author": "示例作者",
        "corpus": {"path": "corpus"},
        "profile": {"path": "profile.json"},
    }
    (tmp_path / "pack.json").write_text(json.dumps(pack), encoding="utf-8")
    data = load_pack(tmp_path)
    assert data["slug"] == "demo"
    assert data["corpus"] == (tmp_path / "corpus").resolve()


def test_pack_loader_allows_pack_without_corpus(tmp_path: Path):
    """不提炼语料的包（只有基线 + profile）必须是合法包：lint/report 不需要 corpus。"""
    from voicekit.core.pack_lib import load_pack

    data = load_pack(write_pack(tmp_path))
    assert data["corpus"] is None
    assert data["profile"] == (tmp_path / "profile.json").resolve()


def test_pack_loader_rejects_missing_declared_corpus(tmp_path: Path):
    """声明了 corpus 但目录不在：仍要报错（防手误，不放松校验）。"""
    from voicekit.core.pack_lib import PackError, load_pack

    with pytest.raises(PackError, match="corpus"):
        load_pack(write_pack(tmp_path, corpus={"path": "nope"}))


def test_pack_corpus_gives_clear_error_for_baseline_only_pack(tmp_path: Path):
    """只发基线的包跑 judge/eval：要报人话错误，不能把 None 当路径用。"""
    from voicekit.core.pack_lib import PackError, load_pack, pack_corpus

    pack = load_pack(write_pack(tmp_path))
    with pytest.raises(PackError, match="没有 corpus"):
        pack_corpus(pack, "judge --pack")

    (tmp_path / "corpus").mkdir()
    with_corpus = load_pack(write_pack(tmp_path, corpus={"path": "corpus"}))
    assert pack_corpus(with_corpus, "judge --pack") == (tmp_path / "corpus").resolve()


def test_humanize_keeps_known_shapes():
    from voicekit.report import humanize

    assert humanize("obs=1.327 base=4.814 tol=±2.984") == "实测 1.327，他的基线 4.814，允许差 ±2.984"
    one = humanize("1 subkey(s) out: booster_per_k: 1.33 vs 0.47 (±0.47)")
    assert one.startswith("1 项偏离") and "1.33" in one
    # 认不出的口径原样返回，不许编
    assert humanize("weird shape") == "weird shape"


def test_record_share_flags_a_record_file():
    from voicekit.report import record_share

    record = "# 签字记录\n\n| 篇 | 日期 |\n| --- | --- |\n| 047 | 2026-11-22 |\n\n- 已签\n- 存档\n"
    prose = "风从西边来。我站在门口，看他把伞收起来，抖了两下水。" * 3
    assert record_share(record) >= 0.25
    assert record_share(prose) < 0.25


def test_sample_docs_reuses_pinned_names(monkeypatch):
    """重建基线沿用旧样本：否则作者样本悄悄换掉，钉死的 exemplar 10 篇全失配。"""
    from voicekit.core import judge_lib as jl

    docs = [{"file": f"{i:03d}_x.md", "text": ""} for i in range(10)]
    monkeypatch.setattr(jl, "_accepted_docs", lambda corpus: docs)

    picked = jl._sample_docs(Path("corpus"), 2, pinned=["007_x.md", "002_x.md"])

    assert [doc["file"] for doc in picked] == ["007_x.md", "002_x.md"]


def test_sample_docs_rejects_missing_pinned_doc(monkeypatch):
    from voicekit.core import judge_lib as jl

    monkeypatch.setattr(jl, "_accepted_docs", lambda corpus: [{"file": "001_x.md", "text": ""}])

    with pytest.raises(jl.JudgeError, match="pinned sample docs missing"):
        jl._sample_docs(Path("corpus"), 1, pinned=["999_gone.md"])


@pytest.mark.skipif(not os.environ.get("VOICEKIT_TEST_PACK"), reason="no VOICEKIT_TEST_PACK")
def test_lint_real_pack(tmp_path: Path):
    pack = Path(os.environ["VOICEKIT_TEST_PACK"])
    draft = tmp_path / "d.md"
    draft.write_text("风从西边来。我站在门口，看他把伞收起来，抖了两下水。\n" * 20, encoding="utf-8")
    done = run_cli("lint", str(draft), "--pack", str(pack))
    assert done.returncode in (0, 1), done.stderr
    assert "/42" in done.stdout or "42)" in done.stdout
