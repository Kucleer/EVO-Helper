from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import Engine, select

from evo_helper.application.workflow import (
    AttackBinding,
    IntegrationWorkflow,
    TargetRecognition,
)
from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.game.action_guard import ActionGuard
from evo_helper.game.capacity import LineCapacityGate
from evo_helper.game.coordinator import DispatchCoordinator
from evo_helper.game.simulator import SimulatedGameAdapter
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import CoordinateScanRow, RunInstance, ScanPlan, ScanRangeRow
from evo_helper.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
ORIGIN = Coordinate(1, 1, 1)
BOT = Coordinate(1, 1, 2)
NON_BOT = Coordinate(1, 1, 3)
PRESET = FleetPresetRef("fleet-a", "sig-a")


class Reader:
    def read_target(self, coordinate: Coordinate) -> TargetRecognition:
        owner = "bot_alpha" if coordinate == BOT else "player_alpha"
        return TargetRecognition(coordinate, owner, confidence=1.0, stable_frames=2)


class Bindings:
    def for_target(self, coordinate: Coordinate) -> AttackBinding | None:
        return AttackBinding(ORIGIN, PRESET) if coordinate == BOT else None


def _workflow(repository: SqlAlchemyRepository) -> IntegrationWorkflow:
    game = SimulatedGameAdapter()
    game.register_preset(PRESET)
    return IntegrationWorkflow(
        repository,
        game,
        Reader(),
        Bindings(),
        DispatchCoordinator(ActionGuard(Settings()), LineCapacityGate(3)),
        dry_run=True,
        now_utc=lambda: NOW,
    )


def _seed(database_path: Path) -> UUID:
    engine = create_database_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions() as session:
        plan = ScanPlan(name="restart-test", created_at_utc=NOW)
        session.add(plan)
        session.flush()
        session.add(
            ScanRangeRow(
                plan_id=plan.id,
                start_galaxy=BOT.galaxy,
                start_system=BOT.system,
                start_position=BOT.position,
                end_galaxy=NON_BOT.galaxy,
                end_system=NON_BOT.system,
                end_position=NON_BOT.position,
                origin_galaxy=ORIGIN.galaxy,
                origin_system=ORIGIN.system,
                origin_position=ORIGIN.position,
                fleet_preset_name=PRESET.name,
                fleet_preset_signature=PRESET.signature,
                priority=0,
            )
        )
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key="restart-e2e-001",
            state="SCANNING",
            created_at_utc=NOW,
        )
        session.add(run)
        session.commit()
        run_id = run.id
    engine.dispose()
    return run_id


def _open_repository(database_path: Path) -> tuple[SqlAlchemyRepository, Engine]:
    engine = create_database_engine(f"sqlite:///{database_path}")
    return SqlAlchemyRepository(create_session_factory(engine)), engine


def test_workflow_resumes_persisted_cursor_after_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "workflow.db"
    run_id = _seed(database_path)

    first_repository, first_engine = _open_repository(database_path)
    assert _workflow(first_repository).scan_once(run_id).coordinate == BOT
    first_engine.dispose()

    restarted_repository, restarted_engine = _open_repository(database_path)
    assert _workflow(restarted_repository).scan_once(run_id).coordinate == NON_BOT
    with restarted_engine.connect() as connection:
        scans = connection.execute(select(CoordinateScanRow)).all()
    restarted_engine.dispose()

    assert len(scans) == 2
