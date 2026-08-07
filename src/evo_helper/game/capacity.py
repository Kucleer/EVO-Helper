"""Line-capacity checks: user limit, reserved lines, game feedback, in-flight."""

from __future__ import annotations

from dataclasses import dataclass

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ports import InflightFleet


@dataclass(frozen=True)
class CapacityCheck:
    available: bool
    reason: str
    in_flight: int
    #: The usable limit, i.e. the configured limit minus reserved lines.
    limit: int


class LineCapacityGate:
    """A slot is free only when user limit, game feedback, and in-flight list agree.

    ``reserved_lines`` are fleet lines the helper must never occupy, so the user
    keeps that many free for their own dispatches. They are subtracted from the
    configured limit, and they must also survive the game's own free-slot count:
    the helper will not take the last reserved slot even if the game says it is
    available.
    """

    def __init__(self, user_limit: int, *, reserved_lines: int = 0) -> None:
        if user_limit < 1:
            raise ValueError("user_limit must be positive")
        if reserved_lines < 0:
            raise ValueError("reserved_lines must not be negative")
        if reserved_lines > user_limit:
            raise ValueError("reserved_lines must not exceed user_limit")
        self._user_limit = user_limit
        self._reserved = reserved_lines

    @property
    def usable_limit(self) -> int:
        return self._user_limit - self._reserved

    def check(
        self,
        in_flight: list[InflightFleet],
        game_feedback_slots: int | None,
        target: Coordinate | None = None,
    ) -> CapacityCheck:
        in_flight_count = len(in_flight)
        usable = self.usable_limit

        if target is not None and any(fleet.target == target for fleet in in_flight):
            return CapacityCheck(
                False, f"target {target} already in flight", in_flight_count, usable
            )

        if in_flight_count >= usable:
            detail = (
                f"user limit reached ({in_flight_count}/{usable})"
                if self._reserved == 0
                else (
                    f"reserved lines protected ({in_flight_count}/{usable} used; "
                    f"{self._reserved} of {self._user_limit} kept free)"
                )
            )
            return CapacityCheck(False, detail, in_flight_count, usable)

        if game_feedback_slots is not None:
            if game_feedback_slots < 0:
                return CapacityCheck(
                    False,
                    "game feedback inconsistent (negative slot count)",
                    in_flight_count,
                    usable,
                )
            # Taking a slot must still leave the reserved lines free.
            if game_feedback_slots <= self._reserved:
                detail = (
                    "game reports full"
                    if game_feedback_slots == 0
                    else (f"only {game_feedback_slots} free line(s) and {self._reserved} reserved")
                )
                return CapacityCheck(False, detail, in_flight_count, usable)
            if in_flight_count >= game_feedback_slots:
                return CapacityCheck(
                    False, "in-flight list conflicts with game feedback", in_flight_count, usable
                )

        return CapacityCheck(True, "capacity available", in_flight_count, usable)
