"""`say()` 的双写：控制台照常打，同一行另外进 `system_log`。

`say()` 是实机脚本唯一的输出出口——136 个调用点（pirate_loop 80、
scan_coordinates 33、bot_loop 14、backfill_reports 等 9）全走它。所以这一条
覆盖的是整条实机链路，而不只是一个函数。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.tools.scan_coordinates import say


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[Collector]:
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def test_say_still_prints_and_now_also_records(
    collector: Collector, capsys: pytest.CaptureFixture[str]
) -> None:
    """**双写**：控制台那份一个字都不许少。

    库连不上时本机 cmd 窗口与 `var/logs/mission-*.log` 是唯一还看得见的东西，
    把 print 换成入库等于把最后的保底也拆了。
    """
    say("扫到 2:137:1")
    _flush()

    printed = capsys.readouterr().out
    assert "扫到 2:137:1" in printed
    assert [entry.message for entry in collector.records] == ["扫到 2:137:1"]


def test_the_recorded_source_is_the_calling_module(collector: Collector) -> None:
    """`source` 认的是**调用方**，不是 `scan_coordinates` 自己。

    从栈里取而不是让 136 个调用点各自传一个参数：漏掉任何一个，那条日志的
    来源就在说谎，而说谎的日志比没有日志更难排障。
    """
    say("从这个测试模块喊一声")
    _flush()

    assert collector.records[-1].source == "test_say_system_log"


def test_the_recorded_line_has_no_console_timestamp_prefix(collector: Collector) -> None:
    """入库的是原文。时刻是单独一列（而且是 UTC），不该在正文里再来一份本地时刻。"""
    say("原文")
    _flush()

    entry = collector.records[-1]
    assert entry.message == "原文"
    assert entry.logged_at_utc.tzinfo is not None


def test_say_survives_a_sink_that_explodes(capsys: pytest.CaptureFixture[str]) -> None:
    """⚠️ 这是整套里最要紧的一条。

    2026-08-10 的事故就是诊断路径上一行输出把整个 runner 崩在半路，级联成
    整条链路停摆。给 `say()` 加一条入库，等于给那条路径加了一整个新的失败面
    （连不上、超时、库被锁）。这条红了就说明那个失败面又漏出来了。
    """

    class Exploding:
        def emit(self, record: SystemLogRecord) -> None:
            raise RuntimeError("出口炸了")

        def close(self, timeout: float = 0.0) -> None:
            return None

    install_system_log_sink(Exploding(), context=SystemLogContext())  # type: ignore[arg-type]
    try:
        say("这一行必须照常打出来")  # 不抛就是通过
    finally:
        shutdown_system_log_sink()

    assert "这一行必须照常打出来" in capsys.readouterr().out


def test_say_without_a_sink_is_just_a_print(capsys: pytest.CaptureFixture[str]) -> None:
    """手工直跑、没装出口时，行为和从前一模一样。"""
    shutdown_system_log_sink()

    say("没人接")

    assert "没人接" in capsys.readouterr().out
