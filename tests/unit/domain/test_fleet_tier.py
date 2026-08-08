"""舰队规模分档。

用户要的不是精确数量，是落在哪一档（2K–5K 甲 / 5K–8K 乙 / 8K+ 丙）。
所以识别防的是**量级错**，不是末位误差。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.fleet_tier import (
    BOUNDARY_MARGIN,
    FleetTier,
    classify,
    parse_fleet_count,
    tier_for,
)


class TestParsing:
    def test_plain_numbers(self) -> None:
        assert parse_fleet_count("517") == 517
        assert parse_fleet_count("2") == 2

    def test_the_k_suffix_means_thousands(self) -> None:
        # 游戏显示 5.36K，真实值在 5355–5364 之间；取 5360 足够定档。
        assert parse_fleet_count("5.36K") == 5360
        assert parse_fleet_count("1.09K") == 1090
        assert parse_fleet_count("5.73k") == 5730

    def test_whitespace_is_tolerated(self) -> None:
        assert parse_fleet_count("  1.11K ") == 1110

    def test_unreadable_text_is_not_guessed(self) -> None:
        for junk in ("", "K", "abc", "5..3K", "1,090"):
            assert parse_fleet_count(junk) is None


class TestTiers:
    def test_each_bucket_maps_to_its_preset(self) -> None:
        assert tier_for(3000).preset == "攻击组合甲"
        assert tier_for(6000).preset == "攻击组合乙"
        assert tier_for(9000).preset == "攻击组合丙"

    def test_below_two_thousand_gets_no_preset(self) -> None:
        # 用户明确说过 2K 以下的误差可以完全忽略。
        assert tier_for(1999) is FleetTier.NEGLIGIBLE
        assert tier_for(1999).preset is None

    def test_boundaries_are_left_closed(self) -> None:
        assert tier_for(2000) is FleetTier.ALPHA
        assert tier_for(5000) is FleetTier.BETA
        assert tier_for(8000) is FleetTier.GAMMA

    def test_the_measured_samples_land_where_expected(self) -> None:
        """用户给的 5 份样本，按逐行合计的量级定档。"""
        assert tier_for(11690) is FleetTier.GAMMA  # bot_2_121_7
        assert tier_for(9970) is FleetTier.GAMMA  # bot_2_132_7
        assert tier_for(3130) is FleetTier.ALPHA  # bot_2_134_16
        assert tier_for(5960) is FleetTier.BETA  # bot_2_127_15


class TestBoundarySensitivity:
    def test_a_reading_far_from_any_edge_is_not_flagged(self) -> None:
        # 档位中间错几十艘没有后果。
        assert not classify(3500).near_boundary
        assert not classify(6500).near_boundary

    def test_a_reading_near_an_edge_is_flagged(self) -> None:
        # 边界附近错几十艘就会换一套攻击组合——这是误差唯一要紧的地方。
        assert classify(4980).near_boundary
        assert classify(5020).near_boundary
        assert classify(2000 - BOUNDARY_MARGIN).near_boundary

    def test_the_verdict_carries_the_preset(self) -> None:
        verdict = classify(6000)
        assert verdict.tier is FleetTier.BETA
        assert verdict.preset == "攻击组合乙"
        assert verdict.total == 6000

    def test_a_rounded_reading_does_not_change_the_bucket(self) -> None:
        """`5.36K` 读成 `5.35K` 不该改变任何结论。"""
        assert tier_for(parse_fleet_count("5.36K") or 0) is tier_for(
            parse_fleet_count("5.35K") or 0
        )

    def test_a_lost_leading_digit_does_change_it(self) -> None:
        """而丢首位会——这正是识别要防的那一类错。"""
        full = parse_fleet_count("5.36K") or 0
        truncated = parse_fleet_count(".36K") or 0
        assert tier_for(full) is not tier_for(truncated)


def test_the_tier_names_are_the_ones_shown_in_the_ui() -> None:
    assert FleetTier.ALPHA.value == "2K–5K"
    with pytest.raises(ValueError):
        FleetTier("nonsense")
