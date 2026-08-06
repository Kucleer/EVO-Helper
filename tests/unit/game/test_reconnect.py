from __future__ import annotations

from collections import deque
from uuid import uuid4

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.domain.ports import (
    DispatchResult,
    InflightFleet,
    NavigationResult,
    PresetObservation,
    ReportNavigationResult,
    ScreenObservation,
)
from evo_helper.game.reconnect import RecoveringGameAdapter, SessionRecoveryGate


class Game:
    def __init__(self, observations: list[ScreenObservation]) -> None:
        self.observations = deque(observations)
        self.navigations: list[Coordinate] = []

    def observe(self) -> ScreenObservation:
        return self.observations[0] if len(self.observations) == 1 else self.observations.popleft()

    def navigate_to(self, coordinate: Coordinate) -> NavigationResult:
        self.navigations.append(coordinate)
        return NavigationResult(success=True)

    def load_fleet_preset(self, preset: FleetPresetRef) -> PresetObservation:
        return PresetObservation(preset.name, preset.signature, 1.0)

    def dispatch_attack(self, _command: DispatchCommand) -> DispatchResult:
        return DispatchResult(accepted=True, dry_run=False)

    def list_inflight(self) -> list[InflightFleet]:
        return []

    def open_battle_reports(self) -> ReportNavigationResult:
        return ReportNavigationResult(success=True)


class Entry:
    def __init__(self, visible: bool, result: bool = True) -> None:
        self.visible = visible
        self.result = result
        self.calls = 0

    def entry_page_visible(self) -> bool:
        return self.visible

    def enter_session(self) -> NavigationResult:
        self.calls += 1
        return NavigationResult(success=self.result)


READY = ScreenObservation("galaxy", "galaxy-v2", 1.0)
ENTRY = ScreenObservation("entry", "entry-v1", 1.0)


def _command() -> DispatchCommand:
    return DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(1, 1, 2),
        preset=FleetPresetRef("fleet", "signature"),
    )


def test_recovery_clicks_known_entry_once_and_requires_two_ready_frames() -> None:
    game = Game([ENTRY, READY, READY])
    entry = Entry(visible=True)

    outcome = SessionRecoveryGate(game, entry).ensure_ready()

    assert outcome.status == "RECOVERED"
    assert entry.calls == 1


def test_recovery_refuses_unknown_page_without_clicking() -> None:
    game = Game([ScreenObservation("popup", "popup-v2", 1.0)])
    entry = Entry(visible=False)

    outcome = SessionRecoveryGate(game, entry).ensure_ready()

    assert outcome.status == "SAFETY_PAUSED"
    assert entry.calls == 0


def test_recovering_adapter_blocks_navigation_when_post_entry_is_unstable() -> None:
    game = Game([ENTRY, READY, ScreenObservation("loading", None, 0.0)])
    entry = Entry(visible=True)
    adapter = RecoveringGameAdapter(game, SessionRecoveryGate(game, entry))

    result = adapter.navigate_to(Coordinate(1, 1, 1))

    assert not result.success
    assert game.navigations == []
    assert entry.calls == 1


def test_recovering_adapter_allows_navigation_when_already_ready() -> None:
    game = Game([READY])
    entry = Entry(visible=True)
    adapter = RecoveringGameAdapter(game, SessionRecoveryGate(game, entry))

    result = adapter.navigate_to(Coordinate(1, 1, 2))

    assert result.success
    assert game.navigations == [Coordinate(1, 1, 2)]
    assert entry.calls == 0


def test_recovering_adapter_does_not_recover_immediately_before_dispatch() -> None:
    game = Game([ENTRY])
    entry = Entry(visible=True)
    adapter = RecoveringGameAdapter(game, SessionRecoveryGate(game, entry))

    result = adapter.dispatch_attack(_command())

    assert result.accepted
    assert entry.calls == 0
