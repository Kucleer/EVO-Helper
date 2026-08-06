"""Safe scan, dry-run dispatch, and report-draining orchestration.

This module is intentionally the only integration seam that composes the game
adapter, visual recognition result, ActionGuard, and persistence ports.  It
never performs a browser click itself; in dry-run mode it records the proposed
dispatch without invoking :meth:`GamePort.dispatch_attack`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.domain.ports import CoordinateClaim, GamePort
from evo_helper.domain.records import (
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
    StateEvent,
)
from evo_helper.domain.rules import cycle_start_utc
from evo_helper.game.coordinator import DispatchCoordinator


@dataclass(frozen=True)
class TargetRecognition:
    """A target read from stable visual evidence after navigation."""

    coordinate: Coordinate
    owner_name: str | None
    confidence: float
    stable_frames: int

    @property
    def is_bot(self) -> bool:
        return self.owner_name is not None and self.owner_name.startswith("bot_")


class TargetReader(Protocol):
    """Adapter for translating a vision observation into a scan result."""

    def read_target(self, expected_coordinate: Coordinate) -> TargetRecognition: ...


@dataclass(frozen=True)
class AttackBinding:
    """Per-range dispatch configuration already approved by the user."""

    origin: Coordinate
    preset: FleetPresetRef


class BindingResolver(Protocol):
    def for_target(self, coordinate: Coordinate) -> AttackBinding | None: ...


class WorkflowRepository(Protocol):
    def claim_next_coordinate(self, run_id: UUID) -> CoordinateClaim | None: ...
    def save_scan(self, scan: object) -> None: ...
    def save_attack_intent(self, intent: object) -> None: ...
    def save_dispatch(self, dispatch: object) -> None: ...
    def append_report(self, report: object) -> None: ...
    def append_state_event(self, event: StateEvent) -> None: ...


@dataclass(frozen=True)
class ScanOutcome:
    status: str
    coordinate: Coordinate | None = None
    detail: str | None = None
    intent_id: UUID | None = None


class IntegrationWorkflow:
    """Run one safe scan step or drain parsed reports.

    Callers are responsible for scheduling and for pausing a run on a
    ``SAFETY_PAUSED`` outcome.  This keeps run state transitions owned by the
    application service while making every decision auditable through events.
    """

    def __init__(
        self,
        repository: WorkflowRepository,
        game: GamePort,
        target_reader: TargetReader,
        bindings: BindingResolver,
        coordinator: DispatchCoordinator,
        *,
        dry_run: bool = True,
        now_utc: Callable[[], datetime] | None = None,
        game_feedback_slots: Callable[[], int | None] | None = None,
    ) -> None:
        self._repository = repository
        self._game = game
        self._target_reader = target_reader
        self._bindings = bindings
        self._coordinator = coordinator
        self._dry_run = dry_run
        self._now_utc = now_utc or (lambda: datetime.now(UTC))
        self._game_feedback_slots = game_feedback_slots or (lambda: None)

    def scan_once(self, run_id: UUID) -> ScanOutcome:
        """Claim and process one coordinate without bypassing ActionGuard."""
        claim = self._repository.claim_next_coordinate(run_id)
        if claim is None:
            self._append_event(run_id, "draining_started", "SCANNING", "DRAINING")
            return ScanOutcome("DRAINING")
        coordinate = claim.coordinate
        if not self._game.navigate_to(coordinate).success:
            return self._safety_pause(run_id, coordinate, "game navigation failed")

        recognition = self._target_reader.read_target(coordinate)
        if recognition.coordinate != coordinate:
            return self._safety_pause(
                run_id, coordinate, "visual coordinate conflicts with claimed cursor"
            )
        if recognition.confidence < 0.99 or recognition.stable_frames < 2:
            self._repository.save_scan(
                CoordinateScan(
                    run_id=run_id,
                    coordinate=coordinate,
                    scanned_at_utc=self._now_utc(),
                    owner_name=recognition.owner_name,
                    confidence=recognition.confidence,
                )
            )
            return self._safety_pause(run_id, coordinate, "target evidence is not stable enough")

        self._repository.save_scan(
            CoordinateScan(
                run_id=run_id,
                coordinate=coordinate,
                scanned_at_utc=self._now_utc(),
                owner_name=recognition.owner_name,
                is_bot=recognition.is_bot,
                confidence=recognition.confidence,
            )
        )
        if not recognition.is_bot:
            return ScanOutcome("SCANNED_NON_BOT", coordinate)

        binding = self._bindings.for_target(coordinate)
        if binding is None:
            return self._safety_pause(run_id, coordinate, "no approved range binding for target")

        preset = self._game.load_fleet_preset(binding.preset)
        if preset.name != binding.preset.name or preset.signature != binding.preset.signature:
            return self._safety_pause(run_id, coordinate, "fleet preset signature mismatch")
        command = DispatchCommand(
            run_id=run_id,
            origin=binding.origin,
            target=coordinate,
            preset=binding.preset,
        )
        plan = self._coordinator.plan(
            command,
            preset,
            self._game.list_inflight(),
            self._game_feedback_slots(),
            self._game.observe(),
        )
        now = self._now_utc()
        intent = AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=binding.origin,
            target=coordinate,
            preset=binding.preset,
            cycle_start_utc=cycle_start_utc(now),
            created_at_utc=now,
            guard_status="ALLOWED" if plan.guard.allowed else "REFUSED",
        )
        self._repository.save_attack_intent(intent)

        if self._dry_run:
            self._repository.save_dispatch(
                AttackDispatch(
                    dispatch_id=uuid4(),
                    intent_id=intent.intent_id,
                    dispatched_at_utc=now,
                    dry_run=True,
                    accepted=False,
                )
            )
            self._append_event(run_id, "dry_run_dispatch_recorded", "SCANNING", "SCANNING")
            return ScanOutcome("DRY_RUN_RECORDED", coordinate, intent_id=intent.intent_id)
        if not plan.dispatchable or plan.guard.token is None:
            return self._safety_pause(run_id, coordinate, plan.guard.reason, intent.intent_id)
        final_guard = self._coordinator.authorize_final_dispatch(
            plan.guard.token, self._game.observe()
        )
        if not final_guard.allowed:
            return self._safety_pause(run_id, coordinate, final_guard.reason, intent.intent_id)
        result = self._game.dispatch_attack(command)
        self._repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent.intent_id,
                dispatched_at_utc=self._now_utc(),
                dry_run=False,
                accepted=result.accepted,
            )
        )
        return ScanOutcome("DISPATCHED" if result.accepted else "DISPATCH_REJECTED", coordinate)

    def drain_reports(self, run_id: UUID, reports: Iterable[BattleReport]) -> int:
        """Append reports only; draining cannot scan or initiate new dispatches."""
        if not self._game.open_battle_reports().success:
            self._safety_pause(run_id, None, "could not open battle reports")
            return 0
        count = 0
        for report in reports:
            self._repository.append_report(report)
            count += 1
        self._append_event(run_id, "reports_drained", "DRAINING", "COMPLETED")
        return count

    def _safety_pause(
        self,
        run_id: UUID,
        coordinate: Coordinate | None,
        detail: str,
        intent_id: UUID | None = None,
    ) -> ScanOutcome:
        self._append_event(run_id, "safety_paused", "SCANNING", "PAUSED")
        return ScanOutcome("SAFETY_PAUSED", coordinate, detail, intent_id)

    def _append_event(self, run_id: UUID, event: str, before: str, after: str) -> None:
        self._repository.append_state_event(
            StateEvent(
                aggregate_type="run",
                aggregate_id=run_id,
                event=event,
                before_state=before,
                after_state=after,
                occurred_at_utc=self._now_utc(),
            )
        )
