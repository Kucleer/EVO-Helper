"""UTC+8 time-window scheduling for scan runs.

Business scheduling uses Asia/Shanghai (UTC+8, no DST), while persisted and
presented timestamps stay timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
from enum import StrEnum

SHANGHAI_OFFSET = timedelta(hours=8)


class WindowPhase(StrEnum):
    BEFORE = "BEFORE"  # window has not opened: ARMED, wait for start
    INSIDE = "INSIDE"  # window is open: SCANNING may start immediately
    AFTER = "AFTER"  # window has closed: ARMED, scheduled for the next day


@dataclass(frozen=True)
class TimeWindow:
    """Inclusive scan window in a fixed-offset schedule timezone."""

    start_hour: int = 8
    end_hour: int = 20
    offset: timedelta = SHANGHAI_OFFSET

    def __post_init__(self) -> None:
        if not (0 <= self.start_hour < self.end_hour <= 23):
            raise ValueError("window hours must satisfy 0 <= start < end <= 23")


@dataclass(frozen=True)
class WindowEvaluation:
    phase: WindowPhase
    now_utc: datetime
    next_start_utc: datetime
    drain_at_utc: datetime


def to_schedule_local(value: datetime, offset: timedelta = SHANGHAI_OFFSET) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expected timezone-aware datetime")
    return value.astimezone(timezone(offset))


def evaluate_time_window(
    now_utc: datetime,
    window: TimeWindow = TimeWindow(),
) -> WindowEvaluation:
    """Evaluate *now_utc* against *window* in schedule-local time.

    Before the window the run arms and waits for the start time; inside the
    window it may scan immediately; after the window it arms and schedules the
    next day's start.
    """
    now_local = to_schedule_local(now_utc, window.offset)
    start_dt = datetime.combine(
        now_local.date(), time(window.start_hour), tzinfo=timezone(window.offset)
    )
    end_dt = datetime.combine(
        now_local.date(), time(window.end_hour), tzinfo=timezone(window.offset)
    )
    if now_local < start_dt:
        phase = WindowPhase.BEFORE
        next_start = start_dt
    elif now_local < end_dt:
        phase = WindowPhase.INSIDE
        next_start = start_dt
    else:
        phase = WindowPhase.AFTER
        next_start = start_dt + timedelta(days=1)
    return WindowEvaluation(
        phase=phase,
        now_utc=now_utc.astimezone(UTC),
        next_start_utc=next_start.astimezone(UTC),
        drain_at_utc=end_dt.astimezone(UTC),
    )
