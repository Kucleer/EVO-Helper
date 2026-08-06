"""A deterministic simulated game implementing GamePort.

The simulator never clicks: dispatching records an intent and reports the
configured dry-run flag. It is used for tests and for shadow/dry-run runs.
"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.domain.ports import (
    DispatchResult,
    InflightFleet,
    NavigationResult,
    PresetObservation,
    ReportNavigationResult,
    ScreenObservation,
)


class SimulatedGameAdapter:
    """Fake GamePort that models state transitions deterministically."""

    def __init__(self, dry_run: bool = True, capacity: int = 3) -> None:
        self.dry_run = dry_run
        self.capacity = capacity
        self._screen = "galaxy"
        self._ui_version = "galaxy-v2"
        self._inflight: list[InflightFleet] = []
        self._dispatches: list[DispatchCommand] = []
        self._presets: dict[str, str] = {}

    def register_preset(self, preset: FleetPresetRef) -> None:
        self._presets[preset.name] = preset.signature

    def observe(self) -> ScreenObservation:
        return ScreenObservation(screen=self._screen, ui_version=self._ui_version, confidence=1.0)

    def navigate_to(self, coordinate: Coordinate) -> NavigationResult:
        self._screen = "galaxy"
        self._ui_version = "galaxy-v2"
        return NavigationResult(success=True)

    def load_fleet_preset(self, preset: FleetPresetRef) -> PresetObservation:
        self._screen = "attack"
        self._ui_version = "attack-v2"
        signature = self._presets.get(preset.name)
        if signature is None:
            return PresetObservation(name=preset.name, signature="", confidence=0.0)
        return PresetObservation(name=preset.name, signature=signature, confidence=1.0)

    def dispatch_attack(self, command: DispatchCommand) -> object:
        self._dispatches.append(command)
        if self.dry_run:
            return DispatchResult(accepted=True, dry_run=True)
        if len(self._inflight) >= self.capacity:
            return DispatchResult(accepted=False, dry_run=False)
        self._inflight.append(InflightFleet(target=command.target))
        return DispatchResult(accepted=True, dry_run=False)

    def list_inflight(self) -> list[InflightFleet]:
        return list(self._inflight)

    def open_battle_reports(self) -> ReportNavigationResult:
        self._screen = "mail_list"
        self._ui_version = "mail-list-v2"
        return ReportNavigationResult(success=True)

    @property
    def dispatched(self) -> tuple[DispatchCommand, ...]:
        return tuple(self._dispatches)
