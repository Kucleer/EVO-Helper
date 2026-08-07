"""Frozen core domain values."""

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


@dataclass(frozen=True, order=True)
class Coordinate:
    galaxy: int
    system: int
    position: int

    def __post_init__(self) -> None:
        if min(self.galaxy, self.system, self.position) < 1:
            raise ValueError("coordinate components must be positive integers")

    def __str__(self) -> str:
        return f"{self.galaxy}:{self.system}:{self.position}"


@dataclass(frozen=True)
class CoordinateRange:
    start: Coordinate
    end: Coordinate

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("range end must not precede its start")

    def contains(self, coordinate: Coordinate) -> bool:
        return self.start <= coordinate <= self.end


class RunState(StrEnum):
    DRAFT = "DRAFT"
    ARMED = "ARMED"
    SCANNING = "SCANNING"
    WAITING_CAPACITY = "WAITING_CAPACITY"
    DRAINING = "DRAINING"
    #: 舰队在飞，助手已松手不持有会话，到 resume_at_utc 再回来。
    AWAITING_REPORT = "AWAITING_REPORT"
    #: 战报到点了但拿不到登录（用户在玩），退避重试中。
    WAITING_SESSION = "WAITING_SESSION"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    EMERGENCY_STOPPED = "EMERGENCY_STOPPED"


@dataclass(frozen=True)
class FleetPresetRef:
    name: str
    signature: str


@dataclass(frozen=True)
class DispatchCommand:
    run_id: UUID
    origin: Coordinate
    target: Coordinate
    preset: FleetPresetRef
