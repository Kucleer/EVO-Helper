"""目标排序：先比银河、再比恒星系，而且绝不把距离存成列。"""

from __future__ import annotations

from evo_helper.domain.distance import distance_key, galaxy_gap, nearest_first, within
from evo_helper.domain.models import Coordinate

HOME = Coordinate(2, 137, 18)


# -- 银河是一个环 --------------------------------------------------------------


def test_the_far_side_of_the_ring_is_close() -> None:
    """⚠️ **这条是实机量出来的，不是推的。**

    同一套编组（速度 14.520 / 100%）从 **2** 系出发，简报页上的飞行时间：

        目标 3 系   3752 秒     线性差 1   环形差 1
        目标 9 系   5305 秒     线性差 7   环形差 2
        目标 8 系   6497 秒     线性差 6   环形差 3

    三个点全部落在 `3750 × √环形差` 上（误差 2 秒）。换成线性差就崩掉：
    9 系会被算成 9922 秒，比实测多出 **4617 秒**。
    """
    assert galaxy_gap(9, 2) == 2
    assert galaxy_gap(8, 2) == 3
    assert galaxy_gap(3, 2) == 1


def test_the_two_ways_round_meet_at_the_same_place() -> None:
    """⚠️ **这条是判别性证据，不是又一个拟合点。**

    从 2 系出发，`2→6` 正着走 4 步、`2→7` 倒着走 4 步（`2→1→9→8→7`）。

        线性差是 4 和 5   → 预言两者时间**不同**
        环形差都是 4      → 预言两者时间**一模一样**

    实测（用户反复核对三遍）：`2时5分2秒 = 7502 秒`、气体 `869.88K`，
    **两者完全一致**。两个模型在这里给出相反的预言，而实测选了环形。

    9 个银河是奇数，所以「两条路等长」只在环形差 4 这一处发生——
    这也是为什么它能当判据：别的距离上两个模型只是数值不同，
    这里是**定性**不同。
    """
    assert galaxy_gap(6, 2) == galaxy_gap(7, 2) == 4
    assert distance_key(Coordinate(6, 1, 1), HOME)[0] == distance_key(Coordinate(7, 1, 1), HOME)[0]


def test_the_ring_does_not_care_which_way_you_came() -> None:
    """绕过去和绕回来一样远。距离是对称的，环也不例外。"""
    assert galaxy_gap(9, 2) == galaxy_gap(2, 9)


def test_nothing_is_more_than_half_the_ring_away() -> None:
    """9 个银河，最远 4 步——绕过半圈就该往回走了。

    这条同时钉住「别把 `total` 写成 8 或 10」：写错的话最远距离会变成 4 或 5，
    而且**只在环的另一半上错**，近处的目标看上去一切正常。
    """
    assert max(galaxy_gap(target, 1) for target in range(1, 10)) == 4
    assert galaxy_gap(6, 1) == 4  # 顺着 5 步，倒着 4 步
    assert galaxy_gap(1, 1) == 0


def test_the_system_number_is_deliberately_not_a_ring() -> None:
    """⚠️ **银河成环不代表恒星系也成环，而这个赌注是不对称的。**

    从 2:137 看 2:499：直的减法是 362，若成环则是 `499-362 = 137`。
    差得很远，而**没有量过**。

    - 猜它成环、其实不成 → 把 362 当成 137，**以为很近其实很远**，舰队一去不回。
    - 猜它不成环、其实成环 → 近目标被排到后面，少打几发。

    所以这里故意用直的减法。要改的话先去量：从 2:137 出发比 `2:499` 和 `2:275`
    （若成环则两者近似等距）。**别凭「银河成环所以恒星系也成环」就动手。**
    """
    assert distance_key(Coordinate(2, 499, 1), HOME)[1] == 362


def test_the_ring_changes_which_galaxy_is_second_nearest() -> None:
    """**这就是写成减法的代价**：不报错，只是一夜的航线全排错。

    从 2 系看，9 系是第二近的银河（环形 2 步），而 5 系是第三近（3 步）。
    用 `abs` 的话，9 系（差 7）会被排到 5 系（差 3）后面。
    """
    targets = [Coordinate(5, 1, 1), Coordinate(9, 1, 1), Coordinate(3, 1, 1)]

    assert [item.galaxy for item in nearest_first(targets, HOME)] == [3, 9, 5]


