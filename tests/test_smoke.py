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


# ---------------------------------------------------------------- judge 子命令
# 全部离线：dry-run 与守卫不碰网络；真跑用注入的假 LLM（不花钱）。


class _FakeResp:
    def __init__(self, content: str, usage: dict):
        self.content = content
        self.usage = usage


class _FakeJudgeLLM:
    """假裁判：记录调用次数，返回预置 verdict 与 usage。"""

    def __init__(self, payload: str, usage: dict | None = None):
        self.payload = payload
        self.usage = usage or {"prompt_tokens": 1000, "completion_tokens": 100}
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return _FakeResp(self.payload, dict(self.usage))


def write_exemplar_pack(dir_path: Path, slug: str = "demo", set_slug: str | None = None) -> Path:
    """写一个泛型作者包：corpus 10 篇 + exemplar_baseline.json（unmeasured）。

    set_slug 用来造「样例集属于别的包」的失配。
    """
    from voicekit.core.exemplar_inputs import clean_body
    from voicekit.core.exemplar_measured import set_sha256, sha256

    corpus = dir_path / "corpus"
    corpus.mkdir(parents=True, exist_ok=True)
    docs = []
    for i in range(10):
        text = f"第{i}篇，他写的是风、是雨、是巷口的灯，还有夜里不太响的脚步声。" * 12
        name = f"{i:03d}__doc.md"
        (corpus / name).write_text(text, encoding="utf-8")
        body = clean_body(text)
        docs.append({"name": name, "sha256": sha256(body), "cjk": len(body)})
    exemplar = {
        "schema": 1,
        "protocol": "exemplar-same-author",
        "pack": set_slug or slug,
        "calibration": "unmeasured",
        "model": "deepseek-chat",
        "prompt_version": "exemplar-v2",
        "judge_logic_version": 1,
        "exemplar_count": len(docs),
        "holdout_count": 0,
        "docs": docs,
        "holdout": [],
        "set_sha256": set_sha256(docs),
        "measured": {},
    }
    (dir_path / "exemplar_baseline.json").write_text(
        json.dumps(exemplar, ensure_ascii=False), encoding="utf-8"
    )
    write_pack(dir_path, slug=slug, corpus={"path": "corpus"}, calibration="unmeasured")
    return dir_path


def write_draft(tmp_path: Path) -> Path:
    draft = tmp_path / "draft.md"
    draft.write_text("风从西边来，我站在门口，看他把伞收起来，抖了两下水。" * 4, encoding="utf-8")
    return draft


def test_judge_dry_run_costs_nothing(tmp_path: Path, monkeypatch):
    """--dry-run：rc 0、只打提示词规模、不建 client（没 API key 也能过）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("VOICEKIT_API_KEY", raising=False)
    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    done = run_cli("judge", str(draft), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "字符" in out and "要真跑才调模型" in out
    assert "missing API key" not in out


def test_judge_dry_run_never_creates_llm(tmp_path: Path):
    """函数级：假工厂一被调用就炸，证明 dry-run 没碰模型。"""
    from voicekit.core import release_gate

    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    lines: list[str] = []

    def boom(*_a, **_kw):
        raise AssertionError("dry-run must not create an LLM client")

    rc = release_gate.run(
        draft, pack, dry_run=True, llm_factory=boom, echo=lines.append
    )
    assert rc == 0
    assert any("要真跑才调模型" in line for line in lines)


def test_judge_missing_exemplar_baseline_is_rc2(tmp_path: Path):
    """包里没有 exemplar_baseline.json：rc 2 + 人话，不许降级成别的判定。"""
    (tmp_path / "pack").mkdir()
    pack = write_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    done = run_cli("judge", str(draft), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "exemplar_baseline.json" in out


def test_judge_pack_set_mismatch_is_rc2(tmp_path: Path):
    """样例集属于别的包：rc 2，两边 slug 都要说出来。"""
    pack = write_exemplar_pack(tmp_path / "pack", slug="demo", set_slug="other-author")
    draft = write_draft(tmp_path)
    done = run_cli("judge", str(draft), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "other-author" in out and "demo" in out


def test_judge_stale_baseline_is_rc2_and_says_how_to_rebuild(tmp_path: Path, monkeypatch):
    """基线过期（profile sha 不符）：rc 2，给出两个指纹短值 + 重建命令。"""
    pack = write_exemplar_pack(tmp_path / "pack")
    (pack / "judge_baseline.json").write_text(
        json.dumps({"profile_sha256": "0" * 64, "calibration": {"passed": True}}),
        encoding="utf-8",
    )
    draft = write_draft(tmp_path)
    done = run_cli("judge", str(draft), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "--build-baseline" in out
    assert "000000000000" in out


def test_judge_real_run_reports_verdict_tokens_cost(tmp_path: Path, monkeypatch):
    """真跑（假 LLM）：verdict / 样例篇数 / 指纹 / 模型 / tokens / cost_usd 都要有。"""
    from voicekit.core import release_gate

    monkeypatch.setenv("DSH_VOICE_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    fake = _FakeJudgeLLM('{"verdict": "same", "reason": "笔性像", "cues": ["逗号推进"]}')
    lines: list[str] = []

    rc = release_gate.run(
        draft, pack, llm_factory=lambda provider, model: fake, echo=lines.append
    )
    out = "\n".join(lines)
    assert rc == 0, out
    assert fake.calls == 1
    announce = [i for i, line in enumerate(lines) if line.startswith("将调用模型")]
    verdict_at = [i for i, line in enumerate(lines) if line.startswith("判定：")]
    assert announce and verdict_at and announce[0] < verdict_at[0], out  # 花钱前先说清楚
    assert "same" in out
    assert "样例 10 篇" in out
    assert "1100" in out  # tokens
    assert "0.000168" in out  # 1000*0.14/1e6 + 100*0.28/1e6
    assert "deepseek-chat" in out


def test_judge_distinct_verdict_is_rc1(tmp_path: Path, monkeypatch):
    """不同人 → not-same，rc 1；unsure 也按 not-same，但答案要露出来。"""
    from voicekit.core import release_gate

    monkeypatch.setenv("DSH_VOICE_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    fake = _FakeJudgeLLM('{"verdict": "unsure", "reason": "拿不准"}')
    lines: list[str] = []
    rc = release_gate.run(
        draft, pack, llm_factory=lambda provider, model: fake, echo=lines.append
    )
    out = "\n".join(lines)
    assert rc == 1, out
    assert "not-same" in out
    assert "unsure" in out


@pytest.mark.skipif(not os.environ.get("VOICEKIT_TEST_PACK"), reason="no VOICEKIT_TEST_PACK")
def test_lint_real_pack(tmp_path: Path):
    pack = Path(os.environ["VOICEKIT_TEST_PACK"])
    draft = tmp_path / "d.md"
    draft.write_text("风从西边来。我站在门口，看他把伞收起来，抖了两下水。\n" * 20, encoding="utf-8")
    done = run_cli("lint", str(draft), "--pack", str(pack))
    assert done.returncode in (0, 1), done.stderr
    assert "/42" in done.stdout or "42)" in done.stdout
