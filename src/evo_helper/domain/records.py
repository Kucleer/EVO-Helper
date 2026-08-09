"""Concrete domain records exchanged with the repository.

The frozen RepositoryPort accepts ``object`` payloads; these records give that
contract deterministic, framework-free shapes for scans, intents, dispatches,
reports, events, revisits, and fleet-diff results. All timestamps are
timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .models import Coordinate, FleetPresetRef


@dataclass(frozen=True)
class CoordinateScan:
    run_id: UUID
    coordinate: Coordinate
    scanned_at_utc: datetime
    owner_name: str | None = None
    is_bot: bool = False
    confidence: float = 0.0
    evidence_artifact_id: UUID | None = None


@dataclass(frozen=True)
class AttackIntent:
    intent_id: UUID
    run_id: UUID
    origin: Coordinate
    target: Coordinate
    preset: FleetPresetRef
    cycle_start_utc: datetime
    created_at_utc: datetime
    guard_status: str = "PENDING"
    forced_revisit: bool = False


@dataclass(frozen=True)
class AttackDispatch:
    dispatch_id: UUID
    intent_id: UUID
    dispatched_at_utc: datetime
    dry_run: bool
    accepted: bool
    evidence_artifact_id: UUID | None = None


@dataclass(frozen=True)
class FleetSnapshotEntry:
    side: str
    ship_type: str
    count: int
    round_no: int | None = None
    #: 这一行的数没有把握，界面上要标出来。
    uncertain: bool = False


@dataclass(frozen=True)
class BattleReport:
    report_id: UUID
    reported_at_utc: datetime
    attacker_origin: Coordinate
    defender_target: Coordinate
    raw_time_text: str | None = None
    ui_version: str | None = None
    match_confidence: float = 0.0
    manual_review_status: str = "PENDING"
    is_from_revisit: bool = False
    fleet: tuple[FleetSnapshotEntry, ...] = ()
    #: 战斗详情页的「单位」总数，与 `fleet` 是两个独立来源；
    #: 大舰队的逐行数量是四舍五入显示，相加凑不出这个数。
    attacker_units: int | None = None
    defender_units: int | None = None


@dataclass(frozen=True)
class StateEvent:
    aggregate_type: str
    aggregate_id: UUID
    event: str
    occurred_at_utc: datetime
    before_state: str | None = None
    after_state: str | None = None


@dataclass(frozen=True)
class UiObservation:
    observation_id: UUID
    screen: str
    ui_version: str | None
    detection_result: str | None
    confidence: float
    observed_at_utc: datetime
    evidence_artifact_id: UUID | None = None


@dataclass(frozen=True)
class TargetRevisit:
    revisit_id: UUID
    scope: str
    reason: str
    requested_at_utc: datetime
    target: Coordinate | None = None
    status: str = "PENDING"
    executed_at_utc: datetime | None = None


@dataclass(frozen=True)
class ReportHistoryEntry:
    report_id: UUID
    reported_at_utc: datetime
    side: str
    ship_type: str
    count: int
    is_from_revisit: bool
    match_confidence: float
    manual_review_status: str


@dataclass(frozen=True)
class ShipTypeDiff:
    ship_type: str
    before_count: int
    after_count: int
    absolute_change: int
    percent_change: float | None
    status: str
    first_seen: bool


@dataclass(frozen=True)
class FleetDiff:
    before_report_id: UUID | None
    after_report_id: UUID
    side: str
    total_before: int
    total_after: int
    total_change: int
    ships: tuple[ShipTypeDiff, ...]
    is_from_revisit: bool
    match_confidence: float
    manual_review_status: str
