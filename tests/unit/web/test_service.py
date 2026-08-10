from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evo_helper.domain.models import Coordinate, RunState
from evo_helper.web.service import (
    ConflictError,
    FakeApplicationService,
    FleetEntryView,
    NotFoundError,
    PlanPatchView,
    ScanPlanView,
    ScanRangeView,
)


def _fixed_clock(value: str) -> FakeApplicationService:
    now = datetime.fromisoformat(value)
    return FakeApplicationService(now_utc=lambda: now)


def _plan_payload(clock: FakeApplicationService) -> ScanPlanView:
    scan_range = ScanRangeView(
        start=Coordinate(1, 1, 1),
        end=Coordinate(1, 1, 20),
        origin=Coordinate(1, 1, 1),
        fleet_preset="main-fleet",
        fleet_preset_signature="main-fleet-v1",
        priority=0,
    )
    return clock.create_plan(
        name="daily",
        enabled=True,
        window_start=datetime.strptime("08:00", "%H:%M").time(),
        window_end=datetime.strptime("20:00", "%H:%M").time(),
        ranges=(scan_range,),
    )


def test_schedule_before_window_is_armed() -> None:
    clock = _fixed_clock("2026-08-05T23:59:00+00:00")  # Shanghai 07:59
    plan = _plan_payload(clock)

    run = clock.start_run(plan.id, "key-before-window")

    assert run.state is RunState.ARMED


def test_schedule_inside_window_scans_immediately() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")  # Shanghai 09:00
    plan = _plan_payload(clock)

    run = clock.start_run(plan.id, "key-inside-window")

    assert run.state is RunState.SCANNING


def test_schedule_after_window_arms_for_next_day() -> None:
    clock = _fixed_clock("2026-08-06T13:00:00+00:00")  # Shanghai 21:00
    plan = _plan_payload(clock)

    run = clock.start_run(plan.id, "key-after-window")

    assert run.state is RunState.ARMED
    assert run.target_date.isoformat() == "2026-08-07"


def test_idempotency_key_is_rejected_on_reuse() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")
    plan = _plan_payload(clock)
    clock.start_run(plan.id, "same-key")

    with pytest.raises(ConflictError):
        clock.start_run(plan.id, "same-key")


def test_invalid_state_transition_is_conflict() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")
    plan = _plan_payload(clock)
    run = clock.start_run(plan.id, "transition-key")

    clock.emergency_stop_run(run.run_id)

    with pytest.raises(ConflictError):
        clock.pause_run(run.run_id)


def test_update_plan_preserves_created_at() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")
    plan = _plan_payload(clock)

    updated = clock.update_plan(plan.id, PlanPatchView(name="renamed"))

    assert updated.name == "renamed"
    assert updated.created_at == plan.created_at


def test_missing_plan_raises_not_found() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")

    with pytest.raises(NotFoundError):
        clock.start_run(uuid4(), "missing-plan-key")


def test_fleet_diff_tracks_changes() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")
    coordinate = Coordinate(3, 2, 1)
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 10), FleetEntryView("cruiser", 5)),
        captured_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
    )
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 12), FleetEntryView("battleship", 2)),
        captured_at_utc=datetime(2026, 8, 6, 2, 0, tzinfo=UTC),
    )

    diff = clock.get_fleet_diff(coordinate)

    assert diff is not None
    assert diff.total_before == 15
    assert diff.total_after == 14
    assert "cruiser" in diff.disappeared
    assert "battleship" in diff.first_seen
    assert any(change.ship_type == "destroyer" and change.delta == 2 for change in diff.changes)
