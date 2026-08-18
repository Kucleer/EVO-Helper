"""Deterministic field parsers for known UI screens.

These parsers work on normalized text produced by an OCR engine. Unknown UI
versions are rejected: the caller must stop and preserve a diagnostic capture
instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import Enum

from evo_helper.domain.flight_estimate import (
    BRIEFING_SKEW_TOLERANCE as _BRIEFING_SKEW_TOLERANCE,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.text import edit_distance
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
    ReplayRound,
    VersusBlock,
    VersusSide,
)

COORDINATE_RE = re.compile(r"(?<!\d)(\d{1,3}):(\d{1,3}):(\d{1,3})(?!\d)")
BOT_NAME_RE = re.compile(r"(?<![A-Za-z0-9_])(bot_[A-Za-z0-9_]{1,32})(?![A-Za-z0-9_])")
OWNER_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z0-9_]{2,32})(?![A-Za-z0-9_])")
FLEET_LINE_RE = re.compile(r"^([A-Za-z0-9_\- \u4e00-\u9fff]{2,40}?)\s*[xX\u00d7]\s*(\d{1,7})$")
ISO_TIME_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?(?:Z|[+-]\d{2}:?\d{2})?"
)


#: ``DD/MM/YYYY HH:MM:SS`` as rendered by the live mail list and report header.
REPORT_TIME_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2}):(\d{2})")

#: 同一个时间戳，但**日期与时刻之间允许没有空白**。
#:
#: 窄 ROI 加纯数字白名单读那一行时，tesseract 稳定地把中间那个空格吞掉：
#: `11/08/2026 01:32:37` 读成 `11/08/202601:32:37`。数字一个不差，只是分隔没了，
#: 而 `REPORT_TIME_RE` 要求 `\s+`，于是明明读对了却匹配不上。
#: **不要为此放松 `REPORT_TIME_RE`**——它还要在整段页眉文本里搜时间，
#: 放松之后会在一长串数字中间凑出一个假时间。
_SQUASHED_REPORT_TIME_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*(\d{2}):(\d{2}):(\d{2})")


def normalise_report_time(text: str | None) -> str | None:
    """把窄 ROI 读出来的时间补回标准写法；认不出返回 None。"""
    match = _SQUASHED_REPORT_TIME_RE.search(text or "")
    if match is None:
        return None
    day, month, year, hour, minute, second = match.groups()
    return f"{day}/{month}/{year} {hour}:{minute}:{second}"


#: The game renders every in-game time in UTC+0 (confirmed 2026-08-07).
#:
#: This is not the schedule timezone. The user's run window (for example
#: 08:00-10:00) is expressed in UTC+8 and lives in ``domain.scheduling``.
#: Conflating the two would shift every report by eight hours and break the
#: strict origin + target + time match against a dispatch.
GAME_DISPLAY_ZONE = UTC

#: Fleet units, in the order the game lists them. Order is kept so a snapshot
#: table reads the same way as the in-game list rather than alphabetically.
#: Supplied by the user from the in-game catalogue on 2026-08-07.
SHIP_ORDER: tuple[str, ...] = (
    "轻型战斗机",
    "重型战斗机",
    "巡洋舰",
    "战列舰",
    "小型运输船",
    "大型运输船",
    "回收船",
    "殖民船",
    "探测器",
    "无畏舰",
    "轰炸机",
    "毁灭者",
    "裂变者",
    "深空吞噬者",
    "噬能截击者",
    "钛能守卫者",
    "收割者",
    "湮灭之星",
)

#: Planet-side units in the same catalogue: missiles, satellites, shields and
#: turrets. They share the fleet row layout but are not ships, so a snapshot
#: must keep them apart from a fleet.
DEFENCE_ORDER: tuple[str, ...] = (
    "行星际导弹",
    "太阳能卫星",
    "拦截导弹",
    "小型护盾",
    "大型护盾",
    "离子炮",
    "火箭发射器",
    "轻型激光炮",
    "重型激光炮",
    "MK2 加农炮",
    "等离子炮",
)

SHIP_NAMES = frozenset(SHIP_ORDER)
DEFENCE_NAMES = frozenset(DEFENCE_ORDER)

#: Every catalogue name, in game order, for display and vocabulary checks.
UNIT_ORDER: tuple[str, ...] = SHIP_ORDER + DEFENCE_ORDER

#: ``名称`` followed by whitespace and a count. A count of ``0`` is a real row.
FLEET_COLUMN_RE = re.compile(r"^(.+?)\s{1,}(\d{1,7})$")


class ReportKind(Enum):
    """Mail report subjects that the report matcher must tell apart.

    Only :attr:`ATTACK` may be matched against a dispatch. ``海盗攻击报告``
    contains ``攻击报告`` as a substring but is a pirate battle, and matching it
    to a bot dispatch would close the wrong attack.
    """

    ATTACK = "attack"
    PIRATE = "pirate"
    SCOUT = "scout"
    #: 这是本方收到的安全提示，不是我方派出的侦察报告；不能进入侦察/攻击
    #: 战报链路，更不能被拿来认领任何派遣。
    PLANET_SCOUTED = "planet_scouted"
    SYSTEM = "system"
    UNKNOWN = "unknown"

    @property
    def is_dispatch_matchable(self) -> bool:
        return self is ReportKind.ATTACK


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


def parse_all_coordinates(text: str, source: str, confidence: float = 1.0) -> list[CoordinateParse]:
    """Return every coordinate in ``text`` in reading order.

    A single OCR line can carry both sides of a report (``1:2:3 -> 9:8:7``), so
    stopping at the first match would drop the defender.
    """
    parsed: list[CoordinateParse] = []
    for match in COORDINATE_RE.finditer(text):
        try:
            value = Coordinate(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        parsed.append(CoordinateParse(value=value, confidence=confidence, sources=(source,)))
    return parsed


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

    A bare time is read in :data:`GAME_DISPLAY_ZONE` (UTC+0, the zone the game
    renders in). Text with an explicit zone keeps that offset. Returns None
    when no timestamp can be parsed.
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
            zone = GAME_DISPLAY_ZONE
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=zone).astimezone(UTC)
    except ValueError:
        return None


def parse_report_timestamp(text: str, display_zone: tzinfo) -> datetime | None:
    """Parse the live ``DD/MM/YYYY HH:MM:SS`` report time and normalize to UTC.

    ``display_zone`` is required rather than defaulted: the zone the game
    renders report times in is a property of the account/server, and silently
    assuming one would shift every report by whole hours and break strict
    dispatch matching. Callers pass the configured zone and keep the raw text.
    """
    match = REPORT_TIME_RE.search(text)
    if match is None:
        return None
    day, month, year, hour, minute, second = (int(value) for value in match.groups())
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=display_zone)
    except ValueError:
        return None
    return local.astimezone(UTC)


def classify_report_subject(subject: str) -> ReportKind:
    """Classify a mail subject. Order matters: pirate is checked before attack."""
    text = subject.strip()
    if "你的行星被侦察" in text:
        return ReportKind.PLANET_SCOUTED
    if "海盗" in text:
        return ReportKind.PIRATE
    if "攻击报告" in text:
        return ReportKind.ATTACK
    if "侦察报告" in text:
        return ReportKind.SCOUT
    if "战报" in text:
        return ReportKind.SYSTEM
    return ReportKind.UNKNOWN


def classify_unit(name: str) -> str:
    if name in SHIP_NAMES:
        return "ship"
    if name in DEFENCE_NAMES:
        return "defence"
    return "unknown"


#: Names shorter than this are never snapped: one edit is too large a share of
#: the string to attribute to OCR rather than to a genuinely different unit.
MIN_SNAP_LENGTH = 3


def snap_unit_name(raw: str, *, max_distance: int = 1) -> tuple[str, str]:
    """Resolve an OCR'd unit name against the known vocabulary.

    Unit names are a closed set, and reading the name column with ``chi_sim``
    lands within one character (``无引舰`` for ``无畏舰``). Snapping recovers the
    exact name, which matters because the name is the key a fleet timeline
    diffs on — a garbled name makes every report look like a first sighting.

    Returns ``(name, category)``. The raw name and ``"unknown"`` are returned
    whenever the match is not unique or not close enough, so a genuinely new
    unit is never rewritten into an existing one.
    """
    name = raw.strip()
    exact = classify_unit(name)
    if exact != "unknown":
        return name, exact
    if len(name) < MIN_SNAP_LENGTH:
        return name, "unknown"
    best: list[str] = []
    best_distance = max_distance + 1
    for candidate in (*SHIP_NAMES, *DEFENCE_NAMES):
        distance = edit_distance(name, candidate)
        if distance < best_distance:
            best_distance, best = distance, [candidate]
        elif distance == best_distance:
            best.append(candidate)
    if best_distance > max_distance or len(best) != 1:
        return name, "unknown"
    return best[0], classify_unit(best[0])


def parse_fleet_column(
    text: str, source: str, confidence: float = 1.0, *, snap: bool = True
) -> tuple[FleetLine, ...]:
    """Parse one column of the 参战战舰 / 剩余战舰 list.

    Rows are ``名称`` then whitespace then a count. A row whose count is ``0``
    is kept: the live UI renders zeroes explicitly and dropping them would make
    a wiped-out ship type indistinguishable from one that never participated.
    """
    lines: list[FleetLine] = []
    for raw in text.splitlines():
        match = FLEET_COLUMN_RE.match(raw.strip())
        if match is None:
            continue
        name = match.group(1).strip()
        if not name:
            continue
        resolved, category = snap_unit_name(name) if snap else (name, classify_unit(name))
        lines.append(
            FleetLine(
                ship_type=resolved,
                count=int(match.group(2)),
                confidence=confidence,
                sources=(source,),
                category=category,
            )
        )
    return tuple(lines)


def parse_versus_block(text: str, source: str, confidence: float = 1.0) -> VersusBlock | None:
    """Parse the two-column VS block into attacker (left) and defender (right).

    Returns ``None`` when either side is incomplete, so the caller fails closed
    instead of attributing one side's coordinate to both.
    """
    left: list[str] = []
    right: list[str] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        parts = re.split(r"\s{2,}", raw.strip())
        if len(parts) < 2:
            return None
        left.append(parts[0].strip())
        right.append(parts[-1].strip())
    if len(left) < 3 or len(right) < 3:
        return None
    attacker = _versus_side(left, source, confidence)
    defender = _versus_side(right, source, confidence)
    if attacker is None or defender is None:
        return None
    return VersusBlock(attacker=attacker, defender=defender)


def parse_mail_rows_v2(
    page: PageObservation,
    rows: list[str],
    display_zone: tzinfo,
    source: str,
) -> MailListObservation:
    """Parse live mail rows of ``subject`` / ``sender`` / ``timestamp``.

    Live rows carry no coordinate. ``MailItem.coordinate`` therefore stays
    ``None``; the coordinate comes from opening the report, never from the list.
    """
    if page.ui_version is None:
        raise UnknownUiVersionError("mail list UI version unknown; refusing to navigate")
    if page.ui_version != "mail-list-v2":
        raise UnknownUiVersionError(f"unsupported mail list UI version: {page.ui_version}")
    items: list[MailItem] = []
    for row in rows:
        lines = [line.strip() for line in row.splitlines() if line.strip()]
        if not lines:
            continue
        subject = lines[0]
        raw_time = next((line for line in lines if REPORT_TIME_RE.search(line)), None)
        sender = next(
            (line for line in lines[1:] if line != raw_time),
            None,
        )
        items.append(
            MailItem(
                subject=subject,
                owner=NameParse(value=sender, confidence=page.confidence, sources=(source,))
                if sender
                else None,
                coordinate=None,
                raw_time_text=raw_time,
            )
        )
    # display_zone is accepted so callers cannot forget it when they later
    # normalize raw_time_text; the row itself keeps only the raw string.
    _ = display_zone
    return MailListObservation(
        ui_version=page.ui_version,
        items=tuple(items),
        page_number=None,
        confidence=page.confidence,
    )


def parse_replay_rounds(
    rounds: list[tuple[int, str, str]],
    source: str,
    confidence: float = 1.0,
) -> tuple[ReplayRound, ...]:
    """Parse ``(round_number, attacker_column, defender_column)`` sections.

    Rounds must arrive strictly increasing from 1. Out-of-order or duplicated
    round numbers mean the scroll capture lost or repeated a section, which
    would silently corrupt a fleet timeline.
    """
    parsed: list[ReplayRound] = []
    for index, (number, attacker_text, defender_text) in enumerate(rounds, start=1):
        if number != index:
            raise ValueError(f"replay round out of sequence: expected {index}, got {number}")
        parsed.append(
            ReplayRound(
                round_number=number,
                attacker=parse_fleet_column(attacker_text, source, confidence),
                defender=parse_fleet_column(defender_text, source, confidence),
            )
        )
    return tuple(parsed)


def _versus_side(lines: list[str], source: str, confidence: float) -> VersusSide | None:
    coordinate = next(
        (
            parsed
            for parsed in (parse_coordinate(line, source, confidence) for line in lines)
            if parsed is not None
        ),
        None,
    )
    if coordinate is None:
        return None
    non_coordinate = [line for line in lines if COORDINATE_RE.search(line) is None]
    if len(non_coordinate) < 2:
        return None
    return VersusSide(
        player=non_coordinate[0],
        planet=non_coordinate[1],
        coordinate=coordinate,
    )


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
    coordinates = parse_all_coordinates(ocr_text, source)
    origin, target = _require_both_coordinates(coordinates, "battle detail")
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
    coordinates = parse_all_coordinates(ocr_text, source)
    origin, target = _require_both_coordinates(coordinates, "battle replay")
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


def _require_both_coordinates(
    coordinates: list[CoordinateParse], screen: str
) -> tuple[CoordinateParse, CoordinateParse]:
    """Return the attacker and defender coordinates, or fail closed.

    A report always shows both sides. Falling back to a placeholder or reusing
    one side for the other would emit ``1:1:1`` — a real coordinate — or match a
    dispatch against its own origin, so a short read is treated as an
    unrecognised screen instead.
    """
    if len(coordinates) < 2:
        raise UnknownUiVersionError(
            f"{screen} needs an attacker and a defender coordinate; read {len(coordinates)}"
        )
    return coordinates[0], coordinates[1]


class MissionType(Enum):
    """派遣简报上的任务类型。

    派攻击之前必须确认这里写的是「攻击」。类型选错会把舰队派成探索或运输，
    既拿不到战报，也白烧一趟燃料。
    """

    ATTACK = "attack"
    EXPLORE = "explore"
    TRANSPORT = "transport"
    RECYCLE = "recycle"
    SCOUT = "scout"
    UNKNOWN = "unknown"


_MISSION_LABELS = {
    "攻击": MissionType.ATTACK,
    "探索": MissionType.EXPLORE,
    "运输": MissionType.TRANSPORT,
    "回收": MissionType.RECYCLE,
    "侦察": MissionType.SCOUT,
}

#: 到达时间与「当前时间 + 飞行时长」允许的偏差。OCR 读的是秒级文本，
#: 而读取本身要花时间，所以留一分钟；超过就说明至少有一处读错了。
#:
#: ⚠️ **定义已经搬到 `domain.flight_estimate`，这里只是原样 re-export。**
#: 那边的三来源合成也要用同一道容差，而 `domain` 不许 import `vision`
#: （方向是反的：本模块自己就 import `domain.report_wait`）。老的 import
#: 路径一个都不用改。**不许在任何一边另起一个数**——两处各调各的，就等于
#: 这道交叉校验在两条链路上说着不同的话。
BRIEFING_SKEW_TOLERANCE = _BRIEFING_SKEW_TOLERANCE


@dataclass(frozen=True)
class DispatchBriefing:
    """派遣简报页（点「出发！」之前的那一屏）。"""

    mission_type: MissionType
    flight: timedelta
    arrival_at_utc: datetime

    @property
    def is_attack(self) -> bool:
        return self.mission_type is MissionType.ATTACK

    @property
    def expected_report_at_utc(self) -> datetime:
        """战报在抵达时产生，所以就是到达时间。"""
        return self.arrival_at_utc

    def duration_agrees(self, *, now_utc: datetime) -> bool:
        """交叉校验：绝对到达时间应当约等于当前时间加飞行时长。

        两处都来自同一屏 OCR，对不上说明至少有一处读错了。用绝对时间作为
        主来源、时长作为校验，比只用其中一个更难悄悄出错。
        """
        implied = now_utc + self.flight
        return abs(implied - self.arrival_at_utc) <= BRIEFING_SKEW_TOLERANCE


def parse_dispatch_briefing(ocr_text: str) -> DispatchBriefing:
    """解析派遣简报页，缺关键字段就 fail closed。

    到达时间是主来源：它不依赖本机时钟与游戏时钟同步，也不会因为「读完到点击
    出发」之间的耗时而漂移。
    """
    flight = _labelled_duration(ocr_text, "飞行时间")
    if flight is None:
        raise UnknownUiVersionError("派遣简报缺少可读的飞行时间；面板可能仍在渲染")

    arrival_line = _line_containing(_join_wrapped_times(ocr_text), "到达时间")
    arrival = parse_report_timestamp(arrival_line, GAME_DISPLAY_ZONE) if arrival_line else None
    if arrival is None:
        raise UnknownUiVersionError("派遣简报缺少可读的预计到达时间；面板可能仍在渲染")

    return DispatchBriefing(
        mission_type=_mission_type(ocr_text), flight=flight, arrival_at_utc=arrival
    )


def _mission_type(ocr_text: str) -> MissionType:
    line = _line_containing(ocr_text, "任务类型") or ""
    for label, mission in _MISSION_LABELS.items():
        if label in line:
            return mission
    return MissionType.UNKNOWN


#: 单独成行的 `HH:MM:SS`——就是被折到下一行的那半个时间戳。
_BARE_TIME_LINE = re.compile(r"^\s*(\d{2}:\d{2}:\d{2})\s*$")

#: 行尾的 `DD/MM/YYYY`，说明这一行的时间戳被截断了。
_TRAILING_DATE = re.compile(r"\d{2}/\d{2}/\d{4}\s*$")


def _join_wrapped_times(ocr_text: str) -> str:
    """把折行的「预计到达时间」拼回一行。

    实机简报页把到达时间排成两行——日期跟在标签后面，时分秒另起一行：

        预计到达时间 ( 约 ) :        09/08/2026
        02:04:27

    而 `REPORT_TIME_RE` 要求日期和时间在同一行。不拼回去，
    每一次派遣都会因为「缺少可读的预计到达时间」被拒——
    简报页明明是好的，字段也确实在画面上。

    只在**上一行以日期结尾、这一行只有一个时分秒**时才拼，
    所以不会把两个不相干的字段粘到一起。
    """
    lines = ocr_text.splitlines()
    merged: list[str] = []
    for line in lines:
        bare = _BARE_TIME_LINE.match(line)
        if bare and merged and _TRAILING_DATE.search(merged[-1]):
            merged[-1] = f"{merged[-1].rstrip()} {bare.group(1)}"
            continue
        merged.append(line)
    return "\n".join(merged)


def _line_containing(ocr_text: str, label: str) -> str | None:
    for line in ocr_text.splitlines():
        if label in line:
            return line
    return None


def _labelled_duration(ocr_text: str, label: str) -> timedelta | None:
    from evo_helper.domain.report_wait import parse_game_duration

    line = _line_containing(ocr_text, label)
    if line is None:
        return None
    # 去掉标签本身，否则「飞行时间」里的数字都没有，但其它行的可能被带进来。
    return parse_game_duration(line.split(label, 1)[-1])
