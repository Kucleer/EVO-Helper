"""五步选靶流水线的护栏。每条钉的都是「改坏了也不报错」的那种。

流水线（用户口径 2026-08-18，整段写在 `domain.target_order` 模块头上）：

1. 剔除 24h 内已攻击的 + 本轮已走完的（住在 `application`，钉在那一侧）
2. 只保留有军力读数的
3. 按读数时间倒序取前 N ＝**时间池**
4. 在时间池里按军力取前 M ＝**军力截断**
5. 按距离由近到远出击
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import (
    DEFAULT_SCORE_MAX_AGE,
    DEFAULT_TIME_POOL,
    TOP_BY_MILITARY,
    ScoredTarget,
    newest_readings_first,
    recent_then_strongest,
    score_is_fresh,
    split_by_freshness,
    strongest_first,
    strongest_then_nearest,
    strongest_within,
    with_a_military_reading,
)

HOME = Coordinate(2, 137, 18)
NOW = datetime(2026, 8, 17, 5, 28, tzinfo=UTC)
TWO_HOURS = timedelta(hours=2)


def _target(
    system: int,
    score: float | None,
    *,
    galaxy: int = 2,
    scanned_at: datetime | None = NOW,
) -> ScoredTarget:
    """默认**给「刚读到」**：不给的话每条用例都要为一个与它无关的理由写读取时刻。

    没有分数时读取时刻也一并抹掉——那才是「从没上过榜」的真实形状。
    """
    return ScoredTarget(Coordinate(galaxy, system, 5), score, None if score is None else scanned_at)


# -- 第 2 步：没有军力读数的不再参与 -------------------------------------------


def test_a_target_that_never_made_the_board_is_out() -> None:
    """⚠️ **用户 2026-08-18 决定：从未上过军力榜的目标不再攻击。**

    这一条**推翻**了从前那版「没有分数的按距离补位」。旧设计的依据是一句错话
    ——「没被榜单扫到过的正是库里最多的一批」——那个数把非 bot 的行也算进了分母。
    实测 628 个，占 bot 总数（3604）的 17.4%。

    放弃这 17.4% 换来的是「军力优先」真的成立：补位不参与按军力排序，补位一多，
    这条链路就退化成「按距离随便打」，而页面上看不出任何差别。
    """
    never_seen = _target(140, None)
    rated = _target(400, 8_000.0)

    assert with_a_military_reading([never_seen, rated]) == [rated]
    assert recent_then_strongest([never_seen, rated]) == (rated,)


def test_a_score_without_a_reading_time_is_out_too() -> None:
    """有分数却说不清什么时候读的，同样进不了池。

    时间池按读数时间排序，一个没有时刻的目标在那把尺子上没有位置。把它当成
    「很旧」或者「很新」都是在编一个没量到的数——而这个仓有一条硬规矩：
    猜出来的数不许长得像量出来的。
    """
    no_clock = ScoredTarget(Coordinate(2, 141, 5), 9_000.0, None)

    assert with_a_military_reading([no_clock]) == []
    assert recent_then_strongest([no_clock]) == ()


def test_a_pool_with_nothing_rated_is_empty_not_a_crash() -> None:
    """一个有读数的都没有时给出空清单——上层据此判「此刻没活干」，而不是崩掉。"""
    assert recent_then_strongest([_target(140, None), _target(141, None)]) == ()


# -- 第 3 步：时间池按**读数时间**取，不是按军力取 -----------------------------


def test_the_time_pool_takes_the_newest_readings_not_the_strongest() -> None:
    """⚠️ **排序键是读数时间，不是军力。**

    换成军力的话这一步就变成第二道军力截断，「用多新的数据」这件事再没人管——
    而最新的那批读数恰恰是军力最低的那些（榜单按军力降序扫，先读到的读数最旧）。

    这里军力与读数时间**刻意反着排**：9000 读得最旧、100 读得最新。按军力取前 2
    会拿到 [9000, 8000]；按读数时间取前 2 才是这条用例要的 [100, 8000]。
    """
    pool = newest_readings_first(
        [
            _target(140, 9_000.0, scanned_at=NOW - timedelta(hours=5)),
            _target(141, 8_000.0, scanned_at=NOW - timedelta(hours=1)),
            _target(142, 100.0, scanned_at=NOW),
        ],
        take=2,
    )

    assert [item.coordinate.system for item in pool] == [142, 141]


def test_the_time_pool_is_never_empty_just_because_everything_expired() -> None:
    """⚠️ **这是本次改动的核心：全部超期时，时间池照样拿得出最新的那批。**

    旧实现把超期的整批滤掉，于是「一个新鲜分数都没有」时池子退化成
    「按距离补位、军力完全不参与」——2026-08-17 晚上实机连续 2.5 小时就是这个状态。
    """
    three_days = NOW - timedelta(days=3)
    all_stale = [
        _target(140, 9_000.0, scanned_at=three_days),
        _target(141, 8_000.0, scanned_at=three_days - timedelta(hours=1)),
    ]

    assert len(newest_readings_first(all_stale, take=500)) == 2


def test_the_time_pool_breaks_ties_by_coordinate() -> None:
    """同一时刻读到的（扫描一屏之内很常见）按坐标定序。

    不定的话，同一批目标每次挑出来的可能不是同一批——而那会让「上一轮打到哪了」
    无从谈起，事后拿日志对账也对不上。
    """
    tied = [_target(300, 1.0), _target(100, 2.0), _target(200, 3.0)]

    assert [item.coordinate.system for item in newest_readings_first(tied, take=3)] == [
        100,
        200,
        300,
    ]


def test_a_time_pool_of_nothing_yields_nothing() -> None:
    """`take=0` 给空清单，不崩也不当成「不设限」。"""
    assert newest_readings_first([_target(140, 9_000.0)], take=0) == ()


# -- 第 4 步：军力是一道**截断**，在时间池**之内**生效 -------------------------


def test_the_cut_happens_inside_the_time_pool() -> None:
    """⚠️ **构造一个「分数最高、却因为读数太旧掉出时间池」的目标：它不该被选中。**

    这一条把两步的从属关系钉死：军力截断只在时间池里挑，不许回头去看池外的目标。
    倒过来接（先按军力截断、再按读数时间取）的话，`2:140` 那个 99999 会顶着一份
    三天前的读数被选出来——而那正是「用多新的数据」要挡的。
    """
    selected = recent_then_strongest(
        [
            _target(140, 99_999.0, scanned_at=NOW - timedelta(days=3)),  # 最强，但读数最旧
            _target(141, 8_000.0, scanned_at=NOW),
            _target(142, 7_000.0, scanned_at=NOW - timedelta(minutes=1)),
        ],
        time_pool=2,
        take=1,
    )

    assert [item.coordinate.system for item in selected] == [141]


def test_the_cut_is_a_cut_not_a_sort() -> None:
    """⚠️ **军力必须真的把人挡在外面，不能只是排个序。**

    第 5 步按距离重排会把排序结果整个抹掉，所以军力只有这一次机会生效。改成
    「只排序不截断」的话，落选的那个会照样出现在结果里——只是排在后面。
    """
    selected = recent_then_strongest(
        [_target(140, 9_000.0), _target(141, 8_000.0), _target(142, 100.0)], time_pool=3, take=2
    )

    assert [item.coordinate.system for item in selected] == [140, 141]
    assert not any(item.coordinate.system == 142 for item in selected), "第 3 名不该出现在结果里"


def test_a_cut_larger_than_the_pool_is_not_an_error() -> None:
    """池子不够 N 个时就全要，而不是报错或者补空。

    ⚠️ 这也正是**军力截断失效**的形状：填成 ≥ 池内目标数，这一刀什么都不挡。
    2026-08-18 之前 `top_n` 被填成 500 而可用候选只有 591，实际就在这一档上。
    """
    assert len(strongest_within([_target(140, 9_000.0)], take=500)) == 1


def test_a_cut_of_nothing_yields_nothing() -> None:
    """`take=0` 要给出空清单——上层据此判「这一轮没得打」，而不是崩掉。"""
    assert strongest_within([_target(140, 9_000.0)], take=0) == ()


# -- 上限 ----------------------------------------------------------------------


def test_the_cap_keeps_the_unbeatable_ones_out_of_the_pool() -> None:
    """用户口径（2026-08-14）：「军力确实要设置上限」。

    太强的目标不是当前预设打得动的，派过去只是白烧一次配额和一趟往返。
    """
    ordered = strongest_then_nearest(
        [_target(140, 1_773_000.0), _target(200, 9_000.0)], HOME, max_score=100_000.0
    )

    assert [item.system for item in ordered] == [200]


def test_no_cap_keeps_even_the_strongest() -> None:
    """默认不设上限。"""
    ordered = strongest_then_nearest([_target(140, 1_773_000.0)], HOME)

    assert [item.system for item in ordered] == [140]


# -- 第 5 步：池内一律按距离 ---------------------------------------------------


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


def test_distance_inside_the_pool_is_measured_round_the_ring() -> None:
    """池内的距离用 `distance_key`，也就是**环形**的。

    从 2:137 看 `2:499` 只有 137 步（绕过 499↔1），而线性减法会算成 362。
    """
    pool = [_target(287, 9_000.0), _target(499, 9_100.0)]

    ordered = strongest_then_nearest(pool, HOME, take=2)

    assert [item.system for item in ordered] == [499, 287]


# -- 排序本身 ------------------------------------------------------------------


def test_an_unknown_score_never_outranks_a_known_one() -> None:
    """⚠️ **0 分是读到的事实，None 是不知道。**

    榜单上真的有 0 分的行。把 None 当成 0 就是把「没数据」伪装成「数据是 0」——
    而这个仓有一条硬规矩：猜出来的数不许长得像量出来的。

    没有分数的目标在第 2 步就出局了，所以这条守的是 `strongest_first` 这个通用
    排序本身：「不知道不等于 0」在哪里都成立。
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


