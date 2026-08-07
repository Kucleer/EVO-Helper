from __future__ import annotations

import pytest

from evo_helper.domain.scan_bounds import TOTAL_GALAXIES
from evo_helper.domain.scan_priority import (
    DEFAULT_GALAXY_ORDER,
    DEFAULT_SEGMENTS,
    ScanSegment,
    scan_segments,
)


class TestRequestedOrder:
    """用户要求：先 2:001–2:200，再 2 系其余，再 1345678。"""

    def test_the_first_segment_is_galaxy_two_up_to_system_200(self) -> None:
        first = scan_segments()[0]
        assert (first.galaxy, first.first_system, first.last_system) == (2, 1, 200)

    def test_the_second_segment_is_the_rest_of_galaxy_two(self) -> None:
        second = scan_segments()[1]
        assert (second.galaxy, second.first_system, second.last_system) == (2, 201, 499)

    def test_galaxy_two_is_fully_covered_by_the_first_two_segments(self) -> None:
        first, second = scan_segments()[:2]
        assert first.last_system + 1 == second.first_system
        assert first.first_system == 1
        assert second.last_system == 499

    def test_the_listed_galaxies_follow_in_the_requested_order(self) -> None:
        after_two = [s.galaxy for s in scan_segments()[2:]]
        assert after_two[: len(DEFAULT_GALAXY_ORDER)] == list(DEFAULT_GALAXY_ORDER)

    def test_the_requested_galaxy_order_is_one_three_through_eight(self) -> None:
        assert DEFAULT_GALAXY_ORDER == (1, 3, 4, 5, 6, 7, 8)


class TestNothingIsSilentlyDropped:
    """优先不能变成只扫——用户的清单漏了 9 系，它必须仍被排在最后。"""

    def test_every_galaxy_is_covered_exactly_once(self) -> None:
        galaxies = [segment.galaxy for segment in scan_segments()]
        # 2 系拆成两段，所以出现两次；其余各一次。
        assert galaxies.count(2) == 2
        for galaxy in range(1, TOTAL_GALAXIES + 1):
            if galaxy != 2:
                assert galaxies.count(galaxy) == 1, galaxy

    def test_a_galaxy_missing_from_the_request_is_appended_not_dropped(self) -> None:
        galaxies = [segment.galaxy for segment in scan_segments()]
        assert 9 in galaxies
        assert galaxies[-1] == 9

    def test_unlisted_galaxies_come_after_the_listed_ones(self) -> None:
        galaxies = [segment.galaxy for segment in scan_segments()]
        assert galaxies.index(9) > galaxies.index(8)

    def test_the_segment_count_matches_the_universe(self) -> None:
        # 8 个非 2 系银河系 + 2 系的两段。
        assert len(scan_segments()) == (TOTAL_GALAXIES - 1) + 2


class TestSegmentGeometry:
    def test_a_segment_reports_its_system_count(self) -> None:
        assert ScanSegment(galaxy=2, first_system=1, last_system=200).system_count == 200

    def test_a_single_system_segment_counts_one(self) -> None:
        assert ScanSegment(galaxy=5, first_system=7, last_system=7).system_count == 1

    def test_a_reversed_segment_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="last_system"):
            ScanSegment(galaxy=2, first_system=200, last_system=1)

    def test_total_systems_equal_the_universe(self) -> None:
        assert sum(s.system_count for s in scan_segments()) == TOTAL_GALAXIES * 499


class TestConfigurability:
    def test_the_split_point_can_be_moved(self) -> None:
        segments = scan_segments(segments=((2, 1, 50), (2, 51, 499)))
        assert segments[0].last_system == 50
        assert segments[1].first_system == 51

    def test_a_segment_beyond_the_system_count_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="systems_per_galaxy"):
            scan_segments(segments=((2, 1, 600),))

    def test_the_default_segments_are_the_two_galaxy_two_halves(self) -> None:
        assert DEFAULT_SEGMENTS == ((2, 1, 200), (2, 201, 499))
