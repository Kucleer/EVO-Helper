from __future__ import annotations

from uuid import uuid4

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate, DispatchCommand, FleetPresetRef
from evo_helper.domain.ports import ScreenObservation
from evo_helper.game.action_guard import ActionGuard
from evo_helper.game.safe_adapter import SafeGameAdapter
from evo_helper.game.simulator import SimulatedGameAdapter


def _command() -> DispatchCommand:
    return DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(9, 8, 7),
        preset=FleetPresetRef("preset-a", "sig-a"),
    )


def test_action_guard_refuses_when_dry_run() -> None:
    guard = ActionGuard(Settings(dry_run=True))
    decision = guard.evaluate(_command(), ScreenObservation("attack", "attack-v2", 1.0))
    assert not decision.allowed
    assert "dry_run" in decision.reason


def test_action_guard_issues_single_use_token_when_enabled() -> None:
    guard = ActionGuard(Settings(dry_run=False))
    command = _command()
    decision = guard.evaluate(command, ScreenObservation("attack", "attack-v2", 1.0))
    assert decision.allowed
    assert decision.token is not None

    final = guard.verify_and_consume(decision.token, ScreenObservation("attack", "attack-v2", 1.0))
    assert final.allowed
    replay = guard.verify_and_consume(decision.token, ScreenObservation("attack", "attack-v2", 1.0))
    assert not replay.allowed
    assert "already consumed" in replay.reason


def test_action_guard_refuses_unstable_reobservation() -> None:
    guard = ActionGuard(Settings(dry_run=False))
    command = _command()
    decision = guard.evaluate(command, ScreenObservation("attack", "attack-v2", 1.0))
    assert decision.token is not None
    final = guard.verify_and_consume(decision.token, ScreenObservation("attack", None, 0.0))
    assert not final.allowed


def test_safe_adapter_records_intent_without_click_in_dry_run() -> None:
    inner = SimulatedGameAdapter(dry_run=True)
    guard = ActionGuard(Settings(dry_run=True))
    clicked: list[DispatchCommand] = []
    adapter = SafeGameAdapter(inner, guard, click=clicked.append)

    result = adapter.dispatch_attack(_command())
    assert not result.accepted
    assert result.dry_run
    assert clicked == []
    assert len(adapter.intents) == 1
    assert not adapter.intents[0][1].allowed


def test_safe_adapter_requires_known_target() -> None:
    inner = SimulatedGameAdapter(dry_run=False)
    guard = ActionGuard(Settings(dry_run=False))
    adapter = SafeGameAdapter(inner, guard, known_targets=frozenset({Coordinate(9, 8, 7)}))
    command = DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(9, 8, 7),
        preset=FleetPresetRef("preset-a", "sig-a"),
    )
    result = adapter.dispatch_attack(command)
    assert result.accepted
    assert not result.dry_run
