from __future__ import annotations

from uuid import uuid4

from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.game.simulator import SimulatedGameAdapter


def test_simulator_records_dispatch_and_occupies_a_line() -> None:
    adapter = SimulatedGameAdapter()
    command = DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(9, 8, 7),
        preset=FleetPresetRef("preset-a", "sig-a"),
    )
    result = adapter.dispatch_attack(command)
    assert result.accepted
    assert adapter.dispatched == (command,)
    assert [fleet.target for fleet in adapter.list_inflight()] == [Coordinate(9, 8, 7)]


def test_simulator_refuses_once_every_line_is_busy() -> None:
    """航线满了就拒——这是模拟器唯一会拒派遣的理由。"""
    adapter = SimulatedGameAdapter(capacity=1)
    for position in (7, 6):
        adapter.dispatch_attack(
            DispatchCommand(
                run_id=uuid4(),
                origin=Coordinate(1, 1, 1),
                target=Coordinate(9, 8, position),
                preset=FleetPresetRef("preset-a", "sig-a"),
            )
        )
    assert len(adapter.list_inflight()) == 1


def test_simulator_implements_game_port_round_trip() -> None:
    adapter = SimulatedGameAdapter()
    adapter.register_preset(FleetPresetRef("preset-a", "sig-a"))
    assert adapter.load_fleet_preset(FleetPresetRef("preset-a", "sig-a")).confidence == 1.0
    assert adapter.navigate_to(Coordinate(1, 1, 1)).success
    assert adapter.open_battle_reports().success
    assert adapter.observe().screen == "mail_list"
