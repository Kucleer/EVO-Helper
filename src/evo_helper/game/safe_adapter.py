"""GamePort wrapper that enforces ActionGuard before any click."""

from __future__ import annotations

from collections.abc import Callable

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.domain.ports import (
    DispatchResult,
    GamePort,
    InflightFleet,
    NavigationResult,
    PresetObservation,
    ReportNavigationResult,
    ScreenObservation,
)

from .action_guard import ActionGuard, ActionGuardDecision


class SafeGameAdapter:
    """Wraps a GamePort so a dispatch click can only happen after ActionGuard."""

    def __init__(
        self,
        inner: GamePort,
        guard: ActionGuard,
        *,
        click: Callable[[DispatchCommand], None] | None = None,
        known_targets: frozenset[Coordinate] = frozenset(),
    ) -> None:
        self._inner = inner
        self._guard = guard
        self._click = click
        self._known_targets = known_targets
        self._intents: list[tuple[DispatchCommand, ActionGuardDecision]] = []

    def observe(self) -> ScreenObservation:
        return self._inner.observe()

    def navigate_to(self, coordinate: Coordinate) -> NavigationResult:
        return self._inner.navigate_to(coordinate)

    def load_fleet_preset(self, preset: FleetPresetRef) -> PresetObservation:
        return self._inner.load_fleet_preset(preset)

    def dispatch_attack(self, command: DispatchCommand) -> DispatchResult:
        self._inner.load_fleet_preset(command.preset)
        decision = self._guard.evaluate(command, self._inner.observe())
        self._intents.append((command, decision))
        if not decision.allowed:
            return DispatchResult(accepted=False)
        if command.target not in self._known_targets:
            self._intents[-1] = (
                command,
                ActionGuardDecision(False, "target not in configured scan range"),
            )
            return DispatchResult(accepted=False)
        # Fresh re-observation immediately before the click.
        token = decision.token
        assert token is not None
        final = self._guard.verify_and_consume(token, self._inner.observe())
        self._intents[-1] = (command, final)
        if not final.allowed:
            return DispatchResult(accepted=False)
        if self._click is not None:
            self._click(command)
        return DispatchResult(accepted=True)

    def list_inflight(self) -> list[InflightFleet]:
        return self._inner.list_inflight()

    def open_battle_reports(self) -> ReportNavigationResult:
        return self._inner.open_battle_reports()

    @property
    def intents(self) -> tuple[tuple[DispatchCommand, ActionGuardDecision], ...]:
        return tuple(self._intents)
