"""军力攻击多 origin 的护栏：每条都是静默做错时必须变红的判据。"""

from __future__ import annotations

from evo_helper.domain.military_attack import (
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_distance,
    tier_for,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget, strongest_within


def _target(system: int, score: float | None) -> ScoredTarget:
    return ScoredTarget(Coordinate(2, system, 5), score)


def test_pool_drops_over_cap_but_keeps_unknown_score() -> None:
    """None 不是 0，也不能因上限被静默扔掉。

    军力截断那一刀现在住在 `domain.target_order.strongest_within`；这条用例跟着
    搬过去指同一个判据，因为它守的是「上限只挡太强，不挡读不出来」，
    而那条规矩和它住在哪个模块无关。
    """
    pool = strongest_within(
        [_target(140, 30_000), _target(141, None), _target(142, 1_000)],
        take=50,
        max_score=20_000,
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


def test_assignment_keeps_each_origin_on_its_nearby_targets() -> None:
    """航线较少不能让 9 系反向抢走 2 系的近目标。"""
    two = Coordinate(2, 137, 18)
    nine = Coordinate(9, 250, 8)
    targets = [
        ScoredTarget(Coordinate(2, 150, 5), 93_050),
        ScoredTarget(Coordinate(9, 271, 9), 93_430),
        ScoredTarget(Coordinate(2, 16, 10), 53_450),
        ScoredTarget(Coordinate(9, 317, 10), 41_046),
        ScoredTarget(Coordinate(2, 3, 9), 93_630),
        ScoredTarget(Coordinate(2, 131, 10), 20_190),
    ]

    assigned = assign_by_capacity_and_distance(
        targets,
        [AttackOrigin(two, 4), AttackOrigin(nine, 2)],
        fallback_preset="BBB",
    )

    by_origin = {
        origin: {item.coordinate for item in assigned if item.origin == origin}
        for origin in (two, nine)
    }
    assert by_origin[two] == {
        Coordinate(2, 150, 5),
        Coordinate(2, 16, 10),
        Coordinate(2, 3, 9),
        Coordinate(2, 131, 10),
    }
    assert by_origin[nine] == {Coordinate(9, 271, 9), Coordinate(9, 317, 10)}


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


# -- 没有分数的目标不再补位（2026-08-18）--------------------------------------
#
# ⚠️ **这一节从前有五条用例，钉的是 `top_up_with_unrated`：主力（有分数的）不满
# 前 N 时，用没有军力分数的目标按距离补齐。整个函数连同那五条一起没了。**
#
# 不是「顺手删掉」：用户 2026-08-18 决定**从未上过军力榜的目标不再攻击**，那五条
# 用例钉的判据（补位怎么挑、补几个、排在哪）在新规格下一条都不成立——它们钉的是
# 一段不该再发生的行为。原来的理由是一句错话（「没被榜单扫到过的正是库里最多的
# 一批」，那个数把非 bot 的行也算进了分母；实测 628 个，占 bot 总数 3604 的
# 17.4%），整段善后写在 `domain.military_attack` 的模块头上。
#
# 接替它们的是 `tests/unit/domain/test_target_order.py` 里
# `test_a_target_that_never_made_the_board_is_out` 那一条：**没有军力读数的目标
# 一个都进不了池**。那是新规格下唯一还该被钉住的事。

HOME = Coordinate(2, 137, 18)


def test_the_cut_never_reorders_what_it_kept() -> None:
    """截断只截，不重排——池内那点次序留给第 5 步（按距离）去定。"""
    kept = strongest_within(
        [_target(400, 9_000), _target(141, 8_000), _target(140, 100)], take=2, max_score=None
    )

    assert [item.coordinate.system for item in kept] == [400, 141]
