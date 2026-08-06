"""Line-capacity checks: user limit, game feedback, and in-flight fleets."""

from __future__ import annotations

from dataclasses import dataclass

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ports import InflightFleet


@dataclass(frozen=True)
class CapacityCheck:
    available: bool
    reason: str
    in_flight: int
    limit: int


class LineCapacityGate:
    """A slot is free only when user limit, game feedback, and in-flight list agree."""

    def __init__(self, user_limit: int) -> None:
        if user_limit < 1:
            raise ValueError("user_limit must be positive")
        self._user_limit = user_limit

    def check(
        self,
        in_flight: list[InflightFleet],
        game_feedback_slots: int | None,
        target: Coordinate | None = None,
    ) -> CapacityCheck:
        in_flight_count = len(in_flight)
        if target is not None and any(fleet.target == target for fleet in in_flight):
            return CapacityCheck(
                False, f"target {target} already in flight", in_flight_count, self._user_limit
            )
        if in_flight_count >= self._user_limit:
            return CapacityCheck(
                False,
                f"user limit reached ({in_flight_count}/{self._user_limit})",
                in_flight_count,
                self._user_limit,
            )
        if game_feedback_slots is not None:
            if game_feedback_slots < 0:
                return CapacityCheck(
                    False,
                    "game feedback inconsistent (negative slot count)",
                    in_flight_count,
                    self._user_limit,
                )
            if game_feedback_slots == 0:
                return CapacityCheck(False, "game reports full", in_flight_count, self._user_limit)
            if in_flight_count >= game_feedback_slots:
                return CapacityCheck(
                    False,
                    "in-flight list conflicts with game feedback",
                    in_flight_count,
                    self._user_limit,
                )
        return CapacityCheck(True, "capacity available", in_flight_count, self._user_limit)
