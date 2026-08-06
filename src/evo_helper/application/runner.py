"""State-synchronizing runner for one safe integration-workflow step."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from evo_helper.domain.models import RunState
from evo_helper.domain.records import BattleReport, StateEvent

from .workflow import IntegrationWorkflow, ReportDrainOutcome, ScanOutcome


class RunStateStore(Protocol):
    def run_state(self, run_id: UUID) -> RunState: ...

    def set_run_state(self, run_id: UUID, target: RunState) -> None: ...

    def append_state_event(self, event: StateEvent) -> None: ...


class WorkflowRunner:
    """Mirror terminal workflow outcomes into the persistent run aggregate."""

    def __init__(self, workflow: IntegrationWorkflow, states: RunStateStore) -> None:
        self._workflow = workflow
        self._states = states

    def scan_once(self, run_id: UUID) -> ScanOutcome:
        current = self._states.run_state(run_id)
        if current is RunState.WAITING_CAPACITY:
            self._append_event(
                run_id,
                "capacity_recheck_started",
                RunState.WAITING_CAPACITY,
                RunState.SCANNING,
            )
            self._states.set_run_state(run_id, RunState.SCANNING)
        elif current is not RunState.SCANNING:
            return ScanOutcome("SAFETY_PAUSED", detail=f"run is not scannable: {current.value}")
        outcome = self._workflow.scan_once(run_id)
        target = {
            "WAITING_CAPACITY": RunState.WAITING_CAPACITY,
            "DRAINING": RunState.DRAINING,
            "SAFETY_PAUSED": RunState.PAUSED,
        }.get(outcome.status)
        if target is not None:
            self._states.set_run_state(run_id, target)
        return outcome

    def drain_reports(self, run_id: UUID, reports: list[BattleReport]) -> ReportDrainOutcome:
        """Finish a report-only pass and persist its terminal run state."""
        current = self._states.run_state(run_id)
        if current is not RunState.DRAINING:
            return ReportDrainOutcome(
                "SAFETY_PAUSED", detail=f"run is not draining: {current.value}"
            )
        outcome = self._workflow.drain_reports_outcome(run_id, reports)
        target = {
            "COMPLETED": RunState.COMPLETED,
            "SAFETY_PAUSED": RunState.PAUSED,
        }.get(outcome.status)
        if target is not None:
            self._states.set_run_state(run_id, target)
        return outcome

    def _append_event(self, run_id: UUID, event: str, before: RunState, after: RunState) -> None:
        self._states.append_state_event(
            StateEvent(
                aggregate_type="run",
                aggregate_id=run_id,
                event=event,
                before_state=before.value,
                after_state=after.value,
                occurred_at_utc=datetime.now(UTC),
            )
        )
