"""Deterministic field parsers for known UI screens.

These parsers work on normalized text produced by an OCR engine. Unknown UI
versions are rejected: the caller must stop and preserve a diagnostic capture
instead of guessing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from evo_helper.domain.models import Coordinate
from evo_helper.vision.models import (
    BattleDetail,
    BattleFleetSnapshot,
    BattleReplay,
    CoordinateParse,
    FleetLine,
    GalaxyObservation,
    MailItem,
    MailListObservation,
    NameParse,
    PageObservation,
    PresetSignatureCheck,
)

COORDINATE_RE = re.compile(r"(?<!\d)(\d{1,3}):(\d{1,3}):(\d{1,3})(?!\d)")
BOT_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(bot_[A-Za-z0-9_]{1,32})(?![A-Za-z0-9_])")
OWNER_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{2,32})(?![A-Za-z0-9_])")
FLEET_LINE_RE = re.compile(r"^([A-Za-z0-9_\- \u4e00-\u9fff]{2,40}?)\s*[xX\u00d7]\s*(\d{1,7})$")
ISO_TIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:Z|[+-]\d{2}:?\d{2})?"
)


class UnknownUiVersionError(RuntimeError):
    """Raised when a screen cannot be recognized; callers must stop safely."""


def parse_coordinate(text: str, source: str, confidence: float = 1.0) -> CoordinateParse | None:
    match = COORDINATE_RE.search(text)
    if match is None:
        return None
    try:
        value = Coordinate(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None
    return CoordinateParse(value=value, confidence=confidence, sources=(source,))


def parse_name(text: str, source: str, confidence: float = 1.0) -> NameParse | None:
    match = BOT_NAME_RE.search(text) or OWNER_RE.search(text)
    if match is None:
        return None
    return NameParse(value=match.group(1), confidence=confidence, sources=(source,))


def parse_fleet_line(text: str, source: str, confidence: float = 1.0) -> FleetLine | None:
    match = FLEET_LINE_RE.match(text.strip())
    if match is None:
        return None
    ship_type = match.group(1).strip()
    if not ship_type:
        return None
    return FleetLine(
        ship_type=ship_type,
        count=int(match.group(2)),
        confidence=confidence,
        sources=(source,),
    )


def parse_iso_utc(text: str) -> datetime | None:
    """Parse an ISO-ish timestamp and normalize it to UTC.

    A bare local time is interpreted as game local time (UTC+8, the configured
    business timezone) and converted to UTC. Text with an explicit zone keeps
    that offset. Returns None when no timestamp can be parsed.
    """
    match = ISO_TIME_RE.search(text)
    if match is None:
        return None
    year, month, day, hour, minute = (int(v) for v in match.groups()[:5])
    second = int(match.group(6) or 0)
    # Recover the zone suffix from the raw match text.
    raw = match.group(0)
    if raw.endswith("Z"):
        zone = UTC
    else:
        zone_match = re.search(r"([+-]\d{2}):?(\d{2})$", raw)
        if zone_match is not None:
            offset_hours = int(zone_match.group(1))
            offset_minutes = int(zone_match.group(2))
            if offset_hours < 0:
                offset_minutes = -offset_minutes
            zone = timezone(timedelta(hours=offset_hours, minutes=offset_minutes), name="offset")
        else:
            zone = timezone(timedelta(hours=8), name="game-local")
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=zone).astimezone(UTC)
    except ValueError:
        return None


def parse_mail_list(page: PageObservation, ocr_text: str, source: str) -> MailListObservation:
    if page.ui_version is None:
        raise UnknownUiVersionError("mail list UI version unknown; refusing to navigate")
    if page.ui_version != "mail-list-v2":
        raise UnknownUiVersionError(f"unsupported mail list UI version: {page.ui_version}")
    items: list[MailItem] = []
    for block in _split_blocks(ocr_text):
        coordinate = parse_coordinate(block, source)
        owner = parse_name(block, source)
        if coordinate is not None:
            items.append(MailItem(subject=block.strip(), owner=owner, coordinate=coordinate))
    return MailListObservation(
        ui_version=page.ui_version, items=tuple(items), page_number=None, confidence=page.confidence
    )


def parse_battle_detail(page: PageObservation, ocr_text: str, source: str) -> BattleDetail:
    if page.ui_version not in {"battle-detail-v2", "battle-detail-v1"}:
        raise UnknownUiVersionError(f"unsupported battle detail UI version: {page.ui_version}")
    coordinates = [
        c
        for c in (parse_coordinate(line, source) for line in ocr_text.splitlines())
        if c is not None
    ]
    origin = coordinates[0] if coordinates else _missing_coordinate()
    target = coordinates[1] if len(coordinates) > 1 else origin
    attacker, defender = _split_fleet_sides(ocr_text, source)
    reported_at = parse_iso_utc(page.raw_time_text or ocr_text)
    return BattleDetail(
        ui_version=page.ui_version,
        attacker_origin=origin,
        defender_target=target,
        attacker_fleet=attacker,
        defender_fleet=defender,
        raw_time_text=page.raw_time_text,
        reported_at_utc=reported_at,
        confidence=page.confidence,
    )


def parse_battle_replay(page: PageObservation, ocr_text: str, source: str) -> BattleReplay:
    if page.ui_version not in {"battle-replay-v2", "battle-replay-v1"}:
        raise UnknownUiVersionError(f"unsupported battle replay UI version: {page.ui_version}")
    coordinates = [
        c
        for c in (parse_coordinate(line, source) for line in ocr_text.splitlines())
        if c is not None
    ]
    origin = coordinates[0] if coordinates else _missing_coordinate()
    target = coordinates[1] if len(coordinates) > 1 else origin
    attacker, defender = _split_fleet_sides(ocr_text, source)
    return BattleReplay(
        ui_version=page.ui_version,
        attacker_origin=origin,
        defender_target=target,
        attacker_fleet=attacker,
        defender_fleet=defender,
        confidence=page.confidence,
    )


def parse_galaxy(page: PageObservation, ocr_text: str, source: str) -> GalaxyObservation:
    if page.ui_version not in {"galaxy-v2", "galaxy-v1"}:
        raise UnknownUiVersionError(f"unsupported galaxy UI version: {page.ui_version}")
    coordinates: list[CoordinateParse] = []
    owners: dict[Coordinate, NameParse] = {}
    for block in _split_blocks(ocr_text):
        coordinate = parse_coordinate(block, source)
        owner = parse_name(block, source)
        if coordinate is not None:
            coordinates.append(coordinate)
            if owner is not None:
                owners[coordinate.value] = owner
    return GalaxyObservation(
        ui_version=page.ui_version,
        coordinates=tuple(coordinates),
        owners=owners,
        confidence=page.confidence,
    )


def check_preset_signature(
    page: PageObservation, expected_name: str, expected_signature: str, ocr_text: str, source: str
) -> PresetSignatureCheck:
    if page.ui_version not in {"attack-v2", "attack-v1"}:
        raise UnknownUiVersionError(f"unsupported attack UI version: {page.ui_version}")
    observed = ocr_text.strip() or None
    matched = observed == expected_signature
    return PresetSignatureCheck(
        expected_name=expected_name,
        expected_signature=expected_signature,
        observed_signature=observed,
        matched=matched,
        confidence=1.0 if matched else 0.0,
    )


def to_fleet_snapshot(
    side: str, fleet: tuple[FleetLine, ...], confidence: float
) -> BattleFleetSnapshot:
    return BattleFleetSnapshot(side=side, fleet=fleet, confidence=confidence)


def _split_blocks(text: str) -> list[str]:
    return [block for block in re.split(r"\n\s*\n", text) if block.strip()]


def _split_fleet_sides(
    ocr_text: str, source: str
) -> tuple[tuple[FleetLine, ...], tuple[FleetLine, ...]]:
    """Split fleet lines into attacker/defender groups by side markers.

    Lines are attributed to a side by the nearest preceding marker; lines
    without any marker stay unattributed and are excluded rather than guessed.
    """
    attacker: list[FleetLine] = []
    defender: list[FleetLine] = []
    current: list[FleetLine] | None = None
    for line in ocr_text.splitlines():
        lowered = line.lower()
        if "attacker" in lowered or "attack" in lowered or "攻方" in line:
            current = attacker
            continue
        if "defender" in lowered or "defense" in lowered or "守方" in line:
            current = defender
            continue
        if current is None:
            continue
        fleet = parse_fleet_line(line, source)
        if fleet is not None:
            current.append(fleet)
    return tuple(attacker), tuple(defender)


def _missing_coordinate() -> CoordinateParse:
    return CoordinateParse(value=Coordinate(1, 1, 1), confidence=0.0, sources=())
