from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, RunState
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView

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
        dry_run=True,
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
    run = service.start_run(plan.id, "persistent-run-0001")
    engine.dispose()

    restarted_engine = create_database_engine(f"sqlite:///{tmp_path / 'web.db'}")
    restarted = PersistentApplicationService(
        create_session_factory(restarted_engine), now_utc=lambda: NOW
    )

    assert restarted.get_plan(plan.id) == plan
    restored_run = restarted.get_run(run.run_id)
    assert restored_run is not None
    assert restored_run.plan_id == plan.id
    assert restored_run.state is RunState.SCANNING
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
            "dry_run": True,
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
