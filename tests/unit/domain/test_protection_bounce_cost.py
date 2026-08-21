"""白占了多少航线时间：`domain.protection_bounce`。

这个数是那条日志里最贵的一项——没有它，一行「撞保护期」读起来和一次三十几秒的
跳过没有区别，而真相是一整趟往返（生产实测 48.9 与 124.1 分钟）。

时间全部注入，一次真实时钟都不读。
"""

from __future__ import annotations

from datetime import UTC, datetime

from evo_helper.domain.battle_outcome import OUTCOME_LABELS, OUTCOME_PROTECTED
from evo_helper.domain.protection_bounce import wasted_line_minutes, wasted_line_seconds

DISPATCHED = datetime(2026, 8, 20, 13, 27, 26, tzinfo=UTC)


def test_protected_is_not_a_banner_word() -> None:
    """⚠️ `OUTCOME_LABELS` 是 **OCR 吸附词表**，不是「所有战果取值」的清单。

    `vision.pirate_reports.parse_outcome` 拿它对横幅读数做编辑距离吸附。把一个
    屏幕上根本不存在的词放进去，只是给横幅的噪声多一个可以吸附过去的靶子——
    而吸附成功的后果是一份真战报的胜负被写成 `PROTECTED`。
    这一档由 `vision.protection_bounce` 那条链路直接写死，一次 OCR 都不经过。
    """
    assert OUTCOME_PROTECTED not in OUTCOME_LABELS
    assert OUTCOME_LABELS == ("VICTORY", "FAIL", "DRAW")


def test_the_booked_line_time_wins_when_the_ledger_has_it() -> None:
    """`line_free_at_utc − dispatched_at_utc` 是调度器自己订下的占线时长。"""
    seconds = wasted_line_seconds(
        dispatched_at_utc=DISPATCHED,
        line_free_at_utc=DISPATCHED.replace(hour=15, minute=31, second=36),
        flight_seconds=3724,
    )

    assert seconds == 7450.0


def test_it_falls_back_to_twice_the_one_way_flight() -> None:
    """往返 = 单程 × 2。这一档在 `line_free_at_utc` 没记上时兜底。"""
    assert (
        wasted_line_seconds(
            dispatched_at_utc=DISPATCHED, line_free_at_utc=None, flight_seconds=1467
        )
        == 2934.0
    )


def test_a_line_free_moment_that_precedes_the_launch_is_ignored() -> None:
    """占线到派出之前是不可能的读数；退回单程 × 2，而不是交出零或负数。"""
    assert (
        wasted_line_seconds(
            dispatched_at_utc=DISPATCHED,
            line_free_at_utc=DISPATCHED,
            flight_seconds=1467,
        )
        == 2934.0
    )


def test_nothing_to_go_on_gives_none_not_zero() -> None:
    """⚠️ 0 会在日志里读成「一分钟都没浪费」——那是句假话，比不说更糟。"""
    assert (
        wasted_line_seconds(
            dispatched_at_utc=DISPATCHED, line_free_at_utc=None, flight_seconds=None
        )
        is None
    )
    assert (
        wasted_line_minutes(dispatched_at_utc=None, line_free_at_utc=None, flight_seconds=None)
        is None
    )


def test_minutes_are_just_the_seconds_divided() -> None:
    minutes = wasted_line_minutes(
        dispatched_at_utc=DISPATCHED, line_free_at_utc=None, flight_seconds=3724
    )

    assert minutes is not None
    assert round(minutes, 1) == 124.1
