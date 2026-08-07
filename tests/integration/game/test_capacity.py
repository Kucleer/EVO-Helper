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


class TestReservedLines:
    """The user keeps some fleet lines free for their own use."""

    def coordinate(self, position: int = 1):  # type: ignore[no-untyped-def]
        from evo_helper.domain.models import Coordinate

        return Coordinate(1, 1, position)

    def in_flight(self, count: int):  # type: ignore[no-untyped-def]
        from evo_helper.domain.ports import InflightFleet

        return [InflightFleet(target=self.coordinate(i + 1)) for i in range(count)]

    def gate(self, limit: int, reserved: int):  # type: ignore[no-untyped-def]
        from evo_helper.game.capacity import LineCapacityGate

        return LineCapacityGate(limit, reserved_lines=reserved)

    def test_reserved_lines_shrink_the_usable_limit(self) -> None:
        check = self.gate(10, 3).check(self.in_flight(7), None)
        assert not check.available
        assert "reserved" in check.reason

    def test_dispatch_is_allowed_below_the_reserved_boundary(self) -> None:
        assert self.gate(10, 3).check(self.in_flight(6), None).available

    def test_zero_reserved_keeps_the_old_behaviour(self) -> None:
        assert self.gate(10, 0).check(self.in_flight(9), None).available
        assert not self.gate(10, 0).check(self.in_flight(10), None).available

    def test_game_feedback_must_leave_the_reserved_lines_free(self) -> None:
        """Three free slots with two reserved means only one may be taken."""
        assert self.gate(10, 2).check(self.in_flight(0), 3).available
        assert not self.gate(10, 2).check(self.in_flight(0), 2).available

    def test_reserving_every_line_blocks_all_dispatch(self) -> None:
        check = self.gate(4, 4).check(self.in_flight(0), None)
        assert not check.available

    def test_reserved_cannot_exceed_the_limit(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="reserved"):
            self.gate(3, 4)

    def test_negative_reserved_is_rejected(self) -> None:
        import pytest

        with pytest.raises(ValueError, match="reserved"):
            self.gate(3, -1)

    def test_check_reports_the_usable_limit(self) -> None:
        check = self.gate(10, 3).check(self.in_flight(0), None)
        assert check.limit == 7
