"""先按军力取前 N 名，再把这 N 个按距离排。每条钉的都是「改坏了也不报错」的那种。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import (
    DEFAULT_SCORE_MAX_AGE,
    TOP_BY_MILITARY,
    ScoredTarget,
    score_is_fresh,
    split_by_freshness,
    strongest_first,
    strongest_then_nearest,
)

HOME = Coordinate(2, 137, 18)
NOW = datetime(2026, 8, 17, 5, 28, tzinfo=UTC)
TWO_HOURS = timedelta(hours=2)


def _target(
    system: int,
    score: float | None,
    *,
    galaxy: int = 2,
    scanned_at: datetime | None = None,
) -> ScoredTarget:
    return ScoredTarget(Coordinate(galaxy, system, 5), score, scanned_at)


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

    ⚠️ 顺带记一笔：这个上限**目前是空转的**。用户口径（2026-08-17）：「目前的 bot
    的军事能力不存在太强这个可能性……已知周一刷新当日 bot 的最高战力只有 70 多 K」。
    留着不删是为了哪天 bot 变强，不是因为它现在在挡什么。
    """
    ordered = strongest_then_nearest([_target(140, None)], HOME, max_score=100_000.0)

    assert [item.system for item in ordered] == [140]


def test_no_cap_keeps_even_the_strongest() -> None:
    """默认不设上限。"""
    ordered = strongest_then_nearest([_target(140, 1_773_000.0)], HOME)

    assert [item.system for item in ordered] == [140]


# -- 读数的新鲜度 --------------------------------------------------------------


def test_a_reading_inside_the_window_is_fresh() -> None:
    """有效期之内的读数照打。边界取「小于」：正好等于有效期算超期。"""
    just_read = _target(140, 9_000.0, scanned_at=NOW - timedelta(minutes=1))
    right_on_the_line = _target(141, 9_000.0, scanned_at=NOW - TWO_HOURS)

    assert score_is_fresh(just_read, now=NOW, max_age=TWO_HOURS) is True
    assert score_is_fresh(right_on_the_line, now=NOW, max_age=TWO_HOURS) is False


def test_a_reading_older_than_the_window_is_not_fresh() -> None:
    """实机 2026-08-17：`4:293:6` 的读数是 01:50 UTC，攻击发生在 05:28——3.6 小时。

    用户设的是 1 小时，而那一版只在日志里记一句就照样派了出去。
    """
    stale = _target(293, 9_000.0, galaxy=4, scanned_at=datetime(2026, 8, 17, 1, 50, tzinfo=UTC))

    assert score_is_fresh(stale, now=NOW, max_age=timedelta(hours=1)) is False


def test_a_target_without_a_score_is_never_called_fresh_or_expired() -> None:
    """⚠️ **没有分数的目标在这里恒为假，而那不表示它出局。**

    `score_is_fresh` 只回答「这个**分数**还能不能用来排序」。没有分数就没有可排的
    东西，所以它为假；能不能打是 `split_by_freshness` 那一层的事，那里把它放进
    补位池。两件事合起来问的那一版，把库里最多的那批目标（从没上过榜的）
    永久排除掉了。
    """
    assert score_is_fresh(_target(140, None), now=NOW, max_age=TWO_HOURS) is False
    assert score_is_fresh(_target(141, None, scanned_at=NOW), now=NOW, max_age=TWO_HOURS) is False

    split = split_by_freshness([_target(140, None)], now=NOW, max_age=TWO_HOURS)
    assert split.expired == (), "没有分数不等于分数过期"
    assert [item.coordinate.system for item in split.unrated] == [140]


def test_a_score_without_a_reading_time_is_expired() -> None:
    """读到过分数、却没有读取时刻，算过期：说不清什么时候读的分数不能拿来排序。"""
    split = split_by_freshness([_target(141, 9_000.0)], now=NOW, max_age=TWO_HOURS)

    assert [item.coordinate.system for item in split.expired] == [141]


def test_the_split_keeps_the_order_it_was_given() -> None:
    """分堆只做分，不做排。排序是后面两步（军力截断、距离补位）的事。"""
    split = split_by_freshness(
        [
            _target(400, 100.0, scanned_at=NOW),
            _target(140, 9_000.0, scanned_at=NOW - timedelta(days=3)),
            _target(200, 8_000.0, scanned_at=NOW),
            _target(300, None),
        ],
        now=NOW,
        max_age=TWO_HOURS,
    )

    assert [item.coordinate.system for item in split.rated] == [400, 200]
    assert [item.coordinate.system for item in split.unrated] == [300]
    assert [item.coordinate.system for item in split.expired] == [140]


def test_the_default_window_is_about_twice_one_scan_round() -> None:
    """⚠️ **默认取「一轮扫描时长的约 2 倍」，不是 1 小时。**

    实测一轮军力榜扫描约 61 分钟（1000 个 · 8.7--16.3 个/分）。军力榜按军力降序排、
    扫描也从上往下读，所以一轮扫完之后先读到的（军力最高的）读数最旧。有效期若卡在
    「刚好一轮时长」附近，任何时刻能通过筛选的恰恰是这一批里**军力最低**的那些
    ——而「军力优先」正是为了打高军力的。改回 1 小时会把这个模式的意义抵消掉。
    """
    assert DEFAULT_SCORE_MAX_AGE == timedelta(hours=2)
