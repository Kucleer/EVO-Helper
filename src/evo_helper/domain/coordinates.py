"""Coordinate parsing and lexicographic range iteration.

A scan range is an inclusive interval over positive ``galaxy:system:position``
triples. Iteration follows dictionary order of (galaxy, system, position); the
position dimension is bounded by ``POSITION_LIMIT`` (the per-system planet
count of the game universe), which is a documented, configurable constant.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from .models import Coordinate

#: Per-system planet count used when carrying the scan cursor to the next system.
POSITION_LIMIT = 499

_COORDINATE_RE = re.compile(r"(?<!\d)(\d{1,4}):(\d{1,4}):(\d{1,4})(?!\d)")


def parse_coordinate(text: str) -> Coordinate | None:
    """Parse the first ``galaxy:system:position`` triple in *text*."""
    match = _COORDINATE_RE.search(text)
    if match is None:
        return None
    galaxy, system, position = (int(group) for group in match.groups())
    try:
        return Coordinate(galaxy, system, position)
    except ValueError:
        return None


def next_coordinate_after(
    current: Coordinate,
    end: Coordinate,
    position_limit: int = POSITION_LIMIT,
    first_position: int = 1,
) -> Coordinate | None:
    """Return the lexicographic successor of *current* within the range end.

    ``first_position`` is the floor a carry into the next system wraps to.
    Positions 1..4 are always pirates, so scanning them costs time and can
    never find a bot.
    """
    if current >= end:
        return None
    if current.position < position_limit:
        return Coordinate(current.galaxy, current.system, current.position + 1)
    if current.system < end.system:
        return Coordinate(current.galaxy, current.system + 1, first_position)
    if current.galaxy < end.galaxy:
        return Coordinate(current.galaxy + 1, 1, first_position)
    return None


def iter_coordinates(
    start: Coordinate,
    end: Coordinate,
    position_limit: int = POSITION_LIMIT,
    first_position: int = 1,
) -> Iterator[Coordinate]:
    """Yield every scannable coordinate in the inclusive range, in dictionary order.

    ``first_position`` skips the leading positions of every system rather than
    scanning them and throwing the result away.
    """
    if end < start:
        raise ValueError("range end must not precede its start")
    if position_limit < 1:
        raise ValueError("position_limit must be a positive integer")
    if first_position < 1:
        raise ValueError("first_position must be a positive integer")
    if start.position < first_position:
        raise ValueError(
            f"range start position {start.position} is below first_position {first_position}"
        )
    if start.position > position_limit or end.position > position_limit:
        raise ValueError("range endpoints exceed position_limit")
    current = start
    while True:
        yield current
        if current == end:
            return
        if current.position < position_limit:
            current = Coordinate(current.galaxy, current.system, current.position + 1)
        elif current.system < end.system:
            current = Coordinate(current.galaxy, current.system + 1, first_position)
        elif current.galaxy < end.galaxy:
            current = Coordinate(current.galaxy + 1, 1, first_position)
        else:
            raise AssertionError("unreachable: range iteration exceeded bounds")
