"""命令行入口的输出不许把进程弄死——包括 `say()` 够不着的那条路。

2026-08-10 的实机事故（记在 `say()` 的注释里）：OCR 从简报上读出个 `™`，
stdout 被调度器重定向到文件、Python 用本地代码页 GBK，`print` 当场抛
`UnicodeEncodeError`；而那一句正在 `_dump_frame` 的诊断路径上，于是一个
「简报认不出、安全地不派这一发」级联成了整条链路停摆。

当时只补了 `say()` 这一处。**同一个教训还有另一条出口：argparse。**
`--help` 和「参数写错了」都直接 `file.write()`，绕开 `say()`；而本仓好几个
命令行的帮助文本里有 `⚠️`（U+26A0），GBK 编不出来。`--help` 打不出来只是难受，
**参数写错那一条要命**：argparse 本来要告诉你错在哪，结果那句话自己崩了，
用户看到的是一段和真实错误毫无关系的编码栈。
"""

from __future__ import annotations

import io
import sys

import pytest

from evo_helper.tools.scan_coordinates import make_console_encoding_safe

#: 本仓帮助文本里真实出现的那个字符。GBK 编不出来。
UNENCODABLE = "⚠️"


def _gbk_stream() -> io.TextIOWrapper:
    """一个和 Windows 中文控制台同编码的流。"""
    return io.TextIOWrapper(io.BytesIO(), encoding="gbk")


def test_a_gbk_stream_really_cannot_take_the_character() -> None:
    """先证明这个坑是真的——否则下面那条测的可能是个不存在的问题。"""
    stream = _gbk_stream()

    with pytest.raises(UnicodeEncodeError):
        stream.write(UNENCODABLE)


def test_the_guard_lets_the_character_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """装上之后写得进去。**不抛异常**才是这条的重点，写成了什么字无所谓。"""
    stream = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    make_console_encoding_safe()
    stream.write(UNENCODABLE)  # 不抛就是过

    assert stream.errors == "replace"


def test_stderr_is_guarded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """**argparse 写的是 stderr**，只护 stdout 等于没护到要命的那一条。"""
    stream = _gbk_stream()
    monkeypatch.setattr(sys, "stderr", stream)

    make_console_encoding_safe()
    stream.write(UNENCODABLE)

    assert stream.errors == "replace"


def test_the_encoding_itself_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """**只换 errors，不换编码。**

    改成 UTF-8 能让 `⚠️` 过去，但会把 GBK 控制台上的所有中文变成乱码——
    而这个仓的日志全是中文。那是拿一个小毛病换一个大毛病。
    """
    stream = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", stream)

    make_console_encoding_safe()

    assert stream.encoding == "gbk"


def test_a_stream_that_cannot_be_reconfigured_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """够不着 `reconfigure` 就什么都不做。**这个函数自己绝不能成为新的崩溃点。**

    实际会遇到：pytest 把 stdout 换成自己的捕获对象、stdout 被重定向成别的类型、
    以及 pythonw 下压根没有 stdout（`sys.stdout is None`）。
    """
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", None)

    make_console_encoding_safe()  # 不抛就是过
