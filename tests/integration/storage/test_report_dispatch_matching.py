"""一份战报认领哪一发派遣：`append_report` / `rematch_report_at`。

## 实机故障（生产库 2026-08-11）

攻击日志上四发预设 AAA 一整天显示「待战报」，而战报**明明已经在库里**：

    battle_reports  2:138:2  13:06:28  VICTORY  match_status=AMBIGUOUS  dispatch_id=NULL

原因是候选集里混进了那一发**侦察**。海盗链路的常态是「先侦察、判定值得打、
再攻击」——同一个出发点、同一个目标、相隔几分钟：

    候选 A  12:45:07  SCOUT   ← 探测器，永远不会有攻击战报
    候选 B  12:51:11  ATTACK  ← 就是它

两个候选 → `AMBIGUOUS` → `dispatch_id` 留空 → `has_report` 永远为假。
2:137:1 / 2:137:3 / 2:136:3 / 2:138:2 四行一模一样，无一例外。

`mission_kind == ATTACK` 这道过滤此前**只有认领这一侧漏了**：
`count_dispatches_since`、`oldest_open_attack_at`、`pending_reports_for_kind`、
`bot_dispatch_facts`、`bot_report_due_at` 五处早就写着它。

## 而修好判据并不能救回已经在库里的那些行

`append_report` 只在写入的那一刻认领一次，此后再没有代码回头看它一眼；而
`has_report_at` 那道去重又保证了它们**永远不会被重新读一遍**。所以还要一条
回头重认的路（`rematch_report_at`），由开工那一趟撞见「库里已有」时触发。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

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
from evo_helper.storage import models as orm

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 138, 2)

#: 实机那一天的三个时刻，原样搬过来。
SCOUT_AT = datetime(2026, 8, 11, 12, 45, 7, tzinfo=UTC)
ATTACK_AT = datetime(2026, 8, 11, 12, 51, 11, tzinfo=UTC)
REPORTED_AT = datetime(2026, 8, 11, 13, 6, 28, tzinfo=UTC)


def _dispatch(  # type: ignore[no-untyped-def]
    repository,
    run_id: UUID,
    *,
    at: datetime,
    mission: str = MISSION_KIND_ATTACK,
    accepted: bool = True,
    target: Coordinate = TARGET,
    preset: str = "AAA",
) -> UUID:
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
    return dispatch_id


def _append(repository, *, at: datetime = REPORTED_AT, target: Coordinate = TARGET) -> UUID:  # type: ignore[no-untyped-def]
    report_id = uuid4()
    repository.append_report(
        BattleReport(
            report_id=report_id,
            reported_at_utc=at,
            attacker_origin=ORIGIN,
            defender_target=target,
            raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
            outcome="VICTORY",
        )
    )
    return report_id


def _row(repository, report_id: UUID) -> orm.BattleReportRow:  # type: ignore[no-untyped-def]
    with repository._session_factory() as session:  # noqa: SLF001 - 直接读列，绕开被测方法
        row = session.scalar(select(orm.BattleReportRow).where(orm.BattleReportRow.id == report_id))
        assert row is not None
        return row


# -- 候选集：侦察发没有资格 --------------------------------------------------


def test_the_scout_leg_of_the_same_target_is_not_a_candidate(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **本文件的重点。** 同一目标当天先侦察后攻击，战报只能是攻击发的。

    这是结构上成立的，不需要拿「时间就近」去猜：侦察发根本不产生
    `battle_reports`，它的报告走 `scout_reports` 那张表。
    """
    _dispatch(repository, run_id, at=SCOUT_AT, mission=MISSION_KIND_SCOUT, preset="侦察")
    attack_id = _dispatch(repository, run_id, at=ATTACK_AT)

    row = _row(repository, _append(repository))

    assert row.dispatch_id == attack_id
    assert row.match_status == "MATCHED"


