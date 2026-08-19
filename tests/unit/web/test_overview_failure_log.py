"""数据概览页查询失败时的留痕。

两条硬要求同时压在这一小段上：

1. **新功能必须带够用的日志**（CLAUDE.md）：出事时能只靠库里的日志定位。
2. **每 tick 可能触发的要限流**（同）：这一页每 5 秒轮询一次，库一断就是每 5 秒
   一条。PR #188 修过一次同形状的事故——当时两条日志占了 `system_log` 全表的 44%。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.web.overview_routes import FAILURE_LOG_INTERVAL_S, _FailureLog

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)


@pytest.fixture
def written() -> Iterator[list[SystemLogRecord]]:
    """装一个把记录攒在内存里的出口。

    ⚠️ **没装出口时 `record_system_log` 是空操作**——不装的话这一份会全绿，
    而什么都没守住。
    """
    records: list[SystemLogRecord] = []
    install_system_log_sink(
        SystemLogSink(records.extend, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield records
    finally:
        shutdown_system_log_sink()


def _flush(records: list[SystemLogRecord]) -> list[SystemLogRecord]:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)
    return records


def test_the_first_failure_is_written(written: list[SystemLogRecord]) -> None:
    _FailureLog().failed(where="此刻", error=RuntimeError("库断了"), now=NOW)

    records = _flush(written)

    assert len(records) == 1
    assert records[0].level == "ERROR"
    assert "此刻" in records[0].message


def test_the_payload_carries_enough_to_locate_the_failure(written: list[SystemLogRecord]) -> None:
    """判据不是「有没有打日志」，而是**出事时能不能只靠库里的日志定位**。

    所以要留下：哪一块查的、报了什么、以及**当时用的那个时刻**——这一页的
    UTC 日切、航线占用、「最早空出」全都压在那个时刻上。
    """
    _FailureLog().failed(where="周期统计", error=ValueError("relation 不存在"), now=NOW)

    payload = _flush(written)[0].payload_json

    assert "周期统计" in payload
    assert "relation 不存在" in payload
    assert NOW.isoformat() in payload


def test_a_storm_of_failures_writes_one_line_not_one_per_poll(
    written: list[SystemLogRecord],
) -> None:
    """⚠️ 这条守的就是「日志把表刷爆」那件事。

    5 秒一轮、连续失败一分钟 = 12 次调用，只该留下 **1** 行。
    """
    log = _FailureLog()
    for tick in range(12):
        log.failed(
            where="此刻", error=RuntimeError("库断了"), now=NOW + timedelta(seconds=5 * tick)
        )

    assert len(_flush(written)) == 1


def test_a_long_outage_still_gets_a_heartbeat(written: list[SystemLogRecord]) -> None:
    """限流不等于沉默：过了窗口要再写一条，否则一场持续两小时的故障在库里
    只有开头那一行，看不出它还在继续。
    """
    log = _FailureLog()
    log.failed(where="此刻", error=RuntimeError("库断了"), now=NOW)
    log.failed(
        where="此刻",
        error=RuntimeError("库断了"),
        now=NOW + timedelta(seconds=FAILURE_LOG_INTERVAL_S + 1),
    )

    assert len(_flush(written)) == 2


def test_recovery_is_written_once(written: list[SystemLogRecord]) -> None:
    """排障的人要看得出这段红是什么时候开始、什么时候结束的。"""
    log = _FailureLog()
    log.failed(where="此刻", error=RuntimeError("库断了"), now=NOW)
    log.recovered(now=NOW + timedelta(seconds=10))
    log.recovered(now=NOW + timedelta(seconds=20))

    records = _flush(written)

    assert [record.level for record in records] == ["ERROR", "INFO"]
    assert "恢复正常" in records[1].message


def test_nothing_is_written_while_everything_works(written: list[SystemLogRecord]) -> None:
    """正常的每一轮都写一条「一切正常」，等于把真正有信息量的那几行淹掉。"""
    log = _FailureLog()
    for tick in range(20):
        log.recovered(now=NOW + timedelta(seconds=5 * tick))

    assert _flush(written) == []


def test_a_new_outage_after_a_recovery_is_written_again(written: list[SystemLogRecord]) -> None:
    """账要翻篇：好了又坏，那是一次新的故障，不是上一次的重复。"""
    log = _FailureLog()
    log.failed(where="此刻", error=RuntimeError("第一次"), now=NOW)
    log.recovered(now=NOW + timedelta(seconds=10))
    log.failed(where="此刻", error=RuntimeError("第二次"), now=NOW + timedelta(seconds=20))

    records = _flush(written)

    assert [record.level for record in records] == ["ERROR", "INFO", "ERROR"]
