"""当天的攻击状态存库、一行读回：`daily_attack_status`。

用户口径（2026-08-11）：「每天的海盗次数（状态）也可以存库，这样也可以快速回读。」

`daily_reconciliations` 原先**只有信箱那一侧的观测数**（`observed_reports`），
答不上两个问题：

- 「今天一共算打了几发」——那个数要现去跑 `count_dispatches_since`；
- 「还有几发在等战报」——库里压根没有。

于是重启之后想知道「今日 X/32、几发在飞」，除了再翻一趟信箱（约 20 秒导航，
还要抢会话）没有别的办法。三列补上之后，一次 `daily_attack_status` 就够。

⚠️ 这张表**仍然不是派遣台账**：一行不代表一发派遣。取大与「绝不伪造派遣」
那两条规则在 `test_daily_reconciliation.py`，这里守的是新增那三列的语义。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    FleetPresetRef,
)

MIDNIGHT = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
NOON = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
ORIGIN = Coordinate(2, 137, 18)


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    at: datetime,
    mission: str = MISSION_KIND_ATTACK,
    accepted: bool = True,
    target: Coordinate | None = None,
    flight: timedelta | None = timedelta(minutes=15),
) -> UUID:
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=target or Coordinate(2, 137, 1),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=at,
            accepted=accepted,
            mission_kind=mission,
        )
    )
    repository.record_flight_time(dispatch_id, flight, at)
    return dispatch_id


def _reconcile(repository, *, observed: int = 0, complete: bool = False, at: datetime = NOON):  # type: ignore[no-untyped-def]
    return repository.record_daily_reconciliation(
        TARGET_KIND_PIRATE,
        day_utc=MIDNIGHT,
        observed_reports=observed,
        complete=complete,
        reconciled_at_utc=at,
    )


def test_the_days_status_is_readable_in_one_row(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """重启之后一行就能答上「今天打了几发、还有几发在等」。"""
    _dispatch(repository, run_id, at=NOON - timedelta(minutes=30))
    _dispatch(repository, run_id, at=NOON - timedelta(minutes=20), target=Coordinate(2, 137, 2))
    _reconcile(repository, observed=1)

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)

    assert status is not None
    assert status.dispatched_count == 2
    assert status.attacks_used == 2
    assert status.awaiting_reports == 2
    assert status.observed_reports == 1


def test_a_day_that_was_never_reconciled_reads_back_as_nothing(repository) -> None:  # type: ignore[no-untyped-def]
    """没对过账的那天返回 None，**不是 0**。

    「今天一发没打」和「今天还没对过账」对「还能不能接着打」的含义完全相反，
    拿 0 顶替就是把后者说成前者。
    """
    assert repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT) is None


def test_the_mailbox_count_can_win_the_used_quota(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """`attacks_used` 是两个下界取大，口径与 `count_dispatches_since` 一致。

    库里一发、信箱里九份（手工打的、崩在写库之前的、换库之前的），已用配额算九。
    """
    _dispatch(repository, run_id, at=NOON)
    _reconcile(repository, observed=9)

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)

    assert status is not None
    assert (status.dispatched_count, status.observed_reports, status.attacks_used) == (1, 9, 9)


def test_the_used_quota_survives_the_database_being_wiped(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **只增不减，而且这一层不能靠 `observed_reports` 代劳。**

    `observed_reports` 自己就是取大的，所以「信箱这趟数少了」不会让配额松回去。
    真正会掉下来的是 `dispatched_count`——**库被换过/清过**的那一天，
    而游戏里当天已经用掉的额度不会跟着退回去。这里就把那一天演出来：
    先派三发、对一次账，再把 `attack_dispatches` 清空、重新对账。

    偏大只让助手提前收手，偏小才会白飞舰队——方向一律往「打得更少」倒。
    """
    from evo_helper.storage import models as orm

    for position in (1, 2, 3):
        _dispatch(repository, run_id, at=NOON, target=Coordinate(2, 137, position))
    _reconcile(repository)
    assert repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT).attacks_used == 3

    with repository._session_factory() as session:  # noqa: SLF001 - 演「库被清过」
        session.query(orm.AttackDispatchRow).delete()
        session.commit()
    _reconcile(repository, at=NOON + timedelta(hours=1))

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)
    assert status is not None
    assert status.dispatched_count == 0
    assert status.attacks_used == 3


def test_the_awaiting_count_falls_back_when_the_reports_arrive(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ `awaiting_reports` 是**瞬时状态**，可增可减。

    做成只增不减的话，舰队全回来之后那个数会永远停在最高水位，
    回读出来的「还在等」全是假的——而这一列存在的理由就是让人信它。
    """
    at = NOON - timedelta(minutes=30)
    _dispatch(repository, run_id, at=at)
    _reconcile(repository)
    first = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)
    assert first is not None and first.awaiting_reports == 1

    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=at + timedelta(minutes=15),
            attacker_origin=ORIGIN,
            defender_target=Coordinate(2, 137, 1),
        )
    )
    _reconcile(repository, at=NOON + timedelta(minutes=1))

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)
    assert status is not None
    assert status.awaiting_reports == 0


def test_scout_legs_are_not_counted_as_attacks(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """侦察也是打向海盗的，但它占的是航线、不是配额。

    不排掉的话一轮 4 发侦察就吃掉 4 次攻击额度，当天 32 次以 4 倍速度静默消失。
    口径与 `count_dispatches_since` 完全一致。
    """
    _dispatch(repository, run_id, at=NOON, mission=MISSION_KIND_SCOUT)
    _dispatch(repository, run_id, at=NOON, mission=MISSION_KIND_SCOUT, target=Coordinate(2, 137, 2))
    _reconcile(repository)

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)

    assert status is not None
    assert (status.dispatched_count, status.awaiting_reports) == (0, 0)


def test_a_refused_dispatch_is_not_counted(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，不消耗配额、也不在等战报。"""
    _dispatch(repository, run_id, at=NOON, accepted=False)
    _reconcile(repository)

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)

    assert status is not None
    assert (status.dispatched_count, status.awaiting_reports) == (0, 0)


def test_yesterdays_dispatches_do_not_land_on_todays_row(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """日界是 UTC 00:00（游戏的配额就是这么切的），不是本地日历天。"""
    _dispatch(repository, run_id, at=MIDNIGHT - timedelta(minutes=5))
    _dispatch(repository, run_id, at=NOON, target=Coordinate(2, 137, 2))
    _reconcile(repository)

    status = repository.daily_attack_status(TARGET_KIND_PIRATE, day_utc=MIDNIGHT)

    assert status is not None
    assert status.dispatched_count == 1


def test_recording_still_never_invents_a_dispatch(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 这三列一个都不许反过来写进 `attack_dispatches`。

    库里多一条不存在的派遣，调度器就会以为一条航线被占着、并等一份永远不来的
    战报，要到 `MAX_REPORT_AGE`（6 小时）才被判缺失清掉。
    """
    from evo_helper.storage import models as orm

    _reconcile(repository, observed=32)

    with repository._session_factory() as session:  # noqa: SLF001 - 直接数行
        assert session.query(orm.AttackDispatchRow).count() == 0
