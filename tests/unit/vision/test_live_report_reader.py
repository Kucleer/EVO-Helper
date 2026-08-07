"""Mail list -> attack report -> replay, driven by per-region OCR text."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.live_reports import (
    LiveReportReader,
    ReportScreens,
)
from evo_helper.vision.models import PageObservation
from evo_helper.vision.parsers import ReportKind, UnknownUiVersionError

MAIL_ROWS = [
    "矮星系统战报\nSystem\n07/08/2026 01:27:02",
    "海盗攻击报告\nSystem\n07/08/2026 00:49:56",
    "攻击报告\nSystem\n06/08/2026 11:45:03",
    "侦察报告\nAries [HQ]\n07/08/2026 00:33:10",
]

HEADER = "发件人: System                    06/08/2026 11:45:03\n主题: 攻击报告"

VERSUS = (
    "Kucleer                    bot_2_149_17\n"
    "奥格瑞玛                   bot_2_149_17's Planet\n"
    "[2:137:18]                 [2:149:17]"
)

ATTACKER_COLUMN = "深空吞噬者  265\n钛能守卫者  178"
DEFENDER_COLUMN = "轻型战斗机  461\n重型战斗机  736\n离子炮  35"


class FakeScreens(ReportScreens):
    def __init__(
        self,
        *,
        rows: list[str] | None = None,
        header: str = HEADER,
        versus: str = VERSUS,
        columns: tuple[str, str] = (ATTACKER_COLUMN, DEFENDER_COLUMN),
        rounds: list[tuple[int, str, str]] | None = None,
    ) -> None:
        self._rows = MAIL_ROWS if rows is None else rows
        self._header = header
        self._versus = versus
        self._columns = columns
        self._rounds = [(1, "深空吞噬者  265", "轻型战斗机  0")] if rounds is None else rounds

    def mail_rows(self) -> list[str]:
        return list(self._rows)

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return self._versus

    def participating_columns(self) -> tuple[str, str]:
        return self._columns

    def round_columns(self) -> list[tuple[int, str, str]]:
        return list(self._rounds)


def mail_page(version: str | None = "mail-list-v2") -> PageObservation:
    return PageObservation(screen="mail_list", ui_version=version, confidence=0.99)


def detail_page(version: str | None = "battle-detail-v2") -> PageObservation:
    return PageObservation(screen="mail_detail", ui_version=version, confidence=0.99)


def replay_page(version: str | None = "battle-replay-v2") -> PageObservation:
    return PageObservation(screen="battle_replay", ui_version=version, confidence=0.99)


class TestSelectingAttackReports:
    def test_only_attack_reports_are_returned(self) -> None:
        reader = LiveReportReader(FakeScreens())
        rows = reader.list_attack_reports(mail_page())

        assert [row.subject for row in rows] == ["攻击报告"]

    def test_pirate_report_is_excluded(self) -> None:
        reader = LiveReportReader(FakeScreens(rows=["海盗攻击报告\nSystem\n07/08/2026 00:49:56"]))
        assert reader.list_attack_reports(mail_page()) == ()

    def test_row_carries_normalized_utc_time(self) -> None:
        reader = LiveReportReader(FakeScreens())
        row = reader.list_attack_reports(mail_page())[0]

        assert row.raw_time_text == "06/08/2026 11:45:03"
        # Game times render in UTC+0, so the clock reading is unchanged.
        assert row.reported_at_utc == datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)

    def test_unknown_mail_version_fails_closed(self) -> None:
        reader = LiveReportReader(FakeScreens())
        with pytest.raises(UnknownUiVersionError):
            reader.list_attack_reports(mail_page(None))

    def test_row_without_readable_time_is_skipped(self) -> None:
        reader = LiveReportReader(FakeScreens(rows=["攻击报告\nSystem\n(loading)"]))
        assert reader.list_attack_reports(mail_page()) == ()


class TestReadingAReport:
    def test_reads_both_sides_and_fleets(self) -> None:
        reader = LiveReportReader(FakeScreens())
        report = reader.read_report(detail_page(), replay_page())

        assert report.kind is ReportKind.ATTACK
        assert report.attacker.coordinate.value == Coordinate(2, 137, 18)
        assert report.defender.coordinate.value == Coordinate(2, 149, 17)
        assert report.defender.is_bot
        assert report.reported_at_utc == datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)
        assert [line.ship_type for line in report.participating_attacker] == [
            "深空吞噬者",
            "钛能守卫者",
        ]
        assert report.participating_defender[2].category == "defence"
        assert report.rounds[0].round_number == 1

    def test_records_each_screen_version_separately(self) -> None:
        reader = LiveReportReader(FakeScreens())
        report = reader.read_report(detail_page(), replay_page())

        assert report.ui_versions == {
            "battle_detail_ui_version": "battle-detail-v2",
            "battle_replay_ui_version": "battle-replay-v2",
        }


class TestFailClosed:
    def test_pirate_report_is_refused(self) -> None:
        header = "发件人: System   07/08/2026 00:49:56\n主题: 海盗攻击报告"
        reader = LiveReportReader(FakeScreens(header=header))
        with pytest.raises(ValueError, match="海盗|pirate|not an attack"):
            reader.read_report(detail_page(), replay_page())

    def test_loading_screen_is_refused(self) -> None:
        """Decorative background text only: no header, no versus block."""
        reader = LiveReportReader(
            FakeScreens(header="-COMMAND OFFICERS\n-TOTAL CREWS\n-17003", versus="")
        )
        with pytest.raises(UnknownUiVersionError):
            reader.read_report(detail_page(), replay_page())

    def test_incomplete_versus_block_is_refused(self) -> None:
        reader = LiveReportReader(FakeScreens(versus="Kucleer\n奥格瑞玛\n[2:137:18]"))
        with pytest.raises(UnknownUiVersionError, match="versus"):
            reader.read_report(detail_page(), replay_page())

    def test_empty_participating_fleet_is_refused(self) -> None:
        """Both columns empty means the replay had not rendered yet."""
        reader = LiveReportReader(FakeScreens(columns=("", "")))
        with pytest.raises(UnknownUiVersionError, match="fleet"):
            reader.read_report(detail_page(), replay_page())

    def test_unknown_detail_version_is_refused(self) -> None:
        reader = LiveReportReader(FakeScreens())
        with pytest.raises(UnknownUiVersionError):
            reader.read_report(detail_page(None), replay_page())

    def test_unknown_replay_version_is_refused(self) -> None:
        reader = LiveReportReader(FakeScreens())
        with pytest.raises(UnknownUiVersionError):
            reader.read_report(detail_page(), replay_page("battle-replay-v9"))

    def test_unreadable_report_time_is_refused(self) -> None:
        reader = LiveReportReader(FakeScreens(header="主题: 攻击报告"))
        with pytest.raises(UnknownUiVersionError, match="time"):
            reader.read_report(detail_page(), replay_page())
