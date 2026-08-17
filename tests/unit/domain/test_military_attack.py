"""军力攻击多 origin 的护栏：每条都是静默做错时必须变红的判据。"""

from __future__ import annotations

from evo_helper.domain.military_attack import (
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_distance,
    military_pool,
    tier_for,
    top_up_with_unrated,
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


# -- 没有分数的目标怎么补位 ----------------------------------------------------
#
# 用户口径（2026-08-17）：「目前的 bot 的军事能力不存在太强这个可能性……已知周一
# 刷新当日 bot 的最高战力只有 70 多 K」。所以「没有分数」这一档打起来毫无风险，
# 它只是排不了序——于是它不参与按军力取前 N，只在主力没取满时按距离补。

HOME = Coordinate(2, 137, 18)


def test_the_unrated_only_fill_the_seats_the_rated_left_empty() -> None:
    """⚠️ **主力优先：先按军力取满前 N，剩下的空位才轮到补位。**

    混排的话，没有分数的那些会占掉前 N 的名额（`strongest_first` 把 None 排在
    最后，但排最后**也是占位**），于是「军力优先」在补位多的夜里退化成「随便打」。
    """
    rated = military_pool([_target(140, 9_000), _target(141, 8_000)], take=3, maximum_score=None)
    unrated = [_target(200, None), _target(300, None)]

    pool = top_up_with_unrated(rated, unrated, [HOME], take=3)

    assert [item.coordinate.system for item in pool] == [140, 141, 200]


def test_a_full_pool_of_rated_targets_takes_no_filler() -> None:
    """主力就把前 N 占满时，一个补位都不许挤进来。"""
    rated = military_pool([_target(140, 9_000), _target(141, 8_000)], take=2, maximum_score=None)

    pool = top_up_with_unrated(rated, [_target(200, None)], [HOME], take=2)

    assert [item.coordinate.system for item in pool] == [140, 141]


def test_the_filler_is_ordered_by_distance_not_by_anything_else() -> None:
    """补位之间没有「更值得打」的依据，唯一还剩的可比量是路程。

    ⚠️ 距离必须是**环形**的（`distance_key`）：从 2:137 看 `2:499` 只有 137 步，
    而线性减法会算成 362，于是真正最近的那个被排到后面。
    """
    unrated = [_target(300, None), _target(499, None), _target(140, None)]

    pool = top_up_with_unrated((), unrated, [HOME], take=2)

    assert [item.coordinate.system for item in pool] == [140, 499]


def test_the_filler_measures_distance_to_the_nearest_origin() -> None:
    """多出发点时只按其中一颗算，等于替另一颗做主，所以取**到最近那颗**的距离。

    `2:139` 离第一颗出发点 2 步、离第二颗 261 步；`2:401` 反过来，离第二颗只有 1 步。
    只按第一颗量的话排出来是 `[139, 401]`——而真正近的是 `401`。
    """
    first = Coordinate(2, 137, 18)
    second = Coordinate(2, 400, 18)
    unrated = [_target(139, None), _target(401, None)]

    pool = top_up_with_unrated((), unrated, [first, second], take=2)

    assert [item.coordinate.system for item in pool] == [401, 139]


def test_the_filler_never_reorders_the_rated_ones() -> None:
    """补位接在主力后面，主力那一段的次序一个字不动。"""
    rated = (_target(400, 9_000), _target(141, 8_000))

    pool = top_up_with_unrated(rated, [_target(140, None)], [HOME], take=5)

    assert [item.coordinate.system for item in pool] == [400, 141, 140]
