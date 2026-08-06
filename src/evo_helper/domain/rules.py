"""Weekly-cycle dedupe, forced revisits, and idempotent start rules."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class CycleDecision:
    allowed: bool
    reason: str


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("expected timezone-aware datetime")
    return value.astimezone(UTC)


def cycle_start_utc(now_utc: datetime) -> datetime:
    """Return the Monday 00:00 UTC boundary of *now_utc*'s weekly cycle."""
    moment = ensure_utc(now_utc)
    monday = moment.date() - timedelta(days=moment.weekday())
    return datetime.combine(monday, time.min, tzinfo=UTC)


def same_cycle(first: datetime, second: datetime) -> bool:
    return cycle_start_utc(first) == cycle_start_utc(second)


def can_attack_target(
    last_attack_at_utc: datetime | None,
    now_utc: datetime,
    forced_revisit: bool = False,
) -> CycleDecision:
    """Decide whether a target may be attacked under the weekly-cycle rule."""
    moment = ensure_utc(now_utc)
    if forced_revisit:
        return CycleDecision(allowed=True, reason="forced_revisit")
    if last_attack_at_utc is None:
        return CycleDecision(allowed=True, reason="first_time")
    if same_cycle(last_attack_at_utc, moment):
        return CycleDecision(allowed=False, reason="already_attacked_this_cycle")
    return CycleDecision(allowed=True, reason="new_cycle")


def validate_start_request(idempotency_key: str) -> None:
    """Validate a caller-supplied start idempotency key."""
    if _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
        raise ValueError("idempotency_key must match ^[A-Za-z0-9_-]{1,128}$")
