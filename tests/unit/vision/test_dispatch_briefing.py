from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.vision.parsers import (
    MissionType,
    UnknownUiVersionError,
    parse_dispatch_briefing,
)

# 实测样本（派遣简报页）。数字列与标签列各自成行，OCR 会按行给出。
BRIEFING = """简报
任务类型:    探索
速度:    12.563    100%
飞行时间:    28分 21秒
预计到达时间（约）:    07/08/2026 10:28:28
气体消耗:    563.21M
-62.48K
"""

ATTACK_BRIEFING = BRIEFING.replace("探索", "攻击")


class TestBriefingFields:
    def test_reads_the_mission_type(self) -> None:
        assert parse_dispatch_briefing(BRIEFING).mission_type is MissionType.EXPLORE

    def test_reads_an_attack_mission_type(self) -> None:
        assert parse_dispatch_briefing(ATTACK_BRIEFING).mission_type is MissionType.ATTACK

    def test_reads_the_flight_duration(self) -> None:
        assert parse_dispatch_briefing(BRIEFING).flight == timedelta(minutes=28, seconds=21)

    def test_reads_the_absolute_arrival_time(self) -> None:
        """绝对到达时间比「当前时间 + 时长」可靠：不依赖时钟同步，也不会漂移。"""
        briefing = parse_dispatch_briefing(BRIEFING)
        assert briefing.arrival_at_utc == datetime(2026, 8, 7, 10, 28, 28, tzinfo=UTC)

    def test_arrival_is_read_as_utc(self) -> None:
        """游戏内时间一律 UTC+0。"""
        assert parse_dispatch_briefing(BRIEFING).arrival_at_utc.tzinfo is not None
        assert parse_dispatch_briefing(BRIEFING).arrival_at_utc.hour == 10

    def test_day_comes_before_month(self) -> None:
        """`07/08/2026` 是 8 月 7 日，与战报头同一格式。"""
        arrival = parse_dispatch_briefing(BRIEFING).arrival_at_utc
        assert (arrival.day, arrival.month) == (7, 8)


class TestExpectedReportTime:
    """战报在**抵达**时产生，所以预计战报时间就是预计到达时间。"""

    def test_expected_report_time_is_the_arrival_time(self) -> None:
        briefing = parse_dispatch_briefing(BRIEFING)
        assert briefing.expected_report_at_utc == briefing.arrival_at_utc

    def test_duration_cross_check_passes_when_consistent(self) -> None:
        briefing = parse_dispatch_briefing(BRIEFING)
        now = briefing.arrival_at_utc - timedelta(minutes=28, seconds=21)
        assert briefing.duration_agrees(now_utc=now)

    def test_duration_cross_check_tolerates_a_small_skew(self) -> None:
        briefing = parse_dispatch_briefing(BRIEFING)
        now = briefing.arrival_at_utc - timedelta(minutes=28, seconds=21) + timedelta(seconds=20)
        assert briefing.duration_agrees(now_utc=now)

    def test_duration_cross_check_fails_on_a_large_disagreement(self) -> None:
        """两处对不上说明至少有一处读错了，必须能发现。"""
        briefing = parse_dispatch_briefing(BRIEFING)
        now = briefing.arrival_at_utc - timedelta(hours=9)
        assert not briefing.duration_agrees(now_utc=now)


class TestFailClosed:
    def test_missing_arrival_time_is_rejected(self) -> None:
        text = BRIEFING.replace("07/08/2026 10:28:28", "")
        with pytest.raises(UnknownUiVersionError, match="到达时间|arrival"):
            parse_dispatch_briefing(text)

    def test_missing_flight_time_is_rejected(self) -> None:
        text = BRIEFING.replace("28分 21秒", "")
        with pytest.raises(UnknownUiVersionError, match="飞行时间|flight"):
            parse_dispatch_briefing(text)

    def test_unknown_mission_type_is_not_guessed(self) -> None:
        text = BRIEFING.replace("探索", "某种新任务")
        assert parse_dispatch_briefing(text).mission_type is MissionType.UNKNOWN

    def test_a_half_rendered_panel_is_rejected(self) -> None:
        with pytest.raises(UnknownUiVersionError):
            parse_dispatch_briefing("简报")


class TestAttackGuard:
    """派攻击之前必须确认简报上写的是攻击，否则就是派错了任务类型。"""

    def test_an_attack_briefing_is_dispatchable_as_an_attack(self) -> None:
        assert parse_dispatch_briefing(ATTACK_BRIEFING).is_attack

    def test_an_explore_briefing_is_not_dispatchable_as_an_attack(self) -> None:
        assert not parse_dispatch_briefing(BRIEFING).is_attack

    def test_an_unknown_type_is_not_dispatchable_as_an_attack(self) -> None:
        text = BRIEFING.replace("探索", "某种新任务")
        assert not parse_dispatch_briefing(text).is_attack
