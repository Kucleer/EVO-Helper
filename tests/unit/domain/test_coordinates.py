import pytest

from evo_helper.domain.coordinates import (
    POSITION_LIMIT,
    iter_coordinates,
    next_coordinate_after,
    parse_coordinate,
)
from evo_helper.domain.models import Coordinate


def test_parse_coordinate_accepts_canonical_text() -> None:
    assert parse_coordinate("1:2:3") == Coordinate(1, 2, 3)
    assert parse_coordinate("battle at 12:34:56") == Coordinate(12, 34, 56)


def test_parse_coordinate_rejects_malformed_text() -> None:
    assert parse_coordinate("1:2") is None
    assert parse_coordinate("0:1:1") is None
    assert parse_coordinate("no coordinate here") is None


def test_iter_coordinates_is_inclusive_and_lexicographic() -> None:
    result = list(iter_coordinates(Coordinate(1, 2, 3), Coordinate(1, 2, 5)))
    assert result == [
        Coordinate(1, 2, 3),
        Coordinate(1, 2, 4),
        Coordinate(1, 2, 5),
    ]


def test_iter_coordinates_carries_across_systems_at_position_limit() -> None:
    result = list(iter_coordinates(Coordinate(1, 2, 3), Coordinate(1, 3, 1), position_limit=4))
    assert result == [Coordinate(1, 2, 3), Coordinate(1, 2, 4), Coordinate(1, 3, 1)]


def test_iter_coordinates_carries_across_galaxies() -> None:
    result = list(iter_coordinates(Coordinate(1, 2, 4), Coordinate(2, 1, 1), position_limit=4))
    assert result == [Coordinate(1, 2, 4), Coordinate(2, 1, 1)]


def test_iter_coordinates_rejects_reversed_range() -> None:
    with pytest.raises(ValueError, match="end must not precede"):
        list(iter_coordinates(Coordinate(2, 1, 1), Coordinate(1, 1, 1)))


def test_iter_coordinates_rejects_endpoint_beyond_position_limit() -> None:
    with pytest.raises(ValueError, match="position_limit"):
        list(iter_coordinates(Coordinate(1, 1, 1), Coordinate(1, 1, POSITION_LIMIT + 1)))


def test_next_coordinate_after_stops_at_range_end() -> None:
    assert next_coordinate_after(Coordinate(1, 1, 1), Coordinate(1, 1, 2)) == Coordinate(1, 1, 2)
    assert next_coordinate_after(Coordinate(1, 1, 2), Coordinate(1, 1, 2)) is None


def test_next_coordinate_after_carries_system() -> None:
    assert next_coordinate_after(
        Coordinate(1, 2, 4),
        Coordinate(1, 3, 1),
        position_limit=4,
    ) == Coordinate(1, 3, 1)
