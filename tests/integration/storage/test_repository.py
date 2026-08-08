from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
    FleetSnapshotEntry,
    StateEvent,
    TargetRevisit,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import (
    AttackIntentRow,
    BattleReportRow,
    BotTargetRow,
    CoordinateScanRow,
    FleetSnapshotRow,
    RunInstance,
    ScanPlan,
    ScanRangeRow,
    TargetRevisitRow,
)
from evo_helper.storage.repository import SqlAlchemyRepository, StorageConflictError


@pytest.fixture
def engine(tmp_path: Path):
    engine = create_database_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
def repository(session_factory):
    return SqlAlchemyRepository(session_factory)


def _seed_run(
    session_factory: sessionmaker[Session],
    *,
    ranges: tuple[tuple[Coordinate, Coordinate], ...] = (
        (Coordinate(1, 1, 1), Coordinate(1, 1, 2)),
    ),
    plan_name: str = "plan-a",
    idempotency_key: str = "start-001",
) -> tuple[int, UUID]:
    with session_factory() as session:
        plan = ScanPlan(name=plan_name, created_at_utc=datetime(2026, 8, 6, 0, 0, tzinfo=UTC))
        session.add(plan)
        session.flush()
        for priority, (start, end) in enumerate(ranges):
            session.add(
                ScanRangeRow(
                    plan_id=plan.id,
                    start_galaxy=start.galaxy,
                    start_system=start.system,
                    start_position=start.position,
                    end_galaxy=end.galaxy,
                    end_system=end.system,
                    end_position=end.position,
                    origin_galaxy=1,
                    origin_system=1,
                    origin_position=1,
                    fleet_preset_name="fleet-a",
                    fleet_preset_signature="sig-a",
                    priority=priority,
                )
            )
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key=idempotency_key,
            state="SCANNING",
            created_at_utc=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
        )
        session.add(run)
        session.commit()
        return plan.id, run.id


def _intent(
    run_id: UUID, *, target: Coordinate = Coordinate(2, 2, 2), **overrides: object
) -> AttackIntent:
    values: dict[str, object] = dict(
        intent_id=uuid4(),
        run_id=run_id,
        origin=Coordinate(1, 1, 1),
        target=target,
        preset=FleetPresetRef(name="fleet-a", signature="sig-a"),
        cycle_start_utc=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        created_at_utc=datetime(2026, 8, 6, 0, 0, tzinfo=UTC),
    )
    values.update(overrides)
    return AttackIntent(**values)


def _report(
    *,
    reported_at: datetime,
    fleet: tuple[FleetSnapshotEntry, ...] = (),
    **overrides: object,
) -> BattleReport:
    values: dict[str, object] = dict(
        report_id=uuid4(),
        reported_at_utc=reported_at,
        attacker_origin=Coordinate(1, 1, 1),
        defender_target=Coordinate(2, 2, 2),
        fleet=fleet,
    )
    values.update(overrides)
    return BattleReport(**values)


def test_sqlite_pragmas_are_enabled(engine) -> None:
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert journal_mode == "wal"
    assert foreign_keys == 1


