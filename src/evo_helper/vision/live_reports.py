"""Read the live mail list -> attack report -> battle replay chain.

The reader is deliberately free of screenshots, ROI geometry and OCR. It asks a
:class:`ReportScreens` implementation for the text of each named region, so the
navigation and safety rules stay testable without a browser, and the geometry
lives with the adapter that owns the window.

Every step fails closed. An unknown UI version, a half-rendered panel, a
missing side or an unreadable time raises instead of yielding a partial report:
a report that is silently wrong closes the wrong dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from evo_helper.vision.models import FleetLine, PageObservation, ReplayRound, VersusSide
from evo_helper.vision.parsers import (
    GAME_DISPLAY_ZONE,
    ReportKind,
    UnknownUiVersionError,
    classify_report_subject,
    parse_fleet_column,
    parse_mail_rows_v2,
    parse_replay_rounds,
    parse_report_timestamp,
    parse_versus_block,
)

SUPPORTED_DETAIL_VERSIONS = frozenset({"battle-detail-v2"})
SUPPORTED_REPLAY_VERSIONS = frozenset({"battle-replay-v2"})


@dataclass(frozen=True)
class AttackReportRow:
    """A mail row that is eligible to be opened as an attack report."""

    subject: str
    sender: str | None
    raw_time_text: str
    reported_at_utc: datetime


@dataclass(frozen=True)
class LiveBattleReport:
    """A fully read attack report, ready for strict dispatch matching."""

    kind: ReportKind
    raw_time_text: str
    reported_at_utc: datetime
    attacker: VersusSide
    defender: VersusSide
    participating_attacker: tuple[FleetLine, ...]
    participating_defender: tuple[FleetLine, ...]
    rounds: tuple[ReplayRound, ...]
    #: Per-screen UI versions. Section 3 forbids one label for the whole chain.
    ui_versions: dict[str, str]


class ReportScreens(Protocol):
    """Per-region OCR text for the screen the adapter is currently showing."""

    def mail_rows(self) -> list[str]:
        """One string per mail row: subject, sender and timestamp lines."""
        ...

    def report_header(self) -> str:
        """The 发件人 / 主题 header block of an opened report."""
        ...

    def versus_block(self) -> str:
        """The two-column VS block, columns separated by run-of-spaces."""
        ...

    def participating_columns(self) -> tuple[str, str]:
        """The 参战战舰 attacker and defender columns."""
        ...

    def round_columns(self) -> list[tuple[int, str, str]]:
        """``(round_number, attacker_column, defender_column)`` per round."""
        ...


class LiveReportReader:
    def __init__(self, screens: ReportScreens, source: str = "ocr") -> None:
        self._screens = screens
        self._source = source

    def list_attack_reports(self, page: PageObservation) -> tuple[AttackReportRow, ...]:
        """Return only the rows that may be matched against a dispatch.

        ``海盗攻击报告`` contains ``攻击报告`` as a substring but is a pirate
        battle, and the live secondary tabs do not filter by report type, so the
        subject is the only thing separating them.
        """
        observation = parse_mail_rows_v2(
            page, self._screens.mail_rows(), GAME_DISPLAY_ZONE, self._source
        )
        rows: list[AttackReportRow] = []
        for item in observation.items:
            if not classify_report_subject(item.subject).is_dispatch_matchable:
                continue
            if item.raw_time_text is None:
                continue
            reported_at = parse_report_timestamp(item.raw_time_text, GAME_DISPLAY_ZONE)
            if reported_at is None:
                continue
            rows.append(
                AttackReportRow(
                    subject=item.subject,
                    sender=item.owner.value if item.owner is not None else None,
                    raw_time_text=item.raw_time_text,
                    reported_at_utc=reported_at,
                )
            )
        return tuple(rows)

    def read_report(
        self, detail_page: PageObservation, replay_page: PageObservation
    ) -> LiveBattleReport:
        self._require_version(detail_page, SUPPORTED_DETAIL_VERSIONS, "battle detail")
        self._require_version(replay_page, SUPPORTED_REPLAY_VERSIONS, "battle replay")

        header = self._screens.report_header()
        subject = _subject_from_header(header)
        if subject is None:
            raise UnknownUiVersionError(
                "report header has no 主题 line; the panel is still rendering"
            )
        kind = classify_report_subject(subject)
        if not kind.is_dispatch_matchable:
            raise ValueError(f"not an attack report: {subject} ({kind.value})")

        raw_time = _time_text_from_header(header)
        reported_at = (
            parse_report_timestamp(raw_time, GAME_DISPLAY_ZONE) if raw_time is not None else None
        )
        if raw_time is None or reported_at is None:
            raise UnknownUiVersionError("report header has no readable time")

        versus = parse_versus_block(self._screens.versus_block(), self._source)
        if versus is None:
            raise UnknownUiVersionError("versus block is incomplete; refusing a one-sided report")

        attacker_text, defender_text = self._screens.participating_columns()
        participating_attacker = parse_fleet_column(attacker_text, self._source)
        participating_defender = parse_fleet_column(defender_text, self._source)
        if not participating_attacker and not participating_defender:
            raise UnknownUiVersionError(
                "participating fleet is empty on both sides; the replay had not rendered"
            )

        rounds = parse_replay_rounds(self._screens.round_columns(), self._source)

        return LiveBattleReport(
            kind=kind,
            raw_time_text=raw_time,
            reported_at_utc=reported_at,
            attacker=versus.attacker,
            defender=versus.defender,
            participating_attacker=participating_attacker,
            participating_defender=participating_defender,
            rounds=rounds,
            ui_versions={
                "battle_detail_ui_version": str(detail_page.ui_version),
                "battle_replay_ui_version": str(replay_page.ui_version),
            },
        )

    @staticmethod
    def _require_version(page: PageObservation, supported: frozenset[str], screen: str) -> None:
        if page.ui_version not in supported:
            raise UnknownUiVersionError(f"unsupported {screen} UI version: {page.ui_version}")


def _subject_from_header(header: str) -> str | None:
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("主题"):
            return stripped.split(":", 1)[-1].split("：", 1)[-1].strip() or None
    return None


def _time_text_from_header(header: str) -> str | None:
    from evo_helper.vision.parsers import REPORT_TIME_RE

    match = REPORT_TIME_RE.search(header)
    return match.group(0) if match is not None else None
