"""`quick`（无基线快检）与句级证据（`lint --annotate` / report）的测试。

两件事的契约：
- `quick` 不需要作者包/作者 profile；只报特征，不判好坏、不给分数、不放行。
- 证据只能指到「句子自己身上的证据」；全篇统计类属性必须说「无句级证据」，不许编位置。
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

#: 够长（>200 汉字）的稿子：里面有碎片句、文言标记、问句、连接词。
DRAFT = (
    "海风把窗子吹得直响。我坐着没动。好。\n\n"
    "他说，这样写其实也没什么，不过是一点念想而已。你不觉得吗？"
    "我们把桌子搬到窗边，光就从纸上滑过去。那时候我以为，日子会一直这样慢下去。"
    "后来才知道，慢的只是我自己的脚步，别的都在往前跑。"
    "母亲在厨房里切菜，刀落在砧板上，一声一声，很稳。"
    "她偶尔抬头看我一眼，什么也不说，又低下去。"
    "我想把这一刻记住，可写下来就散了。于是我就坐着，听风，听刀声，听自己心里那点没说出口的话。"
    "窗外有孩子在跑，笑声一阵一阵地撞进来。原来安静不是没有声音，是声音都退到远处去了。"
    "那年冬天很长，长到我都忘了春天该是什么样子。"
    "再后来，我离开家，去了很远的城市，租一间朝北的小屋，夜里写作，白天睡觉。"
    "有一天我接到母亲的电话，她只说了一句话：饭要按时吃。我站在阳台上，忽然就哭了。"
)


def run_cli(*args: str, cwd: Path | None = None, stdin_text: str | None = None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONPATH=str(ROOT))
    env.pop("VOICEKIT_PACK", None)
    return subprocess.run(
        [sys.executable, "-m", "voicekit", *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(cwd or ROOT),
        env=env,
        timeout=180,
    )


def write_draft(path: Path, text: str = DRAFT) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def write_tiny_pack(dir_path: Path) -> Path:
    """最小作者包：4 条核心轴，L1-12 的容差钉成 0，好让带「而已」的稿子必然不过。"""
    from voicekit.core import voice_lib

    dir_path.mkdir(parents=True, exist_ok=True)
    raw = voice_lib.measure(DRAFT)
    half = {"rel": 1.0, "floor": 1.0}
    attrs = [
        {
            "attribute_id": "L2-01",
            "attribute_name": "句长与分句密度",
            "kind": "dict",
            "observed_value": raw["L2-01"],
            "tolerance": {"per_key": {k: dict(half) for k in raw["L2-01"]}},
        },
        {
            "attribute_id": "L3-05",
            "attribute_name": "句读与停顿节奏",
            "kind": "dict",
            "observed_value": raw["L3-05"],
            "tolerance": {"per_key": {k: dict(half) for k in raw["L3-05"]}},
        },
        {
            "attribute_id": "L7-02",
            "attribute_name": "缓和与增强",
            "kind": "dict",
            "observed_value": raw["L7-02"],
            "tolerance": {"per_key": {k: dict(half) for k in raw["L7-02"]}},
        },
        {
            "attribute_id": "L1-12",
            "attribute_name": "文白夹杂",
            "kind": "scalar",
            "observed_value": 0.0,
            "tolerance": {"rel": 1.0, "floor": 0.0},
        },
    ]
    (dir_path / "profile.json").write_text(
        json.dumps({"attributes": attrs}, ensure_ascii=False), encoding="utf-8"
    )
    pack = {"schema": 1, "slug": "tiny", "author": "示例", "profile": {"path": "profile.json"}}
    (dir_path / "pack.json").write_text(json.dumps(pack, ensure_ascii=False), encoding="utf-8")
    return dir_path


# ---------------------------------------------------------------------------
# quick：没有作者包也要能出结果
# ---------------------------------------------------------------------------


def test_quick_runs_without_pack(tmp_path: Path):
    draft = write_draft(tmp_path / "draft.md")
    done = run_cli("quick", str(draft), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "无作者基线" in done.stdout
    assert "汉字" in done.stdout


def test_quick_never_judges(tmp_path: Path):
    """quick 只报特征：不许出现分数、通过/不通过、像不像他。"""
    draft = write_draft(tmp_path / "draft.md")
    done = run_cli("quick", str(draft), cwd=tmp_path)
    assert done.returncode == 0
    for word in ("/ 100", "通过", "不通过", "像不像他：", "放行"):
        assert word not in done.stdout, f"quick 输出里不该有判定词：{word}"


def test_quick_stdin_same_numbers_as_file(tmp_path: Path):
    """Windows 管道默认 cp1252；`-` 必须按 UTF-8 字节读，数字要和读文件一致。"""
    draft = write_draft(tmp_path / "draft.md")
    by_file = json.loads(run_cli("quick", str(draft), "--json", cwd=tmp_path).stdout)
    by_stdin = run_cli("quick", "-", "--json", cwd=tmp_path, stdin_text=DRAFT)
    assert by_stdin.returncode == 0, by_stdin.stderr
    assert json.loads(by_stdin.stdout)["measurements"] == by_file["measurements"]


def test_quick_json_declares_no_baseline(tmp_path: Path):
    draft = write_draft(tmp_path / "draft.md")
    data = json.loads(run_cli("quick", str(draft), "--json", cwd=tmp_path).stdout)
    assert data["no_author_baseline"] is True
    assert data["diagnostic_only"] is True
    assert {"cjk_chars", "paragraphs", "sentences"} <= set(data["measurements"])


@pytest.mark.parametrize("case", ["missing", "empty", "english"])
def test_quick_input_errors_exit_2(tmp_path: Path, case: str):
    path = tmp_path / "draft.md"
    if case == "empty":
        write_draft(path, "   \n\n")
    elif case == "english":
        write_draft(path, "hello world. this is not chinese text.\n")
    done = run_cli("quick", str(path), cwd=tmp_path)
    assert done.returncode == 2, done.stdout + done.stderr


def test_quick_short_text_warns_but_still_prints(tmp_path: Path):
    path = write_draft(tmp_path / "d.md", "今天下午我去了一趟海边，风很大，浪也高。\n")
    done = run_cli("quick", str(path), cwd=tmp_path)
    assert done.returncode == 0
    assert "样本偏小" in done.stdout
    assert "汉字" in done.stdout and "平均句长" in done.stdout, "提醒归提醒，数字照出"


def test_quick_warns_on_record_like_file(tmp_path: Path):
    """标题/表格/列表占满的文件，要说明它不全是正文，但仍然给数（rc=0）。"""
    path = write_draft(
        tmp_path / "rec.md",
        "# 标题\n\n| 项目 | 值 |\n|---|---|\n| 一 | 二 |\n\n## 小标题\n\n- 列表一\n- 列表二\n",
    )
    done = run_cli("quick", str(path), cwd=tmp_path)
    assert done.returncode == 0
    assert "不是一篇正文" in done.stdout


def test_quick_json_has_no_score_or_verdict(tmp_path: Path):
    """JSON 顶层只许有实测字段，不许混进分数/结论/放行。"""
    draft = write_draft(tmp_path / "draft.md")
    data = json.loads(run_cli("quick", str(draft), "--json", cwd=tmp_path).stdout)
    assert set(data) == {"path", "no_author_baseline", "diagnostic_only", "measurements", "warnings"}
    flat = json.dumps(data, ensure_ascii=False)
    for word in ("score", "verdict", "pass", "like"):
        assert word not in flat, f"quick 的 JSON 里不该有 {word}"


# ---------------------------------------------------------------------------
# 证据：指到句，或者明说没有
# ---------------------------------------------------------------------------


def test_spots_point_at_fragment_sentence():
    from voicekit.core import evidence

    found = evidence.spots(DRAFT, "L2-08", limit=3)
    assert found, "稿子里有「好。」这种碎片句，必须给位置"
    for spot in found:
        assert spot["para"] >= 1 and spot["sent"] >= 1
        assert spot["label"], "句子特征要写在 label 里"
        assert spot["quote"] and spot["quote"].strip("…") in DRAFT


def test_spots_hit_is_inside_quote_for_all_token_attributes():
    from voicekit.core import evidence

    for aid in evidence.TOKENS:
        for spot in evidence.spots(DRAFT, aid):
            assert spot["hit"] in spot["quote"], f"{aid} 的证据没包住命中词"


def test_quote_window_keeps_hit_in_long_sentence():
    """长句里命中词在很后面时，截窗必须围着它裁，不能截成只剩句首。"""
    from voicekit.core import evidence

    long_sentence = (
        "我在这个城市里住了很久很久，久到已经记不清搬过几次家，"
        "也记不清楼下的店铺换过几轮主人，只是每天照旧走过去而已。"
    )
    found = evidence.spots(long_sentence, "L1-12")
    assert found, "长句里有「而已」，必须给位置"
    spot = found[0]
    assert spot["hit"] in spot["quote"], f"截窗把命中词切掉了：{spot['quote']}"
    assert spot["quote"].startswith("…"), "前面还有内容，必须用 … 标出截断"
    assert len(spot["quote"]) <= evidence.QUOTE_WIDTH + 2


def test_document_level_attributes_have_no_evidence():
    """词汇多样性/签名 n-gram 是全篇统计，硬指一句就是编证据。"""
    from voicekit.core import evidence

    for aid in ("L1-11", "L1-08", "L1-10", "L2-03", "L2-04", "L1-09"):
        assert evidence.spots(DRAFT, aid) == []


def test_spot_paragraph_numbers_follow_cleaned_body():
    """measure() 先剥平台噪声（含重复标题）；证据的段号必须用同一份正文，否则整体错位。"""
    from voicekit.core import evidence

    text = "标题\n\n标题\n\n其实正文一段。\n\n正文二段。\n"
    found = evidence.spots(text, "L4-05")
    assert found and found[0]["para"] == 2, f"段号应指向清洗后的第 2 段，实际 {found}"


#: 平台注入很多的稿子：3 行头（Title/URL/本文首发于）+ 重复标题 + 行内平台短语。
NOISY = (
    "Title: 平台标题\n"
    "URL Source: https://example.com/x\n"
    "本文首发于某某公众号\n"
    "同一行标题\n\n"
    "同一行标题\n\n"
    "第一段第一句。第二句这里有个问号吗？第三句。\n"
    "第二段（图源网络）只有一句。\n"
)


def test_spot_paragraph_numbers_skip_platform_noise():
    """平台注入行与重复标题不占段号：不剥的话 ¶ 会整体后移 4 段。"""
    from voicekit.core import evidence, voice_lib

    doc = voice_lib.measure(NOISY)["_doc"]
    found = evidence.spots(NOISY, "L2-05")
    assert found, "问句应被找到"
    spot = found[0]
    assert (spot["para"], spot["sent"]) == (2, 2), f"应指向清洗后 ¶2 句2，实际 {spot}"
    assert "问号" in spot["quote"] and spot["hit"] == "？", spot
    assert doc["paragraphs"] == 3, f"measure 报的段数应是 3，实际 {doc['paragraphs']}"
    assert evidence.spots(NOISY, "L1-01") == [], "全篇统计量（标点谱）不许给位置"


def test_spot_numbers_stable_across_line_endings():
    from voicekit.core import evidence

    lf = DRAFT
    crlf = DRAFT.replace("\n", "\r\n")
    bom = "\ufeff" + DRAFT
    picks = lambda text: [  # noqa: E731
        (s["para"], s["sent"], s["hit"]) for s in evidence.spots(text, "L4-05")
    ]
    assert picks(lf) == picks(crlf) == picks(bom)


# ---------------------------------------------------------------------------
# lint --annotate 与 report
# ---------------------------------------------------------------------------


def test_lint_annotate_prints_location(tmp_path: Path):
    draft = write_draft(tmp_path / "draft.md")
    pack = write_tiny_pack(tmp_path / "pack")
    done = run_cli("lint", str(draft), "--pack", str(pack), "--annotate", cwd=tmp_path)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "句级证据" in done.stdout
    assert "¶" in done.stdout


def test_lint_annotate_json_stays_parseable(tmp_path: Path):
    draft = write_draft(tmp_path / "draft.md")
    pack = write_tiny_pack(tmp_path / "pack")
    done = run_cli("lint", str(draft), "--pack", str(pack), "--annotate", "--json", cwd=tmp_path)
    data = json.loads(done.stdout)  # 混进非 JSON 文本就会在这里炸
    failed = [r for r in data["rows"] if not r["ok"]]
    assert failed, "L1-12 的容差是 0，必须有一条不过"
    assert any(r["evidence"] for r in failed), "不过的属性要么给位置，要么明说没有"
    assert all("evidence_note" in r for r in failed)


def test_lint_annotate_json_touches_only_failing_rows(tmp_path: Path):
    """证据只挂在不过的行上；过的行不许被塞 `evidence`。"""
    draft = write_draft(tmp_path / "draft.md")
    pack = write_tiny_pack(tmp_path / "pack")
    done = run_cli("lint", str(draft), "--pack", str(pack), "--annotate", "--json", cwd=tmp_path)
    data = json.loads(done.stdout)
    passed = [r for r in data["rows"] if r["ok"]]
    failed = [r for r in data["rows"] if not r["ok"]]
    assert passed and failed, "这是「一半过一半不过」的稿子，两种行都要有"
    assert all("evidence" not in r and "evidence_note" not in r for r in passed)
    assert all("evidence_note" in r for r in failed)


def test_missing_pack_exits_2_not_1(tmp_path: Path):
    """没作者包是环境问题：rc 必须是 2。rc 1 会被脚本读成「不像他」。"""
    draft = write_draft(tmp_path / "draft.md")
    for cmd in ("lint", "report", "judge"):
        done = run_cli(cmd, str(draft), cwd=tmp_path)  # tmp_path 下没有 voices/
        assert done.returncode == 2, f"{cmd} 没包时 rc={done.returncode}：{done.stderr}"
        assert "no voice pack" in done.stderr or "[FAIL]" in done.stderr


def test_report_shows_evidence_line(tmp_path: Path):
    from voicekit import report
    from voicekit.core import voice_lib

    draft = write_draft(tmp_path / "draft.md")
    pack = write_tiny_pack(tmp_path / "pack")
    # 加一条「读者称呼」：基线翻倍、容差 0，必定不过；稿子里有「我/你」，能指到句
    raw = voice_lib.measure(DRAFT)["L7-05"]
    profile = json.loads((pack / "profile.json").read_text(encoding="utf-8"))
    profile["attributes"].append(
        {
            "attribute_id": "L7-05",
            "attribute_name": "读者称呼",
            "kind": "scalar",
            "observed_value": float(raw) * 2,
            "tolerance": {"rel": 0.0, "floor": 0.0},
        }
    )
    (pack / "profile.json").write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")

    page = Path(report.render(draft, pack, tmp_path / "out.html", "测试"))
    html = page.read_text(encoding="utf-8")
    assert "句级证据（只标位置，不判好坏）：" in html, "指到句时要说清只标位置"
    assert "¶" in html and "处：" in html, "要给段句位置，并写清全文共几处"
    assert "句级证据：无句级证据" not in html, "别把「没有证据」写成一句结巴"


def test_doc_level_row_says_no_evidence():
    from voicekit.core import evidence

    rows = [{"attribute_id": "L2-01", "ok": False}]
    evidence.attach(rows, DRAFT)
    assert rows[0]["evidence"] == []
    assert rows[0]["evidence_note"] == "无句级证据（全文统计）"


def test_evidence_note_counts_whole_document():
    """低偏差时要能看出「全文就这几处」，不能只甩两句让人以为那就是全部。"""
    from voicekit.core import evidence

    rows = [{"attribute_id": "L2-08", "ok": False}]
    evidence.attach(rows, DRAFT)
    note = rows[0]["evidence_note"]
    total = len(evidence.spots(DRAFT, "L2-08", limit=999))
    assert total >= 1
    assert f"共 {total} 处" in note or f"全文 {total} 处" in note


def test_report_no_pack_flag_is_gone(tmp_path: Path):
    """`--no-pack` 从来没成功过（默认 profile 不在 wheel 里）；删掉它，别留假选项。"""
    draft = write_draft(tmp_path / "draft.md")
    done = run_cli("report", str(draft), "--no-pack", cwd=tmp_path)
    assert done.returncode == 2
    assert "no-pack" in done.stderr