def test_claim_next_coordinate_walks_ranges_in_priority_order(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(
        session_factory,
        ranges=(
            (Coordinate(1, 1, 1), Coordinate(1, 1, 2)),
            (Coordinate(1, 1, 5), Coordinate(1, 1, 5)),
        ),
    )

    claims = []
    for _ in range(3):
        claim = repository.claim_next_coordinate(run_id)
        assert claim is not None
        claims.append(claim)
        repository.complete_coordinate(run_id, claim.coordinate)

    assert [claim.coordinate for claim in claims if claim is not None] == [
        Coordinate(1, 1, 1),
        Coordinate(1, 1, 2),
        Coordinate(1, 1, 5),
    ]
    assert repository.claim_next_coordinate(run_id) is None


def test_pending_coordinate_is_retried_until_completed(session_factory, repository) -> None:
    _plan_id, run_id = _seed_run(session_factory)

    first = repository.claim_next_coordinate(run_id)
    second = repository.claim_next_coordinate(run_id)

    assert first is not None and second is not None
    assert second.coordinate == first.coordinate
    repository.complete_coordinate(run_id, first.coordinate)
    advanced = repository.claim_next_coordinate(run_id)
    assert advanced is not None
    assert advanced.coordinate > first.coordinate


def test_cursor_carries_into_the_next_system_at_the_range_position_limit(
    session_factory,
    repository,
) -> None:
    """位数窗口来自区间本身（这里 5–7），不是 `POSITION_LIMIT` 的 499。

    499 是每银河系的**恒星系数**。拿它当位数上限，游标会在每个恒星系里空转
    几百个不存在的行星位——扫描看起来一直在跑，实际一个新坐标都没到。
    """
    _plan_id, run_id = _seed_run(
        session_factory,
        ranges=((Coordinate(2, 121, 5), Coordinate(2, 122, 7)),),
    )

    claimed = []
    for _ in range(6):
        claim = repository.claim_next_coordinate(run_id)
        assert claim is not None
        claimed.append(claim.coordinate)
        repository.complete_coordinate(run_id, claim.coordinate)

    assert claimed == [
        Coordinate(2, 121, 5),
        Coordinate(2, 121, 6),
        Coordinate(2, 121, 7),
        # 进位落到区间起始位 5，而不是海盗位 1。
        Coordinate(2, 122, 5),
        Coordinate(2, 122, 6),
        Coordinate(2, 122, 7),
    ]
    assert repository.claim_next_coordinate(run_id) is None


def test_claim_unknown_run_raises(repository) -> None:
    with pytest.raises(ValueError, match="unknown run"):
        repository.claim_next_coordinate(uuid4())


def test_save_scan_appends_and_updates_target_aggregate(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    coordinate = Coordinate(2, 2, 2)
    first = CoordinateScan(
        run_id=run_id,
        coordinate=coordinate,
        scanned_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        owner_name="player_one",
        is_bot=False,
        confidence=0.9,
    )
    second = CoordinateScan(
        run_id=run_id,
        coordinate=coordinate,
        scanned_at_utc=datetime(2026, 8, 6, 1, 5, tzinfo=UTC),
        owner_name="bot_alpha",
        is_bot=True,
        confidence=0.99,
    )

    repository.save_scan(first)
    repository.save_scan(second)

    with session_factory() as session:
        scan_rows = session.scalars(
            select(CoordinateScanRow).where(CoordinateScanRow.run_id == run_id)
        ).all()
        target = session.scalar(select(BotTargetRow))
    assert len(scan_rows) == 2
    assert target is not None
    assert target.latest_owner_name == "bot_alpha"
    assert target.is_bot is True
    assert target.last_scanned_at_utc == datetime(2026, 8, 6, 1, 5, tzinfo=UTC)


def test_save_scan_rejects_naive_timestamp(repository, session_factory) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    scan = CoordinateScan(
        run_id=run_id,
        coordinate=Coordinate(1, 1, 1),
        scanned_at_utc=datetime(2026, 8, 6, 1, 0),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.save_scan(scan)


def test_duplicate_attack_intent_rejected_but_forced_revisit_allowed(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    intent = _intent(run_id)
    repository.save_attack_intent(intent)

    with pytest.raises(StorageConflictError, match="duplicate"):
        repository.save_attack_intent(_intent(run_id))

    forced = _intent(run_id, forced_revisit=True)
    repository.save_attack_intent(forced)
    with session_factory() as session:
        rows = session.scalars(
            select(AttackIntentRow).where(AttackIntentRow.run_id == run_id)
        ).all()
    assert len(rows) == 2


def test_dispatch_requires_known_intent(repository) -> None:
    dispatch = AttackDispatch(
        dispatch_id=uuid4(),
        intent_id=uuid4(),
        dispatched_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        dry_run=True,
        accepted=True,
    )
    with pytest.raises(ValueError, match="unknown attack intent"):
        repository.save_dispatch(dispatch)


def test_report_strict_matching_closes_dispatch_once(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    intent = _intent(run_id)
    repository.save_attack_intent(intent)
    dispatched_at = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched_at,
            dry_run=False,
            accepted=True,
        )
    )

    first_report = _report(
        reported_at=dispatched_at + timedelta(minutes=5),
        fleet=(
            FleetSnapshotEntry(side="defender", ship_type="light fighter", count=10),
            FleetSnapshotEntry(side="attacker", ship_type="recycler", count=600),
        ),
    )
    repository.append_report(first_report)
    second_report = _report(reported_at=dispatched_at + timedelta(hours=1))
    repository.append_report(second_report)

    with session_factory() as session:
        matched = session.scalars(
            select(BattleReportRow).where(BattleReportRow.match_status == "MATCHED")
        ).all()
        unmatched = session.scalars(
            select(BattleReportRow).where(BattleReportRow.match_status == "UNMATCHED")
        ).all()
        linked_dispatches = session.scalars(
            select(BattleReportRow.dispatch_id).where(BattleReportRow.dispatch_id.is_not(None))
        ).all()
        snapshots = session.scalars(select(FleetSnapshotRow)).all()
    assert len(matched) == 1
    assert matched[0].dispatch_id is not None
    assert len(unmatched) == 1
    assert len(linked_dispatches) == 1
    assert len(snapshots) == 2


def test_report_outside_time_tolerance_is_unmatched(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    intent = _intent(run_id)
    repository.save_attack_intent(intent)
    dispatched_at = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched_at,
            dry_run=False,
            accepted=True,
        )
    )

    repository.append_report(_report(reported_at=dispatched_at + timedelta(hours=13)))

    with session_factory() as session:
        report = session.scalar(select(BattleReportRow))
    assert report is not None
    assert report.match_status == "UNMATCHED"
    assert report.dispatch_id is None


def test_report_never_matches_a_dry_run_dispatch(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    intent = _intent(run_id)
    repository.save_attack_intent(intent)
    dispatched_at = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched_at,
            dry_run=True,
            accepted=False,
        )
    )

    repository.append_report(_report(reported_at=dispatched_at + timedelta(minutes=5)))

    with session_factory() as session:
        report = session.scalar(select(BattleReportRow))
    assert report is not None
    assert report.match_status == "UNMATCHED"
    assert report.dispatch_id is None


def test_fleet_diff_computes_statuses_and_first_seen(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    intent = _intent(run_id)
    repository.save_attack_intent(intent)
    dispatched_at = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched_at,
            dry_run=False,
            accepted=True,
        )
    )
    first = _report(
        reported_at=dispatched_at + timedelta(minutes=5),
        fleet=(
            FleetSnapshotEntry(side="defender", ship_type="fighter", count=10),
            FleetSnapshotEntry(side="defender", ship_type="cruiser", count=5),
            FleetSnapshotEntry(side="defender", ship_type="destroyer", count=3),
        ),
    )
    second = _report(
        reported_at=dispatched_at + timedelta(hours=2),
        fleet=(
            FleetSnapshotEntry(side="defender", ship_type="fighter", count=12),
            FleetSnapshotEntry(side="defender", ship_type="battleship", count=2),
        ),
        is_from_revisit=True,
        match_confidence=0.97,
        manual_review_status="REVIEWED",
    )
    repository.append_report(first)
    repository.append_report(second)

    diff = repository.fleet_diff(after_report_id=second.report_id, before_report_id=first.report_id)

    by_type = {ship.ship_type: ship for ship in diff.ships}
    assert diff.total_before == 18
    assert diff.total_after == 14
    assert diff.total_change == -4
    assert diff.is_from_revisit is True
    assert diff.match_confidence == 0.97
    assert diff.manual_review_status == "REVIEWED"
    assert by_type["fighter"].status == "INCREASED"
    assert by_type["fighter"].absolute_change == 2
    assert by_type["fighter"].percent_change == 20.0
    assert by_type["fighter"].first_seen is False
    assert by_type["cruiser"].status == "REMOVED"
    assert by_type["cruiser"].percent_change == -100.0
    assert by_type["destroyer"].status == "REMOVED"
    assert by_type["battleship"].status == "ADDED"
    assert by_type["battleship"].first_seen is True
    assert by_type["battleship"].percent_change is None


