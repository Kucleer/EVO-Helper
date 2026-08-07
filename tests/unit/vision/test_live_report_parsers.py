"""Parsers for the 2026-08-07 live UI layouts.

Field structure comes from datasets/manifests/live-ui-observations-20260807.json
and the capture batch evo-20260807-live.
"""

from __future__ import annotations

from datetime import UTC, timedelta, timezone

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.models import PageObservation
from evo_helper.vision.parsers import (
    ReportKind,
    UnknownUiVersionError,
    classify_report_subject,
    parse_fleet_column,
    parse_mail_rows_v2,
    parse_replay_rounds,
    parse_report_timestamp,
    parse_versus_block,
)

GAME_LOCAL = timezone(timedelta(hours=8), name="game-local")

# Left/right column text as OCR would read each ROI of the 参战战舰 section.
ATTACKER_COLUMN = """
深空吞噬者  265
钛能守卫者  178
"""

DEFENDER_COLUMN = """
轻型战斗机  461
重型战斗机  736
巡洋舰      257
战列舰      148
无畏舰      95
轰炸机      166
毁灭者      97
裂变者      5
深空吞噬者  2
钛能守卫者  2
离子炮      35
火箭发射器  51
轻型激光炮  55
MK2 加农炮  48
等离子炮    16
"""

VERSUS_TEXT = """
Kucleer                    bot_2_149_17
奥格瑞玛                   bot_2_149_17's Planet
[2:137:18]                 [2:149:17]
"""


class TestReportTimestamp:
    def test_parses_day_first_report_time(self) -> None:
        parsed = parse_report_timestamp("06/08/2026 11:45:03", GAME_LOCAL)
        assert parsed is not None
        assert parsed.tzinfo == UTC
        # 11:45:03 UTC+8 -> 03:45:03 UTC on the same day
        assert (parsed.year, parsed.month, parsed.day) == (2026, 8, 6)
        assert (parsed.hour, parsed.minute, parsed.second) == (3, 45, 3)

    def test_day_first_is_not_month_first(self) -> None:
        """13/08 has no valid month-first reading, so day-first must be used."""
        parsed = parse_report_timestamp("13/08/2026 00:00:00", UTC)
        assert parsed is not None
        assert (parsed.month, parsed.day) == (8, 13)

    def test_rejects_impossible_date(self) -> None:
        assert parse_report_timestamp("32/08/2026 00:00:00", UTC) is None

    def test_rejects_text_without_timestamp(self) -> None:
        assert parse_report_timestamp("主题: 攻击报告", UTC) is None


class TestReportSubject:
    def test_attack_report_is_matchable(self) -> None:
        kind = classify_report_subject("攻击报告")
        assert kind is ReportKind.ATTACK
        assert kind.is_dispatch_matchable

    def test_pirate_report_is_not_matchable(self) -> None:
        """海盗攻击报告 contains 攻击报告 but must never match a bot dispatch."""
        kind = classify_report_subject("海盗攻击报告")
        assert kind is ReportKind.PIRATE
        assert not kind.is_dispatch_matchable

    def test_scout_and_system_reports_are_not_matchable(self) -> None:
        assert classify_report_subject("侦察报告") is ReportKind.SCOUT
        assert classify_report_subject("矮星系统战报") is ReportKind.SYSTEM
        assert not classify_report_subject("侦察报告").is_dispatch_matchable

    def test_unknown_subject_is_not_matchable(self) -> None:
        kind = classify_report_subject("联盟公告")
        assert kind is ReportKind.UNKNOWN
        assert not kind.is_dispatch_matchable


class TestFleetColumn:
    def test_parses_whitespace_separated_name_and_count(self) -> None:
        lines = parse_fleet_column(ATTACKER_COLUMN, "ocr")
        assert [(line.ship_type, line.count) for line in lines] == [
            ("深空吞噬者", 265),
            ("钛能守卫者", 178),
        ]

    def test_keeps_ground_defence_distinguishable_from_ships(self) -> None:
        lines = parse_fleet_column(DEFENDER_COLUMN, "ocr")
        by_name = {line.ship_type: line for line in lines}
        assert by_name["巡洋舰"].category == "ship"
        assert by_name["离子炮"].category == "defence"
        assert by_name["MK2 加农炮"].category == "defence"
        assert by_name["等离子炮"].category == "defence"

    def test_unrecognised_name_is_marked_unknown_not_guessed(self) -> None:
        lines = parse_fleet_column("虚构舰种  7", "ocr")
        assert lines[0].category == "unknown"

    def test_explicit_zero_is_kept_as_a_row(self) -> None:
        lines = parse_fleet_column("轻型战斗机  0\n重型战斗机  0", "ocr")
        assert len(lines) == 2
        assert all(line.count == 0 for line in lines)

    def test_ignores_lines_without_a_count(self) -> None:
        lines = parse_fleet_column("参战战舰\n深空吞噬者  265", "ocr")
        assert [line.ship_type for line in lines] == ["深空吞噬者"]


