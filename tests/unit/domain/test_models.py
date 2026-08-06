import pytest

from evo_helper.domain.models import Coordinate, CoordinateRange


def test_coordinate_orders_lexicographically_and_range_is_inclusive() -> None:
    start = Coordinate(1, 2, 3)
    end = Coordinate(1, 2, 5)
    coordinate_range = CoordinateRange(start=start, end=end)

    assert coordinate_range.contains(start)
    assert coordinate_range.contains(end)
    assert not coordinate_range.contains(Coordinate(1, 2, 6))


def test_coordinate_rejects_non_positive_component() -> None:
    with pytest.raises(ValueError, match="positive"):
        Coordinate(0, 1, 1)