def test_fleet_diff_without_baseline_marks_everything_added(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    report = _report(
        reported_at=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        fleet=(FleetSnapshotEntry(side="defender", ship_type="fighter", count=7),),
    )
    repository.append_report(report)

    diff = repository.fleet_diff(after_report_id=report.report_id)

    assert diff.before_report_id is None
    assert diff.total_before == 0
    assert diff.total_after == 7
    assert diff.ships[0].status == "ADDED"
    assert diff.ships[0].first_seen is True


def test_history_is_append_only_and_ordered(
    session_factory,
    repository,
) -> None:
    _plan_id, run_id = _seed_run(session_factory)
    target = Coordinate(2, 2, 2)
    earlier = _report(
        reported_at=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        fleet=(FleetSnapshotEntry(side="defender", ship_type="fighter", count=5),),
    )
    later = _report(
        reported_at=datetime(2026, 8, 6, 2, 0, tzinfo=UTC),
        fleet=(FleetSnapshotEntry(side="defender", ship_type="battleship", count=1),),
    )
    repository.append_report(earlier)
    repository.append_report(later)

    history = repository.history_for_coordinate(target)

    assert [entry.report_id for entry in history] == [earlier.report_id, later.report_id]
    assert [entry.reported_at_utc for entry in history] == sorted(
        entry.reported_at_utc for entry in history
    )
    assert history[1].is_from_revisit is False


def test_state_events_append_and_replay(session_factory, repository) -> None:
    run_id = uuid4()
    repository.append_state_event(
        StateEvent(
            aggregate_type="run",
            aggregate_id=run_id,
            event="start",
            before_state="DRAFT",
            after_state="SCANNING",
            occurred_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
        )
    )
    repository.append_state_event(
        StateEvent(
            aggregate_type="run",
            aggregate_id=run_id,
            event="drain",
            before_state="SCANNING",
            after_state="DRAINING",
            occurred_at_utc=datetime(2026, 8, 6, 2, 0, tzinfo=UTC),
        )
    )

    events = repository.state_events_for("run", run_id)

    assert [event.event for event in events] == ["start", "drain"]
    assert events[0].occurred_at_utc.tzinfo == UTC


def test_target_revisit_round_trip(session_factory, repository) -> None:
    revisit = TargetRevisit(
        revisit_id=uuid4(),
        scope="target",
        reason="manual re-check",
        target=Coordinate(2, 2, 2),
        requested_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
    )
    repository.add_target_revisit(revisit)

    with session_factory() as session:
        rows = session.scalars(select(TargetRevisitRow)).all()
    assert len(rows) == 1
    assert rows[0].target_galaxy == 2
    assert rows[0].status == "PENDING"


def test_foreign_keys_are_enforced(session_factory) -> None:
    with session_factory() as session:
        session.add(
            CoordinateScanRow(
                run_id=uuid4(),
                galaxy=1,
                system=1,
                position=1,
                scanned_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_idempotency_key_is_unique(session_factory) -> None:
    _plan_id, _run_id = _seed_run(session_factory)
    with pytest.raises(IntegrityError):
        _seed_run(session_factory, plan_name="plan-b")
