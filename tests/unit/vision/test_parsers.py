from __future__ import annotations

from datetime import UTC

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.models import PageObservation
from evo_helper.vision.parsers import (
    UnknownUiVersionError,
    parse_battle_detail,
    parse_battle_replay,
    parse_galaxy,
    parse_iso_utc,
    parse_mail_list,
)

MAIL_OCR = """
battle report: bot_alice
1:2:3

battle report: bot_bob
4:5:6
"""


def test_parse_mail_list_extracts_owners_and_coordinates() -> None:
    page = PageObservation(screen="mail_list", ui_version="mail-list-v2", confidence=0.99)
    result = parse_mail_list(page, MAIL_OCR, "ocr")

    assert result.ui_version == "mail-list-v2"
    assert len(result.items) == 2
    assert result.items[0].coordinate is not None
    assert result.items[0].coordinate.value == Coordinate(1, 2, 3)
    assert result.items[0].owner is not None
    assert result.items[0].owner.value == "bot_alice"
    assert result.items[0].owner.is_bot


def test_parse_mail_list_refuses_unknown_version() -> None:
    page = PageObservation(screen="mail_list", ui_version=None, confidence=0.9)
    with pytest.raises(UnknownUiVersionError):
        parse_mail_list(page, MAIL_OCR, "ocr")


def test_parse_mail_list_refuses_legacy_version() -> None:
    page = PageObservation(screen="mail_list", ui_version="mail-list-v1", confidence=0.99)
    with pytest.raises(UnknownUiVersionError, match="mail-list-v1"):
        parse_mail_list(page, MAIL_OCR, "ocr")


def test_parse_battle_detail_matches_both_sides() -> None:
    page = PageObservation(
        screen="battle_detail",
        ui_version="battle-detail-v2",
        confidence=0.99,
        raw_time_text="2026-08-06 14:30:00",
    )
    ocr = """
attack from 1:2:3
defense at 9:8:7
attacker fleet:
light fighter x10
heavy fighter x4
defender fleet:
destroyer x2
"""
    result = parse_battle_detail(page, ocr, "ocr")

    assert result.attacker_origin.value == Coordinate(1, 2, 3)
    assert result.defender_target.value == Coordinate(9, 8, 7)
    assert result.reported_at_utc is not None
    assert result.reported_at_utc.tzinfo == UTC
    assert result.reported_at_utc.hour == 6  # 14:30 UTC+8 -> 06:30 UTC
    assert len(result.attacker_fleet) == 2
    assert result.attacker_fleet[0].ship_type == "light fighter"
    assert result.attacker_fleet[0].count == 10


def test_parse_battle_replay_uses_legacy_valid_ui() -> None:
    page = PageObservation(screen="battle_replay", ui_version="battle-replay-v1", confidence=0.98)
    ocr = """
1:2:3 -> 9:8:7
attacker fleet:
light fighter x10
defender fleet:
destroyer x2
"""
    result = parse_battle_replay(page, ocr, "ocr")
    assert result.ui_version == "battle-replay-v1"
    assert result.attacker_origin.value == Coordinate(1, 2, 3)
    assert len(result.attacker_fleet) == 1
    assert result.defender_fleet[0].ship_type == "destroyer"


def test_parse_galaxy_tracks_bot_owners() -> None:
    page = PageObservation(screen="galaxy", ui_version="galaxy-v2", confidence=0.99)
    ocr = """
1:1:1 bot_alice

1:1:2 player_carol

1:1:3 bot_dave
"""
    result = parse_galaxy(page, ocr, "ocr")
    assert len(result.coordinates) == 3
    assert result.owners[Coordinate(1, 1, 1)].value == "bot_alice"
    assert result.owners[Coordinate(1, 1, 1)].is_bot
    assert not result.owners[Coordinate(1, 1, 2)].is_bot


def test_parse_iso_utc_handles_explicit_and_implicit_zones() -> None:
    explicit = parse_iso_utc("2026-08-06T06:30:00Z")
    implicit = parse_iso_utc("2026-08-06 14:30:00")
    assert explicit is not None and explicit.hour == 6
    assert implicit is not None and implicit.hour == 6
    assert parse_iso_utc("no timestamp here") is None
