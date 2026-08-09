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
from evo_helper.domain.report_wait import MAX_REPORT_AGE

#: 宽限期取 `scheduler_config.report_grace_minutes` 的默认值。
GRACE = timedelta(minutes=30)


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

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1
    assert _pending(repository, TARGET_KIND_BOT, now) == []


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

    pending = _pending(repository, TARGET_KIND_PIRATE, now)

    assert pending[0].expected_report_at_utc is None


# -- 放弃规则：不放弃就是永久卡死 --------------------------------------------


def test_an_old_dispatch_with_no_expected_time_is_abandoned(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**这一条守的是调度器不空转。**

    `ReportWaitPlanner` 见到任何一条 `expected_report_at_utc` 为 NULL 的派遣就
    无条件返回 `COLLECT`，而库里现存的派遣**全是 NULL**（飞行时间从来没人读过，
    历史也不回填）。不按 `dispatched_at_utc` 判老，「有到期未收的战报」就永久为真：
    调度器每个 tick 都去起一次 runner，收一封永远不会到的战报，扫描永远抢不到空隙。
    防卡死机制会原样变成卡死机制。
    """
    now = datetime.now(UTC)
    _dispatch(
        repository,
        run_id,
        TARGET_KIND_PIRATE,
        position=4,
        dispatched_at=now - MAX_REPORT_AGE - timedelta(minutes=1),
    )

    assert _pending(repository, TARGET_KIND_PIRATE, now) == []


def test_a_fresh_dispatch_with_no_expected_time_is_still_pending(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """刚派出去的那发不能跟着一起排掉——它的战报本来就还没到。"""
    now = datetime.now(UTC)
    _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=5, dispatched_at=now - timedelta(minutes=3)
    )

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


def test_a_dispatch_past_its_grace_period_is_abandoned(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """预计时间之后再等一个宽限期还读不到，就判缺失并排除。

    留着的话它同样会把 `COLLECT` 钉死——只是钉死的理由从「未知」换成「过期」。
    """
    now = datetime.now(UTC)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=6, dispatched_at=now - timedelta(hours=2)
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=10), now - timedelta(hours=2))

    assert _pending(repository, TARGET_KIND_PIRATE, now) == []


def test_a_dispatch_within_its_grace_period_is_still_pending(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """刚过预计时间、还在宽限期内的，仍然该去收。"""
    now = datetime.now(UTC)
    dispatched_at = now - timedelta(minutes=15)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=7, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(minutes=5), dispatched_at)

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


def test_a_long_flight_is_not_abandoned_for_being_old(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """`MAX_REPORT_AGE` **只管 NULL 那一档**。

    飞行时间读到了的那些，老不老由它自己的预计时间说了算。拿派出时刻一起卡，
    会把一发飞十小时、还没到的远征当成缺失排掉。
    """
    now = datetime.now(UTC)
    dispatched_at = now - MAX_REPORT_AGE - timedelta(hours=1)
    dispatch_id = _dispatch(
        repository, run_id, TARGET_KIND_PIRATE, position=8, dispatched_at=dispatched_at
    )
    repository.record_flight_time(dispatch_id, timedelta(hours=10), dispatched_at)

    assert len(_pending(repository, TARGET_KIND_PIRATE, now)) == 1


def _pending(repository, target_kind: str, now: datetime):  # type: ignore[no-untyped-def]
    return repository.pending_reports_for_kind(
        target_kind, now_utc=now, grace=GRACE, max_age=MAX_REPORT_AGE
    )


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    target_kind: str,
    *,
    position: int,
    dispatched_at: datetime,
    accepted: bool = True,
    dry_run: bool = False,
):
    """一条意图 + 一条派遣。返回派遣 id，好让调用方补写飞行时间。"""
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, position),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=dispatched_at,
            created_at_utc=dispatched_at,
            target_kind=target_kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=dispatched_at,
            dry_run=dry_run,
            accepted=accepted,
        )
    )
    return dispatch_id


def _utc_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
