from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from evo_helper.application.workflow import (
    AttackBinding,
    IntegrationWorkflow,
    TargetRecognition,
)
from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.ports import CoordinateClaim, ScreenObservation
from evo_helper.domain.records import BattleReport, FleetSnapshotEntry
from evo_helper.game.action_guard import ActionGuard
from evo_helper.game.capacity import LineCapacityGate
from evo_helper.game.coordinator import DispatchCoordinator
from evo_helper.game.simulator import SimulatedGameAdapter

NOW = datetime(2026, 8, 6, 1, 0, tzinfo=UTC)
ORIGIN = Coordinate(1, 1, 1)
BOT = Coordinate(1, 1, 2)
NON_BOT = Coordinate(1, 1, 3)
PRESET = FleetPresetRef("fleet-a", "sig-a")


@dataclass
class MemoryRepository:
    remaining: deque[Coordinate]
    scans: list[object] = field(default_factory=list)
    intents: list[object] = field(default_factory=list)
    dispatches: list[object] = field(default_factory=list)
    reports: list[object] = field(default_factory=list)
    events: list[object] = field(default_factory=list)

    def claim_next_coordinate(self, _run_id: UUID) -> CoordinateClaim | None:
        return CoordinateClaim(self.remaining.popleft()) if self.remaining else None

    def complete_coordinate(self, _run_id: UUID, _coordinate: Coordinate) -> None:
        return None

    def save_scan(self, scan: object) -> None:
        self.scans.append(scan)

    def save_attack_intent(self, intent: object) -> None:
        self.intents.append(intent)

    def save_dispatch(self, dispatch: object) -> None:
        self.dispatches.append(dispatch)

    def append_report(self, report: object) -> None:
        self.reports.append(report)

    def append_state_event(self, event: object) -> None:
        self.events.append(event)


class Reader:
    def read_target(self, coordinate: Coordinate) -> TargetRecognition:
        owner = "bot_alpha" if coordinate == BOT else "player_alpha"
        return TargetRecognition(coordinate, owner, confidence=1.0, stable_frames=2)


class Bindings:
    def for_target(self, coordinate: Coordinate) -> AttackBinding | None:
        return AttackBinding(ORIGIN, PRESET) if coordinate == BOT else None


def _workflow(
    repository: MemoryRepository,
    game: SimulatedGameAdapter,
    *,
    game_feedback_slots: int | None = None,
) -> IntegrationWorkflow:
    coordinator = DispatchCoordinator(ActionGuard(Settings()), LineCapacityGate(user_limit=3))
    return IntegrationWorkflow(
        repository,
        game,
        Reader(),
        Bindings(),
        coordinator,
        dry_run=True,
        now_utc=lambda: NOW,
        game_feedback_slots=lambda: game_feedback_slots,
    )


def test_dry_run_scan_dispatch_drain_closure_never_clicks() -> None:
    repository = MemoryRepository(deque([BOT, NON_BOT]))
    game = SimulatedGameAdapter(dry_run=False)
    game.register_preset(PRESET)
    workflow = _workflow(repository, game)
    run_id = uuid4()

    assert workflow.scan_once(run_id).status == "DRY_RUN_RECORDED"
    assert workflow.scan_once(run_id).status == "SCANNED_NON_BOT"
    assert workflow.scan_once(run_id).status == "DRAINING"

    report = BattleReport(
        report_id=uuid4(),
        reported_at_utc=NOW,
        attacker_origin=ORIGIN,
        defender_target=BOT,
        fleet=(FleetSnapshotEntry("defender", "fighter", 9),),
    )
    assert workflow.drain_reports(run_id, [report]) == 1
    assert len(repository.scans) == 2
    assert len(repository.intents) == 1
    assert len(repository.dispatches) == 1
    assert repository.dispatches[0].dry_run is True
    assert repository.reports == [report]
    assert game.dispatched == ()


def test_new_workflow_instance_resumes_from_repository_cursor() -> None:
    repository = MemoryRepository(deque([BOT, NON_BOT]))
    game = SimulatedGameAdapter()
    game.register_preset(PRESET)
    run_id = uuid4()

    assert _workflow(repository, game).scan_once(run_id).coordinate == BOT
    assert _workflow(repository, game).scan_once(run_id).coordinate == NON_BOT
    assert len(repository.scans) == 2


def test_unknown_attack_ui_pauses_before_recording_an_intent() -> None:
    class UnknownUiGame(SimulatedGameAdapter):
        def observe(self) -> ScreenObservation:
            return ScreenObservation("attack", None, 1.0)

    repository = MemoryRepository(deque([BOT]))
    game = UnknownUiGame()
    game.register_preset(PRESET)

    outcome = _workflow(repository, game).scan_once(uuid4())

    assert outcome.status == "SAFETY_PAUSED"
    assert repository.intents == []
    assert repository.dispatches == []
    assert game.dispatched == ()


def test_full_capacity_waits_before_recording_a_dry_run_dispatch() -> None:
    repository = MemoryRepository(deque([BOT]))
    game = SimulatedGameAdapter()
    game.register_preset(PRESET)

    outcome = _workflow(repository, game, game_feedback_slots=0).scan_once(uuid4())

    assert outcome.status == "WAITING_CAPACITY"
    assert "full" in (outcome.detail or "")
    assert repository.intents == []
    assert repository.dispatches == []
    assert game.dispatched == ()
