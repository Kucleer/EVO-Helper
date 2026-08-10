from __future__ import annotations

from uuid import uuid4

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


def test_action_guard_issues_single_use_token_when_enabled() -> None:
    guard = ActionGuard()
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
    guard = ActionGuard()
    command = _command()
    decision = guard.evaluate(command, ScreenObservation("attack", "attack-v2", 1.0))
    assert decision.token is not None
    final = guard.verify_and_consume(decision.token, ScreenObservation("attack", None, 0.0))
    assert not final.allowed


def test_action_guard_refuses_final_check_on_wrong_known_screen() -> None:
    guard = ActionGuard()
    decision = guard.evaluate(_command(), ScreenObservation("attack", "attack-v2", 1.0))
    assert decision.token is not None

    final = guard.verify_and_consume(
        decision.token, ScreenObservation("mail_list", "mail-list-v2", 1.0)
    )

    assert not final.allowed
    assert "immediately before dispatch" in final.reason


def test_safe_adapter_refuses_unknown_target_without_clicking() -> None:
    """目标不在配置好的扫描范围里就不点——闸门放行也不算数。"""
    inner = SimulatedGameAdapter()
    guard = ActionGuard()
    clicked: list[DispatchCommand] = []
    adapter = SafeGameAdapter(inner, guard, click=clicked.append)

    result = adapter.dispatch_attack(_command())
    assert not result.accepted
    assert clicked == []
    assert len(adapter.intents) == 1
    assert not adapter.intents[0][1].allowed
    assert "not in configured scan range" in adapter.intents[0][1].reason


def test_safe_adapter_requires_known_target() -> None:
    inner = SimulatedGameAdapter()
    guard = ActionGuard()
    adapter = SafeGameAdapter(inner, guard, known_targets=frozenset({Coordinate(9, 8, 7)}))
    command = DispatchCommand(
        run_id=uuid4(),
        origin=Coordinate(1, 1, 1),
        target=Coordinate(9, 8, 7),
        preset=FleetPresetRef("preset-a", "sig-a"),
    )
    result = adapter.dispatch_attack(command)
    assert result.accepted
