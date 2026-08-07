"""Fleet condition trees for the intel search.

A query is a tree of conditions over one target's latest *defender* fleet
snapshot. It never mixes in the attacker's fleet or a combined total: the
question the user is asking is what the bot is holding.

The tree is pure domain logic: it validates and evaluates itself. The storage
layer bounds the candidate set in SQL (coordinate span, latest report per
target) and then calls :meth:`ConditionGroup.matches` here, so the AND/OR
semantics exist in exactly one place.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from evo_helper.domain.models import Coordinate, CoordinateRange

#: A position is 1..N; a shorthand range end must cover the whole system.
MAX_POSITION = 999

_COORDINATE_RE = re.compile(r"^\s*(\d{1,3})\s*:\s*(\d{1,3})(?:\s*:\s*(\d{1,3}))?\s*$")


class InvalidQueryError(ValueError):
    """A query the user must fix. The message is shown in the UI."""


class Operator(StrEnum):
    GT = ">"
    GTE = ">="
    LT = "<"
    LTE = "<="
    EQ = "="
    NEQ = "!="


class GroupOperator(StrEnum):
    AND = "AND"
    OR = "OR"


@dataclass(frozen=True)
class QueryField:
    """Either the fleet total or one named ship type."""

    ship_type: str | None

    @classmethod
    def total(cls) -> QueryField:
        return cls(ship_type=None)

    @classmethod
    def ship(cls, name: str) -> QueryField:
        cleaned = name.strip()
        if not cleaned:
            raise InvalidQueryError("a ship condition needs a ship name")
        return cls(ship_type=cleaned)

    @property
    def is_total(self) -> bool:
        return self.ship_type is None

    @property
    def label(self) -> str:
        return "舰队总数" if self.is_total else str(self.ship_type)


@dataclass(frozen=True)
class FleetCondition:
    field: QueryField
    operator: Operator
    value: int

    def __post_init__(self) -> None:
        if self.value < 0:
            raise InvalidQueryError(
                f"{self.field.label} cannot be compared against a negative count"
            )

    def matches(self, counts: Mapping[str, int] | None) -> bool:
        if counts is None:
            return False
        actual = sum(counts.values()) if self.field.is_total else counts.get(self.field.label, 0)
        return bool(_COMPARE[self.operator](actual, self.value))

    def ship_names(self) -> Iterable[str]:
        if not self.field.is_total:
            yield self.field.label

    def matched_labels(self, counts: Mapping[str, int] | None) -> Iterable[str]:
        if self.matches(counts):
            yield f"{self.field.label} {self.operator.value} {self.value}"


@dataclass(frozen=True)
class ConditionGroup:
    operator: GroupOperator
    children: tuple[FleetCondition | ConditionGroup, ...]

    def __post_init__(self) -> None:
        if not self.children:
            raise InvalidQueryError("a condition group cannot be empty")

    def matches(self, counts: Mapping[str, int] | None) -> bool:
        """A target with no snapshot never matches, whatever the conditions say.

        Otherwise a condition like ``钛能守卫者 = 0`` would report every bot
        that has never been scanned as a hit.
        """
        if counts is None:
            return False
        results = (child.matches(counts) for child in self.children)
        return all(results) if self.operator is GroupOperator.AND else any(results)

    def ship_names(self) -> Iterable[str]:
        for child in self.children:
            yield from child.ship_names()

    def matched_labels(self, counts: Mapping[str, int] | None) -> Iterable[str]:
        """Labels of the leaf conditions that held, for the result summary.

        An OR group reports only the branches that actually matched, so the
        summary explains why this row is a hit rather than restating the query.
        """
        if counts is None:
            return
        for child in self.children:
            yield from child.matched_labels(counts)

    def validate_ship_names(self, known: Sequence[str] | set[str]) -> None:
        """Reject conditions on ship types the project has never seen.

        A typo would otherwise silently match nothing and read as "no bot has
        this ship" rather than "this query is wrong".
        """
        unknown = sorted({name for name in self.ship_names() if name not in known})
        if unknown:
            raise InvalidQueryError(f"unknown ship type(s): {', '.join(unknown)}")


def parse_coordinate_span(start: str, end: str) -> CoordinateRange:
    """Parse a coordinate range, accepting the same-galaxy shorthand.

    ``1:100`` - ``1:200`` means every position in systems 100 through 200.
    A start shorthand takes position 1; an end shorthand takes the last
    position, so both endpoints of the user's range are included.
    """
    first = _parse_endpoint(start, "start", default_position=1)
    last = _parse_endpoint(end, "end", default_position=MAX_POSITION)
    try:
        return CoordinateRange(start=first, end=last)
    except ValueError as exc:
        # CoordinateRange raises a bare ValueError. Re-raise as a query error so
        # the search surfaces one error type the UI can show verbatim.
        raise InvalidQueryError(f"coordinate range is out of order: {first} - {last}") from exc


def _parse_endpoint(text: str, label: str, *, default_position: int) -> Coordinate:
    match = _COORDINATE_RE.match(text or "")
    if match is None:
        raise InvalidQueryError(f"{label} coordinate must look like 1:100:1 or 1:100, got {text!r}")
    galaxy, system, position = match.groups()
    return Coordinate(int(galaxy), int(system), int(position or default_position))


_COMPARE = {
    Operator.GT: lambda actual, value: actual > value,
    Operator.GTE: lambda actual, value: actual >= value,
    Operator.LT: lambda actual, value: actual < value,
    Operator.LTE: lambda actual, value: actual <= value,
    Operator.EQ: lambda actual, value: actual == value,
    Operator.NEQ: lambda actual, value: actual != value,
}
