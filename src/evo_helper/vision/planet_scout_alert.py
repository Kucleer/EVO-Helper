"""Read the in-game ``你的行星被侦察`` security-mail format.

This is deliberately separate from combat and outbound-scout report readers:
the message is evidence that *someone else* probed one of our planets, not a
result that can close a dispatch or influence attack scheduling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.domain.models import Coordinate
from evo_helper.vision.parsers import GAME_DISPLAY_ZONE, parse_report_timestamp

COORDINATE_RE = re.compile(r"\[(\d{1,3}):(\d{1,3}):(\d{1,3})\]")
INTERCEPTED_PROBES_RE = re.compile(r"拦截了\s*(\d+)\s*个?敌方侦察探测器")
SOURCE_NAME_RE = re.compile(r"来自\s*\[\d{1,3}:\d{1,3}:\d{1,3}\]\s*(.+?)\s*的敌方")


class PlanetScoutAlertScreens(Protocol):
    def report_header(self) -> str: ...

    def security_message(self) -> str: ...


class PlanetScoutAlertUnreadable(ValueError):
    """Raised when a security mail does not contain the two required coordinates."""


@dataclass(frozen=True)
class PlanetScoutAlertReading:
    raw_time_text: str
    reported_at_utc: datetime
    source: Coordinate
    target: Coordinate
    source_name: str | None
    intercepted_probes: int | None
    subject: str
    raw_body: str


def read_planet_scout_alert(
    screens: PlanetScoutAlertScreens, *, subject: str
) -> PlanetScoutAlertReading:
    """Parse the message body without guessing missing source/target fields."""
    header = screens.report_header()
    raw_body = screens.security_message()
    reported_at = parse_report_timestamp(header, GAME_DISPLAY_ZONE)
    if reported_at is None:
        raise PlanetScoutAlertUnreadable("邮件页眉没有可用的时间")
    coordinates = [
        Coordinate(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        for match in COORDINATE_RE.finditer(raw_body)
    ]
    if len(coordinates) < 2:
        raise PlanetScoutAlertUnreadable("正文没有同时读到侦察来源与本方目标坐标")
    source_name_match = SOURCE_NAME_RE.search(raw_body)
    intercepted_match = INTERCEPTED_PROBES_RE.search(raw_body)
    return PlanetScoutAlertReading(
        raw_time_text=_time_text(header),
        reported_at_utc=reported_at,
        source=coordinates[0],
        target=coordinates[1],
        source_name=(source_name_match.group(1).strip() if source_name_match else None),
        intercepted_probes=(int(intercepted_match.group(1)) if intercepted_match else None),
        subject=subject,
        raw_body=raw_body,
    )


def _time_text(header: str) -> str:
    match = re.search(r"\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b", header)
    if match is None:
        raise PlanetScoutAlertUnreadable("邮件页眉没有可用的时间原文")
    return match.group(0)
