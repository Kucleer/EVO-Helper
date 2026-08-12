"""舰队规模分档。

用户要的不是精确数量，是落在哪一档（默认 2K / 4K / 8K 三道边界 → AAA / BBB /
CCC）。所以识别防的是**量级错**，不是末位误差。

三道边界可配（存 `scheduler_config`，页面在 `/tiers`），**档位数量与预设名不可配**。
这里的函数全是纯的：阈值由调用方传进来，模块自己不去查库。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.fleet_tier import (
    BOUNDARY_MARGIN,
    DEFAULT_TIER_THRESHOLDS,
    FleetTier,
    TierThresholdError,
    TierThresholds,
    classify,
    parse_fleet_count,
    tier_for,
)

#: 大多数用例用的就是用户给的那一套，写在这里省得每行重复。
EDGES = DEFAULT_TIER_THRESHOLDS


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

    def test_the_m_suffix_is_refused_on_purpose(self) -> None:
        """`M` 不认。识别侧的白名单本来也只放行 `0123456789.K`
        （`vision.optional.report_screens.UNIT_WHITELIST`），`M` 根本进不来；
        在这里认了它，就等于凭一个从未在实机上见过的后缀把舰队送出去。
        「没读到」在调用方那边的处置是不打，这是安全的那一侧。
        """
        for text in ("1.5M", "2M", "8m"):
            assert parse_fleet_count(text) is None

    def test_the_k_suffix_needs_its_decimal_point(self) -> None:
        """实机 2026-08-11 的量级错**不是**在这里发生的。

        2:48:12 的守方单位实为 `1.22K`，这个函数给出 1220（2K 以下，不该打）；
        入库的 122000 来自 `122K`——小数点在 OCR 那一层就掉了，修在
        `vision.fleet_counts.pick_count`。
        """
        assert parse_fleet_count("1.22K") == 1220
        assert parse_fleet_count("122K") == 122000


class TestThresholds:
    def test_the_defaults_are_the_ones_the_user_asked_for(self) -> None:
        """用户口径（2026-08-11）：2K 以下不打、2–4K AAA、4–8K BBB、8K+ CCC。

        ⚠️ 中间那道是 **4000**。原先代码里写的是 5000，用户在同一句话里把它
        改掉了；钉住这个数，免得哪次「顺手改回去」没人发现。
        """
        assert DEFAULT_TIER_THRESHOLDS.edges == (2000, 4000, 8000)

    def test_a_non_increasing_set_is_refused(self) -> None:
        """不递增就有一档取不到，而页面上三个框都填着数、看不出问题。

        比如 BBB 起点设成 9000 而 CCC 仍是 8000：8000 以上一律先撞上 CCC，
        BBB 再也轮不到。
        """
        with pytest.raises(TierThresholdError):
            TierThresholds(alpha_from=2000, beta_from=9000, gamma_from=8000)

    def test_equal_edges_are_refused_too(self) -> None:
        """相等同样是死区：区间宽度为零，那一档一个数都装不下。"""
        with pytest.raises(TierThresholdError):
            TierThresholds(alpha_from=2000, beta_from=4000, gamma_from=4000)

    def test_the_first_edge_must_be_above_zero(self) -> None:
        for edges in ((0, 4000, 8000), (-1, 4000, 8000)):
            with pytest.raises(TierThresholdError):
                TierThresholds(*edges)

    def test_the_refusal_says_which_numbers_are_wrong(self) -> None:
        """错误信息要说人话。静默排序或截断被明确排除——见 `TierThresholdError`。"""
        with pytest.raises(TierThresholdError) as caught:
            TierThresholds(alpha_from=2000, beta_from=9000, gamma_from=8000)

        assert "严格递增" in str(caught.value)
        assert "9000" in str(caught.value)

    def test_the_band_labels_follow_the_configured_numbers(self) -> None:
        """区间文字按当前取值现算，不写死在 `FleetTier` 上。

        写死的话，改完阈值之后日志和页面还会照旧念旧区间，而那一刻谁都看不出
        自己在读一句过期的话。
        """
        edges = TierThresholds(alpha_from=3000, beta_from=6500, gamma_from=9000)

        assert edges.label(FleetTier.NEGLIGIBLE) == "3K 以下"
        assert edges.label(FleetTier.ALPHA) == "3K–6.5K"
        assert edges.label(FleetTier.BETA) == "6.5K–9K"
        assert edges.label(FleetTier.GAMMA) == "9K+"

    def test_small_edges_are_written_out_in_full(self) -> None:
        """1000 以下不缩写：`0.9K` 比 `900` 难读，而这一档本来就是给人看的。"""
        assert TierThresholds(1, 900, 8000).label(FleetTier.ALPHA) == "1–900"


class TestTiers:
    def test_each_bucket_maps_to_its_preset(self) -> None:
        assert tier_for(3000, EDGES).preset == "AAA"
        assert tier_for(6000, EDGES).preset == "BBB"
        assert tier_for(9000, EDGES).preset == "CCC"

    def test_below_the_first_edge_gets_no_preset(self) -> None:
        # 用户明确说过最低那一档的误差可以完全忽略。
        assert tier_for(1999, EDGES) is FleetTier.NEGLIGIBLE
        assert tier_for(1999, EDGES).preset is None

    def test_boundaries_are_left_closed(self) -> None:
        assert tier_for(2000, EDGES) is FleetTier.ALPHA
        assert tier_for(4000, EDGES) is FleetTier.BETA
        assert tier_for(8000, EDGES) is FleetTier.GAMMA

    def test_the_thresholds_actually_move_the_buckets(self) -> None:
        """同一个读数，换一套阈值就该换一档——否则「可配」是假的。

        6000 在默认那一套里是 BBB；把中间那道推到 7000 之后它必须变成 AAA。
        """
        loosened = TierThresholds(alpha_from=2000, beta_from=7000, gamma_from=8000)

        assert tier_for(6000, EDGES) is FleetTier.BETA
        assert tier_for(6000, loosened) is FleetTier.ALPHA

    def test_the_measured_samples_land_where_expected(self) -> None:
        """用户给的 5 份样本，按逐行合计的量级定档（按当前默认阈值）。"""
        assert tier_for(11690, EDGES) is FleetTier.GAMMA  # bot_2_121_7
        assert tier_for(9970, EDGES) is FleetTier.GAMMA  # bot_2_132_7
        assert tier_for(3130, EDGES) is FleetTier.ALPHA  # bot_2_134_16
        assert tier_for(5960, EDGES) is FleetTier.BETA  # bot_2_127_15


class TestBoundarySensitivity:
    def test_a_reading_far_from_any_edge_is_not_flagged(self) -> None:
        # 档位中间错几十艘没有后果。
        assert not classify(3000, EDGES).near_boundary
        assert not classify(6500, EDGES).near_boundary

    def test_a_reading_near_an_edge_is_flagged(self) -> None:
        # 边界附近错几十艘就会换一套攻击组合——这是误差唯一要紧的地方。
        assert classify(3980, EDGES).near_boundary
        assert classify(4020, EDGES).near_boundary
        assert classify(2000 - BOUNDARY_MARGIN, EDGES).near_boundary

    def test_the_flagged_edges_follow_the_configuration(self) -> None:
        """「离边界多近」也按配置算：5000 在默认那一套里已经不是边界了。"""
        old = TierThresholds(alpha_from=2000, beta_from=5000, gamma_from=8000)

        assert classify(5020, old).near_boundary
        assert not classify(5020, EDGES).near_boundary

    def test_the_verdict_carries_the_preset(self) -> None:
        verdict = classify(6000, EDGES)

        assert verdict.tier is FleetTier.BETA
        assert verdict.preset == "BBB"
        assert verdict.total == 6000

    def test_a_rounded_reading_does_not_change_the_bucket(self) -> None:
        """`5.36K` 读成 `5.35K` 不该改变任何结论。"""
        assert tier_for(parse_fleet_count("5.36K") or 0, EDGES) is tier_for(
            parse_fleet_count("5.35K") or 0, EDGES
        )

    def test_a_lost_leading_digit_does_change_it(self) -> None:
        """而丢首位会——这正是识别要防的那一类错。"""
        full = parse_fleet_count("5.36K") or 0
        truncated = parse_fleet_count(".36K") or 0

        assert tier_for(full, EDGES) is not tier_for(truncated, EDGES)


def test_the_tier_names_carry_no_numbers() -> None:
    """`FleetTier` 的值里不许出现阈值。

    原先写的是 `"2K–5K"`。三道边界一旦可配，那种标签就成了一句过期的话，而它
    过期的时候页面和日志上都看不出来——要给人看的区间一律问
    `TierThresholds.label()`。
    """
    for tier in FleetTier:
        assert not any(character.isdigit() for character in tier.value)
    with pytest.raises(ValueError):
        FleetTier("nonsense")
