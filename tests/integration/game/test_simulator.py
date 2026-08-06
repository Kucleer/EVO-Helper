from __future__ import annotations

from uuid import uuid4

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.game.simulator import SimulatedGameAdapter


def test_simulator_records_dispatch_intent_in_dry_run() -> None:
    adapter = SimulatedGameAdapter(dry_run=True)
    command = DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(9, 8, 7),
        preset=FleetPresetRef("preset-a", "sig-a"),
    )
    result = adapter.dispatch_attack(command)
    assert result.accepted
    assert result.dry_run
    assert adapter.dispatched == (command,)
    assert adapter.list_inflight() == []


def test_simulator_implements_game_port_round_trip() -> None:
    adapter = SimulatedGameAdapter(dry_run=False)
    adapter.register_preset(FleetPresetRef("preset-a", "sig-a"))
    assert adapter.load_fleet_preset(FleetPresetRef("preset-a", "sig-a")).confidence == 1.0
    assert adapter.navigate_to(Coordinate(1, 1, 1)).success
    assert adapter.open_battle_reports().success
    assert adapter.observe().screen == "mail_list"
