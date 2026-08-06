from __future__ import annotations

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ports import InflightFleet
from evo_helper.game.capacity import LineCapacityGate


def test_capacity_available_when_below_limit() -> None:
    gate = LineCapacityGate(user_limit=3)
    result = gate.check([], 3)
    assert result.available


def test_capacity_refuses_inflight_target() -> None:
    gate = LineCapacityGate(user_limit=3)
    target = Coordinate(9, 8, 7)
    result = gate.check([InflightFleet(target=target)], 3, target=target)
    assert not result.available
    assert "already in flight" in result.reason


def test_capacity_refuses_user_limit() -> None:
    gate = LineCapacityGate(user_limit=2)
    in_flight = [InflightFleet(Coordinate(1, 1, 1)), InflightFleet(Coordinate(2, 2, 2))]
    result = gate.check(in_flight, 5)
    assert not result.available
    assert "user limit" in result.reason


def test_capacity_refuses_game_feedback_full() -> None:
    gate = LineCapacityGate(user_limit=3)
    result = gate.check([], 0)
    assert not result.available
    assert "game reports full" in result.reason


def test_capacity_refuses_feedback_conflict() -> None:
    gate = LineCapacityGate(user_limit=3)
    in_flight = [InflightFleet(Coordinate(1, 1, 1)), InflightFleet(Coordinate(2, 2, 2))]
    result = gate.check(in_flight, 1)
    assert not result.available
    assert "conflicts" in result.reason


def test_capacity_requires_positive_limit() -> None:
    with pytest.raises(ValueError):
        LineCapacityGate(user_limit=0)
