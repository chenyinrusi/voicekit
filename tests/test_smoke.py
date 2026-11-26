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
    draft.write_text("风从西边来，我站在门口，看他把伞收起来，抖了两下水。" * 20, encoding="utf-8")
    return draft


def write_pinned_pack(dir_path: Path) -> Path:
    """钉死包（像 CAS 的 panchen-voice）：样例集没有 pack/calibration 键，名单靠包里的 judge_baseline.json 推。"""
    from voicekit.core import judge_lib as jl
    from voicekit.core import voice_lib as lib
    from voicekit.core.exemplar_inputs import clean_body
    from voicekit.core.exemplar_measured import set_sha256, sha256

    dir_path.mkdir(parents=True, exist_ok=True)
    corpus = dir_path / "corpus"
    corpus.mkdir()
    names = []
    for i in range(1, 13):
        name = f"{i:03d}__doc.md"
        text = f"第{i}篇。他写风、写雨、写巷口的灯，还有夜里不太响的脚步声。" * 14
        (corpus / name).write_text(text, encoding="utf-8")
        names.append(name)
    pack_dir = write_pack(dir_path, corpus={"path": "corpus"})
    # 基线：profile 指纹 + 12 篇样本（exemplar_names 从 sample 里取前 10）
    baseline = {
        "schema": 1,
        "profile_sha256": jl.profile_sha256(pack_dir / "profile.json"),
        "sample": {"docs": list(names)},
    }
    (dir_path / "judge_baseline.json").write_text(
        json.dumps(baseline), encoding="utf-8"
    )
    docs = []
    for name in names[:10]:
        body = clean_body((corpus / name).read_text(encoding="utf-8"))
        docs.append({"name": name, "sha256": sha256(body), "cjk": lib.cjk_count(body)})
    exemplar = {
        "schema": 1,
        "protocol": "exemplar-same-author",
        "model": "deepseek-chat",
        "prompt_version": "exemplar-v2",
        "judge_logic_version": 1,
        "exemplar_count": 10,
        "holdout_count": 0,
        "docs": docs,
        "holdout": [],
        "set_sha256": set_sha256(docs),
        "measured": {
            "model": "deepseek-chat",
            "runs": 1,
            "author_pool": {"n": 0},
            "author_holdout": {"n": 12},
            "other_author": {"n": 46},
            "generic_ai": {"n": 2},
        },
    }
    (dir_path / "exemplar_baseline.json").write_text(
        json.dumps(exemplar, ensure_ascii=False), encoding="utf-8"
    )
    return pack_dir


def test_verify_pinned_set_reads_pack_baseline(tmp_path: Path, monkeypatch):
    """钉死集的名单只能从包里的基线推；开发者树里的默认基线在 wheel 里不存在。"""
    from voicekit.core import exemplar_judge as ej
    from voicekit.core import judge_lib as jl

    monkeypatch.setattr(ej, "HOLDOUT_COUNT", 0)
    monkeypatch.setattr(ej, "HOLDOUT_NAMES", ())
    pack_dir = write_pinned_pack(tmp_path / "pack")
    set_obj = json.loads((pack_dir / "exemplar_baseline.json").read_text(encoding="utf-8"))
    corpus = pack_dir / "corpus"
    baseline = pack_dir / "judge_baseline.json"

    real_load = jl.load_baseline
    monkeypatch.setattr(
        jl, "load_baseline", lambda path=None: None if path is None else real_load(path)
    )
    assert any(
        "no judge baseline" in p for p in ej.verify_set(set_obj, corpus)
    ), "没有包基线时必须报缺基线"
    assert ej.verify_set(set_obj, corpus, baseline=baseline) == []


def test_judge_pinned_pack_works_without_dev_tree(tmp_path: Path, monkeypatch):
    """装成 wheel 时没有 voices/：钉死包要能靠自带的 judge_baseline.json 跑 dry-run。"""
    from voicekit.core import exemplar_judge as ej
    from voicekit.core import judge_lib as jl
    from voicekit.core import release_gate

    real_load = jl.load_baseline
    monkeypatch.setattr(
        jl, "load_baseline", lambda path=None: None if path is None else real_load(path)
    )
    monkeypatch.setattr(ej, "HOLDOUT_COUNT", 0)
    monkeypatch.setattr(ej, "HOLDOUT_NAMES", ())
    pack = write_pinned_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    lines: list[str] = []
    rc = release_gate.run(draft, pack, dry_run=True, echo=lines.append)
    assert rc == 0, "\n".join(lines)
    assert any("字符" in line for line in lines)