def test_the_galaxy_outranks_the_system() -> None:
    """**实机数据（同一套编组，速度 14.520）：**

        2:499:18   同银河、系差 362      1969 秒
        3:303:18   环形差 1、系差 166    3752 秒

    跨一个银河比同银河内最远的那一头还贵近一倍，所以银河这一位永远压过恒星系。
    这条钉住那个余量：同银河最远端也要排在隔壁银河最近端**前面**。

    跨银河的三个点还落在一条**只含环形银河距离**的曲线上（`≈3750×√环形差`，
    误差 2 秒），而它们的恒星系号各不相同——也就是说跨银河时恒星系号根本不影响
    时间（详见 `domain.distance` 模块头）。这不改变这里的排序，但它说明第二段
    `|Δ恒星系|` 对**跨银河**目标只是并列打破，不是距离。
    """
    far_same_galaxy = Coordinate(2, 499, 18)
    near_next_galaxy = Coordinate(3, 137, 18)

    assert nearest_first([near_next_galaxy, far_same_galaxy], HOME) == (
        far_same_galaxy,
        near_next_galaxy,
    )


def test_within_one_galaxy_the_closer_system_wins() -> None:
    ordered = nearest_first(
        [Coordinate(2, 200, 1), Coordinate(2, 140, 1), Coordinate(2, 90, 1)], HOME
    )

    assert [item.system for item in ordered] == [140, 90, 200]


def test_the_same_distance_either_side_ties() -> None:
    """左右对称：137±20 一样近。距离是绝对值，不是有向的。"""
    assert distance_key(Coordinate(2, 117, 1), HOME) == distance_key(Coordinate(2, 157, 1), HOME)


def test_the_order_is_the_same_every_time() -> None:
    """⚠️ **同一批目标每次都要排成同一个样子。**

    位次进排序键只为这个——不放的话，同距离目标的先后取决于库里的返回顺序，
    而那个顺序换一次查询、换一次索引就会变，事后拿日志对账时对不上。
    """
    targets = [Coordinate(2, 140, 9), Coordinate(2, 140, 2), Coordinate(2, 140, 20)]

    assert [item.position for item in nearest_first(targets, HOME)] == [2, 9, 20]
    assert nearest_first(targets, HOME) == nearest_first(reversed(targets), HOME)


def test_a_different_origin_gives_a_different_order() -> None:
    """**这条是「绝不把距离存成列」的理由本身。**

    同一批目标，从不同的出发星球看，顺序完全不同。存成列的那天，第二颗出发星球
    上的任务会拿着按主星算的距离排序——而且**完全不报错**，只是打的顺序莫名其妙。
    用户 2026-08-14 明确要求兼容多出发星球，所以这条必须是活的。
    """
    targets = [Coordinate(2, 100, 1), Coordinate(2, 300, 1)]

    from_home = nearest_first(targets, HOME)
    from_far = nearest_first(targets, Coordinate(2, 290, 5))

    assert [item.system for item in from_home] == [100, 300]
    assert [item.system for item in from_far] == [300, 100]


def test_within_drops_the_other_galaxies_entirely() -> None:
    """跨银河的不是「远一点」，是贵一个量级——不该靠把半径调大来够到它们。"""
    targets = [Coordinate(3, 137, 1), Coordinate(2, 150, 1), Coordinate(2, 400, 1)]

    assert within(targets, HOME, systems=20) == (Coordinate(2, 150, 1),)


def test_within_keeps_the_boundary_itself() -> None:
    """区间是闭的：正好 20 系的那个要留下。差一个的取舍在这里说死。"""
    assert within([Coordinate(2, 157, 1)], HOME, systems=20) == (Coordinate(2, 157, 1),)
    assert within([Coordinate(2, 158, 1)], HOME, systems=20) == ()
