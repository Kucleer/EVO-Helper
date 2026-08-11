"""开工那一趟带在手上的**那张单子**：`due_attack_dispatches`。

用户口径（2026-08-11）：「项目启动时，是否可以先读数据库中，理论上已经到达的
报告，然后更新数据再开始后面的任务。」

原先那一趟是「翻信箱、读到什么算什么、读到库里已有的就早停」。早停假定
「库里已有 ⇒ 往下都读过了」，而这个假定在**报告已入库、却没接到该接的那一发
派遣上**时是假的（成因见 `test_report_dispatch_matching`），于是那几发永远
补不回来。反过来由库驱动之后，单子上还有没找到的就不许收工。

单子必须**有界**，否则早停彻底失效、每一趟都要把开封预算烧满（每封约八秒）。
四条判据各自的成因见下面每一条测试。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    FleetPresetRef,
)

NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)
MAX_AGE = timedelta(hours=6)
ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 138, 2)


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    at: datetime,
    flight: timedelta | None = timedelta(minutes=15),
    mission: str = MISSION_KIND_ATTACK,
    accepted: bool = True,
    kind: str = TARGET_KIND_PIRATE,
    target: Coordinate = TARGET,
) -> UUID:
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=target,
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=kind,
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


def _due(repository) -> list:  # type: ignore[no-untyped-def,type-arg]
    return repository.due_attack_dispatches(TARGET_KIND_PIRATE, now_utc=NOW, max_age=MAX_AGE)


def test_a_dispatch_past_its_expected_report_time_is_on_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """派出 30 分钟、飞行 15 分钟：战报理论上早该有了，而库里没有。"""
    dispatch_id = _dispatch(repository, run_id, at=NOW - timedelta(minutes=30))

    (item,) = _due(repository)

    assert item.dispatch_id == dispatch_id
    assert item.target == TARGET
    assert item.expected_report_at_utc == NOW - timedelta(minutes=15)


def test_a_fleet_still_in_flight_is_not_on_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """还没到点的不算：它的战报本来就还不存在，为它多开几封信只是白花时间。"""
    _dispatch(repository, run_id, at=NOW - timedelta(minutes=5))

    assert _due(repository) == []


def test_an_unknown_flight_time_counts_as_due_now(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """飞行时间没读到（`expected_report_at_utc` 为 NULL）当作「现在就该有了」。

    降级方向与 `ReportWaitPlanner.plan` 里那条「未知即立即收取」一致：宁可白开
    一封，也不能无限等一个不知道何时到的战报。
    """
    _dispatch(repository, run_id, at=NOW - timedelta(minutes=5), flight=None)

    assert len(_due(repository)) == 1


def test_a_dispatch_whose_report_arrived_leaves_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """战报认领上了就不在单子上——这正是「单子空了才收工」的终止条件。"""
    at = NOW - timedelta(minutes=30)
    _dispatch(repository, run_id, at=at)
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=at + timedelta(minutes=15),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
        )
    )

    assert _due(repository) == []


def test_a_given_up_dispatch_leaves_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 超过 `MAX_REPORT_AGE` 的**必须**掉出单子。

    少了这一条，一发战报真丢了的派遣会让单子**永远非空**，早停就此彻底失效：
    每一趟都要把开封预算烧满，而那是每封约八秒。这个常量与
    `_unmatched_dispatch_candidates` / `bot_dispatch_facts` 用的是同一个。
    """
    _dispatch(repository, run_id, at=NOW - MAX_AGE - timedelta(minutes=1))

    assert _due(repository) == []


def test_a_scout_leg_is_never_on_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """侦察发不产生 `battle_reports`，留在单子上就是一条永远找不到的条目。

    同一条排除在 `pending_reports_for_kind` / `oldest_open_attack_at` 也都有。
    """
    _dispatch(repository, run_id, at=NOW - timedelta(minutes=30), mission=MISSION_KIND_SCOUT)

    assert _due(repository) == []


def test_a_refused_dispatch_is_never_on_the_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，也就永远不会有战报。"""
    _dispatch(repository, run_id, at=NOW - timedelta(minutes=30), accepted=False)

    assert _due(repository) == []


def test_each_chain_carries_its_own_list(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """两条链路各带各的单子：bot 在等的那一发不该让海盗那趟多开几封。"""
    _dispatch(repository, run_id, at=NOW - timedelta(minutes=30), kind=TARGET_KIND_BOT)

    assert _due(repository) == []
    assert (
        repository.due_attack_dispatches(TARGET_KIND_BOT, now_utc=NOW, max_age=MAX_AGE) is not None
    )
    assert len(repository.due_attack_dispatches(TARGET_KIND_BOT, now_utc=NOW, max_age=MAX_AGE)) == 1


def test_the_list_is_oldest_first(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """按派出时刻排：日志里先说最早那一发，翻信箱的下界也是照它定的。"""
    later = _dispatch(repository, run_id, at=NOW - timedelta(minutes=30))
    earlier = _dispatch(
        repository, run_id, at=NOW - timedelta(hours=2), target=Coordinate(2, 138, 3)
    )

    assert [item.dispatch_id for item in _due(repository)] == [earlier, later]
