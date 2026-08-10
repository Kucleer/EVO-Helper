from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from evo_helper.config import Settings
from evo_helper.web.runtime import create_runtime_app


def test_runtime_migrates_database_and_serves_persistent_api(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'runtime.db'}"
    app = create_runtime_app(Settings(database_url=database_url), local_token="runtime-token")
    client = TestClient(app)
    response = client.post(
        "/api/plans",
        headers={"X-Evo-Helper-Token": "runtime-token"},
        json={
            "name": "runtime-plan",
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

    engine = create_engine(database_url)
    assert {"public_id", "updated_at_utc"} <= {
        column["name"] for column in inspect(engine).get_columns("scan_plans")
    }
    # 调度器的可调项走迁移加列。漏了这条迁移，模型和真实的表就会静默分叉：
    # 本地测试用 `create_all` 建表，一路全绿，只有真实的库会在启动时炸。
    assert "restart_cooldown_seconds" in {
        column["name"] for column in inspect(engine).get_columns("scheduler_config")
    }
    assert engine.connect().execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_applying_migrations_does_not_silence_application_logging(tmp_path: Path) -> None:
    """Alembic's fileConfig defaults to disabling every existing logger.

    The runtime migrates at startup, so that default would kill the
    report-timing log for the rest of the process.
    """
    import logging

    from evo_helper.web.runtime import _upgrade_database

    logger = logging.getLogger("evo_helper.vision.live_reports")
    logger.setLevel(logging.INFO)

    _upgrade_database(f"sqlite:///{tmp_path / 'migrated.db'}")

    assert logger.isEnabledFor(logging.INFO)
    assert not logger.disabled
