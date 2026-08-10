"""`say()` 永远不许把进程弄死——一次实机事故的守卫。

事故（2026-08-10，首次真派遣）：OCR 从简报上读出来的字里带了个 `™`，而调度器
把 runner 的 stdout 重定向到文件、Python 用本地代码页 GBK，`print` 当场抛
`UnicodeEncodeError`。

要命的是这一句正在 `_dump_frame` 的**诊断路径**上：本来是「简报认不出，安全地
不派这一发」这种可恢复的判定失败，结果变成整个 runner 崩在半路、游戏被留在一个
开着的面板上；接着填空隙的扫描也认不出画面、连挂三次被自动停用。一个判定失败
级联成了整条链路停摆。

OCR 的输出本来就什么字符都可能有，所以这一句必须永不抛。
"""

from __future__ import annotations

import io

import pytest

from evo_helper.tools.scan_coordinates import say


class _GbkStream(io.TextIOWrapper):
    """一个 GBK 且 errors='strict' 的流，模拟重定向到文件时的 stdout。"""

    def __init__(self) -> None:
        super().__init__(io.BytesIO(), encoding="gbk", errors="strict")


@pytest.mark.parametrize(
    "message",
    [
        "简报写的是 ™（读不出），不是侦察",  # 事故里真实出现的那个字符
        "度数 45° 偏差",
        "emoji 🚀 也不许炸",
        "普通中文没有问题",
    ],
)
def test_say_never_raises_on_unencodable_characters(
    message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = _GbkStream()
    monkeypatch.setattr("sys.stdout", stream)
    say(message)  # 不抛就算过
    stream.flush()


def test_say_still_writes_something(monkeypatch: pytest.MonkeyPatch) -> None:
    """兜底不能变成「什么都不打」——诊断信息还是要留下来。"""
    stream = _GbkStream()
    monkeypatch.setattr("sys.stdout", stream)
    say("简报写的是 ™（读不出）")
    stream.flush()
    written = stream.buffer.getvalue().decode("gbk", errors="replace")  # type: ignore[attr-defined]
    assert "简报写的是" in written
    assert "读不出" in written


def test_the_encodable_part_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有编不出来的那个字符被替换，其余原样保留。

    没有这条，「兜底」可能退化成把整行换成一句无用的占位文字，
    而这一行正是事后复盘唯一的线索。
    """
    stream = _GbkStream()
    monkeypatch.setattr("sys.stdout", stream)
    say("坐标 2:137:4 判定 ™ 完成")
    stream.flush()
    written = stream.buffer.getvalue().decode("gbk", errors="replace")  # type: ignore[attr-defined]
    assert "2:137:4" in written
    assert "判定" in written
    assert "完成" in written
