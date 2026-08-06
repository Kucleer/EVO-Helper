"""Frozen adapter protocols; imports here must stay framework-free."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from .models import Coordinate, DispatchCommand, FleetPresetRef


@dataclass(frozen=True)
class ScreenObservation:
    screen: str
    ui_version: str | None
    confidence: float


@dataclass(frozen=True)
class NavigationResult:
    success: bool


@dataclass(frozen=True)
class PresetObservation:
    name: str
    signature: str
    confidence: float


@dataclass(frozen=True)
class DispatchResult:
    accepted: bool
    dry_run: bool


@dataclass(frozen=True)
class InflightFleet:
    target: Coordinate


@dataclass(frozen=True)
class ReportNavigationResult:
    success: bool


@dataclass(frozen=True)
class CoordinateClaim:
    coordinate: Coordinate


@dataclass(frozen=True)
class ArtifactPayload:
    media_type: str
    content: bytes


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    sha256: str


class GamePort(Protocol):
    def observe(self) -> ScreenObservation: ...
    def navigate_to(self, coordinate: Coordinate) -> NavigationResult: ...
    def load_fleet_preset(self, preset: FleetPresetRef) -> PresetObservation: ...
    def dispatch_attack(self, command: DispatchCommand) -> DispatchResult: ...
    def list_inflight(self) -> list[InflightFleet]: ...
    def open_battle_reports(self) -> ReportNavigationResult: ...


class RepositoryPort(Protocol):
    def claim_next_coordinate(self, run_id: UUID) -> CoordinateClaim | None: ...
    def save_scan(self, scan: object) -> None: ...
    def save_attack_intent(self, intent: object) -> None: ...
    def save_dispatch(self, dispatch: object) -> None: ...
    def append_report(self, report: object) -> None: ...


class ClockPort(Protocol):
    def now_utc(self) -> datetime: ...
    def to_schedule_timezone(self, value: datetime) -> datetime: ...


class ArtifactPort(Protocol):
    def save(self, artifact: ArtifactPayload) -> ArtifactRef: ...
