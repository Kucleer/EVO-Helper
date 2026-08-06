"""Framework-free vision result types.

Every parsed value carries a confidence and the list of sources that agreed,
so the safety layer can reject low-confidence or conflicting observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from evo_helper.domain.models import Coordinate


@dataclass(frozen=True)
class CoordinateParse:
    value: Coordinate
    confidence: float
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class NameParse:
    value: str
    confidence: float
    sources: tuple[str, ...] = ()

    @property
    def is_bot(self) -> bool:
        return self.value.startswith("bot_")


@dataclass(frozen=True)
class FleetLine:
    ship_type: str
    count: int
    confidence: float
    sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class BattleFleetSnapshot:
    side: str
    fleet: tuple[FleetLine, ...]
    confidence: float


@dataclass(frozen=True)
class PageObservation:
    """A recognized page with its independent UI version field."""

    screen: str
    ui_version: str | None
    confidence: float
    fields: dict[str, str] = field(default_factory=dict)
    raw_time_text: str | None = None


@dataclass(frozen=True)
class MailItem:
    subject: str
    owner: NameParse | None
    coordinate: CoordinateParse | None
    raw_time_text: str | None = None


@dataclass(frozen=True)
class MailListObservation:
    ui_version: str | None
    items: tuple[MailItem, ...]
    page_number: int | None
    confidence: float


@dataclass(frozen=True)
class BattleDetail:
    ui_version: str | None
    attacker_origin: CoordinateParse
    defender_target: CoordinateParse
    attacker_fleet: tuple[FleetLine, ...]
    defender_fleet: tuple[FleetLine, ...]
    raw_time_text: str | None
    reported_at_utc: datetime | None
    confidence: float


@dataclass(frozen=True)
class BattleReplay:
    ui_version: str | None
    attacker_origin: CoordinateParse
    defender_target: CoordinateParse
    attacker_fleet: tuple[FleetLine, ...]
    defender_fleet: tuple[FleetLine, ...]
    confidence: float


@dataclass(frozen=True)
class GalaxyObservation:
    ui_version: str | None
    coordinates: tuple[CoordinateParse, ...]
    owners: dict[Coordinate, NameParse]
    confidence: float


@dataclass(frozen=True)
class PresetSignatureCheck:
    expected_name: str
    expected_signature: str
    observed_signature: str | None
    matched: bool
    confidence: float
