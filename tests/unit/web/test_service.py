from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.web.service import (
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


# 三条排期用例（窗口前 ARMED / 窗口内 SCANNING / 窗口后顺延一天）与
# `test_idempotency_key_is_rejected_on_reuse` 随 `start_run` 一起删了：它们验的是
# `FakeApplicationService.start_run` 与它私有的 `_schedule_state`，而 `start_run`
# 是 `POST /api/runs/start` 唯一的实现，接口删了它也就没了调用方。
#
# 这两件事都**不是**生产链路上的判据：真实的运行实例由 `tools/scan_coordinates.py`
# 与 `tools/pirate_loop.py` 按 `PLAN_NAME` / `RUN_KEY` 建，起停时间由常驻调度器
# （`application/mission_scheduler.py`，有 `tests/unit/application` 守着）决定，
# 幂等键的唯一性由库上的唯一约束保证
# （`tests/integration/storage/test_repository.py::test_idempotency_key_is_unique`）。

# `test_invalid_state_transition_is_conflict` 随「运行详情」页一起删了：
# 它守的是 `pause_run` / `emergency_stop_run` 那条状态机路径，而那两个方法
# 只有 `run.html` 上的按钮调用过。运行状态在真实链路里由
# `SqlAlchemyRepository.set_run_state` 推进，`domain.state_machine` 的转移
# 规则本身仍有 `tests/unit/domain` 与 `test_report_wait_persistence` 守着。


def test_update_plan_preserves_created_at() -> None:
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")
    plan = _plan_payload(clock)

    updated = clock.update_plan(plan.id, PlanPatchView(name="renamed"))

    assert updated.name == "renamed"
    assert updated.created_at == plan.created_at


def test_missing_plan_raises_not_found() -> None:
    """认不出的计划 id 要走 `NotFoundError`（接口层映射成 404），不是静默当空。

    原先是拿 `start_run` 验的，改用 `update_plan`：`start_run` 已删，而这条用例
    守的从来是「查不到就报 404」这条路径本身。
    """
    clock = _fixed_clock("2026-08-06T01:00:00+00:00")

    with pytest.raises(NotFoundError):
        clock.update_plan(uuid4(), PlanPatchView(name="missing-plan"))


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
