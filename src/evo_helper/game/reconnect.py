"""Fail-closed session recovery around a :class:`GamePort`.

The browser adapter identifies the logged-in EVO entry page and exposes its
single safe navigation action through :class:`EntrySessionPort`.  This wrapper
never retries an unknown page and only resumes normal game calls after two
stable observations of the expected post-entry screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class EntrySessionPort(Protocol):
    """Narrow browser-facing port for a logged-in entry/reconnect screen."""

    def entry_page_visible(self) -> bool: ...

    def enter_session(self) -> NavigationResult: ...


@dataclass(frozen=True)
class RecoveryOutcome:
    status: str
    detail: str | None = None

    @property
    def ready(self) -> bool:
        return self.status in {"ALREADY_READY", "RECOVERED"}


class SessionRecoveryGate:
    """Recover a logged-in entry page once, or stop safely with evidence."""

    def __init__(
        self,
        game: GamePort,
        entry: EntrySessionPort,
        *,
        ready_screen: str = "galaxy",
        min_confidence: float = 0.99,
        stable_frames: int = 2,
    ) -> None:
        self._game = game
        self._entry = entry
        self._ready_screen = ready_screen
        self._min_confidence = min_confidence
        self._stable_frames = stable_frames

    def ensure_ready(self) -> RecoveryOutcome:
        if self._is_ready(self._game.observe()):
            return RecoveryOutcome("ALREADY_READY")
        if not self._entry.entry_page_visible():
            return RecoveryOutcome(
                "SAFETY_PAUSED", "game page is neither ready nor a known entry page"
            )
        if not self._entry.enter_session().success:
            return RecoveryOutcome("SAFETY_PAUSED", "entry navigation failed")
        for _ in range(self._stable_frames):
            if not self._is_ready(self._game.observe()):
                return RecoveryOutcome(
                    "SAFETY_PAUSED", "post-entry screen was not stable and recognized"
                )
        return RecoveryOutcome("RECOVERED")

    def _is_ready(self, observation: ScreenObservation) -> bool:
        return (
            observation.screen == self._ready_screen
            and observation.ui_version is not None
            and observation.confidence >= self._min_confidence
        )


class RecoveringGameAdapter:
    """GamePort wrapper that recovers only at the known logged-in entry page."""

    def __init__(self, inner: GamePort, recovery: SessionRecoveryGate) -> None:
        self._inner = inner
        self._recovery = recovery

    def observe(self) -> ScreenObservation:
        return self._inner.observe()

    def navigate_to(self, coordinate: Coordinate) -> NavigationResult:
        if not self._recovery.ensure_ready().ready:
            return NavigationResult(success=False)
        return self._inner.navigate_to(coordinate)

    def load_fleet_preset(self, preset: FleetPresetRef) -> PresetObservation:
        return self._inner.load_fleet_preset(preset)

    def dispatch_attack(self, command: DispatchCommand) -> DispatchResult:
        # Never attempt session recovery immediately before an action. The
        # ActionGuard is responsible for refusing a changed/unknown final UI.
        return self._inner.dispatch_attack(command)

    def list_inflight(self) -> list[InflightFleet]:
        return self._inner.list_inflight()

    def open_battle_reports(self) -> ReportNavigationResult:
        return self._inner.open_battle_reports()
