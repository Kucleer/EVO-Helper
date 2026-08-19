"""AI 选靶的纯判据：picks 解析、硬校验、软核对。全部不碰网络与库。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.ai_targeting import (
    AiPick,
    PickVocabulary,
    SoftReference,
    parse_pick,
    soft_check_picks,
    validate_picks,
)
from evo_helper.domain.models import Coordinate

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

ORIGIN_A = Coordinate(4, 277, 15)
ORIGIN_B = Coordinate(9, 250, 8)

TARGETS = [
    Coordinate(4, 269, 8),
    Coordinate(4, 393, 10),
    Coordinate(9, 245, 14),
]

PRESETS = frozenset({"BBB"})


def _pick(target: Coordinate, *, origin: Coordinate = ORIGIN_A, **overrides: object) -> AiPick:
    base: dict[str, object] = {
        "target": target,
        "origin": origin,
        "preset": "BBB",
        "rank": 1,
        "military": 18_550.0,
        "reading_age_hours": 0.3,
        "round_trip_minutes": 34.0,
    }
    base.update(overrides)
    return AiPick(**base)  # type: ignore[arg-type]


def _vocabulary(total_budget: int = 2) -> PickVocabulary:
    return PickVocabulary(
        targets=frozenset(TARGETS),
        origins=frozenset({ORIGIN_A, ORIGIN_B}),
        presets=PRESETS,
        budget_by_origin={ORIGIN_A: 2, ORIGIN_B: 0},
        total_budget=total_budget,
    )


class TestParsePick:
    def test_a_well_formed_pick_parses(self) -> None:
        pick = parse_pick(
            {
                "target": "4:269:8",
                "origin": "4:277:15",
                "preset": "BBB",
                "rank": 1,
                "military": 18550,
                "reading_age_hours": 0.3,
                "round_trip_minutes": 34,
                "reason": "折算后最高",
            }
        )
        assert pick is not None
        assert pick.target == Coordinate(4, 269, 8)
        assert pick.origin == ORIGIN_A
        assert pick.preset == "BBB"
        assert pick.military == 18550.0
        assert pick.reason == "折算后最高"

    def test_missing_preset_is_rejected(self) -> None:
        assert parse_pick({"target": "4:269:8", "origin": "4:277:15"}) is None

    def test_broken_coordinate_is_rejected(self) -> None:
        assert (
            parse_pick({"target": "not-a-coordinate", "origin": "4:277:15", "preset": "BBB"})
            is None
        )

    def test_optional_numbers_stay_none(self) -> None:
        pick = parse_pick(
            {"target": "4:269:8", "origin": "4:277:15", "preset": "BBB", "reason": ""}
        )
        assert pick is not None
        assert pick.military is None
        assert pick.reading_age_hours is None
        assert pick.round_trip_minutes is None


class TestValidatePicks:
    def test_an_exact_budget_is_clean_and_counts_overlap(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(TARGETS[1])]
        violations, overlap = validate_picks(picks, _vocabulary(), {TARGETS[0]})
        assert violations == []
        assert overlap == 1

    def test_too_few_picks_is_rejected(self) -> None:
        violations, _ = validate_picks([_pick(TARGETS[0])], _vocabulary())
        assert any(item["code"] == "budget_mismatch" for item in violations)

    def test_too_many_picks_is_rejected(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(TARGETS[1]), _pick(TARGETS[2])]
        violations, _ = validate_picks(picks, _vocabulary())
        assert any(item["code"] == "budget_mismatch" for item in violations)

    def test_an_unknown_target_is_rejected(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(Coordinate(9, 9, 9))]
        violations, _ = validate_picks(picks, _vocabulary())
        assert any(item["code"] == "unknown_target" for item in violations)

    def test_an_unknown_origin_is_rejected(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(TARGETS[1], origin=Coordinate(1, 1, 1))]
        violations, _ = validate_picks(picks, _vocabulary())
        assert any(item["code"] == "unknown_origin" for item in violations)

    def test_an_unknown_preset_is_rejected(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(TARGETS[1], preset="CCC")]
        violations, _ = validate_picks(picks, _vocabulary())
        assert any(item["code"] == "unknown_preset" for item in violations)

    def test_a_duplicate_target_is_rejected(self) -> None:
        picks = [_pick(TARGETS[0]), _pick(TARGETS[0])]
        violations, _ = validate_picks(picks, _vocabulary())
        assert any(item["code"] == "duplicate_target" for item in violations)

    def test_an_origin_that_exceeds_its_budget_is_rejected(self) -> None:
        # ORIGIN_A 预算 2，塞进 3 发（总预算虚增到 3 以通过数量校验）。
        picks = [_pick(TARGETS[0]), _pick(TARGETS[1]), _pick(TARGETS[2])]
        vocabulary = PickVocabulary(
            targets=frozenset(TARGETS),
            origins=frozenset({ORIGIN_A}),
            presets=PRESETS,
            budget_by_origin={ORIGIN_A: 2},
            total_budget=3,
        )
        violations, _ = validate_picks(picks, vocabulary)
        assert any(item["code"] == "origin_budget_exceeded" for item in violations)


def _reference() -> SoftReference:
    return SoftReference(
        military={TARGETS[0]: 18_550.0, TARGETS[1]: 32_290.0},
        reading_age_hours={TARGETS[0]: 0.3, TARGETS[1]: 0.2},
        round_trip_minutes={
            TARGETS[0]: {ORIGIN_A: 34.0},
            TARGETS[1]: {ORIGIN_A: 62.0},
        },
        last_attack_at={TARGETS[0]: None, TARGETS[1]: NOW - timedelta(hours=3)},
        protected_until={TARGETS[0]: None, TARGETS[1]: None},
        now=NOW,
    )


class TestSoftCheck:
    def test_an_exact_report_is_clean(self) -> None:
        pick = _pick(TARGETS[0])
        violations = soft_check_picks([pick], _reference())
        assert violations == ()

    def test_a_fabricated_military_number_is_caught(self) -> None:
        pick = _pick(TARGETS[0], military=999_999.0)
        violations = soft_check_picks([pick], _reference())
        assert any(item["code"] == "self_consistency_military" for item in violations)

    def test_an_over_stated_age_is_caught(self) -> None:
        pick = _pick(TARGETS[0], reading_age_hours=5.0)
        violations = soft_check_picks([pick], _reference())
        assert any(item["code"] == "self_consistency_age" for item in violations)

    def test_an_over_stated_round_trip_is_caught(self) -> None:
        pick = _pick(TARGETS[0], round_trip_minutes=200.0)
        violations = soft_check_picks([pick], _reference())
        assert any(item["code"] == "self_consistency_round_trip" for item in violations)

    def test_a_pick_inside_the_protection_period_is_caught(self) -> None:
        reference = _reference()
        reference.protected_until[TARGETS[0]] = NOW + timedelta(hours=4)  # type: ignore[union-attr]
        violations = soft_check_picks([_pick(TARGETS[0])], reference)
        assert any(item["code"] == "rule_in_protection" for item in violations)

    def test_a_pick_attacked_within_eight_hours_is_caught(self) -> None:
        # TARGETS[1] 我方 3 小时前刚打过（< 游戏规则 8 小时）。
        violations = soft_check_picks([_pick(TARGETS[1])], _reference())
        assert any(item["code"] == "rule_attacked_too_recently" for item in violations)

    def test_an_attack_seven_days_ago_is_fine(self) -> None:
        reference = _reference()
        reference.last_attack_at[TARGETS[0]] = NOW - timedelta(days=7)  # type: ignore[union-attr]
        violations = soft_check_picks([_pick(TARGETS[0])], reference)
        assert all(item["code"] != "rule_attacked_too_recently" for item in violations)

    def test_the_threshold_is_the_game_rule_not_the_revisit_knob(self) -> None:
        """★ 10 小时前打过的**不算违规**——判据是游戏规则的 8 小时。

        ⚠️ 需求 5.3 第 2 条专门警告过这一点：**不要用那个 24 小时旋钮**
        （`bot_revisit_hours`）。10 小时落在两者之间，是唯一能把两个数分开的区间；
        少了这一条，把 8 悄悄换成 24 不会让任何用例转红（2026-08-19 变异实测）。
        """
        reference = _reference()
        reference.last_attack_at[TARGETS[0]] = NOW - timedelta(hours=10)  # type: ignore[union-attr]
        violations = soft_check_picks([_pick(TARGETS[0])], reference)
        assert all(item["code"] != "rule_attacked_too_recently" for item in violations)

    def test_seven_and_a_half_hours_is_still_a_violation(self) -> None:
        """边界的另一侧：7.5 小时 < 8 小时，仍然违规。"""
        reference = _reference()
        reference.last_attack_at[TARGETS[0]] = NOW - timedelta(hours=7, minutes=30)  # type: ignore[union-attr]
        violations = soft_check_picks([_pick(TARGETS[0])], reference)
        assert any(item["code"] == "rule_attacked_too_recently" for item in violations)

    def test_the_protection_window_is_eight_hours_after_it_was_seen(self) -> None:
        """保护期这一条的边界同理：撞上时刻 + 8 小时之后就不再算违规。

        ⚠️ 保护期到什么时候是**估**的：我们只知道「那一刻撞上了」，不知道它什么
        时候开始（`game.pirate_ui.DIALOG_NO_MISSION`），所以这是上界。
        """
        reference = _reference()
        reference.protected_until[TARGETS[0]] = NOW - timedelta(minutes=1)  # type: ignore[union-attr]
        violations = soft_check_picks([_pick(TARGETS[0])], reference)
        assert all(item["code"] != "rule_in_protection" for item in violations)

    def test_a_military_number_for_a_target_with_no_reading_is_caught(self) -> None:
        """★ 全池里那批**从没上过军力榜**的：AI 给它报一个军力数就是编的。

        ⚠️ 这一条是「喂全池」带来的新失败模式。`eligible` 里每个都有读数，
        所以这种情形以前不存在；`candidates` 里有一大批没有。只让它们在
        `military` 映射里缺席的话，核对会当成「无从核对」放过去。
        """
        naked = TARGETS[2]
        reference = SoftReference(
            military={},
            reading_age_hours={},
            round_trip_minutes={naked: {ORIGIN_A: 34.0}},
            last_attack_at={naked: None},
            protected_until={naked: None},
            now=NOW,
            targets_without_reading=frozenset({naked}),
        )
        violations = soft_check_picks([_pick(naked, military=18_550.0)], reference)
        assert any(item["code"] == "self_consistency_military" for item in violations)

    def test_an_unknown_target_without_that_marker_is_not_accused(self) -> None:
        """反面：不在「已知没读数」名单里的，缺读数就是「无从核对」，不该记违规。"""
        naked = TARGETS[2]
        reference = SoftReference(
            military={},
            reading_age_hours={},
            round_trip_minutes={naked: {ORIGIN_A: 34.0}},
            last_attack_at={naked: None},
            protected_until={naked: None},
            now=NOW,
        )
        violations = soft_check_picks([_pick(naked, military=18_550.0)], reference)
        assert all(item["code"] != "self_consistency_military" for item in violations)
