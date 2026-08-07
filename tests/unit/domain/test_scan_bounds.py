from __future__ import annotations

import pytest

from evo_helper.domain.coordinates import iter_coordinates, next_coordinate_after
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import (
    MAX_POSITION,
    PIRATE_POSITIONS,
    SYSTEMS_PER_GALAXY,
    TOTAL_GALAXIES,
    ScanBounds,
    galaxy_scan_order,
)


class TestPirateSkip:
    """1–4 号位恒为海盗，扫了不会有 bot。"""

    def test_first_position_skips_the_pirate_block(self) -> None:
        assert ScanBounds().first_position == 5

    def test_pirate_positions_are_one_through_four(self) -> None:
        assert PIRATE_POSITIONS == (1, 2, 3, 4)

    def test_pirate_positions_are_skipped(self) -> None:
        bounds = ScanBounds()
        for position in PIRATE_POSITIONS:
            assert bounds.skips(position), position

    def test_scannable_positions_are_not_skipped(self) -> None:
        bounds = ScanBounds()
        for position in (5, 12, 20):
            assert not bounds.skips(position), position

    def test_sixteen_positions_are_scannable_per_system(self) -> None:
        """最大 20 位，跳过 1–4，剩 5–20 共 16 位。"""
        assert ScanBounds().position_limit == MAX_POSITION == 20
        assert ScanBounds().positions_per_system == 16

    def test_disabling_the_skip_scans_every_position(self) -> None:
        bounds = ScanBounds(first_position=1)
        assert bounds.positions_per_system == 20
        assert not bounds.skips(1)

    def test_a_limit_below_the_floor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="position_limit"):
            ScanBounds(first_position=10, position_limit=9)


class TestIterationHonoursTheFloor:
    def test_wrapping_returns_to_the_floor_not_to_one(self) -> None:
        seen = list(
            iter_coordinates(
                Coordinate(2, 121, 19),
                Coordinate(2, 122, 6),
                position_limit=20,
                first_position=5,
            )
        )
        assert seen[:2] == [Coordinate(2, 121, 19), Coordinate(2, 121, 20)]
        assert seen[2] == Coordinate(2, 122, 5)

    def test_no_pirate_position_is_ever_yielded(self) -> None:
        seen = list(
            iter_coordinates(
                Coordinate(2, 121, 5),
                Coordinate(2, 123, 20),
                position_limit=20,
                first_position=5,
            )
        )
        assert all(coordinate.position >= 5 for coordinate in seen)

    def test_the_count_matches_positions_per_system(self) -> None:
        seen = list(
            iter_coordinates(
                Coordinate(2, 121, 5),
                Coordinate(2, 125, 20),
                position_limit=20,
                first_position=5,
            )
        )
        assert len(seen) == 5 * 16

    def test_successor_wraps_to_the_floor(self) -> None:
        assert next_coordinate_after(
            Coordinate(2, 121, 20),
            Coordinate(2, 125, 20),
            position_limit=20,
            first_position=5,
        ) == Coordinate(2, 122, 5)

    def test_a_start_below_the_floor_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="first_position"):
            list(iter_coordinates(Coordinate(2, 121, 1), Coordinate(2, 121, 9), first_position=5))


class TestGalaxyPriority:
    """先扫 2 系——玩家自己在那里，情报最有用。"""

    def test_the_preferred_galaxy_comes_first(self) -> None:
        assert galaxy_scan_order()[0] == 2

    def test_every_galaxy_appears_exactly_once(self) -> None:
        order = galaxy_scan_order()
        assert sorted(order) == list(range(1, TOTAL_GALAXIES + 1))

    def test_priority_does_not_become_exclusivity(self) -> None:
        """只改顺序，不改集合。"""
        assert len(galaxy_scan_order()) == TOTAL_GALAXIES

    def test_the_rest_stay_in_ascending_order(self) -> None:
        assert galaxy_scan_order() == [2, 1, 3, 4, 5, 6, 7, 8, 9]

    def test_a_preferred_galaxy_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="preferred"):
            galaxy_scan_order(total_galaxies=9, preferred=10)

    def test_a_single_galaxy_universe_works(self) -> None:
        assert galaxy_scan_order(total_galaxies=1, preferred=1) == [1]


class TestUniverseSize:
    def test_the_full_sweep_size_is_derived_not_guessed(self) -> None:
        systems = TOTAL_GALAXIES * SYSTEMS_PER_GALAXY
        assert systems == 4491
        assert systems * ScanBounds().positions_per_system == 71856
