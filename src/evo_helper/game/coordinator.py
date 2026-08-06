"""Application-facing coordination of capacity checks and ActionGuard."""

from __future__ import annotations

from dataclasses import dataclass

from evo_helper.domain.models import DispatchCommand
from evo_helper.domain.ports import InflightFleet, PresetObservation, ScreenObservation

from .action_guard import ActionGuard, ActionGuardDecision
from .capacity import CapacityCheck, LineCapacityGate


@dataclass(frozen=True)
class DispatchPlan:
    command: DispatchCommand
    preset_observation: PresetObservation
    capacity: CapacityCheck
    guard: ActionGuardDecision

    @property
    def dispatchable(self) -> bool:
        return (
            self.preset_observation.confidence >= 0.99
            and self.capacity.available
            and self.guard.allowed
        )


class DispatchCoordinator:
    """Orders checks so a real dispatch cannot bypass capacity or the guard."""

    def __init__(self, guard: ActionGuard, capacity_gate: LineCapacityGate) -> None:
        self._guard = guard
        self._capacity_gate = capacity_gate

    def plan(
        self,
        command: DispatchCommand,
        preset_observation: PresetObservation,
        in_flight: list[InflightFleet],
        game_feedback_slots: int | None,
        screen_observation: ScreenObservation,
    ) -> DispatchPlan:
        capacity = self._capacity_gate.check(in_flight, game_feedback_slots, command.target)
        guard = self._guard.evaluate(command, screen_observation)
        return DispatchPlan(
            command=command,
            preset_observation=preset_observation,
            capacity=capacity,
            guard=guard,
        )