# -- 两个默认值 ----------------------------------------------------------------


def test_the_two_knobs_have_the_defaults_the_user_asked_for() -> None:
    """用户口径（2026-08-18）：时间池 500、军力截断 100。

    ⚠️ **两个数管两件事，必须分开。** 时间池管「用多新的军力数据」，截断管
    「只打多强的」。合成一个数的话，想放宽数据新鲜度就只能连带把攻击面一起放宽。

    ⚠️ 截断从 50 改成 100。50 是 2026-08-15 那版「先取前 50 名」的口径，那时
    这一刀之前没有时间池；2026-08-18 重排流水线时用户把它定成 100。
    """
    assert DEFAULT_TIME_POOL == 500
    assert TOP_BY_MILITARY == 100


# -- 有效期：从判据降级成提示信号 ----------------------------------------------


def test_the_freshness_window_no_longer_filters_anything() -> None:
    """⚠️ **`score_is_fresh` 2026-08-18 起只是提示，它不挡任何目标。**

    当过滤器用的那一版，在「一个新鲜分数都没有」的夜里会把军力整个踢出选靶
    （2026-08-17 实机连续 2.5 小时）。这里两个目标的分数都超期了三天，
    而它们照样被选出来。
    """
    three_days = NOW - timedelta(days=3)
    all_stale = [
        _target(140, 9_000.0, scanned_at=three_days),
        _target(141, 8_000.0, scanned_at=three_days),
    ]

    assert all(not score_is_fresh(item, now=NOW, max_age=TWO_HOURS) for item in all_stale)
    assert len(recent_then_strongest(all_stale, time_pool=500, take=100)) == 2


