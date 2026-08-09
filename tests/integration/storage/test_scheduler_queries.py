"""调度器要问数据库的四件事。

这些查询是调度判据的事实来源。它们和 `/logs` 页面读的是同一批表——
判据和页面分叉，是这套东西最容易悄悄出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)


def test_todays_pirate_dispatches_are_counted_from_utc_midnight(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """重置点是 UTC 00:00，也就是本地 UTC+8 的每天早上 8 点。"""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    for dispatched_at in (yesterday, now):
        intent_id = uuid4()
        repository.save_attack_intent(
            AttackIntent(
                intent_id=intent_id,
                run_id=run_id,
                origin=Coordinate(2, 137, 18),
                target=Coordinate(2, 137, 1),
                preset=FleetPresetRef(name="AAA", signature="sig"),
                cycle_start_utc=dispatched_at,
                created_at_utc=dispatched_at,
                target_kind=TARGET_KIND_PIRATE,
            )
        )
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at,
                dry_run=False,
                accepted=True,
            )
        )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 1


def test_bot_dispatches_do_not_count_towards_the_pirate_quota(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """标错就白飞一趟舰队——这条测试守的就是 Task 1 修的那个 bug。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 140, 3),
            preset=FleetPresetRef(name="BBB", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 0
    assert repository.count_dispatches_since(TARGET_KIND_BOT, since=_utc_midnight(now)) == 1


def test_pending_reports_are_scoped_by_target_kind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """海盗和 bot 各等各的报告。混在一起，一条链路会替另一条判「该回去收了」。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 2),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    assert len(repository.pending_reports_for_kind(TARGET_KIND_PIRATE)) == 1
    assert repository.pending_reports_for_kind(TARGET_KIND_BOT) == []


def test_a_dispatch_with_no_flight_time_is_reported_as_unknown(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读不到飞行时间时 expected 为 None，等待调度器据此立即尝试收取。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 3),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    pending = repository.pending_reports_for_kind(TARGET_KIND_PIRATE)

    assert pending[0].expected_report_at_utc is None


def _utc_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
