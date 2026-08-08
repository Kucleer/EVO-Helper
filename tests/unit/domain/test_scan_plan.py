"""扫描计划的顺序、位数窗口与续扫。"""

from __future__ import annotations

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import ScanBounds
from evo_helper.domain.scan_plan import (
    CursorNotInPlanError,
    iter_scan_coordinates,
    planned_segments,
    total_coordinates,
)
from evo_helper.domain.scan_priority import ScanSegment

TINY = (ScanSegment(2, 1, 2), ScanSegment(1, 1, 1))
NARROW = ScanBounds(first_position=5, position_limit=6)


def test_skips_the_pirate_positions() -> None:
    coordinates = list(iter_scan_coordinates(segments=[ScanSegment(2, 7, 7)], bounds=NARROW))
    assert coordinates == [Coordinate(2, 7, 5), Coordinate(2, 7, 6)]


def test_follows_segment_priority_not_lexicographic_order() -> None:
    galaxies = [c.galaxy for c in iter_scan_coordinates(segments=TINY, bounds=NARROW)]
    # 2 系整段排在 1 系之前，字典序则会反过来。
    assert galaxies == [2, 2, 2, 2, 1, 1]


def test_default_plan_covers_every_galaxy_exactly_once() -> None:
    galaxies = [segment.galaxy for segment, _start, _end in planned_segments()]
    assert sorted(set(galaxies)) == list(range(1, 10))
    # 2 系被拆成两段，其余各一段——「优先」只改顺序，不改集合。
    assert len(galaxies) == 10
    assert galaxies[:3] == [2, 2, 1]
    assert galaxies[-1] == 9


def test_default_plan_totals_the_whole_universe() -> None:
    assert total_coordinates() == 4491 * 16


def test_segment_bounds_use_the_position_window() -> None:
    _segment, start, end = planned_segments(bounds=NARROW)[0]
    assert (start, end) == (Coordinate(2, 1, 5), Coordinate(2, 200, 6))


def test_resumes_after_the_cursor() -> None:
    rest = list(iter_scan_coordinates(segments=TINY, bounds=NARROW, after=Coordinate(2, 2, 5)))
    assert rest == [Coordinate(2, 2, 6), Coordinate(1, 1, 5), Coordinate(1, 1, 6)]


def test_resuming_at_the_last_coordinate_yields_nothing() -> None:
    assert (
        list(iter_scan_coordinates(segments=TINY, bounds=NARROW, after=Coordinate(1, 1, 6))) == []
    )


def test_a_cursor_outside_the_plan_stops_instead_of_guessing() -> None:
    # 从头重扫要白跑几万个坐标，当成扫完则留下静默缺口——两个都不能选。
    with pytest.raises(CursorNotInPlanError):
        list(iter_scan_coordinates(segments=TINY, bounds=NARROW, after=Coordinate(4, 4, 4)))
