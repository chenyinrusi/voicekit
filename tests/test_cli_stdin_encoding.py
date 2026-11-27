"""stdin 编码：UTF-8 与 Windows 中文控制台 GBK 两种管道都要能读。"""

import io
import sys

from voicekit import cli


class _FakeStdin:
    def __init__(self, raw: bytes) -> None:
        self.buffer = io.BytesIO(raw)


def _read_via_stdin(monkeypatch, raw: bytes) -> str:
    monkeypatch.setattr(sys, "stdin", _FakeStdin(raw))
    return cli._read_text("-")


def test_stdin_utf8(monkeypatch):
    text = "昨晚下了雨。早上院子里有股土味。"
    assert _read_via_stdin(monkeypatch, text.encode("utf-8")) == text


def test_stdin_utf8_with_bom(monkeypatch):
    text = "第一段。\n第二段。"
    assert _read_via_stdin(monkeypatch, b"\xef\xbb\xbf" + text.encode("utf-8")) == text


def test_stdin_gbk(monkeypatch):
    """`cmd /c chcp 936 & type x | voicekit quick -` 进来的是 GBK 字节。"""
    text = "昨晚下了雨。早上院子里有股土味。"
    assert _read_via_stdin(monkeypatch, text.encode("gbk")) == text


def test_stdin_garbage_is_replaced_not_raised(monkeypatch):
    assert _read_via_stdin(monkeypatch, b"\xff\xfe\x00\x81") is not None
