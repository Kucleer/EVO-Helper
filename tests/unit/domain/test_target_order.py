"""五步选靶流水线的护栏。每条钉的都是「改坏了也不报错」的那种。

流水线（用户口径 2026-08-18，整段写在 `domain.target_order` 模块头上）：

1. 剔除 24h 内已攻击的 + 本轮已走完的（住在 `application`，钉在那一侧）
2. 只保留有军力读数的
3. 只保留读数落在**有效期窗口**内的；窗口内不足军力截断要的个数时**放弃窗口**
4. 在这一池里按军力取前 M ＝**军力截断**
5. 按距离由近到远出击

⚠️ **这一整节 2026-08-18 重写过第二次。** 上一版（PR #176）的第 3 步是「按读数
时间取前 N 个」，而那是错的：军力榜从强到弱扫，「读数最新」系统性地等价于
「军力最弱」，于是「军力优先」选出了全库最弱的一批。改写理由与生产实测分段表
在 `domain.target_order` 模块头第 3 步。被改写的每一条用例各自在 docstring 里
说明为什么。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import (
    DEFAULT_SCORE_MAX_AGE,
    TOP_BY_MILITARY,
    ScoredTarget,
    choose_by_military,
    score_is_fresh,
    strongest_first,
    strongest_then_nearest,
    strongest_within,
    with_a_military_reading,
    within_score_window,
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


def _chosen(targets: list[ScoredTarget], *, take: int, max_age: timedelta = TWO_HOURS) -> list[int]:
    """跑完第 2--4 步，把选中的恒星系号列出来。用例里最常问的就是这个。"""
    choice = choose_by_military(targets, now=NOW, max_age=max_age, take=take)
    return [item.coordinate.system for item in choice.selected]


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
    assert _chosen([never_seen, rated], take=10) == [400]


def test_a_score_without_a_reading_time_is_out_too() -> None:
    """有分数却说不清什么时候读的，同样进不了池。

    窗口按读数时刻划线，一个没有时刻的目标在那把尺子上没有位置。把它当成
    「很旧」或者「很新」都是在编一个没量到的数——而这个仓有一条硬规矩：
    猜出来的数不许长得像量出来的。
    """
    no_clock = ScoredTarget(Coordinate(2, 141, 5), 9_000.0, None)

    assert with_a_military_reading([no_clock]) == []
    assert _chosen([no_clock], take=10) == []


def test_a_pool_with_nothing_rated_is_empty_not_a_crash() -> None:
    """一个有读数的都没有时给出空清单——上层据此判「此刻没活干」，而不是崩掉。"""
    assert _chosen([_target(140, None), _target(141, None)], take=10) == []


# -- 第 3 步：窗口筛选（用例 a / d 就在这一节） ---------------------------------


def test_a_target_outside_the_window_stays_out_however_strong_it_is() -> None:
    """⚠️ **用例 (a)：窗口内够用时，窗口外的再强也不进。**

    ⚠️ **这条用例翻转了 PR #176 的 `test_the_time_pool_takes_the_newest_readings_not_the_strongest`
    与 `test_the_cut_happens_inside_the_time_pool`。** 那两条钉的是「按读数时间取
    前 N 个」，而那一步是错的：军力榜从强到弱扫，「读数最新」系统性地等价于
    「军力最弱」（实测分段表在 `domain.target_order` 模块头第 3 步），于是那一步
    实际上是一道**反向的军力截断**。它挡住了 99999 那种目标，靠的却是错误的理由
    ——所以那两条用例在新规格下仍然会绿，但它们守的是一件已经不存在的事。

    这里 `2:400` 的军力是全场最高（99999），读数却是三天前的——**它不该被选中**。
    窗口内还剩 3 个，够军力截断（2 个）用，所以窗口不必放弃。

    改成「按读数时间取前 N 个」（N 是个大数）的话，`2:400` 会连同所有人一起进池，
    再按军力截断就把它选出来了——这条用例因此会红。
    """
    targets = [
        _target(400, 99_999.0, scanned_at=NOW - timedelta(days=3)),  # 最强，但在窗口外
        _target(141, 9_000.0),
        _target(142, 8_000.0),
        _target(143, 7_000.0),
    ]

    choice = choose_by_military(targets, now=NOW, max_age=TWO_HOURS, take=2)

    assert [item.coordinate.system for item in choice.selected] == [141, 142]
    assert not any(item.coordinate.system == 400 for item in choice.selected), (
        "窗口外的目标再强也不该进来"
    )
    assert not choice.widened, "窗口内够用，不该报「放宽」"


def test_a_short_window_gives_up_the_window_instead_of_reaching_for_the_next_newest() -> None:
    """⚠️ **用例 (d)：不足时是「放弃窗口」，不是「按时间往下补」。**

    往下补捞到的正是**刚出窗口**那一批，而军力榜从强到弱扫，那一批恰恰是最弱的
    ——补下去等于把 PR #176 的缺陷换个地方原样复发。放弃窗口后在全部有读数的目标
    里按军力截断，至少拿到的是全库最强的那一批。

    这里刻意让「更新的」和「更强的」分开站：

    | 目标 | 读数 | 军力 | 谁会选它 |
    |---|---|---|---|
    | `2:100` | 刚读到（窗口内） | 100 | 两种都选不上（太弱） |
    | `2:200` / `2:300` | 3 小时前（刚出窗口） | 200 / 300 | **按时间往下补**会选 |
    | `2:900` / `2:800` | 3 天前 | 90000 / 80000 | **按军力截断**会选 |

    截断要 3 个而窗口内只有 1 个 → 放弃窗口 → 按军力取 [90000, 80000, 300]。
    改成「按时间往下补」的话出来的是 [100, 300, 200]，这条用例因此会红。
    """
    targets = [
        _target(100, 100.0),
        _target(200, 200.0, scanned_at=NOW - timedelta(hours=3)),
        _target(300, 300.0, scanned_at=NOW - timedelta(hours=3)),
        _target(900, 90_000.0, scanned_at=NOW - timedelta(days=3)),
        _target(800, 80_000.0, scanned_at=NOW - timedelta(days=3)),
    ]

    choice = choose_by_military(targets, now=NOW, max_age=TWO_HOURS, take=3)

    assert [item.coordinate.system for item in choice.selected] == [900, 800, 300]
    assert choice.widened, "放弃了窗口就得说出来"
    assert len(choice.in_window) == 1, "告警里那句「窗口内只有几个」取的就是这个数"


def test_the_window_draws_a_line_it_does_not_rank() -> None:
    """⚠️ **窗口筛选按时间划线，不按名次截断——这正是它和「取最新 N 个」的区别。**

    名次截断带选择偏差（「读数最新」≈「军力最弱」）；划线不带：线的位置只由
    `max_age` 定，与「这一批有多少个」「谁排第几」都无关。**上一版就是把这两件事
    判成了等价才写错的**，所以这条用例把「划线」这个性质本身钉下来：同一时刻读到
    的四个目标，无论军力高低，要么整批留下、要么整批出局。
    """
    half_an_hour_ago = NOW - timedelta(minutes=30)
    same_moment = [
        _target(100, 90_000.0, scanned_at=half_an_hour_ago),
        _target(200, 9_000.0, scanned_at=half_an_hour_ago),
        _target(300, 900.0, scanned_at=half_an_hour_ago),
        _target(400, 90.0, scanned_at=half_an_hour_ago),
    ]

    assert len(within_score_window(same_moment, now=NOW, max_age=TWO_HOURS)) == 4
    assert within_score_window(same_moment, now=NOW, max_age=timedelta(minutes=10)) == ()


def test_the_window_keeps_the_order_it_was_given() -> None:
    """窗口只筛，不排序。排序是第 4 步（军力）和第 5 步（距离）各自的事。

    在这里顺手排一次的话，两处排序会打架，而打架的症状是「同一批目标每次挑出来
    的不是同一批」——事后拿日志对账就对不上了。
    """
    given = [_target(300, 1.0), _target(100, 2.0), _target(200, 3.0)]

    assert [
        item.coordinate.system for item in within_score_window(given, now=NOW, max_age=TWO_HOURS)
    ] == [300, 100, 200]


def test_a_window_that_keeps_everything_never_reports_a_widening() -> None:
    """⚠️ **窗口内不足 K，但库里本来就只有这些读数——这不叫「用了旧数据」。**

    放弃窗口一个目标都没多捞到，所以不该告警。判据因此是「选中的这批里有没有
    窗口外的」，而不是「有没有走放宽那条分支」。

    少了这条，一个「`len(in_window) < take` 就报警」的实现会全绿，而它会在库里
    目标本来就少的时候每轮都响——**每轮都响的告警和不响的一样没用**。
    """
    only_two = [_target(140, 9_000.0), _target(141, 8_000.0)]

    choice = choose_by_military(only_two, now=NOW, max_age=TWO_HOURS, take=100)

    assert [item.coordinate.system for item in choice.selected] == [140, 141]
    assert not choice.widened


def test_a_pool_where_everything_expired_still_attacks_by_military() -> None:
    """⚠️ **2026-08-17 那晚的复现：全部超期也不许让这一轮空手。**

    那一版把超期的整批滤掉，于是「一个新鲜分数都没有」时候选池退化成
    「军力完全不参与」，实机连续停摆 2.5 小时。窗口重新会筛，但**筛空了就放弃
    窗口**，所以那种停摆回不来。

    这里三个目标的读数都是三天前的，窗口是 2 小时——一个都不在窗口内，
    而军力截断照样在全部有读数的目标里正常生效。
    """
    three_days = NOW - timedelta(days=3)
    all_stale = [
        _target(140, 9_000.0, scanned_at=three_days),
        _target(141, 8_000.0, scanned_at=three_days),
        _target(142, 100.0, scanned_at=three_days),
    ]

    choice = choose_by_military(all_stale, now=NOW, max_age=TWO_HOURS, take=2)

    assert [item.coordinate.system for item in choice.selected] == [140, 141]
    assert choice.widened, "一个新鲜读数都没有还照打，这件事必须说出来"
    assert choice.in_window == ()


# -- 第 4 步：军力是一道**截断** -----------------------------------------------


def test_the_cut_is_a_cut_not_a_sort() -> None:
    """⚠️ **军力必须真的把人挡在外面，不能只是排个序。**

    第 5 步按距离重排会把排序结果整个抹掉，所以军力只有这一次机会生效。改成
    「只排序不截断」的话，落选的那个会照样出现在结果里——只是排在后面。
    """
    selected = _chosen([_target(140, 9_000.0), _target(141, 8_000.0), _target(142, 100.0)], take=2)

    assert selected == [140, 141]
    assert 142 not in selected, "第 3 名不该出现在结果里"


def test_a_cut_larger_than_the_pool_is_not_an_error() -> None:
    """池子不够 N 个时就全要，而不是报错或者补空。

    ⚠️ 这也正是**军力截断失效**的形状：填成 ≥ 池内目标数，这一刀什么都不挡。
    2026-08-18 之前 `top_n` 被填成 500 而可用候选只有 591，实际就在这一档上。
    """
    assert len(strongest_within([_target(140, 9_000.0)], take=500)) == 1


def test_a_cut_of_nothing_yields_nothing() -> None:
    """`take=0` 要给出空清单——上层据此判「这一轮没得打」，而不是崩掉。"""
    assert strongest_within([_target(140, 9_000.0)], take=0) == ()
    assert _chosen([_target(140, 9_000.0)], take=0) == []


# -- 上限 ----------------------------------------------------------------------


def test_the_cap_keeps_the_unbeatable_ones_out_of_the_pool() -> None:
    """用户口径（2026-08-14）：「军力确实要设置上限」。

    太强的目标不是当前预设打得动的，派过去只是白烧一次配额和一趟往返。
    """
    ordered = strongest_then_nearest(
        [_target(140, 1_773_000.0), _target(200, 9_000.0)],
        HOME,
        now=NOW,
        max_score=100_000.0,
    )

    assert [item.system for item in ordered] == [200]


def test_no_cap_keeps_even_the_strongest() -> None:
    """默认不设上限。"""
    ordered = strongest_then_nearest([_target(140, 1_773_000.0)], HOME, now=NOW)

    assert [item.system for item in ordered] == [140]


# -- 第 5 步：池内一律按距离（用例 e） -----------------------------------------


def test_military_only_decides_who_gets_in_the_pool() -> None:
    """⚠️ **用例 (e)：军力只用来截断，进了池子一律按距离。**

    这两步合成一个排序键的话，一夜的航线会在银河之间来回横跳：相邻两个目标的
    军力差可能只有几十点，而距离差是同银河 30 分钟 vs 跨银河 2.6 小时（实测）。

    第 5 步换成按军力排序的话，出来的是 `[400, 140]`——这条用例因此会红。
    """
    pool_of_two = [
        _target(400, 9_000.0),  # 更强，但远
        _target(140, 8_000.0),  # 稍弱，但近
        _target(200, 100.0),  # 太弱，进不了池
    ]

    ordered = strongest_then_nearest(pool_of_two, HOME, now=NOW, take=2)

    assert [item.system for item in ordered] == [140, 400], "池内按距离，不按军力"


def test_distance_inside_the_pool_is_measured_round_the_ring() -> None:
    """池内的距离用 `distance_key`，也就是**环形**的。

    从 2:137 看 `2:499` 只有 137 步（绕过 499↔1），而线性减法会算成 362。
    """
    pool = [_target(287, 9_000.0), _target(499, 9_100.0)]

    ordered = strongest_then_nearest(pool, HOME, now=NOW, take=2)

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


def test_the_knobs_have_the_defaults_the_user_asked_for() -> None:
    """用户口径（2026-08-18）：军力截断 100、有效期窗口 2 小时。

    ⚠️ **这条用例改过一次。** 它从前还断言 `DEFAULT_TIME_POOL == 500`，而「时间池」
    这个旋钮已经随那个错误设计一起删掉了（理由在 `domain.target_order` 模块头
    第 3 步）。剩下的两个数管的仍然是两件事：截断管「只打多强的」，窗口管
    「用多新的数据」。

    ⚠️ 截断从 50 改成 100。50 是 2026-08-15 那版「先取前 50 名」的口径；
    2026-08-18 重排流水线时用户把它定成 100。

    窗口默认取「一轮扫描时长的约 2 倍」：实测一轮军力榜扫描约 61 分钟
    （1000 个 · 8.7--16.3 个/分）。
    """
    assert TOP_BY_MILITARY == 100
    assert DEFAULT_SCORE_MAX_AGE == timedelta(hours=2)


# -- 新鲜度判据本身 ------------------------------------------------------------


def test_a_reading_inside_the_window_is_fresh() -> None:
    """有效期之内的读数算新。边界取「小于」：正好等于有效期算超期。"""
    just_read = _target(140, 9_000.0, scanned_at=NOW - timedelta(minutes=1))
    right_on_the_line = _target(141, 9_000.0, scanned_at=NOW - TWO_HOURS)

    assert score_is_fresh(just_read, now=NOW, max_age=TWO_HOURS) is True
    assert score_is_fresh(right_on_the_line, now=NOW, max_age=TWO_HOURS) is False


def test_a_reading_older_than_the_window_is_not_fresh() -> None:
    """实机 2026-08-17：`4:293:6` 的读数是 01:50 UTC，攻击发生在 05:28——3.6 小时。

    用户设的是 1 小时。这个判据本身仍然要准——告警里那句「最旧读数是什么时候」
    全靠它，而**日志说假话比不说更糟**。
    """
    stale = _target(293, 9_000.0, galaxy=4, scanned_at=datetime(2026, 8, 17, 1, 50, tzinfo=UTC))

    assert score_is_fresh(stale, now=NOW, max_age=timedelta(hours=1)) is False


def test_a_target_without_a_score_is_never_called_fresh() -> None:
    """没有分数就没有可排的东西，所以恒为假；它在第 2 步就已经出局了。"""
    assert score_is_fresh(_target(140, None), now=NOW, max_age=TWO_HOURS) is False
