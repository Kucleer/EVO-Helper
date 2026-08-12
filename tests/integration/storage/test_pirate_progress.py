"""海盗目标的状态查询：侦察 → 判定 → 攻击 → 战报，走到哪一步了。

用户口径（2026-08-11）：「侦查海盗战果获得战报后，有 2 个结果，需要更新状态：
不触发攻击 / 触发攻击 / 攻击完成（获得攻击完成战报后更新）。」

这里测的重点有两处，都在「不显示错的东西」这一侧：

1. `UNREADABLE`（没看清）不许显示成「不触发攻击」。
2. 一列判定都不新增到库里——`pirate_progress` 只从已有的四张表推。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.pirate_round import PiratePhase
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
    ScoutReport,
    ScoutTriggerShip,
)

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 140, 1)
OTHER = Coordinate(2, 140, 2)

DAY = datetime(2026, 8, 11, tzinfo=UTC)
NEXT_DAY = DAY + timedelta(days=1)
SCOUT_AT = DAY + timedelta(hours=21, minutes=40)
ATTACK_AT = DAY + timedelta(hours=21, minutes=45)

#: 四个触发舰种全部读到、全部 0——唯一能得出「不值得打」的读数。
ALL_ZERO = {"深空吞噬者": 0, "噬能截击者": 0, "钛能守卫者": 0, "收割者": 0}
#: 实机 2026-08-11 的 2:140:1：三格读到，`收割者` 那格 NULL。
ONE_BLIND = {"深空吞噬者": 1, "噬能截击者": 0, "钛能守卫者": 1, "收割者": None}
#: 有实打实的舰队。
HAS_FLEET = {"深空吞噬者": 8, "噬能截击者": 4, "钛能守卫者": 1, "收割者": None}


# -- 侦察报告回来之前 --------------------------------------------------------


def test_a_scout_in_flight_shows_as_awaiting_its_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.target == TARGET
    assert row.phase is PiratePhase.AWAITING_SCOUT_REPORT
    assert row.verdict is None
    assert row.scout_reported_at_utc is None


# -- 判定：三个结论各自一态 --------------------------------------------------


def test_a_fully_read_empty_pirate_shows_as_no_attack(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, ALL_ZERO)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.NO_ATTACK
    assert row.scout_reported_at_utc == SCOUT_AT


def test_a_blind_cell_does_not_show_as_no_attack(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """**这条是这个文件存在的理由。**

    实机那份报告三格读到 1/0/1、`收割者` 那格是 NULL。判定是「没看清」，
    而不是「这里是空的」；显示成「不触发攻击」就等于把一格从来没读出来的
    数据当成了一个已经问出来的答案。
    """
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, ONE_BLIND)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.SCOUT_UNREADABLE
    assert row.phase is not PiratePhase.NO_ATTACK
    assert row.verdict == "UNREADABLE"


def test_a_verdict_of_attack_with_no_dispatch_is_visible_as_such(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """判定要打、攻击却没派出去，要看得出来。

    实机上这一步被拦下的原因有好几种（航线满、面板认不出、找不到预设），
    每一种都会让目标停在这里；停在这里必须和「不值得打」长得不一样。
    """
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, HAS_FLEET)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.NEEDS_ATTACK
    assert row.attack_count == 0


# -- 攻击派出之后 ------------------------------------------------------------


def test_a_dispatched_attack_waits_for_its_battle_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, HAS_FLEET)
    _attack_dispatch(repository, run_id, TARGET)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.AWAITING_ATTACK_REPORT
    assert (row.attack_count, row.attack_reports) == (1, 0)
    assert row.latest_attack_at_utc == ATTACK_AT


def test_the_battle_report_completes_the_target(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, HAS_FLEET)
    _attack_dispatch(repository, run_id, TARGET, has_report=True)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.ATTACK_DONE
    assert (row.attack_count, row.attack_reports) == (1, 1)


def test_a_refused_attack_never_becomes_a_permanent_wait(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """被游戏拒掉的那一发没有舰队飞出去，也就永远不会有战报。

    算进来就是一个永久的「已触发攻击 · 待战报」，而判定说的其实是「该打」。
    口径与 `bot_dispatch_facts` / `pending_reports_for_kind` 同源。
    """
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, HAS_FLEET)
    _attack_dispatch(repository, run_id, TARGET, accepted=False)

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.NEEDS_ATTACK
    assert row.attack_count == 0


# -- 取哪些行、取哪一份报告 --------------------------------------------------


def test_bot_targets_stay_out_of_the_pirate_view(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """两条链路的判据完全不同：bot 看战报里的守方单位数分档，海盗看侦察报告。

    混进来的 bot 目标没有侦察报告，会一律显示成「待侦察报告」——一个永远
    不会推进的假状态。
    """
    _scout_dispatch(repository, run_id, TARGET)
    _attack_dispatch(repository, run_id, OTHER, target_kind=TARGET_KIND_BOT)

    assert [row.target for row in repository.pirate_progress(since=DAY, until=NEXT_DAY)] == [TARGET]


def test_yesterdays_dispatch_is_outside_the_window(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET, at=DAY - timedelta(hours=2))

    assert repository.pirate_progress(since=DAY, until=NEXT_DAY) == []


def test_a_report_that_lands_after_midnight_still_explains_todays_scout(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """侦察报告**不按窗口筛**，按目标取最近的一份。

    派出到报告落进信箱要几分钟，跨过 UTC 零点就会出现「意图在昨天、报告在
    今天」。按窗口筛的话那一发会永远显示成「待侦察报告」。
    """
    late = NEXT_DAY + timedelta(minutes=3)
    _scout_dispatch(repository, run_id, TARGET, at=NEXT_DAY - timedelta(minutes=2))
    _scout_report(repository, TARGET, ALL_ZERO, at=late)

    (row,) = repository.pirate_progress(since=DAY, until=None)

    assert row.phase is PiratePhase.NO_ATTACK
    assert row.scout_reported_at_utc == late


def test_a_later_report_does_not_explain_an_earlier_window(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """晚于窗口的报告不算数：今天那份不该拿去解释昨天那一发。

    没有这条上界，翻查历史某一天会看到那天之后才产生的判定——库里那颗海盗
    今天有舰队，不代表昨天派侦察的时候有。
    """
    _scout_dispatch(repository, run_id, TARGET, at=DAY + timedelta(hours=1))
    _scout_report(repository, TARGET, HAS_FLEET, at=NEXT_DAY + timedelta(hours=1))

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.AWAITING_SCOUT_REPORT
    assert row.verdict is None


def test_the_newest_report_wins_when_a_target_was_scouted_twice(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, ALL_ZERO, at=SCOUT_AT)
    _scout_report(repository, TARGET, HAS_FLEET, at=SCOUT_AT + timedelta(hours=1))

    (row,) = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert row.phase is PiratePhase.NEEDS_ATTACK


def test_each_target_gets_its_own_report(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """报告按**整个坐标**认，不是按银河或恒星系。

    同一系里的两颗海盗读数完全不同：认错一颗，就会按邻居的舰队量决定打不打，
    而这条链路的判定结果直接决定舰队送不送出去。
    """
    _scout_dispatch(repository, run_id, TARGET)
    _scout_dispatch(repository, run_id, OTHER, at=SCOUT_AT + timedelta(minutes=1))
    _scout_report(repository, TARGET, ALL_ZERO)
    _scout_report(repository, OTHER, HAS_FLEET, at=SCOUT_AT + timedelta(minutes=1))

    rows = {row.target: row.phase for row in repository.pirate_progress(since=DAY, until=NEXT_DAY)}

    assert rows == {TARGET: PiratePhase.NO_ATTACK, OTHER: PiratePhase.NEEDS_ATTACK}


def test_rows_come_back_in_coordinate_order(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    _scout_dispatch(repository, run_id, OTHER)
    _scout_dispatch(repository, run_id, TARGET, at=SCOUT_AT + timedelta(minutes=1))

    rows = repository.pirate_progress(since=DAY, until=NEXT_DAY)

    assert [row.target for row in rows] == [TARGET, OTHER]


def test_no_new_table_or_column_was_added_for_the_verdict(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """判定是**现算**的，库里一列都不存（`domain.records.ScoutReport`）。

    钉这条是因为「加一列存结论」是最容易被后来人当成改进的做法，而门槛与
    舰种表都会变——存下来的那一列过两天就没人说得清是按哪版规则算的。
    """
    from sqlalchemy import inspect

    _scout_dispatch(repository, run_id, TARGET)
    _scout_report(repository, TARGET, ONE_BLIND)
    with repository._session_factory() as session:  # noqa: SLF001
        columns = {
            table: {column["name"] for column in inspect(session.bind).get_columns(table)}
            for table in ("scout_reports", "scout_trigger_ships")
        }

    assert not any("verdict" in names or "phase" in names for names in columns.values())


# -- 夹具 --------------------------------------------------------------------


def _scout_dispatch(repository, run_id, target, *, at: datetime = SCOUT_AT):  # type: ignore[no-untyped-def]
    return _dispatch(repository, run_id, target, at=at, mission_kind=MISSION_KIND_SCOUT)


def _attack_dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    target,
    *,
    at: datetime = ATTACK_AT,
    accepted: bool = True,
    has_report: bool = False,
    target_kind: str = TARGET_KIND_PIRATE,
):
    return _dispatch(
        repository,
        run_id,
        target,
        at=at,
        mission_kind=MISSION_KIND_ATTACK,
        accepted=accepted,
        has_report=has_report,
        target_kind=target_kind,
        preset="AAA",
    )


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id,
    target,
    *,
    at: datetime,
    mission_kind: str,
    accepted: bool = True,
    has_report: bool = False,
    target_kind: str = TARGET_KIND_PIRATE,
    preset: str = "侦察",
):
    intent_id = uuid4()
    dispatch_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=ORIGIN,
            target=target,
            preset=FleetPresetRef(name=preset, signature="sig"),
            cycle_start_utc=at,
            created_at_utc=at,
            target_kind=target_kind,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=at,
            accepted=accepted,
            mission_kind=mission_kind,
        )
    )
    if has_report:
        _attach_report(repository, dispatch_id, target, at)
    return dispatch_id


def _scout_report(repository, target, counts, *, at: datetime = SCOUT_AT) -> None:  # type: ignore[no-untyped-def]
    repository.append_scout_report(
        ScoutReport(
            report_id=uuid4(),
            reported_at_utc=at,
            raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
            origin=ORIGIN,
            target=target,
            trigger_ships=tuple(
                ScoutTriggerShip(ship_type=name, count=count) for name, count in counts.items()
            ),
        )
    )


def _attach_report(repository, dispatch_id, target, reported_at: datetime) -> None:  # type: ignore[no-untyped-def]
    """直接把战报挂到那一发上，绕开 `append_report` 的坐标+时间认领算法。

    这里测的是「有没有战报」这一个事实，不该顺带依赖认领算法。
    """
    from evo_helper.storage import models as orm

    with repository._session_factory() as session:  # noqa: SLF001
        session.add(
            orm.BattleReportRow(
                id=uuid4(),
                dispatch_id=dispatch_id,
                reported_at_utc=reported_at,
                attacker_origin_galaxy=ORIGIN.galaxy,
                attacker_origin_system=ORIGIN.system,
                attacker_origin_position=ORIGIN.position,
                defender_target_galaxy=target.galaxy,
                defender_target_system=target.system,
                defender_target_position=target.position,
            )
        )
        session.commit()
