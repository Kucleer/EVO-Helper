"""军力攻击多 origin 的护栏：每条都是静默做错时必须变红的判据。

⚠️ **2026-08-18 起第 4 步的判据是 `军力 ÷ 往返小时`，不再是「按距离由近到远」。**
贪心的**键**换了，贪心的**形状**没换：仍然在整个候选池里挑最划算的
`(目标, origin)` 配对，而不是「先把每个目标分给最近的星球、再各自排序」——
后者会让航线数较少的那颗星球反过来抢走另一颗星球的近目标。整段在
`domain.military_attack` 的模块头上。
"""

from __future__ import annotations

from evo_helper.domain.military_attack import (
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_value,
    tier_for,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget, within_max_score


def _target(system: int, score: float | None) -> ScoredTarget:
    return ScoredTarget(Coordinate(2, system, 5), score)


def test_pool_drops_over_cap_but_keeps_unknown_score() -> None:
    """None 不是 0，也不能因上限被静默扔掉。

    安全线那一刀现在住在 `domain.target_order.within_max_score`；这条用例跟着
    搬过去指同一个判据，因为它守的是「上限只挡太强，不挡读不出来」，
    而那条规矩和它住在哪个模块无关。
    """
    pool = within_max_score(
        [_target(140, 30_000), _target(141, None), _target(142, 1_000)],
        max_score=20_000,
    )

    assert [item.coordinate.system for item in pool] == [141, 142]


def test_unknown_score_is_not_assigned_to_lowest_tier() -> None:
    """把未知当 0 会错误地选择 AAA；必须走显式回落预设。"""
    tiers = (MilitaryTier(5_000, "BBB"), MilitaryTier(0, "AAA"))

    assert tier_for(None, tiers) is None
    assigned = assign_by_capacity_and_value(
        [_target(140, None)],
        [AttackOrigin(Coordinate(2, 137, 18), 1)],
        fallback_preset="BBB",
        tiers=tiers,
    )
    assert assigned[0].preset == "BBB"


def test_assignment_uses_each_origin_capacity_instead_of_piling_on_nearest() -> None:
    """忽略容量时三发都会给主星，第二颗星球会无声闲置。"""
    near = Coordinate(2, 137, 18)
    far = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_value(
        [_target(138, 9_000), _target(139, 8_000), _target(140, 7_000)],
        [AttackOrigin(near, 2), AttackOrigin(far, 1)],
        fallback_preset="BBB",
    )

    assert [item.origin for item in assigned].count(near) == 2
    assert [item.origin for item in assigned].count(far) == 1


def test_assignment_keeps_each_origin_on_its_nearby_targets() -> None:
    """⚠️ **航线较少不能让 9 系反向抢走 2 系的近目标。**

    这条守的是贪心的**形状**（全局挑最划算的配对），而不是它的键。跨银河往返
    2.9 小时、同银河 0.5--1.1 小时，所以只要判据里还有「成本」这一项，每颗星球
    就该先吃自己邻域的目标。

    改成「先按某个规则定归属、再各自排序」的话，`9:250` 只有 2 条航线，
    它会先把两个 2 系目标抢走——这条用例因此会红。
    """
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

    assigned = assign_by_capacity_and_value(
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
    """abs(137 - 499) 会把环形最近的目标误派给另一颗星球。

    `2:499` 离 `2:137` 是 137 步、离 `2:300` 是 199 步，所以从左边打过去更划算。
    线性减法会算成 362 步对 199 步，于是派给右边——不报错，只是排错。
    """
    left = Coordinate(2, 137, 18)
    right = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_value(
        [_target(499, 9_000)],
        [AttackOrigin(left, 1), AttackOrigin(right, 1)],
        fallback_preset="BBB",
    )

    assert assigned[0].origin == left


def test_results_are_grouped_by_origin_for_one_switch_per_runner() -> None:
    """结果不能按原始目标顺序交替，否则每发都要重新切星球。"""
    first = Coordinate(2, 137, 18)
    second = Coordinate(2, 300, 18)
    assigned = assign_by_capacity_and_value(
        [_target(138, 9_000), _target(299, 8_000)],
        [AttackOrigin(first, 1), AttackOrigin(second, 1)],
        fallback_preset="BBB",
    )

    assert len({item.origin for item in assigned[:1]}) == 1
    assert [item.origin for item in assigned] == sorted(item.origin for item in assigned)


def test_inside_one_origin_group_the_best_score_goes_first() -> None:
    """⚠️ **组内也按得分排，不是按距离。**

    组内的先后是有后果的：这一轮的航线预算可能不够把整组派完
    （`_military_command` 的 `budget`），排在后面的要等下一轮。组内改回按距离排的话，
    「按得分出击」会在最后一步被悄悄抹掉——正如从前「按军力截断」被按距离重排
    抹掉过一次。

    这里 `2:400` 远但强（得分 21,884），`2:140` 近但弱（15,284）：
    按得分是 `[400, 140]`，按距离是 `[140, 400]`。
    """
    home = Coordinate(2, 137, 18)
    assigned = assign_by_capacity_and_value(
        [_target(140, 8_000), _target(400, 30_000)],
        [AttackOrigin(home, 2)],
        fallback_preset="BBB",
    )

    assert [item.coordinate.system for item in assigned] == [400, 140]


def test_the_greedy_takes_the_most_valuable_pair_not_the_nearest_one() -> None:
    """⚠️ **贪心的键是得分，不是距离——预算不够时这一条才看得出来。**

    只有一条航线，两个目标：`2:400`（远而强，得分 21,884）与 `2:140`
    （近而弱，得分 15,284）。按得分挑走的是 `2:400`；按距离挑走的是 `2:140`。

    这是把「② 改回纯就近」这一处变异钉红的用例：航线预算把两者的差别逼到了
    「谁被派出去、谁被留下」，而不只是先后。
    """
    home = Coordinate(2, 137, 18)
    assigned = assign_by_capacity_and_value(
        [_target(140, 8_000), _target(400, 30_000)],
        [AttackOrigin(home, 1)],
        fallback_preset="BBB",
    )

    assert [item.coordinate.system for item in assigned] == [400]


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
