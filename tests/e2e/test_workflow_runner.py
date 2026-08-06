from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.bindings import DatabaseBindingResolver
from evo_helper.application.runner import WorkflowRunner
from evo_helper.application.workflow import IntegrationWorkflow, TargetRecognition
from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef, RunState
from evo_helper.game.action_guard import ActionGuard
from evo_helper.game.capacity import LineCapacityGate
from evo_helper.game.coordinator import DispatchCoordinator
from evo_helper.game.simulator import SimulatedGameAdapter
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import RunInstance, ScanPlan, ScanRangeRow
from evo_helper.storage.repository import SqlAlchemyRepository

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
ORIGIN = Coordinate(1, 1, 1)
TARGET = Coordinate(1, 1, 2)
PRESET = FleetPresetRef("fleet-a", "sig-a")


class Reader:
    def read_target(self, coordinate: Coordinate) -> TargetRecognition:
        return TargetRecognition(coordinate, "bot_alpha", 1.0, 2)


def test_runner_persists_waiting_and_draining_states(tmp_path: Path) -> None:
    database_path = tmp_path / "runner.db"
    run_id, repository, sessions = _seed(database_path)
    game = SimulatedGameAdapter()
    game.register_preset(PRESET)
    slots = [0]
    workflow = IntegrationWorkflow(
        repository,
        game,
        Reader(),
        DatabaseBindingResolver(sessions, run_id),
        DispatchCoordinator(ActionGuard(Settings()), LineCapacityGate(1)),
        dry_run=True,
        now_utc=lambda: NOW,
        game_feedback_slots=lambda: slots[0],
    )
    runner = WorkflowRunner(workflow, repository)

    assert runner.scan_once(run_id).status == "WAITING_CAPACITY"
    assert repository.run_state(run_id) is RunState.WAITING_CAPACITY

    slots[0] = 1
    assert runner.scan_once(run_id).status == "DRY_RUN_RECORDED"
    assert repository.run_state(run_id) is RunState.SCANNING
    assert runner.scan_once(run_id).status == "DRAINING"
    assert repository.run_state(run_id) is RunState.DRAINING


def _seed(
    database_path: Path,
) -> tuple[UUID, SqlAlchemyRepository, sessionmaker[Session]]:
    engine = create_database_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    with sessions() as session:
        plan = ScanPlan(name="runner", created_at_utc=NOW)
        session.add(plan)
        session.flush()
        session.add(
            ScanRangeRow(
                plan_id=plan.id,
                start_galaxy=TARGET.galaxy,
                start_system=TARGET.system,
                start_position=TARGET.position,
                end_galaxy=TARGET.galaxy,
                end_system=TARGET.system,
                end_position=TARGET.position,
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
            idempotency_key="runner-state-0001",
            state="SCANNING",
            created_at_utc=NOW,
        )
        session.add(run)
        session.commit()
    return run.id, SqlAlchemyRepository(sessions), sessions
