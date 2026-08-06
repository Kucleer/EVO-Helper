from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.rules import (
    can_attack_target,
    cycle_start_utc,
    same_cycle,
    validate_start_request,
)


def test_cycle_starts_on_monday_utc() -> None:
    # 2026-08-06 is a Thursday.
    assert cycle_start_utc(datetime(2026, 8, 6, 14, 0, tzinfo=UTC)) == datetime(
        2026, 8, 3, 0, 0, tzinfo=UTC
    )


def test_same_cycle_within_week_but_not_across() -> None:
    thursday = datetime(2026, 8, 6, 14, 0, tzinfo=UTC)
    assert same_cycle(thursday, thursday + timedelta(days=1))
    assert not same_cycle(thursday, thursday + timedelta(days=7))


def test_first_attack_is_allowed() -> None:
    decision = can_attack_target(None, datetime(2026, 8, 6, 2, 0, tzinfo=UTC))
    assert decision.allowed
    assert decision.reason == "first_time"


def test_same_cycle_attack_is_denied() -> None:
    last = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    now = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    decision = can_attack_target(last, now)
    assert not decision.allowed
    assert decision.reason == "already_attacked_this_cycle"


def test_new_cycle_attack_is_allowed() -> None:
    last = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    now = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    decision = can_attack_target(last, now)
    assert decision.allowed
    assert decision.reason == "new_cycle"


def test_forced_revisit_bypasses_cycle_dedupe() -> None:
    last = datetime(2026, 8, 3, 1, 0, tzinfo=UTC)
    now = datetime(2026, 8, 6, 2, 0, tzinfo=UTC)
    decision = can_attack_target(last, now, forced_revisit=True)
    assert decision.allowed
    assert decision.reason == "forced_revisit"


def test_idempotency_key_validation() -> None:
    validate_start_request("run-2026-08-06-01")
    with pytest.raises(ValueError, match="idempotency_key"):
        validate_start_request("")
    with pytest.raises(ValueError, match="idempotency_key"):
        validate_start_request("has spaces")
    with pytest.raises(ValueError, match="idempotency_key"):
        validate_start_request("x" * 129)
