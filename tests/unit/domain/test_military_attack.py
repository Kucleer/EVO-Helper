"""军力攻击多 origin 的护栏：每条都是静默做错时必须变红的判据。"""

from __future__ import annotations

from evo_helper.domain.military_attack import (
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_distance,
    military_pool,
    tier_for,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget


def _target(system: int, score: float | None) -> ScoredTarget:
    return ScoredTarget(Coordinate(2, system, 5), score)


def test_pool_drops_over_cap_but_keeps_unknown_score() -> None:
    """None 不是 0，也不能因上限被静默扔掉。"""
    pool = military_pool(
        [_target(140, 30_000), _target(141, None), _target(142, 1_000)],
        take=50,
        maximum_score=20_000,
    )

    assert [item.coordinate.system for item in pool] == [142, 141]


def test_unknown_score_is_not_assigned_to_lowest_tier() -> None:
    """把未知当 0 会错误地选择 AAA；必须走显式回落预设。"""
    tiers = (MilitaryTier(5_000, "BBB"), MilitaryTier(0, "AAA"))

    assert tier_for(None, tiers) is None
    assigned = assign_by_capacity_and_distance(
        [_target(140, None)],
        [AttackOrigin(Coordinate(2, 137, 18), 1)],
        fallback_preset="BBB",
        tiers=tiers,
    )
    assert assigned[0].preset == "BBB"


def test_assignment_uses_each_origin_capacity_instead_of_piling_on_nearest() -> None:
    """忽略容量时两发都会给主星，第二颗星球会无声闲置。"""
    near = Coordinate(2, 137, 18)
    far = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_distance(
        [_target(138, 9_000), _target(139, 8_000), _target(140, 7_000)],
        [AttackOrigin(near, 2), AttackOrigin(far, 1)],
        fallback_preset="BBB",
    )

    assert [item.origin for item in assigned].count(near) == 2
    assert [item.origin for item in assigned].count(far) == 1


def test_assignment_measures_system_distance_round_the_ring() -> None:
    """abs(137 - 499) 会把环形最近的目标误派给另一颗星球。"""
    left = Coordinate(2, 137, 18)
    right = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_distance(
        [_target(499, 9_000)],
        [AttackOrigin(left, 1), AttackOrigin(right, 1)],
        fallback_preset="BBB",
    )

    assert assigned[0].origin == left


def test_results_are_grouped_by_origin_for_one_switch_per_runner() -> None:
    """结果不能按原始目标顺序交替，否则每发都要重新切星球。"""
    first = Coordinate(2, 137, 18)
    second = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_distance(
        [_target(138, 9_000), _target(299, 8_000)],
        [AttackOrigin(first, 1), AttackOrigin(second, 1)],
        fallback_preset="BBB",
    )

    assert len({item.origin for item in assigned[:1]}) == 1
    assert [item.origin for item in assigned] == sorted(item.origin for item in assigned)
