from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.records import StateEvent
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import RunInstance, ScanPlan
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)


def test_web_configuration_and_run_survive_service_restart(tmp_path: Path) -> None:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: NOW)
    plan = service.create_plan(
        name="persisted",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(
                Coordinate(1, 1, 1),
                Coordinate(1, 1, 2),
                Coordinate(1, 1, 1),
                "fleet-a",
                "fleet-a-signature",
                0,
            ),
        ),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="persistent-run-0001", created_at_utc=NOW
    )
    SqlAlchemyRepository(factory).append_state_event(
        StateEvent(
            aggregate_type="run",
            aggregate_id=run_id,
            event="started",
            before_state=None,
            after_state=RunState.SCANNING.value,
            occurred_at_utc=NOW,
        )
    )
    engine.dispose()

    restarted_engine = create_database_engine(f"sqlite:///{tmp_path / 'web.db'}")
    restarted_factory = create_session_factory(restarted_engine)
    restarted = PersistentApplicationService(restarted_factory, now_utc=lambda: NOW)

    assert restarted.get_plan(plan.id) == plan
    # 运行实例这一侧直接读库：`get_run` 随 `GET /api/runs/{run_id}` 一起删了，
    # 而这条用例要守的本来就是「行还在、还挂在同一个计划上」，不是那个接口。
    with restarted_factory() as session:
        restored_run = session.get(RunInstance, run_id)
        assert restored_run is not None
        assert restored_run.state == RunState.SCANNING.value
        restored_plan = session.get(ScanPlan, restored_run.plan_id)
        assert restored_plan is not None
        assert restored_plan.public_id == plan.id
    assert restarted.list_events(10)[0].event == "started"


def test_persistent_app_retains_plan_across_app_recreation(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'api.db'}"
    first_engine = create_database_engine(database_url)
    Base.metadata.create_all(first_engine)
    first = TestClient(
        create_persistent_app(create_session_factory(first_engine), local_token="test")
    )
    response = first.post(
        "/api/plans",
        headers={"X-Evo-Helper-Token": "test"},
        json={
            "name": "api-persisted",
            "window_start": "08:00",
            "window_end": "20:00",
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 1, "position": 1},
                    "end": {"galaxy": 1, "system": 1, "position": 2},
                    "origin": {"galaxy": 1, "system": 1, "position": 1},
                    "fleet_preset": "fleet-a",
                    "fleet_preset_signature": "fleet-a-signature",
                }
            ],
        },
    )
    assert response.status_code == 201
    first_engine.dispose()

    second_engine = create_database_engine(database_url)
    second = TestClient(
        create_persistent_app(create_session_factory(second_engine), local_token="test")
    )
    plans = second.get("/api/plans").json()

    assert [plan["name"] for plan in plans] == ["api-persisted"]


def test_persistent_plan_accepts_an_origin_outside_the_range(tmp_path: Path) -> None:
    """Both services enforce plan rules, so both must allow an outside origin.

    The fake service was fixed first and the persistent one still rejected the
    plan, which is what the console hit with real coordinates.
    """
    engine = create_database_engine(f"sqlite:///{tmp_path / 'origin.db'}")
    Base.metadata.create_all(engine)
    service = PersistentApplicationService(create_session_factory(engine), now_utc=lambda: NOW)

    plan = service.create_plan(
        name="morning-scan",
        enabled=True,
        window_start=time(8),
        window_end=time(10),
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

    assert plan.ranges[0].origin == Coordinate(2, 137, 18)
    assert plan.ranges[0].fleet_preset == "探路"
    assert plan.ranges[0].fleet_preset_signature == "轻型战斗机:1"