def test_expected_sizes_uses_pack_baseline(tmp_path: Path, monkeypatch):
    """评测样本量也从包里的基线算。读不到默认基线时算错 → 真跑 rc=2（wheel 现场报错）。"""
    from voicekit.core import exemplar_judge as ej
    from voicekit.core import judge_lib as jl

    real_load = jl.load_baseline
    monkeypatch.setattr(
        jl, "load_baseline", lambda path=None: None if path is None else real_load(path)
    )
    pack_dir = write_pinned_pack(tmp_path / "pack")
    corpus = pack_dir / "corpus"
    baseline = pack_dir / "judge_baseline.json"
    assert ej.expected_sizes(corpus, baseline)["author_pool"] == 0
    assert ej.expected_sizes(corpus)["author_pool"] == 12


def test_judge_pinned_real_run_without_dev_tree(tmp_path: Path, monkeypatch):
    """钉死包真跑（假 LLM）：缺开发树时也不能被「measured n != 0」挡死。"""
    from voicekit.core import exemplar_judge as ej
    from voicekit.core import judge_lib as jl
    from voicekit.core import release_gate

    real_load = jl.load_baseline
    monkeypatch.setattr(
        jl, "load_baseline", lambda path=None: None if path is None else real_load(path)
    )
    monkeypatch.setattr(ej, "HOLDOUT_COUNT", 0)
    monkeypatch.setattr(ej, "HOLDOUT_NAMES", ())
    monkeypatch.setenv("DSH_VOICE_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_pinned_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    fake = _FakeJudgeLLM('{"verdict": "same", "reason": "笔性像"}')
    lines: list[str] = []
    rc = release_gate.run(
        draft, pack, llm_factory=lambda provider, model: fake, echo=lines.append
    )
    assert rc == 0, "\n".join(lines)


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


def test_judge_bad_inputs_are_rc2_not_rc1(tmp_path: Path):
    """环境/文件坏了都是 rc=2；rc=1 只能表示「不像」（脚本靠这个分叉）。"""
    draft = write_draft(tmp_path)
    cases = []

    # 1) --pack 指到不是包的目录
    (tmp_path / "nope").mkdir()
    cases.append(("--pack", str(tmp_path / "nope")))

    # 2) exemplar_baseline.json 坏 JSON
    pack = write_exemplar_pack(tmp_path / "pack_a")
    (pack / "exemplar_baseline.json").write_text("{oops", encoding="utf-8")
    cases.append(("--pack", str(pack)))

    # 3) judge_baseline.json 坏 JSON
    pack = write_exemplar_pack(tmp_path / "pack_b")
    (pack / "judge_baseline.json").write_text("{oops", encoding="utf-8")
    cases.append(("--pack", str(pack)))

    for flag, value in cases:
        done = run_cli("judge", str(draft), flag, value, "--dry-run", cwd=tmp_path)
        out = done.stdout + done.stderr
        assert done.returncode == 2, (flag, value, done.returncode, out)
        assert "[FAIL]" in out, out


def test_judge_broken_baseline_json_names_the_file(tmp_path: Path):
    pack = write_exemplar_pack(tmp_path / "pack")
    (pack / "judge_baseline.json").write_text("[1, 2]", encoding="utf-8")
    draft = write_draft(tmp_path)
    done = run_cli("judge", str(draft), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "judge_baseline.json" in out


@pytest.mark.skipif(not os.environ.get("VOICEKIT_TEST_PACK"), reason="no VOICEKIT_TEST_PACK")
def test_lint_real_pack(tmp_path: Path):
    pack = Path(os.environ["VOICEKIT_TEST_PACK"])
    draft = tmp_path / "d.md"
    draft.write_text("风从西边来。我站在门口，看他把伞收起来，抖了两下水。\n" * 20, encoding="utf-8")
    done = run_cli("lint", str(draft), "--pack", str(pack))
    assert done.returncode in (0, 1), done.stderr
    assert "/42" in done.stdout or "42)" in done.stdout


def test_weights_come_from_active_pack(tmp_path: Path):
    """加权表要跟包走：包里有 weights.json 就用它，不许只认开发树。

    装成 wheel 后旧写法指向 site-packages/voices（不存在），加权表静默退化成等权：
    `score` 不变（放行用等权），但 `score_weighted` 会少报真实值 —— 实测 CAS 包
    047 号稿：wheel 61.9 / 源码树 68.6。
    """
    from voicekit import paths
    from voicekit.core import voice_lib as lib

    (tmp_path / "pack").mkdir()
    pack = write_pack(tmp_path / "pack")
    (pack / "weights.json").write_text(
        json.dumps({"tiers": [{"weight": 7.0, "ids": ["L9-99"]}]}), encoding="utf-8"
    )
    old = paths._active_pack
    try:
        paths.use_pack(pack)
        assert lib.load_weights()["table"].get("L9-99") == 7.0
    finally:
        paths._active_pack = old
        lib.reset_weights()


def test_weights_do_not_leak_between_packs(tmp_path: Path):
    """同一进程连开两个包（脚本常见），加权表不能沿用上一个包的结果。"""
    from voicekit import paths
    from voicekit.core import voice_lib as lib

    (tmp_path / "p1").mkdir()
    first = write_pack(tmp_path / "p1")
    (first / "weights.json").write_text(
        json.dumps({"tiers": [{"weight": 7.0, "ids": ["L9-99"]}]}), encoding="utf-8"
    )
    (tmp_path / "p2").mkdir()
    second = write_pack(tmp_path / "p2")
    (second / "weights.json").write_text(
        json.dumps({"tiers": [{"weight": 0.5, "ids": ["L9-99"]}]}), encoding="utf-8"
    )
    old = paths._active_pack
    try:
        paths.use_pack(first)
        assert lib.load_weights()["table"].get("L9-99") == 7.0
        paths.use_pack(second)
        assert lib.load_weights()["table"].get("L9-99") == 0.5
    finally:
        paths._active_pack = old
        lib.reset_weights()


def test_sample_corpus_found_from_another_cwd(tmp_path: Path, monkeypatch):
    """样例集记的相对 corpus 要按「仓库根」找，不能只按 cwd。

    CAS 的 panchen-voice 包写着 "corpus": "archive/dev/..."（相对仓库根）。以前从别的
    目录跑 judge 会报「样例集记的 corpus 目录不存在」，还打印一个假的绝对路径。
    """
    from voicekit.core import release_gate as rg

    repo = tmp_path / "repo"
    corpus = repo / "archive" / "dev" / "x" / "corpus"
    corpus.mkdir(parents=True)
    pack_dir = repo / "skills" / "panchen-voice"
    (pack_dir / "corpus").mkdir(parents=True)  # 包里没有正文，只留空目录占位
    set_obj = {"corpus": "archive/dev/x/corpus"}
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    found, how = rg._corpus({"slug": "panchen-voice", "dir": pack_dir}, set_obj)
    assert found is not None, how
    assert found.resolve() == corpus.resolve(), (found, how)


def test_short_draft_is_refused(tmp_path: Path):
    """<200 汉字的稿子不给判：10 篇样例比 10 个字，钱花了买噪声。"""
    pack = write_exemplar_pack(tmp_path / "pack")
    tiny = tmp_path / "tiny.md"
    tiny.write_text("风从西边来，我站在门口。", encoding="utf-8")
    done = run_cli("judge", str(tiny), "--pack", str(pack), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "太短" in out, out


def test_draft_without_pack_is_rc2(tmp_path: Path):
    """draft 缺包要 rc 2；draft 的 rc 1 只能表示「事实有问题」。"""
    material = tmp_path / "material.md"
    material.write_text("事实一\n", encoding="utf-8")
    done = run_cli("draft", "--material", str(material), "--dry-run", cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "[FAIL]" in out, out


def test_report_missing_file_is_rc2(tmp_path: Path):
    """report 撞到坏文件 rc 2；以前 SystemExit 漏成 rc 1，和「不像」同码。"""
    pack = write_exemplar_pack(tmp_path / "pack")
    done = run_cli("report", str(tmp_path / "nosuch.md"), "--pack", str(pack), cwd=tmp_path)
    out = done.stdout + done.stderr
    assert done.returncode == 2, out
    assert "[FAIL]" in out, out


def test_judge_cache_dir_defaults_to_user_dir(tmp_path: Path, monkeypatch):
    """缓存默认放用户目录：wheel 装在只读的系统目录也能写。"""
    from voicekit.core import judge_lib as jl

    monkeypatch.delenv("VOICEKIT_JUDGE_CACHE", raising=False)
    monkeypatch.delenv("DSH_VOICE_JUDGE_CACHE", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    path = jl.cache_dir()
    assert path == tmp_path / ".cache" / "voicekit" / "judge", path