def test_two_attack_legs_are_still_ambiguous(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 改的是「谁有资格当候选」，**不是「多个候选时挑一个」**。

    真有两发攻击都对得上时照旧记 `AMBIGUOUS`——认错一发比认不上更糟：
    攻击日志会把战果挂到没打过的那一发头上，而没人会回头核。
    """
    _dispatch(repository, run_id, at=ATTACK_AT - timedelta(minutes=10))
    _dispatch(repository, run_id, at=ATTACK_AT)

    row = _row(repository, _append(repository))

    assert row.dispatch_id is None
    assert row.match_status == "AMBIGUOUS"


def test_a_lone_scout_leg_matches_nothing(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """只有侦察发时，这份战报**一发都不认领**，不能退而求其次挂到侦察上。

    挂上去等于宣称「那发探测器打了一仗」，而它同时把一发真的攻击的位置占掉了。
    """
    _dispatch(repository, run_id, at=SCOUT_AT, mission=MISSION_KIND_SCOUT, preset="侦察")

    row = _row(repository, _append(repository))

    assert row.dispatch_id is None
    assert row.match_status == "UNMATCHED"


# -- 回头重认 ----------------------------------------------------------------


def test_a_stuck_report_is_relinked_once_the_rule_is_fixed(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """库里那些 `AMBIGUOUS` 的旧行要能被重新认领。

    这里先造出实机那个局面（侦察发当候选 → `AMBIGUOUS`），再按现在的判据重认。
    没有这条路，判据修好之后那四发照样永远是「待战报」——因为 `has_report_at`
    保证了它们再也不会被读一遍。
    """
    scout_id = _dispatch(repository, run_id, at=SCOUT_AT, mission=MISSION_KIND_SCOUT, preset="侦察")
    attack_id = _dispatch(repository, run_id, at=ATTACK_AT)
    report_id = _append(repository)
    # 把库倒回「侦察发也算候选」的年代：那时这一行是 AMBIGUOUS、dispatch_id 空。
    with repository._session_factory() as session:  # noqa: SLF001 - 造历史现场
        row = session.get(orm.BattleReportRow, report_id)
        assert row is not None
        row.dispatch_id = None
        row.match_status = "AMBIGUOUS"
        row.match_confidence = 0.0
        session.commit()

    assert repository.rematch_report_at(TARGET, REPORTED_AT) is True

    row = _row(repository, report_id)
    assert row.dispatch_id == attack_id
    assert row.match_status == "MATCHED"
    assert row.dispatch_id != scout_id


def test_rematching_leaves_an_already_linked_report_alone(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **只碰 `dispatch_id` 为空的行。**

    已经认领上的不重算：那会把一次判据变动变成一次静默的改档，而 `dispatch_id`
    上有唯一约束——重算过程中一旦算错，原本对的那一发也一起丢了。
    """
    attack_id = _dispatch(repository, run_id, at=ATTACK_AT)
    report_id = _append(repository)

    assert repository.rematch_report_at(TARGET, REPORTED_AT) is False

    row = _row(repository, report_id)
    assert row.dispatch_id == attack_id
    # `match_status` 也要原样留住：那一发已经被这份战报认领，所以它不再是
    # 自己的候选，重算一遍只会得出「零个候选」——`MATCHED` 会被改写成
    # `UNMATCHED`，而 `dispatch_id` 还挂着。攻击日志从此显示成一份没认上的战报。
    assert row.match_status == "MATCHED"


def test_rematching_never_invents_a_dispatch(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """认不上就认不上，**一行派遣都不许补**。

    库里多一条不存在的派遣，调度器就会以为一条航线被占着、并等一份永远不来的
    战报，要到 `MAX_REPORT_AGE`（6 小时）才被判缺失清掉。
    """
    report_id = _append(repository)

    assert repository.rematch_report_at(TARGET, REPORTED_AT) is False

    with repository._session_factory() as session:  # noqa: SLF001 - 直接数行
        assert session.query(orm.AttackDispatchRow).count() == 0
    assert _row(repository, report_id).match_status == "UNMATCHED"


# -- 只读地问「库里那一行认领上了吗」 ----------------------------------------


def test_report_claims_at_names_the_row_and_the_leg_it_claimed(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **本节的重点。** 去重挡下一封时，要能说出被挡下的是哪一行、它认了谁。

    实机（生产库 2026-08-17）：日志只有一句「这份战报已经在库里；不重复入库」，
    而攻击日志页上 4:480:6 还挂着「待战报」——看上去自相矛盾。库里那一行的
    `match_status` 才是答案，而当时没有任何一条路把它说出来。
    """
    attack_id = _dispatch(repository, run_id, at=ATTACK_AT)
    report_id = _append(repository)

    (claim,) = repository.report_claims_at(TARGET, REPORTED_AT)

    assert claim.report_id == report_id
    assert claim.match_status == "MATCHED"
    assert claim.dispatch_id == attack_id
    # 派出时刻一并带出来：排障要对的是它与页面上那一行对不对得上，
    # 光有 UUID 还得再查一次库。
    assert claim.dispatched_at_utc == ATTACK_AT


def test_report_claims_at_shows_an_unclaimed_row_as_such(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """没认领上的那一行要看得出来——**这才是 4:480:6 那天的真实形状**。

    战报在库里、却不属于页面上任何一发派遣，于是它的战果永远不会出现在攻击
    日志的战果列上。这不是矛盾，是两次不同的攻击；说不出这一句就只能靠猜。
    """
    _append(repository)

    (claim,) = repository.report_claims_at(TARGET, REPORTED_AT)

    assert claim.match_status == "UNMATCHED"
    assert claim.dispatch_id is None
    assert claim.dispatched_at_utc is None


def test_report_claims_at_writes_nothing(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 这条路**只读**。与 `rematch_report_at` 分家正是为了这一点：

    排障可以随便问，不必担心问一次就改一次库。
    """
    _dispatch(repository, run_id, at=SCOUT_AT, mission=MISSION_KIND_SCOUT, preset="侦察")
    report_id = _append(repository)
    before = _row(repository, report_id).match_status

    repository.report_claims_at(TARGET, REPORTED_AT)

    assert _row(repository, report_id).match_status == before


def test_report_claims_at_is_empty_for_a_moment_nothing_was_stored_at(repository) -> None:  # type: ignore[no-untyped-def]
    """键上没有行就返回空——调用方据此说「查不到」，而不是编一句话出来。"""
    _append(repository)

    assert repository.report_claims_at(TARGET, REPORTED_AT + timedelta(seconds=1)) == ()
