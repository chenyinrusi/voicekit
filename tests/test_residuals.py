"""0.1.11 复审遗留项的回归测试：重试可见、缓存写坏不丢钱、花费台账、包可携带、help 说清 rc。

先红后绿：这些测试在 0.1.10 上失败，修完应为绿。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_smoke import (  # noqa: E402
    _FakeJudgeLLM,
    run_cli,
    write_draft,
    write_exemplar_pack,
)


def test_retry_is_announced_and_counted(tmp_path: Path, monkeypatch, capsys):
    """第一次输出不能用 → 必须明说再判一次，并把尝试次数带回来。"""
    from voicekit.core import exemplar_judge as ej

    monkeypatch.setenv("VOICEKIT_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_exemplar_pack(tmp_path / "pack")
    set_obj = json.loads((pack / "exemplar_baseline.json").read_text(encoding="utf-8"))
    draft = write_draft(tmp_path).read_text(encoding="utf-8")

    class _Flaky(_FakeJudgeLLM):
        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return type("R", (), {"content": "不像话的一段散文", "usage": self.usage})()
            return type("R", (), {"content": self.payload, "usage": dict(self.usage)})()

    fake = _Flaky('{"verdict": "same", "reason": "笔性像"}')
    import asyncio

    payload = asyncio.run(
        ej.judge_text(draft, set_obj, fake, "deepseek-chat", corpus=pack / "corpus")
    )
    err = capsys.readouterr().err
    assert fake.calls == 2
    assert payload["attempts"] == 2, payload
    assert "[retry 1/1]" in err, err


def test_cache_write_failure_keeps_paid_verdict(tmp_path: Path, monkeypatch, capsys):
    """缓存目录写不进去（只读安装）：判定照常返回，只警告，不许把花钱的结论丢掉。"""
    from voicekit.core import exemplar_judge as ej
    from voicekit.core import judge_lib as jl

    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(jl, "cache_dir", lambda: blocker / "judge")
    pack = write_exemplar_pack(tmp_path / "pack")
    set_obj = json.loads((pack / "exemplar_baseline.json").read_text(encoding="utf-8"))
    draft = write_draft(tmp_path).read_text(encoding="utf-8")
    fake = _FakeJudgeLLM('{"verdict": "same", "reason": "笔性像"}')
    import asyncio

    payload = asyncio.run(
        ej.judge_text(draft, set_obj, fake, "deepseek-chat", corpus=pack / "corpus")
    )
    assert payload["verdict"] == "same"
    assert "判定缓存写不进去" in capsys.readouterr().err


def test_usage_log_appends_one_jsonl_row(tmp_path: Path, monkeypatch):
    """每次真调模型追一行；显式空串 = 关掉。"""
    from voicekit import usage_log

    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("VOICEKIT_USAGE_LOG", str(log))
    path = usage_log.record({"command": "judge", "model": "deepseek-chat", "cost_usd": 0.1})
    assert path == log
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["command"] == "judge"
    assert row["ts"]  # 时间戳

    monkeypatch.setenv("VOICEKIT_USAGE_LOG", "")
    assert usage_log.record({"command": "judge"}) is None


def test_judge_run_writes_usage_ledger(tmp_path: Path, monkeypatch):
    """判同人真跑（假 LLM）→ 台账里能看到这次用量的钱。"""
    from voicekit.core import release_gate

    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("VOICEKIT_USAGE_LOG", str(log))
    monkeypatch.setenv("VOICEKIT_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    fake = _FakeJudgeLLM('{"verdict": "same", "reason": "笔性像"}')
    rc = release_gate.run(draft, pack, llm_factory=lambda provider, model: fake, echo=lambda _l: None)
    assert rc == 0
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["command"] == "judge"
    assert row["prompt_tokens"] == 1000
    assert abs(row["cost_usd"] - 0.000168) < 1e-9


def test_judge_failure_still_logs_spent_tokens(tmp_path: Path, monkeypatch):
    """输出一直不能用 → rc 2，但花掉的 token 必须进台账（不能白花）。"""
    from voicekit.core import release_gate

    log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("VOICEKIT_USAGE_LOG", str(log))
    monkeypatch.setenv("VOICEKIT_JUDGE_CACHE", str(tmp_path / "cache"))
    pack = write_exemplar_pack(tmp_path / "pack")
    draft = write_draft(tmp_path)
    fake = _FakeJudgeLLM("这不是 JSON")
    lines: list[str] = []
    rc = release_gate.run(
        draft, pack, llm_factory=lambda provider, model: fake, echo=lines.append
    )
    assert rc == 2, "\n".join(lines)
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert rows and rows[0]["cost_usd"] and rows[0]["cost_usd"] > 0


def test_pack_rejects_absolute_corpus(tmp_path: Path):
    """包里的 corpus 必须相对包目录：绝对路径换机器就断。"""
    from voicekit.core import pack_lib as pk

    pack = write_exemplar_pack(tmp_path / "pack")
    data = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
    data["corpus"] = {"path": str((pack / "corpus").resolve())}
    (pack / "pack.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(pk.PackError, match="相对"):
        pk.load_pack(pack)


def test_reset_pack_clears_active_pack(tmp_path: Path):
    """钉住的包要能显式松手：同一进程里换包/测试不串。"""
    from voicekit import paths

    pack = write_exemplar_pack(tmp_path / "pack")
    paths.use_pack(pack)
    assert paths.maybe_active_pack() is not None
    paths.reset_pack()
    assert paths.maybe_active_pack() is None


def test_help_prints_rc_table_and_env(tmp_path: Path):
    """外行看 --help 就要知道 0/1/2 各是什么意思、钱和缓存受哪些环境变量管。"""
    done = run_cli("--help")
    out = done.stdout
    assert done.returncode == 0
    for token in ("0", "1", "2", "rc", "VOICEKIT_PACK", "VOICEKIT_JUDGE_CACHE", "VOICEKIT_USAGE_LOG"):
        assert token in out, out


def test_judge_help_shows_retry_and_cache_switches(tmp_path: Path):
    done = run_cli("judge", "--help")
    out = done.stdout
    assert done.returncode == 0
    assert "--retries" in out
    assert "--no-cache" in out


def test_draft_help_does_not_show_main_py(tmp_path: Path):
    """内部 parser 没写 prog 时，--help 会露出 `__main__.py`：用户读不懂。"""
    done = run_cli("draft", "--help")
    out = done.stdout
    assert done.returncode == 0
    assert "__main__.py" not in out, out
    assert "voicekit draft" in out
