from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.scheduling import (
    TimeWindow,
    WindowPhase,
    evaluate_time_window,
    to_schedule_local,
)


def test_before_window_arms_until_start_time() -> None:
    now = datetime(2026, 8, 5, 23, 50, tzinfo=UTC)  # 07:50 UTC+8 next day
    evaluation = evaluate_time_window(now)

    assert evaluation.phase is WindowPhase.BEFORE
    assert evaluation.next_start_utc == datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
    assert evaluation.drain_at_utc == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def test_inside_window_scans_immediately() -> None:
    now = datetime(2026, 8, 6, 0, 10, tzinfo=UTC)  # 08:10 UTC+8
    evaluation = evaluate_time_window(now)

    assert evaluation.phase is WindowPhase.INSIDE
    assert evaluation.next_start_utc == datetime(2026, 8, 6, 0, 0, tzinfo=UTC)


def test_after_window_schedules_next_day() -> None:
    now = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)  # 20:30 UTC+8
    evaluation = evaluate_time_window(now)

    assert evaluation.phase is WindowPhase.AFTER
    assert evaluation.next_start_utc == datetime(2026, 8, 7, 0, 0, tzinfo=UTC)


def test_custom_window_evaluates_in_schedule_timezone() -> None:
    window = TimeWindow(start_hour=20, end_hour=23)
    now = datetime(2026, 8, 6, 12, 30, tzinfo=UTC)  # 20:30 UTC+8
    evaluation = evaluate_time_window(now, window)

    assert evaluation.phase is WindowPhase.INSIDE
    assert evaluation.drain_at_utc == datetime(2026, 8, 6, 15, 0, tzinfo=UTC)


def test_window_boundaries_are_exclusive_for_end() -> None:
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)  # exactly 20:00 UTC+8
    assert evaluate_time_window(now).phase is WindowPhase.AFTER


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError, match="window hours"):
        TimeWindow(start_hour=20, end_hour=8)


def test_to_schedule_local_requires_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_schedule_local(datetime(2026, 8, 6, 0, 0))


def test_to_schedule_local_shifts_by_eight_hours() -> None:
    value = datetime(2026, 8, 6, 0, 0, tzinfo=UTC)
    assert to_schedule_local(value).hour == 8
    assert to_schedule_local(value).utcoffset() == timedelta(hours=8)