class TestVersusBlock:
    def test_extracts_both_sides(self) -> None:
        block = parse_versus_block(VERSUS_TEXT, "ocr")
        assert block is not None
        assert block.attacker.player == "Kucleer"
        assert block.attacker.planet == "奥格瑞玛"
        assert block.attacker.coordinate.value == Coordinate(2, 137, 18)
        assert block.defender.player == "bot_2_149_17"
        assert block.defender.coordinate.value == Coordinate(2, 149, 17)
        assert block.defender.is_bot
        assert not block.attacker.is_bot

    def test_returns_none_when_a_side_is_missing(self) -> None:
        assert parse_versus_block("Kucleer\n奥格瑞玛\n[2:137:18]", "ocr") is None


class TestMailRows:
    def test_parses_subject_sender_and_time(self) -> None:
        page = PageObservation(screen="mail_list", ui_version="mail-list-v2", confidence=0.99)
        rows = [
            "矮星系统战报\nSystem\n07/08/2026 01:27:02",
            "海盗攻击报告\nSystem\n07/08/2026 00:49:56",
            "攻击报告\nSystem\n06/08/2026 11:45:03",
        ]
        result = parse_mail_rows_v2(page, rows, GAME_LOCAL, "ocr")

        assert len(result.items) == 3
        attack = result.items[2]
        assert attack.subject == "攻击报告"
        assert attack.raw_time_text == "06/08/2026 11:45:03"

    def test_rows_carry_no_coordinate(self) -> None:
        """The live list shows no coordinates; inventing one would break matching."""
        page = PageObservation(screen="mail_list", ui_version="mail-list-v2", confidence=0.99)
        result = parse_mail_rows_v2(page, ["攻击报告\nSystem\n06/08/2026 11:45:03"], UTC, "ocr")
        assert result.items[0].coordinate is None

    def test_refuses_unknown_version(self) -> None:
        page = PageObservation(screen="mail_list", ui_version=None, confidence=0.9)
        with pytest.raises(UnknownUiVersionError):
            parse_mail_rows_v2(page, [], UTC, "ocr")


class TestReplayRounds:
    def test_splits_rounds_and_keeps_side_columns(self) -> None:
        rounds = parse_replay_rounds(
            [
                (1, "深空吞噬者  265\n钛能守卫者  178", "轻型战斗机  0\n轰炸机  141"),
                (2, "深空吞噬者  265", "轻型战斗机  0"),
            ],
            "ocr",
        )
        assert [r.round_number for r in rounds] == [1, 2]
        assert rounds[0].defender[1].ship_type == "轰炸机"
        assert rounds[0].defender[1].count == 141
        assert rounds[0].attacker[0].count == 265

    def test_rejects_duplicate_or_unordered_rounds(self) -> None:
        with pytest.raises(ValueError, match="round"):
            parse_replay_rounds([(2, "a  1", "b  1"), (1, "a  1", "b  1")], "ocr")


class TestPlaceholderCoordinateIsRefused:
    """A report with no readable coordinate must fail closed, not invent 1:1:1."""

    def test_battle_detail_without_coordinates_raises(self) -> None:
        from evo_helper.vision.parsers import parse_battle_detail

        page = PageObservation(
            screen="battle_detail", ui_version="battle-detail-v2", confidence=0.99
        )
        with pytest.raises(UnknownUiVersionError, match="coordinate"):
            parse_battle_detail(page, "attacker fleet:\nlight fighter x10", "ocr")

    def test_battle_replay_without_coordinates_raises(self) -> None:
        from evo_helper.vision.parsers import parse_battle_replay

        page = PageObservation(
            screen="battle_replay", ui_version="battle-replay-v2", confidence=0.99
        )
        with pytest.raises(UnknownUiVersionError, match="coordinate"):
            parse_battle_replay(page, "attacker fleet:\nlight fighter x10", "ocr")

    def test_single_coordinate_does_not_become_both_sides(self) -> None:
        from evo_helper.vision.parsers import parse_battle_detail

        page = PageObservation(
            screen="battle_detail", ui_version="battle-detail-v2", confidence=0.99
        )
        with pytest.raises(UnknownUiVersionError, match="coordinate"):
            parse_battle_detail(
                page, "attack from 1:2:3\nattacker fleet:\nlight fighter x10", "ocr"
            )
