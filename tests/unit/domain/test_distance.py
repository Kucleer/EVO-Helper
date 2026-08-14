"""目标排序：先比银河、再比恒星系，而且绝不把距离存成列。"""

from __future__ import annotations

from evo_helper.domain.distance import distance_key, nearest_first, within
from evo_helper.domain.models import Coordinate

HOME = Coordinate(2, 137, 18)


def test_the_galaxy_outranks_the_system() -> None:
    """**实机数据（同一套编组，速度 14.520）：**

        2:499:18   同银河、系差 362    飞行 1969 秒
        3:303:18   跨一个银河、系差 166  飞行 3752 秒

    跨一个银河比同银河内最远的那一头还贵近一倍，所以银河这一位永远压过恒星系。
    这条钉住那个余量：同银河最远端也要排在隔壁银河最近端**前面**。
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
