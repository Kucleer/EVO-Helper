"""把优先级分段展开成一串待扫坐标，并支持从游标续扫。

顺序由 `scan_priority.scan_segments()` 决定（2:001–200 优先，9 系补在末尾），
每个恒星系内的行星位由 `scan_bounds.ScanBounds` 决定（跳过恒为海盗的 1–4 位）。

**续扫按计划顺序，不按字典序。** 计划顺序里 `2:201–499` 排在 `1:001–499` 之前，
拿字典序比大小会把整个 1 系判成「已扫过」——那是看不见的数据缺口。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import ScanBounds
from evo_helper.domain.scan_priority import ScanSegment, scan_segments


class CursorNotInPlanError(ValueError):
    """游标不在当前计划里——多半是分段或位数边界改过了。

    这时既不能从头重扫（白跑几万个坐标），也不能当成扫完（留下静默缺口），
    所以只能停下来让人确认。
    """


def segment_bounds(
    segment: ScanSegment, bounds: ScanBounds | None = None
) -> tuple[Coordinate, Coordinate]:
    """返回分段的首尾坐标，位数窗口取自 `bounds`。"""
    window = bounds or ScanBounds()
    return (
        Coordinate(segment.galaxy, segment.first_system, window.first_position),
        Coordinate(segment.galaxy, segment.last_system, window.position_limit),
    )


def planned_segments(
    *,
    segments: Sequence[ScanSegment] | None = None,
    bounds: ScanBounds | None = None,
    priority_planets: Sequence[Coordinate] = (),
) -> tuple[tuple[ScanSegment, Coordinate, Coordinate], ...]:
    """分段连同各自的首尾坐标，按扫描优先级排好。"""
    window = bounds or ScanBounds()
    ordered = (
        tuple(segments)
        if segments is not None
        else scan_segments(priority_planets=priority_planets)
    )
    return tuple((seg, *segment_bounds(seg, window)) for seg in ordered)


def iter_scan_coordinates(
    *,
    segments: Sequence[ScanSegment] | None = None,
    bounds: ScanBounds | None = None,
    after: Coordinate | None = None,
    priority_planets: Sequence[Coordinate] = (),
) -> Iterator[Coordinate]:
    """按计划顺序产出待扫坐标；给了 `after` 就从它之后接着扫。"""
    window = bounds or ScanBounds()
    resumed = after is None
    for segment, _start, _end in planned_segments(
        segments=segments, bounds=window, priority_planets=priority_planets
    ):
        for system in range(segment.first_system, segment.last_system + 1):
            for position in range(window.first_position, window.position_limit + 1):
                coordinate = Coordinate(segment.galaxy, system, position)
                if resumed:
                    yield coordinate
                elif coordinate == after:
                    resumed = True
    if not resumed:
        raise CursorNotInPlanError(f"游标 {after} 不在当前扫描计划内")


def total_coordinates(
    *,
    segments: Sequence[ScanSegment] | None = None,
    bounds: ScanBounds | None = None,
    priority_planets: Sequence[Coordinate] = (),
) -> int:
    """计划覆盖的坐标总数。用乘法算，不展开迭代器。"""
    window = bounds or ScanBounds()
    return sum(
        segment.system_count * window.positions_per_system
        for segment, _start, _end in planned_segments(
            segments=segments, bounds=window, priority_planets=priority_planets
        )
    )
