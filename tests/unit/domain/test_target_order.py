"""先按军力取前 N 名，再把这 N 个按距离排。每条钉的都是「改坏了也不报错」的那种。"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import (
    TOP_BY_MILITARY,
    ScoredTarget,
    strongest_first,
    strongest_then_nearest,
)

HOME = Coordinate(2, 137, 18)


def _target(system: int, score: float | None, *, galaxy: int = 2) -> ScoredTarget:
    return ScoredTarget(Coordinate(galaxy, system, 5), score)


# -- 两步的地位完全不同 --------------------------------------------------------


def test_military_only_decides_who_gets_in_the_pool() -> None:
    """⚠️ **军力只用来截断，进了池子一律按距离。**

    这两步合成一个排序键的话，一夜的航线会在银河之间来回横跳：相邻两个目标的
    军力差可能只有几十点，而距离差是同银河 30 分钟 vs 跨银河 2.6 小时（实测）。
    """
    pool_of_two = [
        _target(400, 9_000.0),  # 更强，但远
        _target(140, 8_000.0),  # 稍弱，但近
        _target(200, 100.0),  # 太弱，进不了池
    ]

    ordered = strongest_then_nearest(pool_of_two, HOME, take=2)

    assert [item.system for item in ordered] == [140, 400], "池内按距离，不按军力"


def test_the_weak_ones_are_left_out_of_the_pool_entirely() -> None:
    """截断是硬的：没进前 N 名的这一轮根本不打。"""
    targets = [_target(140, 100.0), _target(141, 200.0), _target(142, 9_000.0)]

    ordered = strongest_then_nearest(targets, HOME, take=1)

    assert [item.system for item in ordered] == [142]


def test_distance_inside_the_pool_is_measured_round_the_ring() -> None:
    """池内的距离用 `distance_key`，也就是**环形**的。

    从 2:137 看 `2:499` 只有 137 步（绕过 499↔1），而线性减法会算成 362。
    """
    pool = [_target(287, 9_000.0), _target(499, 9_100.0)]

    ordered = strongest_then_nearest(pool, HOME, take=2)

    assert [item.system for item in ordered] == [499, 287]


# -- 挑谁进池子 ----------------------------------------------------------------


def test_an_unknown_score_never_outranks_a_known_one() -> None:
    """⚠️ **0 分是读到的事实，None 是不知道。**

    榜单上真的有 0 分的行。把 None 当成 0 就是把「没数据」伪装成「数据是 0」——
    而这个仓有一条硬规矩：猜出来的数不许长得像量出来的。

    排最后而不是最前：用户要的是「先打强的」，而一个不知道强弱的目标
    既谈不上强也谈不上弱，先把知道的打完再说。
    """
    ordered = strongest_first([_target(140, None), _target(141, 0.0)])

    assert [item.coordinate.system for item in ordered] == [141, 140]


def test_the_pool_is_the_same_every_time() -> None:
    """⚠️ 军力相同时按坐标定序。

    不定的话，同一批目标每次挑出来的前 N 个可能不一样——而那会让
    「上一轮打到哪了」无从谈起，事后拿日志对账也对不上。
    """
    tied = [_target(300, 9_000.0), _target(100, 9_000.0), _target(200, 9_000.0)]

    assert strongest_first(tied) == strongest_first(list(reversed(tied)))
    assert [item.coordinate.system for item in strongest_first(tied)] == [100, 200, 300]


def test_the_default_pool_size_is_the_one_the_user_asked_for() -> None:
    """用户口径（2026-08-15）：「先取前 50 名」。"""
    assert TOP_BY_MILITARY == 50


def test_a_pool_larger_than_the_target_list_is_not_an_error() -> None:
    """库里不够 50 个时就全要，而不是报错或者补空。"""
    ordered = strongest_then_nearest([_target(140, 9_000.0)], HOME, take=50)

    assert [item.system for item in ordered] == [140]


def test_a_pool_of_nothing_yields_nothing() -> None:
    """`take=0` 要给出空清单——上层据此判「这一轮没得打」，而不是崩掉。"""
    assert strongest_then_nearest([_target(140, 9_000.0)], HOME, take=0) == ()


# -- 上限 ----------------------------------------------------------------------


def test_the_cap_keeps_the_unbeatable_ones_out_of_the_pool() -> None:
    """用户口径（2026-08-14）：「军力确实要设置上限」。

    太强的目标不是当前预设打得动的，派过去只是白烧一次配额和一趟往返。
    """
    ordered = strongest_then_nearest(
        [_target(140, 1_773_000.0), _target(200, 9_000.0)], HOME, max_score=100_000.0
    )

    assert [item.system for item in ordered] == [200]


def test_the_cap_never_drops_a_target_whose_score_is_unknown() -> None:
    """⚠️ **上限只挡「太强」，不挡「读不出来」。**

    「不知道多强」不构成「一定太强」。按上限把 None 一起扔掉的话，凡是没被
    榜单扫到过的 bot 就永远不会被攻击——而那正是库里最多的一批。
    """
    ordered = strongest_then_nearest([_target(140, None)], HOME, max_score=100_000.0)

    assert [item.system for item in ordered] == [140]


def test_no_cap_keeps_even_the_strongest() -> None:
    """默认不设上限。"""
    ordered = strongest_then_nearest([_target(140, 1_773_000.0)], HOME)

    assert [item.system for item in ordered] == [140]
