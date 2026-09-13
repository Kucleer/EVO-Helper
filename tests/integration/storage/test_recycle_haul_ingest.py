"""回收实收的入库与认领。

⚠️ 这里守的是**反过来的认领方向**：攻击战报自带出发点与目标、拿它们去找派遣；
回收报告的坐标 OCR 会读错（实测 `[1:55:6]` 读成 `[1:55:5]`），所以整条链路
不读坐标，只拿抵达时刻去找那一发派遣，**坐标从认下来的那一发抄过来**。

⚠️⚠️ 另一条要守的是「不唯一就不认」：生产库 169 发回收派遣里，相邻预计抵达
时刻有 9.5% 落在 60 秒以内。认错的代价是一整趟资源记到别人头上，而认不上的代价
只是页面上多停一会儿「待回收」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.battle_outcome import OUTCOME_RECYCLE
from evo_helper.domain.models import Coordinate
from evo_helper.domain.quantities import Quantity
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_RECYCLE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.recycle_mail_screen import RecycleMailReading

ORIGIN = Coordinate(1, 55, 6)
TARGET = Coordinate(1, 27, 19)
ARRIVED = datetime(2026, 9, 12, 3, 26, 46, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="回收船:全部")

#: 实拍语料 `recycle-12092026-032646.png` 读出来的那一封：273 艘 × 20K ≈ 5.46M。
AMOUNTS = (
    Quantity(Decimal(3_070_000), approximate=True, uncertainty=5_000),
    Quantity(Decimal(2_100_000), approximate=True, uncertainty=50_000),
    Quantity(Decimal(280_250), approximate=True, uncertainty=50),
)


def _reading(arrived: datetime = ARRIVED) -> RecycleMailReading:
    return RecycleMailReading(
        raw_time_text="12/09/2026 03:26:46",
        reported_at_utc=arrived,
        amounts=AMOUNTS,
        ships=273,
    )


def _dispatch(
    repository: SqlAlchemyRepository,
    session_factory: sessionmaker[Session],
    run_id: object,
    *,
    target: Coordinate,
    expected_at: datetime,
    kind: str = MISSION_KIND_RECYCLE,
) -> AttackDispatch:
    """派一发出去，并把预计抵达时刻补上（那一列只有 `record_flight_time` 会写）。"""
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=target,
        preset=PRESET,
        cycle_start_utc=expected_at - timedelta(hours=1),
        created_at_utc=expected_at - timedelta(minutes=30),
    )
    repository.save_attack_intent(intent)
    dispatch = AttackDispatch(
        dispatch_id=uuid4(),
        intent_id=intent.intent_id,
        dispatched_at_utc=expected_at - timedelta(minutes=20),
        accepted=True,
        mission_kind=kind,
    )
    repository.save_dispatch(dispatch)
    with session_factory() as session:
        row = session.get(orm.AttackDispatchRow, dispatch.dispatch_id)
        assert row is not None
        row.expected_report_at_utc = expected_at
        session.commit()
    return dispatch


def _reports(session_factory: sessionmaker[Session]) -> list[orm.BattleReportRow]:
    with session_factory() as session:
        return list(session.scalars(select(orm.BattleReportRow)))


class TestClaimingTheDispatch:
    def test_the_coordinates_come_from_the_dispatch_not_from_the_mail(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """⚠️ **这是这条链路的全部要点。**

        `RecycleMailReading` 里**根本没有坐标字段** —— 读出来的坐标会错，所以
        压根不读。落库那一行的出发点与目标只可能来自被认下来的那一发派遣。
        """
        dispatch = _dispatch(
            repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED
        )

        claimed = repository.append_recycle_report(_reading(), report_id=uuid4())

        assert claimed == dispatch.dispatch_id
        (row,) = _reports(session_factory)
        assert (row.attacker_origin_galaxy, row.attacker_origin_system) == (
            ORIGIN.galaxy,
            ORIGIN.system,
        )
        assert (
            row.defender_target_galaxy,
            row.defender_target_system,
            row.defender_target_position,
        ) == (TARGET.galaxy, TARGET.system, TARGET.position)
        assert row.outcome == OUTCOME_RECYCLE
        assert row.dispatch_id == dispatch.dispatch_id

    def test_the_three_resources_land_with_their_precision_marks(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """画面上写的是 `3.07M` 这种缩写，`approximate` 与误差要一路带进库。"""
        _dispatch(repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED)

        repository.append_recycle_report(_reading(), report_id=uuid4())

        with session_factory() as session:
            rows = list(
                session.scalars(
                    select(orm.BattleReportResourceRow).order_by(orm.BattleReportResourceRow.slot)
                )
            )
        assert [(row.slot, row.amount, row.approximate) for row in rows] == [
            (0, 3_070_000, True),
            (1, 2_100_000, True),
            (2, 280_250, True),
        ]

    def test_a_recycle_report_carries_no_battle_numbers_at_all(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """⚠️ 回收没有战斗，战损与参战舰队**不存在**，不是没读到。

        填 0 会让这一趟变成「打了一仗零战损」，直接污染战斗统计。
        """
        _dispatch(repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED)

        repository.append_recycle_report(_reading(), report_id=uuid4())

        (row,) = _reports(session_factory)
        assert row.attacker_losses is None
        assert row.defender_losses is None
        assert row.attacker_units is None
        with session_factory() as session:
            assert list(session.scalars(select(orm.FleetSnapshotRow))) == []


class TestRefusingToGuess:
    def test_two_recycles_arriving_together_are_both_left_alone(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """⚠️ 生产实测 9.5% 的相邻回收预计抵达落在 60 秒以内（最小 0 秒）。

        认错的代价是一整趟资源记到别人头上；认不上只是这一封下一趟再读。
        """
        _dispatch(repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED)
        _dispatch(
            repository,
            session_factory,
            run_id,
            target=Coordinate(1, 27, 20),
            expected_at=ARRIVED + timedelta(seconds=30),
        )

        claimed = repository.append_recycle_report(_reading(), report_id=uuid4())

        assert claimed is None
        assert _reports(session_factory) == []

    def test_nothing_in_the_window_writes_nothing(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """窗口外的那一发不算候选，**也不许退而求其次认最近的一发**。"""
        _dispatch(
            repository,
            session_factory,
            run_id,
            target=TARGET,
            expected_at=ARRIVED + timedelta(hours=3),
        )

        assert repository.append_recycle_report(_reading(), report_id=uuid4()) is None
        assert _reports(session_factory) == []

    def test_an_attack_dispatch_in_the_window_is_not_a_candidate(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """⚠️ 攻击发同一时刻抵达是常事，**它不产出回收报告**。

        把它算进候选会让「唯一」这条判据在最常见的情形下失效，
        整条链路退化成「几乎从不认领」——而那看起来和「邮件没读到」一模一样。
        """
        recycle = _dispatch(repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED)
        _dispatch(
            repository,
            session_factory,
            run_id,
            target=Coordinate(4, 44, 10),
            expected_at=ARRIVED + timedelta(seconds=5),
            kind=MISSION_KIND_ATTACK,
        )

        assert (
            repository.append_recycle_report(_reading(), report_id=uuid4()) == recycle.dispatch_id
        )

    def test_a_dispatch_that_already_has_its_haul_is_not_claimed_twice(
        self,
        repository: SqlAlchemyRepository,
        session_factory: sessionmaker[Session],
        run_id: object,
    ) -> None:
        """同一发只有一趟实收。重复认领会把两封不同的信记到同一发上。"""
        _dispatch(repository, session_factory, run_id, target=TARGET, expected_at=ARRIVED)

        first = repository.append_recycle_report(_reading(), report_id=uuid4())
        second = repository.append_recycle_report(_reading(), report_id=uuid4())

        assert first is not None
        assert second is None
        assert len(_reports(session_factory)) == 1
