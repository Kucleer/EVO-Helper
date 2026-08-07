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


class TestGameDisplayZone:
    """Game times render in UTC+0 (confirmed by the user on 2026-08-07)."""

    def test_game_display_zone_is_utc(self) -> None:
        from evo_helper.vision.parsers import GAME_DISPLAY_ZONE

        assert GAME_DISPLAY_ZONE.utcoffset(None) == timedelta(0)

    def test_report_timestamp_in_game_zone_is_not_shifted(self) -> None:
        from evo_helper.vision.parsers import GAME_DISPLAY_ZONE

        parsed = parse_report_timestamp("06/08/2026 11:45:03", GAME_DISPLAY_ZONE)
        assert parsed is not None
        assert (parsed.hour, parsed.minute, parsed.second) == (11, 45, 3)

    def test_bare_iso_game_time_is_read_as_utc(self) -> None:
        """A bare report time must not be shifted by the UTC+8 schedule zone."""
        from evo_helper.vision.parsers import parse_iso_utc

        parsed = parse_iso_utc("2026-08-06 14:30:00")
        assert parsed is not None
        assert parsed.hour == 14

    def test_explicit_zone_still_wins(self) -> None:
        from evo_helper.vision.parsers import parse_iso_utc

        parsed = parse_iso_utc("2026-08-06T14:30:00+08:00")
        assert parsed is not None
        assert parsed.hour == 6


class TestUnitNameSnapping:
    """chi_sim reads names within one character; counts come from a second pass."""

    def test_snaps_a_one_character_misread(self) -> None:
        from evo_helper.vision.parsers import snap_unit_name

        assert snap_unit_name("无引舰") == ("无畏舰", "ship")
        assert snap_unit_name("友炸机") == ("轰炸机", "ship")
        assert snap_unit_name("深空吞噶者") == ("深空吞噬者", "ship")

    def test_snaps_a_missing_character(self) -> None:
        from evo_helper.vision.parsers import snap_unit_name

        assert snap_unit_name("MK 加农炮") == ("MK2 加农炮", "defence")

    def test_exact_name_is_unchanged(self) -> None:
        from evo_helper.vision.parsers import snap_unit_name

        assert snap_unit_name("巡洋舰") == ("巡洋舰", "ship")

    def test_far_name_is_kept_raw_and_unknown(self) -> None:
        """A genuinely new unit must not be snapped onto an existing one."""
        from evo_helper.vision.parsers import snap_unit_name

        assert snap_unit_name("星门要塞") == ("星门要塞", "unknown")

    def test_ambiguous_match_is_refused(self) -> None:
        """轻型战斗机 and 重型战斗机 differ by one char; a tie must not be resolved."""
        from evo_helper.vision.parsers import snap_unit_name

        name, category = snap_unit_name("X型战斗机")
        assert name == "X型战斗机"
        assert category == "unknown"

    def test_short_names_are_not_snapped(self) -> None:
        """One edit on a two-character name is too much of the string to guess."""
        from evo_helper.vision.parsers import snap_unit_name

        assert snap_unit_name("炮") == ("炮", "unknown")

    def test_parse_fleet_column_snaps_by_default(self) -> None:
        lines = parse_fleet_column("无引舰  95\n友炸机  166", "ocr")
        assert [(line.ship_type, line.category) for line in lines] == [
            ("无畏舰", "ship"),
            ("轰炸机", "ship"),
        ]

    def test_snapping_can_be_disabled(self) -> None:
        lines = parse_fleet_column("无引舰  95", "ocr", snap=False)
        assert lines[0].ship_type == "无引舰"
        assert lines[0].category == "unknown"


class TestUnitCatalogue:
    """The catalogue is the in-game list supplied by the user on 2026-08-07."""

    def test_ships_and_defences_do_not_overlap(self) -> None:
        from evo_helper.vision.parsers import DEFENCE_NAMES, SHIP_NAMES

        assert SHIP_NAMES & DEFENCE_NAMES == frozenset()

    def test_unit_order_covers_every_name_exactly_once(self) -> None:
        from evo_helper.vision.parsers import DEFENCE_NAMES, SHIP_NAMES, UNIT_ORDER

        assert len(UNIT_ORDER) == len(set(UNIT_ORDER))
        assert set(UNIT_ORDER) == SHIP_NAMES | DEFENCE_NAMES

    def test_order_matches_the_in_game_list(self) -> None:
        from evo_helper.vision.parsers import SHIP_ORDER

        assert SHIP_ORDER[:4] == ("轻型战斗机", "重型战斗机", "巡洋舰", "战列舰")
        assert SHIP_ORDER[-4:] == ("噬能截击者", "钛能守卫者", "收割者", "湮灭之星")

    def test_late_game_ships_are_classified_as_ships(self) -> None:
        from evo_helper.vision.parsers import classify_unit

        for name in ("收割者", "湮灭之星", "噬能截击者"):
            assert classify_unit(name) == "ship", name

    def test_missiles_and_shields_are_not_ships(self) -> None:
        from evo_helper.vision.parsers import classify_unit

        for name in ("行星际导弹", "拦截导弹", "太阳能卫星", "小型护盾", "大型护盾"):
            assert classify_unit(name) == "defence", name

    def test_transport_names_match_the_catalogue(self) -> None:
        """The earlier guesses 运输舰 / 间谍探测器 were wrong."""
        from evo_helper.vision.parsers import classify_unit

        assert classify_unit("小型运输船") == "ship"
        assert classify_unit("大型运输船") == "ship"
        assert classify_unit("探测器") == "ship"
        assert classify_unit("运输舰") == "unknown"


class TestListColumnsAreRealUnits:
    def test_every_list_column_is_in_the_catalogue(self) -> None:
        from evo_helper.vision.parsers import UNIT_ORDER
        from evo_helper.web.display import LIST_SHIP_COLUMNS

        assert set(LIST_SHIP_COLUMNS) <= set(UNIT_ORDER)


class TestPresetSignature:
    """Safety invariant 9: the preset name AND its composition must match."""

    def test_composition_signature_round_trips(self) -> None:
        from evo_helper.domain.fleet_preset import composition_signature

        assert composition_signature({"轻型战斗机": 1}) == "轻型战斗机:1"

    def test_signature_is_order_independent(self) -> None:
        from evo_helper.domain.fleet_preset import composition_signature

        first = composition_signature({"轻型战斗机": 1, "巡洋舰": 2})
        second = composition_signature({"巡洋舰": 2, "轻型战斗机": 1})
        assert first == second

    def test_different_counts_give_different_signatures(self) -> None:
        from evo_helper.domain.fleet_preset import composition_signature

        assert composition_signature({"轻型战斗机": 1}) != composition_signature({"轻型战斗机": 2})

    def test_default_preset_is_the_scouting_preset(self) -> None:
        from evo_helper.domain.fleet_preset import DEFAULT_PRESET

        assert DEFAULT_PRESET.name == "探路"
        assert DEFAULT_PRESET.signature == "轻型战斗机:1"

    def test_a_two_character_name_is_below_the_snap_threshold(self) -> None:
        """探路 cannot be OCR-repaired, which is why composition must also match."""
        from evo_helper.vision.parsers import snap_unit_name

        name, category = snap_unit_name("探路")
        assert (name, category) == ("探路", "unknown")