def test_a_reading_inside_the_window_is_fresh() -> None:
    """有效期之内的读数算新。边界取「小于」：正好等于有效期算超期。"""
    just_read = _target(140, 9_000.0, scanned_at=NOW - timedelta(minutes=1))
    right_on_the_line = _target(141, 9_000.0, scanned_at=NOW - TWO_HOURS)

    assert score_is_fresh(just_read, now=NOW, max_age=TWO_HOURS) is True
    assert score_is_fresh(right_on_the_line, now=NOW, max_age=TWO_HOURS) is False


def test_a_reading_older_than_the_window_is_not_fresh() -> None:
    """实机 2026-08-17：`4:293:6` 的读数是 01:50 UTC，攻击发生在 05:28——3.6 小时。

    用户设的是 1 小时。这个判据本身仍然要准——日志里那句「这批分数已经超期多久」
    全靠它，而**日志说假话比不说更糟**。
    """
    stale = _target(293, 9_000.0, galaxy=4, scanned_at=datetime(2026, 8, 17, 1, 50, tzinfo=UTC))

    assert score_is_fresh(stale, now=NOW, max_age=timedelta(hours=1)) is False


def test_a_target_without_a_score_is_never_called_fresh() -> None:
    """没有分数就没有可排的东西，所以恒为假；它在第 2 步就已经出局了。"""
    assert score_is_fresh(_target(140, None), now=NOW, max_age=TWO_HOURS) is False


def test_the_split_only_keeps_accounts_now() -> None:
    """三堆只用来记账（日志里那句「其中 N 个已超期」），**不再决定谁出局**。

    ⚠️ 别把它接回选靶去：`expired` 那一堆现在照样参与，接回去等于把 2026-08-17
    那晚的停摆重新装上。
    """
    split = split_by_freshness(
        [
            _target(400, 100.0),
            _target(140, 9_000.0, scanned_at=NOW - timedelta(days=3)),
            _target(200, 8_000.0),
            _target(300, None),
        ],
        now=NOW,
        max_age=TWO_HOURS,
    )

    assert [item.coordinate.system for item in split.rated] == [400, 200]
    assert [item.coordinate.system for item in split.unrated] == [300]
    assert [item.coordinate.system for item in split.expired] == [140]


def test_the_default_window_is_about_twice_one_scan_round() -> None:
    """默认取「一轮扫描时长的约 2 倍」，因为那句提示得有个说得出来的基准。

    实测一轮军力榜扫描约 61 分钟（1000 个 · 8.7--16.3 个/分）。
    """
    assert DEFAULT_SCORE_MAX_AGE == timedelta(hours=2)
