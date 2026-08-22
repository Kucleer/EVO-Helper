"""挂机运行时长的口径。

这一份钉的三条全是「说假话比不说更糟」那一类：

1. **没有观测的那些天必须是「无数据」，不是 0**——写 0 等于说「那天没开机」，
   而心跳是 2026-08-20 才加的，之前的天补不回来。
2. **进程被杀之后挂机时长不许继续涨**——崩溃时没人会写「已停止」。
3. **超过阈值的空档不许被接进同一段**——机器睡了 / tick 卡了那阵不是挂机。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.uptime import (
    HEARTBEAT_INTERVAL_S,
    MAX_HEARTBEAT_GAP_S,
    UptimeSegment,
    due_for_a_beat,
    opens_a_new_segment,
    partially_observed,
    uptime_seconds,
)

DAY = datetime(2026, 8, 20, tzinfo=UTC)
NEXT_DAY = DAY + timedelta(days=1)


def _at(hours: float) -> datetime:
    return DAY + timedelta(hours=hours)


# -- 落拍与另开一段 -------------------------------------------------------------


def test_the_first_beat_of_a_process_always_lands() -> None:
    """上一拍不知道（刚起进程）时必须落一拍，否则第一段永远开不起来。"""
    assert due_for_a_beat(last_beat=None, now=DAY) is True
    assert opens_a_new_segment(last_beat=None, now=DAY) is True


def test_beats_are_throttled_to_the_interval() -> None:
    """⚠️ tick 是**每秒一次**的。不限流就是一天 86,400 次 UPDATE。

    先例是 `record_unrecognised_screen` 那 120 秒（CLAUDE.md：每 tick 可能触发的
    都要限流）。
    """
    last = DAY

    assert due_for_a_beat(last_beat=last, now=last + timedelta(seconds=1)) is False
    assert (
        due_for_a_beat(last_beat=last, now=last + timedelta(seconds=HEARTBEAT_INTERVAL_S)) is True
    )


def test_a_short_stall_keeps_the_same_segment() -> None:
    """一次卡顿（tick 去查库、起子进程，`wait(5)` 都在同一个线程里）不该把一段
    连续的挂机切成碎段。"""
    last = DAY

    assert opens_a_new_segment(last_beat=last, now=last + timedelta(seconds=90)) is False


def test_a_gap_beyond_the_limit_opens_a_new_segment() -> None:
    """⚠️ 超过阈值的空档里，要么进程死了、要么机器睡了。

    接进同一段等于把关着的那阵算成开着——这个数就开始说大话了。
    """
    last = DAY
    beyond = last + timedelta(seconds=MAX_HEARTBEAT_GAP_S + 1)

    assert opens_a_new_segment(last_beat=last, now=beyond) is True


def test_the_gap_limit_is_well_clear_of_the_beat_interval() -> None:
    """阈值必须明显大于一拍的间隔，否则一次抖动就切段；也必须明显小于「重启要
    多久」，否则重启前后会被接成一段。"""
    assert MAX_HEARTBEAT_GAP_S >= 3 * HEARTBEAT_INTERVAL_S


# -- 无观测 vs 0 ---------------------------------------------------------------


def test_a_day_before_the_heartbeat_existed_reports_no_data_instead_of_zero() -> None:
    """⚠️ **这一条是硬要求：心跳之前的那些天必须「无数据」，不许显示 0。**

    显示 0 等于说「那天没开机」，而事实是「那天没人在记」。历史补不回来
    （用户口径 2026-08-20），所以只能照实说不知道。
    """
    observed_since = datetime(2026, 8, 20, 12, tzinfo=UTC)
    earlier_day = (datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 16, tzinfo=UTC))

    assert (
        uptime_seconds(
            (),
            observed_since=observed_since,
            window_start=earlier_day[0],
            window_end=earlier_day[1],
        )
        is None
    )


def test_a_database_without_a_single_beat_reports_no_data_for_every_window() -> None:
    """一拍都没有（刚升级完、还没起过调度器）时，每一档都是「—」。"""
    assert uptime_seconds((), observed_since=None, window_start=DAY, window_end=NEXT_DAY) is None


def test_a_day_that_is_observed_but_idle_really_is_zero() -> None:
    """⚠️ 反过来也要成立：**有观测、但那天调度器一次都没开** = 真的 0。

    这一格和上一格必须分得开，否则「没开机」和「没记录」在页面上长得一样，
    而这个指标存在的全部意义就是分开它们。
    """
    observed_since = datetime(2026, 8, 19, tzinfo=UTC)

    assert (
        uptime_seconds((), observed_since=observed_since, window_start=DAY, window_end=NEXT_DAY)
        == 0.0
    )


# -- 段的累加 -------------------------------------------------------------------


def test_the_segments_in_the_window_are_added_up() -> None:
    """一天里开了两段，加起来。"""
    segments = (
        UptimeSegment(start=_at(1), last_beat=_at(4)),
        UptimeSegment(start=_at(9), last_beat=_at(10.5)),
    )

    assert (
        uptime_seconds(segments, observed_since=_at(1), window_start=DAY, window_end=NEXT_DAY)
        == 4.5 * 3600
    )


def test_a_segment_crossing_midnight_is_split_between_the_two_days() -> None:
    """跨零点那一段两天各算一截，同航线占用（`overlap_seconds`）。"""
    segment = (UptimeSegment(start=_at(22), last_beat=_at(25)),)

    first = uptime_seconds(segment, observed_since=_at(22), window_start=DAY, window_end=NEXT_DAY)
    second = uptime_seconds(
        segment,
        observed_since=_at(22),
        window_start=NEXT_DAY,
        window_end=NEXT_DAY + timedelta(days=1),
    )

    assert (first, second) == (2 * 3600, 1 * 3600)


def test_a_killed_process_stops_the_clock_at_its_last_beat() -> None:
    """⚠️ **进程被杀的情形：挂机时长不许一直涨。**

    崩溃时不会有人写「已停止」。这一段的右端是**最后一拍**，所以无论「现在」
    走到多远，它都只算到那一拍为止——12:00 起、13:00 最后一拍、之后进程被 kill，
    到 23:00 再看仍然是 1 小时。

    换成「起了就一直算到现在」的写法，这里会算出 11 小时。
    """
    killed = (UptimeSegment(start=_at(12), last_beat=_at(13)),)

    assert (
        uptime_seconds(killed, observed_since=_at(12), window_start=DAY, window_end=_at(23))
        == 1 * 3600
    )


def test_a_window_that_starts_before_the_first_beat_is_flagged_as_partial() -> None:
    """「合计」那一档起点固定在 2026-08-17，一定跨在心跳上线之前。

    照样给数，但那个数是**下界**，页面要把这件事说出来。
    """
    observed_since = _at(6)

    assert partially_observed(observed_since=observed_since, window_start=DAY) is True
    assert partially_observed(observed_since=observed_since, window_start=_at(7)) is False
    assert partially_observed(observed_since=None, window_start=DAY) is False
