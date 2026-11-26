"""起稿只认作者包：`draft --pack` 在仓库外的 cwd 也要能跑（装成 wheel 时没有 voices/）。

红先记录（2026-11-27）：旧代码 draft 没有 --pack，且 profile/corpus 是
`Path(__file__).resolve().parents[2] / "voices/..."` 这种开发树常量；装成 wheel 后
`voicekit draft --material 材料.md` 直接 FileNotFoundError: site-packages\\voices\\...。

全部离线：dry-run 不建 LLM 客户端；真跑用注入的假 LLM（不花钱）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: 只给 draft 要的三条属性（style_digest 读 L2-01 / L1-12 / L1-13）。
#: 数字故意和仓库里的 panchen profile 不同：提示词里出现 31 才能证明读的是这个包。
PROFILE = {
    "schema": 1,
    "attributes": [
        {"attribute_id": "L2-01",
         "observed_value": {"mean": 31.0, "median": 27.0, "clauses_per_sentence": 3.5}},
        {"attribute_id": "L1-12", "observed_value": 0.47},
        {"attribute_id": "L1-13", "observed_value": 0.09},
    ],
}
CORPUS_BODY = "他坐在窗边，看风把纸吹得直响，觉得这天下午过得很快。" * 4


def write_pack(dir_path: Path, with_corpus: bool = False) -> Path:
    """一个只发基线（可选带 corpus）的最小作者包。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "profile.json").write_text(
        json.dumps(PROFILE, ensure_ascii=False), encoding="utf-8"
    )
    data = {
        "schema": 1,
        "slug": "demo",
        "author": "示例作者",
        "profile": {"path": "profile.json"},
    }
    if with_corpus:
        corpus = dir_path / "corpus"
        corpus.mkdir()
        (corpus / "001__doc.md").write_text(
            "第一篇\n" + CORPUS_BODY + "\n", encoding="utf-8"
        )
        data["corpus"] = {"path": "corpus"}
    (dir_path / "pack.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return dir_path


def write_material(dir_path: Path) -> Path:
    path = dir_path / "material.md"
    path.write_text("2026年11月8日，他在店里向员工道歉。\n", encoding="utf-8")
    return path


def run_cli(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """cwd 在仓库外；只把仓库放进 PYTHONPATH，模拟「包已装好、开发树不在」。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    env.pop("VOICEKIT_PACK", None)
    return subprocess.run(
        [sys.executable, "-m", "voicekit", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
        timeout=180,
    )


def test_draft_dry_run_works_with_baseline_only_pack(tmp_path: Path):
    """只发基线、没有 corpus 的包：dry-run rc=0，提示词里是这个包的 profile 数字。"""
    pack = write_pack(tmp_path / "pack")
    material = write_material(tmp_path)
    done = run_cli(
        "draft", "--material", str(material), "--pack", str(pack), "--dry-run",
        cwd=tmp_path,
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "Traceback" not in out
    assert "句子平均 31 字" in out, out[-2000:]
    assert "他现在的腔" in out


def test_draft_dry_run_with_corpus_uses_pack_corpus(tmp_path: Path):
    """带 corpus 的包：提示词里出现这个包的语料开头；缺 corpus 时该节为空、不报错。"""
    pack = write_pack(tmp_path / "pack", with_corpus=True)
    material = write_material(tmp_path)
    done = run_cli(
        "draft", "--material", str(material), "--pack", str(pack), "--dry-run",
        cwd=tmp_path,
    )
    out = done.stdout + done.stderr
    assert done.returncode == 0, out
    assert "他坐在窗边" in out, out[-2000:]
    assert "（001）" in out
    assert "句子平均 31 字" in out


def test_draft_material_must_be_a_file(tmp_path: Path):
    """`--material` 收的是文件路径；直接给一段话要给人话错误，不能抛 OSError。"""
    pack = write_pack(tmp_path / "pack")
    done = run_cli(
        "draft", "--material", "<一句话材料>", "--pack", str(pack), "--dry-run",
        cwd=tmp_path,
    )
    out = done.stdout + done.stderr
    assert done.returncode != 0, out
    assert "材料文件" in out, out
    assert "Traceback" not in out


def test_draft_real_run_with_fake_llm_writes_body(tmp_path: Path, monkeypatch):
    """真跑路径（假 LLM）：rc=0，输出文件有正文；只发基线的包也不许因为缺 corpus 退出。"""
    from voicekit.core import draft as draft_mod
    from voicekit.core import rewrite

    pack = write_pack(tmp_path / "pack")
    material = write_material(tmp_path)
    out_path = tmp_path / "out.md"
    body = "2026年11月8日，他在店里向员工道歉。"
    monkeypatch.setattr(rewrite, "_llm_callable", lambda *a, **kw: (lambda messages: body))

    rc = draft_mod.main([
        "--file", str(material), "--pack", str(pack), "--out", str(out_path),
    ])
    assert rc == 0
    assert out_path.is_file()
    assert out_path.read_text(encoding="utf-8").strip() == body
