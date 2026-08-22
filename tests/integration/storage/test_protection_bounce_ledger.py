"""到达时撞保护期的那一发怎么结账：`repository.record_protection_bounce`。

## 这一发结构上永远不会有战报

舰队飞到了才发现目标在保护期，原路返航——游戏不产出战报。于是在这条判据上线
之前，它会**永久**沉在「未读回」里：回收率被往下拽、`protection_seen_at_utc`
不写所以那个坐标下一轮又被挑中、而白占掉的一整趟往返在账上和「战报还没回来」
长得一模一样。

## 为什么用 `battle_reports` 结账

「未读回」全仓只有一个判据：`battle_reports.dispatch_id` 指没指着这一发
（`pending_reports_for_kind`、`storage.overview.unread_reports`、
`storage.origin_efficiency._counts`、`bot_dispatch_facts` 都是同一个 LEFT JOIN）。
写下这一行，四处同时结清。

⚠️ 但它**不是「成功但收获为 0」**：`outcome` 是独立的第四档，收获一行都不写。
`test_no_resource_rows_are_written` 守的就是这一条——收益统计从
`battle_report_resources` 往外联表，没有行就是压根不参与，不会被当成一个 0。

夹具坐标全是编的：真实那两个不进公开仓库。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.battle_outcome import OUTCOME_PROTECTED
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    TARGET_KIND_BOT,
    AttackDispatch,
    AttackIntent,
    RankingTarget,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(4, 321, 9)

#: 派出、单程、到达。到达那一刻**就是**邮件时刻（生产实测两发差 0 秒与 1 秒）。
DISPATCHED_AT = datetime(2026, 8, 20, 13, 27, 26, tzinfo=UTC)
ONE_WAY = timedelta(seconds=3724)
MAIL_AT = DISPATCHED_AT + ONE_WAY + timedelta(seconds=1)

#: 「我们翻到这封信的时刻」——只用来证明写进库的是**邮件时刻**而不是它。
PROCESSED_AT = MAIL_AT + timedelta(hours=3)

#: 问「还有几发在等战报」时站的那个时刻。
#:
#: ⚠️ 必须离抵达够近：`pending_reports_for_kind` 会把 `expected_report_at_utc`
#: 已经过去太久的那些整条剔掉（判成「战报永远不会来」），站在三小时之后去问，
#: 会得到一张空单子——那样这几条用例就什么都没在守。
CHECKED_AT = MAIL_AT + timedelta(minutes=5)


def _known_target(repository: SqlAlchemyRepository, coordinate: Coordinate = TARGET) -> None:
    """先让 `bot_targets` 里有这一行：`note_protection_period` 只更新、绝不插新行。"""
    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=coordinate,
                military_score=20960.0,
                military_score_at_utc=DISPATCHED_AT,
            )
        ]
    )


def _dispatch(
    repository: SqlAlchemyRepository,
    run_id: UUID,
    *,
    at: datetime = DISPATCHED_AT,
    flight: timedelta | None = ONE_WAY,
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
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=at,
            accepted=True,
            mission_kind=MISSION_KIND_ATTACK,
        )
    )
    if flight is not None:
        repository.record_flight_time(dispatch_id, flight, at)
    return dispatch_id


def _still_open(repository: SqlAlchemyRepository, *, now: datetime = CHECKED_AT) -> list[str]:
    """调度器眼里「还在等战报」的那几发。**结掉了就不该再出现在这里。**"""
    pending = repository.pending_reports_for_kind(
        TARGET_KIND_BOT,
        now_utc=now,
        grace=timedelta(minutes=10),
        max_age=MAX_REPORT_AGE,
        origin=ORIGIN,
    )
    return [str(item.dispatch_id) for item in pending if not item.closed]


def test_the_protection_period_is_recorded_at_the_mail_time_not_now(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ 写的是**邮件时刻**（= 到达那一刻），不是我们翻到它的时刻。

    写成处理时刻会把保护期的起点往后推——信箱是隔一阵才翻一次的，这里差了三个
    小时，那个坐标于是被多排除三个小时。
    """
    _known_target(repository)
    _dispatch(repository, run_id)

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.protection_noted is True
    with session_factory() as session:
        row = session.scalar(
            select(orm.BotTargetRow).where(orm.BotTargetRow.galaxy == TARGET.galaxy)
        )
    assert row is not None
    assert row.protection_seen_at_utc == MAIL_AT
    assert row.protection_seen_at_utc != PROCESSED_AT


def test_the_dispatch_stops_counting_as_awaiting_a_report(
    repository: SqlAlchemyRepository, run_id: UUID
) -> None:
    _known_target(repository)
    dispatch_id = _dispatch(repository, run_id)
    assert _still_open(repository) == [str(dispatch_id)]

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.closed is True
    assert outcome.dispatch_id == dispatch_id
    assert _still_open(repository) == []


