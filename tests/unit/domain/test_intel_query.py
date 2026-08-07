from __future__ import annotations

import pytest

from evo_helper.domain.intel_query import (
    ConditionGroup,
    FleetCondition,
    GroupOperator,
    InvalidQueryError,
    Operator,
    QueryField,
    parse_coordinate_span,
)
from evo_helper.domain.models import Coordinate

TOTAL = QueryField.total()
GUARDIAN = QueryField.ship("钛能守卫者")


def total_over(value: int) -> FleetCondition:
    return FleetCondition(field=TOTAL, operator=Operator.GT, value=value)


def guardians_over(value: int) -> FleetCondition:
    return FleetCondition(field=GUARDIAN, operator=Operator.GT, value=value)


class TestCoordinateSpan:
    def test_full_start_and_end(self) -> None:
        span = parse_coordinate_span("1:100:1", "1:200:15")
        assert span.start == Coordinate(1, 100, 1)
        assert span.end == Coordinate(1, 200, 15)

    def test_same_galaxy_shorthand(self) -> None:
        """`1:100-1:200` means the whole systems 100..200 in galaxy 1."""
        span = parse_coordinate_span("1:100", "1:200")
        assert span.start == Coordinate(1, 100, 1)
        assert span.end.galaxy == 1
        assert span.end.system == 200

    def test_shorthand_end_covers_the_last_position(self) -> None:
        span = parse_coordinate_span("1:100", "1:200")
        assert span.contains(Coordinate(1, 200, 15))

    def test_span_includes_both_endpoints(self) -> None:
        span = parse_coordinate_span("1:100:1", "1:200:15")
        assert span.contains(Coordinate(1, 100, 1))
        assert span.contains(Coordinate(1, 200, 15))

    def test_span_excludes_outside(self) -> None:
        span = parse_coordinate_span("1:100", "1:200")
        assert not span.contains(Coordinate(1, 99, 5))
        assert not span.contains(Coordinate(2, 150, 5))

    def test_incomplete_range_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="coordinate"):
            parse_coordinate_span("1:100", "")

    def test_malformed_coordinate_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="coordinate"):
            parse_coordinate_span("1:100", "not-a-coordinate")

    def test_reversed_range_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="precede|order"):
            parse_coordinate_span("1:200", "1:100")

    def test_mixed_shorthand_and_full_is_allowed(self) -> None:
        span = parse_coordinate_span("1:100", "1:200:9")
        assert span.start == Coordinate(1, 100, 1)
        assert span.end == Coordinate(1, 200, 9)


class TestConditionValidation:
    def test_negative_value_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="negative"):
            FleetCondition(field=TOTAL, operator=Operator.GT, value=-1)

    def test_zero_is_allowed(self) -> None:
        """`= 0` is how a user asks for a ship type that is absent."""
        assert FleetCondition(field=GUARDIAN, operator=Operator.EQ, value=0).value == 0

    def test_blank_ship_name_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="ship"):
            QueryField.ship("   ")

    def test_empty_group_is_rejected(self) -> None:
        with pytest.raises(InvalidQueryError, match="empty"):
            ConditionGroup(operator=GroupOperator.AND, children=())

    def test_group_of_empty_groups_is_rejected(self) -> None:
        inner_error = "empty"
        with pytest.raises(InvalidQueryError, match=inner_error):
            ConditionGroup(
                operator=GroupOperator.AND,
                children=(ConditionGroup(operator=GroupOperator.OR, children=()),),
            )

    def test_unknown_ship_is_reported_against_the_vocabulary(self) -> None:
        group = ConditionGroup(
            operator=GroupOperator.AND,
            children=(
                FleetCondition(field=QueryField.ship("星门要塞"), operator=Operator.GT, value=1),
            ),
        )
        with pytest.raises(InvalidQueryError, match="星门要塞"):
            group.validate_ship_names({"钛能守卫者", "轻型战斗机"})

    def test_known_ships_pass_validation(self) -> None:
        group = ConditionGroup(operator=GroupOperator.AND, children=(guardians_over(5),))
        group.validate_ship_names({"钛能守卫者"})

    def test_total_field_needs_no_ship_vocabulary(self) -> None:
        group = ConditionGroup(operator=GroupOperator.AND, children=(total_over(2000),))
        group.validate_ship_names(set())


class TestEvaluation:
    """The spec's worked example: total > 2000 AND 钛能守卫者 > 5."""

    def group(self) -> ConditionGroup:
        return ConditionGroup(
            operator=GroupOperator.AND, children=(total_over(2000), guardians_over(5))
        )

    def test_matches_when_both_hold(self) -> None:
        assert self.group().matches({"钛能守卫者": 6, "轻型战斗机": 2000})

    def test_rejects_when_total_is_short(self) -> None:
        assert not self.group().matches({"钛能守卫者": 6, "轻型战斗机": 100})

    def test_rejects_when_ship_count_is_short(self) -> None:
        assert not self.group().matches({"钛能守卫者": 5, "轻型战斗机": 3000})

    def test_missing_ship_counts_as_zero(self) -> None:
        assert not self.group().matches({"轻型战斗机": 3000})

    def test_or_group_matches_either_side(self) -> None:
        group = ConditionGroup(
            operator=GroupOperator.OR, children=(total_over(9000), guardians_over(5))
        )
        assert group.matches({"钛能守卫者": 6})

    def test_nested_groups(self) -> None:
        group = ConditionGroup(
            operator=GroupOperator.AND,
            children=(
                total_over(1000),
                ConditionGroup(
                    operator=GroupOperator.OR, children=(guardians_over(100), total_over(2000))
                ),
            ),
        )
        assert group.matches({"钛能守卫者": 1, "轻型战斗机": 2500})
        assert not group.matches({"钛能守卫者": 1, "轻型战斗机": 1500})

    def test_every_operator(self) -> None:
        counts = {"钛能守卫者": 5}
        cases = [
            (Operator.GT, 4, True),
            (Operator.GT, 5, False),
            (Operator.GTE, 5, True),
            (Operator.LT, 6, True),
            (Operator.LTE, 5, True),
            (Operator.EQ, 5, True),
            (Operator.NEQ, 5, False),
        ]
        for operator, value, expected in cases:
            condition = FleetCondition(field=GUARDIAN, operator=operator, value=value)
            assert condition.matches(counts) is expected, (operator, value)

    def test_total_uses_the_sum_of_all_counts(self) -> None:
        assert total_over(9).matches({"a": 5, "b": 5})
        assert not total_over(10).matches({"a": 5, "b": 5})


class TestNoSnapshotIsNotAHit:
    def test_target_without_a_snapshot_never_matches(self) -> None:
        """A bot with no fleet data must not be reported as a condition hit."""
        group = ConditionGroup(operator=GroupOperator.AND, children=(total_over(0),))
        assert not group.matches(None)

    def test_even_a_zero_condition_does_not_match_a_missing_snapshot(self) -> None:
        group = ConditionGroup(
            operator=GroupOperator.AND,
            children=(FleetCondition(field=GUARDIAN, operator=Operator.EQ, value=0),),
        )
        assert not group.matches(None)
