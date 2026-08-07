"""派出之后助手松手：等待状态必须完全靠数据库恢复。

用户会在助手派出舰队后切换登录去玩，助手不持有会话、进程可以整个退出。
所以「现在该等还是该收」只能从持久化的时间算出来，不能依赖任何内存状态。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from evo_helper.domain.models import Coordinate, FleetPresetRef, RunState
from evo_helper.domain.records import AttackDispatch, AttackIntent
from evo_helper.domain.report_wait import ReportWaitPlanner, WaitAction
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView

DISPATCHED = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
FLIGHT = timedelta(hours=3, minutes=20)
# 周期从周一 00:00 UTC 开始。
CYCLE_START = datetime(2026, 8, 3, tzinfo=UTC)


def _plan_and_run(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="wait",
        enabled=True,
        window_start=time(8),
        window_end=time(10),
        dry_run=True,
        ranges=(
            ScanRangeView(
                Coordinate(1, 100, 1),
                Coordinate(1, 200, 15),
                Coordinate(2, 137, 18),
                "探路",
                "轻型战斗机:1",
                0,
            ),
        ),
    )
    run = service.start_run(plan.id, f"idem-{name}")
    return engine, factory, run


def _real_dispatch(repo: SqlAlchemyRepository, run_id, target: Coordinate):  # type: ignore[no-untyped-def]
    intent_id, dispatch_id = uuid4(), uuid4()
    repo.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=target,
            preset=FleetPresetRef(name="探路", signature="轻型战斗机:1"),
            cycle_start_utc=CYCLE_START,
            created_at_utc=DISPATCHED,
            guard_status="PASSED",
        )
    )
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=DISPATCHED,
            dry_run=False,
            accepted=True,
        )
    )
    return dispatch_id


def test_the_wake_up_time_survives_a_restart(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "restart.db")
    repo = SqlAlchemyRepository(factory)
    dispatch_id = _real_dispatch(repo, run.run_id, Coordinate(1, 149, 17))
    repo.record_flight_time(dispatch_id, FLIGHT, DISPATCHED)
    engine.dispose()

    # 助手在这里完全退出：新引擎、新仓储，没有任何内存状态。
    reopened = create_database_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    repo2 = SqlAlchemyRepository(create_session_factory(reopened))

    pending = repo2.pending_reports(run.run_id)
    assert len(pending) == 1
    assert pending[0].expected_report_at_utc == DISPATCHED + FLIGHT
    assert not pending[0].closed


def test_a_fleet_still_in_flight_makes_the_run_wait(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "inflight.db")
    repo = SqlAlchemyRepository(factory)
    repo.record_flight_time(
        _real_dispatch(repo, run.run_id, Coordinate(1, 149, 17)), FLIGHT, DISPATCHED
    )

    plan = ReportWaitPlanner().plan(
        repo.pending_reports(run.run_id), now_utc=DISPATCHED + timedelta(hours=1)
    )

    assert plan.action is WaitAction.WAIT
    assert plan.resume_at_utc is not None
    engine.dispose()


def test_an_arrived_fleet_makes_the_run_collect(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "arrived.db")
    repo = SqlAlchemyRepository(factory)
    repo.record_flight_time(
        _real_dispatch(repo, run.run_id, Coordinate(1, 149, 17)), FLIGHT, DISPATCHED
    )

    plan = ReportWaitPlanner().plan(
        repo.pending_reports(run.run_id), now_utc=DISPATCHED + FLIGHT + timedelta(minutes=5)
    )

    assert plan.action is WaitAction.COLLECT
    engine.dispose()


def test_a_dry_run_dispatch_never_holds_the_run_open(tmp_path: Path) -> None:
    """演习模式不会产生战报；算进来运行就永远等不完。"""
    engine, factory, run = _plan_and_run(tmp_path, "dry.db")
    repo = SqlAlchemyRepository(factory)
    intent_id, dispatch_id = uuid4(), uuid4()
    repo.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run.run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(1, 149, 17),
            preset=FleetPresetRef(name="探路", signature="轻型战斗机:1"),
            cycle_start_utc=CYCLE_START,
            created_at_utc=DISPATCHED,
            guard_status="REFUSED",
        )
    )
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=DISPATCHED,
            dry_run=True,
            accepted=False,
        )
    )

    assert repo.pending_reports(run.run_id) == []
    assert (
        ReportWaitPlanner().plan(repo.pending_reports(run.run_id), now_utc=DISPATCHED).action
        is WaitAction.COMPLETE
    )
    engine.dispose()


def test_unknown_flight_time_is_stored_as_unknown(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "unknown.db")
    repo = SqlAlchemyRepository(factory)
    repo.record_flight_time(
        _real_dispatch(repo, run.run_id, Coordinate(1, 149, 17)), None, DISPATCHED
    )

    pending = repo.pending_reports(run.run_id)
    assert pending[0].expected_report_at_utc is None
    # 未知不能变成无限等待。
    assert ReportWaitPlanner().plan(pending, now_utc=DISPATCHED).action is WaitAction.COLLECT
    engine.dispose()


def test_resume_time_and_session_attempts_round_trip(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "resume.db")
    repo = SqlAlchemyRepository(factory)
    wake = DISPATCHED + FLIGHT

    repo.set_resume_at(run.run_id, wake)
    assert repo.note_session_attempt(run.run_id, succeeded=False) == 1
    assert repo.note_session_attempt(run.run_id, succeeded=False) == 2
    # 拿到会话后归零，下一次中断重新从短退避开始。
    assert repo.note_session_attempt(run.run_id, succeeded=True) == 0
    engine.dispose()


def test_waiting_states_are_persisted(tmp_path: Path) -> None:
    engine, factory, run = _plan_and_run(tmp_path, "states.db")
    repo = SqlAlchemyRepository(factory)

    repo.set_run_state(run.run_id, RunState.SCANNING)
    repo.set_run_state(run.run_id, RunState.DRAINING)
    repo.set_run_state(run.run_id, RunState.AWAITING_REPORT)
    assert repo.run_state(run.run_id) is RunState.AWAITING_REPORT

    repo.set_run_state(run.run_id, RunState.WAITING_SESSION)
    assert repo.run_state(run.run_id) is RunState.WAITING_SESSION

    repo.set_run_state(run.run_id, RunState.DRAINING)
    assert repo.run_state(run.run_id) is RunState.DRAINING
    engine.dispose()
