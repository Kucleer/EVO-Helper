"""State-synchronizing runner for one safe integration-workflow step."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from evo_helper.domain.models import RunState

from .workflow import IntegrationWorkflow, ScanOutcome


class RunStateStore(Protocol):
    def run_state(self, run_id: UUID) -> RunState: ...

    def set_run_state(self, run_id: UUID, target: RunState) -> None: ...


class WorkflowRunner:
    """Mirror terminal workflow outcomes into the persistent run aggregate."""

    def __init__(self, workflow: IntegrationWorkflow, states: RunStateStore) -> None:
        self._workflow = workflow
        self._states = states

    def scan_once(self, run_id: UUID) -> ScanOutcome:
        current = self._states.run_state(run_id)
        if current is RunState.WAITING_CAPACITY:
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