def test_the_row_is_a_protected_outcome_not_a_zero_gain_victory(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ 「打了但没打成」，不是「成功但收获为 0」，也不是「战报丢了」。"""
    _known_target(repository)
    dispatch_id = _dispatch(repository, run_id)

    repository.record_protection_bounce(
        TARGET, mail_at_utc=MAIL_AT, raw_time_text="20/08/2026 14:29:32"
    )

    with session_factory() as session:
        report = session.scalar(select(orm.BattleReportRow))
    assert report is not None
    assert report.outcome == OUTCOME_PROTECTED
    assert report.dispatch_id == dispatch_id
    assert report.match_status == "MATCHED"
    assert report.reported_at_utc == MAIL_AT
    assert report.raw_time_text == "20/08/2026 14:29:32"
    # 没有战斗，所以这些数**不存在**——不是没读到，更不是 0。
    assert report.attacker_units is None
    assert report.defender_units is None
    assert report.attacker_losses is None
    assert report.defender_losses is None


def test_no_resource_rows_are_written(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """收益统计从 `battle_report_resources` 往外联表；一行都没有 = 压根不参与。

    写 12 个 0 会让这一发变成「打赢了但一无所获」，直接把每线小时的稀有产出拉低。
    """
    _known_target(repository)
    _dispatch(repository, run_id)

    repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    with session_factory() as session:
        resources = session.scalars(select(orm.BattleReportResourceRow)).all()
        fleet = session.scalars(select(orm.FleetSnapshotRow)).all()
    assert resources == []
    assert fleet == []


def test_reading_the_same_mail_twice_does_not_duplicate_the_row(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """信箱每趟都会翻到同样那几行；第二趟不许再写一条。"""
    _known_target(repository)
    _dispatch(repository, run_id)

    first = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)
    second = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert first.already_recorded is False
    assert second.already_recorded is True
    assert second.closed is True
    with session_factory() as session:
        assert len(session.scalars(select(orm.BattleReportRow)).all()) == 1


def test_an_unidentifiable_dispatch_still_records_the_protection_period(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """认不出是哪一发时**只记保护期、不写战报行**。

    `battle_reports.attacker_origin_*` 非空，而这封通知里根本没有出发点——它只写了
    目标。凭猜写下去等于把这一发的账挂到别人头上，而那一发从此再也不会被认领。
    邮件本身仍然是「这个坐标那一刻在保护期」的完整证据，所以那一半照做。
    """
    _known_target(repository)
    # 飞行时长没读到 → `expected_report_at_utc` 为 NULL → 抵达窗口无从谈起。
    dispatch_id = _dispatch(repository, run_id, flight=None)

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.protection_noted is True
    assert outcome.closed is False
    assert outcome.dispatch_id is None
    assert outcome.unmatched_candidates == 1
    assert outcome.ambiguous_arrivals == 0
    with session_factory() as session:
        assert session.scalars(select(orm.BattleReportRow)).all() == []
    # 说实话的那一档：这一发**仍然**算未读回，日志据此把话说清楚。
    assert _still_open(repository) == [str(dispatch_id)]


def test_two_dispatches_arriving_in_the_same_window_are_not_guessed_between(
    repository: SqlAlchemyRepository, run_id: UUID, session_factory: sessionmaker[Session]
) -> None:
    """两发同时抵达就分不开；不猜。"""
    _known_target(repository)
    _dispatch(repository, run_id)
    _dispatch(repository, run_id, at=DISPATCHED_AT + timedelta(seconds=30))

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.closed is False
    assert outcome.ambiguous_arrivals == 2
    with session_factory() as session:
        assert session.scalars(select(orm.BattleReportRow)).all() == []


def test_a_coordinate_with_no_bot_target_row_is_reported_honestly(
    repository: SqlAlchemyRepository, run_id: UUID
) -> None:
    """`bot_targets` 里没有这一行时**不插新行**（同 `note_protection_period`）。

    但那一发照样结掉——两件事各自成败，日志分开说。
    """
    dispatch_id = _dispatch(repository, run_id)

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.protection_noted is False
    assert outcome.closed is True
    assert outcome.dispatch_id == dispatch_id


def test_the_military_score_travels_with_the_outcome_for_the_log(
    repository: SqlAlchemyRepository, run_id: UUID
) -> None:
    """排障第一个问题是「这个白跑的目标值不值得救」。"""
    _known_target(repository)
    _dispatch(repository, run_id)

    outcome = repository.record_protection_bounce(TARGET, mail_at_utc=MAIL_AT)

    assert outcome.military_score == 20960.0
    assert outcome.flight_seconds == int(ONE_WAY.total_seconds())
    assert outcome.dispatched_at_utc == DISPATCHED_AT
