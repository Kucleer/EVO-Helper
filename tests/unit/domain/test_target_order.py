"""四步选靶流水线的护栏。每条钉的都是「改坏了也不报错」的那种。

流水线（用户口径 2026-08-18，整段写在 `domain.target_order` 模块头上）：

1. 剔除 24h 内已攻击的 + 本轮已走完的（住在 `application`，钉在那一侧）
2. 只保留有**本周期**军力读数的（读数早于本周一 UTC+0 的一律当作没有读数）
3. 只保留读数落在**有效期窗口**内的；窗口内不足**窗口门限**时**放弃窗口**
4. 过军力上限这道安全线，按 **军力 ÷ 往返小时** 降序出击

⚠️ **这一整节 2026-08-18 重写过第三次。**

- 上上版（PR #176）的第 3 步是「按读数时间取前 N 个」，而军力榜从强到弱扫，
  「读数最新」系统性地等价于「军力最弱」——那一版把全库最弱的一批选了出来。
- 上一版把第 4、5 步写成「窗口内按军力硬截断前 `top_n` 名」＋「这批人按距离
  由近到远出击」。两步各自说得通，合起来说不清：「第 101 名一个都不打」和
  「第 1 名与第 100 名之间只按远近分先后」是两条互相矛盾的口径，而它们之间那道墙
  纯粹是拍出来的。**这一版把两步合成一条判据**：`军力 ÷ 往返小时`。

被改写的每一条用例各自在 docstring 里说明为什么。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import (
    DEFAULT_SCORE_MAX_AGE,
    MILITARY_EXPONENT,
    WINDOW_POOL_FLOOR,
    ScoredTarget,
    attack_value,
    choose_by_military,
    has_a_military_reading,
    most_valuable_first,
    reading_is_from_this_cycle,
    score_is_fresh,
    value_key,
    with_a_military_reading,
    within_max_score,
    within_score_window,
)

HOME = Coordinate(2, 137, 18)
#: **周四**，刻意不是周一。第 2 步有一条按「本周期起点（周一 00:00 UTC）」划的线
#: （`reading_is_from_this_cycle`），而这一整组里绝大多数用例量的是**窗口**那条线。
#: `NOW` 落在周一凌晨的话，「三天前的读数」会先被周期边界吃掉，那些用例就再也验不到
#: 窗口了——**红是红了，红的却是另一件事**。周期边界那条线自己有一节用例
#: （「第 2 步的另一半」），那一节各自摆自己的时刻。
NOW = datetime(2026, 8, 20, 5, 28, tzinfo=UTC)
#: 本周期的起点：`NOW` 那一周的周一 00:00 UTC。写死一个数而不是调
#: `cycle_start_utc(NOW)`——用例里再调一次被测的那个函数，等于让实现给自己判卷。
CYCLE_START = datetime(2026, 8, 17, tzinfo=UTC)
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


def _eligible(
    targets: list[ScoredTarget], *, window_floor: int, max_age: timedelta = TWO_HOURS
) -> list[int]:
    """跑完第 2--3 步与安全线，把有资格被打的恒星系号列出来。"""
    choice = choose_by_military(targets, now=NOW, max_age=max_age, window_floor=window_floor)
    return [item.coordinate.system for item in choice.eligible]


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

    assert with_a_military_reading([never_seen, rated], now=NOW) == [rated]
    assert _eligible([never_seen, rated], window_floor=10) == [400]


def test_a_score_without_a_reading_time_is_out_too() -> None:
    """有分数却说不清什么时候读的，同样进不了池。

    窗口按读数时刻划线，一个没有时刻的目标在那把尺子上没有位置。把它当成
    「很旧」或者「很新」都是在编一个没量到的数——而这个仓有一条硬规矩：
    猜出来的数不许长得像量出来的。
    """
    no_clock = ScoredTarget(Coordinate(2, 141, 5), 9_000.0, None)

    assert with_a_military_reading([no_clock], now=NOW) == []
    assert _eligible([no_clock], window_floor=10) == []


def test_a_pool_with_nothing_rated_is_empty_not_a_crash() -> None:
    """一个有读数的都没有时给出空清单——上层据此判「此刻没活干」，而不是崩掉。"""
    assert _eligible([_target(140, None), _target(141, None)], window_floor=10) == []


# -- 第 2 步的另一半：周期边界（bot 军力每周一 UTC+0 刷新） ---------------------
#
# 用户口径（2026-08-19）：「周一刷新那一刻，全部 bot 的军力读数同时作废」。
# 这一节的用例**各自摆自己的时刻**，不用上面那个周四的 `NOW`——它们量的正是
# 「现在离周一边界多远」，而那恰恰是 `NOW` 那条线要避开的东西。


#: 刷新之后半小时。这一节大多数用例站在这里：上周日晚上的读数才刚过 31 分钟，
#: **有效期窗口一点都拦不住它**——能拦住它的只有周期边界。
JUST_AFTER_THE_REFRESH = datetime(2026, 8, 17, 0, 30, tzinfo=UTC)


def test_a_reading_taken_before_this_weeks_refresh_counts_as_no_reading() -> None:
    """⚠️ **周一刷新那一刻，全库军力读数同时作废——上周期的读数等于没有读数。**

    这里那条读数只有 31 分钟大，**2 小时的有效期窗口对它毫无意见**。所以这条用例
    只可能被周期边界那一条判据守住：把它删掉（或者写成「不管什么时候读的都算」），
    这条立刻红。

    作废的理由不是「旧」，是**描述的对象换了**：刷新之后榜上是这周的 bot，
    上周的读数再新也是在说另一批数。
    """
    last_night = ScoredTarget(
        Coordinate(2, 400, 5), 99_999.0, JUST_AFTER_THE_REFRESH - timedelta(minutes=31)
    )

    assert score_is_fresh(last_night, now=JUST_AFTER_THE_REFRESH, max_age=TWO_HOURS), (
        "前提摆错了：这条读数必须在有效期之内，否则守住它的是窗口而不是周期边界"
    )
    assert not has_a_military_reading(last_night, now=JUST_AFTER_THE_REFRESH)
    assert with_a_military_reading([last_night], now=JUST_AFTER_THE_REFRESH) == []

    choice = choose_by_military(
        [last_night], now=JUST_AFTER_THE_REFRESH, max_age=TWO_HOURS, window_floor=1
    )
    assert choice.eligible == ()


def test_widening_the_window_cannot_reach_back_into_the_previous_cycle() -> None:
    """⚠️ **这一条是整条改动的要害：放宽窗口不许把上周期的读数捞回来。**

    第 3 步「窗口内不足门限就放弃窗口、改用 `with_readings`」这条路，在周一凌晨
    一定会走到——窗口内是 0 个。上周期的读数若还留在 `with_readings` 里，放宽之后
    进池的**全是失效数据**，而页面上只会显示「军力读数已放宽窗口」这句听起来完全
    正常的告警——**比不打还糟，因为它看起来在正常工作**。

    所以判据必须待在**第 2 步**：`with_readings` 本身就不含它们，放宽也捞不回来。
    把那条判据挪到第 3 步之后、或者在放宽那里补一个 `if`，这条都会红。

    ⚠️ **`widened` 必须是假。** 报「已放宽」等于替页面说了句好听的假话：这一轮
    该说的是「本周期还没有读数」（`TaskStatus.MISSING_MILITARY_SCORES`）。
    """
    last_week = [
        ScoredTarget(
            Coordinate(2, 400 + index, 5),
            90_000.0 - index,
            JUST_AFTER_THE_REFRESH - timedelta(days=1, hours=index),
        )
        for index in range(5)
    ]

    choice = choose_by_military(
        last_week, now=JUST_AFTER_THE_REFRESH, max_age=TWO_HOURS, window_floor=100
    )

    assert choice.in_window == (), "窗口内本来就该是空的，放宽那条路一定会走到"
    assert choice.with_readings == (), "上周期的读数留在了第 2 步的结果里，放宽就会捞回来"
    assert choice.considered == ()
    assert choice.eligible == (), "放宽窗口把上周期的失效读数捞了回来"
    assert not choice.widened, "这一轮该说的是「本周期没读数」，不是听起来正常的「已放宽」"
    assert len(choice.from_previous_cycles) == 5


def test_the_cycle_boundary_does_not_replace_the_max_age_window() -> None:
    """⚠️ **两条判据都生效，取更严的那个——谁都不许替代谁。**

    - **本周期读到、但超出有效期**：周期边界放行，窗口挡下 → 走「放弃窗口」那条路，
      照打，并报 `widened`。周期边界要是把窗口顶掉了，`widened` 就成了假。
    - **上周期读到、但比有效期还新**：窗口放行，周期边界挡下 → 出局。
      窗口要是把周期边界顶掉了，它会进池。

    两半各钉一个方向。只写一半的话，「用周期边界替代 `max_age`」这种实现会全绿。
    """
    # 周四：本周期，但读数是 5 小时前的，超出 2 小时窗口。
    this_cycle_but_stale = ScoredTarget(Coordinate(2, 401, 5), 9_000.0, NOW - timedelta(hours=5))
    assert this_cycle_but_stale.military_score_at_utc is not None
    assert this_cycle_but_stale.military_score_at_utc > CYCLE_START

    widened = choose_by_military(
        [this_cycle_but_stale], now=NOW, max_age=TWO_HOURS, window_floor=100
    )
    assert widened.eligible == (this_cycle_but_stale,), "周期边界不该把有效期窗口顶掉"
    assert widened.widened, "超期的读数照打，但这件事必须说出来"

    # 周一凌晨：读数才 31 分钟，窗口放行，周期边界照样挡下。
    previous_cycle_but_fresh = ScoredTarget(
        Coordinate(2, 402, 5), 9_000.0, JUST_AFTER_THE_REFRESH - timedelta(minutes=31)
    )
    a_very_wide_window = choose_by_military(
        [previous_cycle_but_fresh],
        now=JUST_AFTER_THE_REFRESH,
        max_age=timedelta(days=30),
        window_floor=1,
    )
    assert a_very_wide_window.eligible == (), "把有效期调宽就能拉回上周期的读数了"


def test_a_reading_taken_at_the_refresh_moment_itself_belongs_to_this_cycle() -> None:
    """边界取「大于等于」：正好落在周一 00:00:00 UTC 那一秒读到的算**本周期**。

    差一秒读到的那条则出局。写成 `>` 的话，刷新那一秒采到的第一批会被自己丢掉；
    写成「同一天算数」的话，上周日整天的读数会跟着混进来。
    """
    on_the_dot = ScoredTarget(Coordinate(2, 403, 5), 9_000.0, CYCLE_START)
    a_second_early = ScoredTarget(
        Coordinate(2, 404, 5), 9_000.0, CYCLE_START - timedelta(seconds=1)
    )

    assert reading_is_from_this_cycle(on_the_dot, now=JUST_AFTER_THE_REFRESH)
    assert not reading_is_from_this_cycle(a_second_early, now=JUST_AFTER_THE_REFRESH)


def test_the_line_is_the_weekly_refresh_not_a_fixed_age() -> None:
    """⚠️ **线的位置由「本周一 UTC+0」定，不是「多少小时以内」。**

    周日深夜回头看，周一凌晨读到的那条已经快 7 天大了，**它仍然算本周期**——
    因为这中间没发生过刷新。而周一凌晨那条只有 31 分钟的读数反而出局。

    **「越新越算数」在这条线上不成立**，这正是它和有效期窗口的分野。把实现换成
    任何一个固定时长（`now - 7 天`、`now - 24 小时`……），这条用例必红。
    """
    sunday_night = datetime(2026, 8, 23, 23, 0, tzinfo=UTC)
    read_at_the_start_of_the_week = ScoredTarget(
        Coordinate(2, 405, 5), 9_000.0, datetime(2026, 8, 17, 1, 0, tzinfo=UTC)
    )

    assert reading_is_from_this_cycle(read_at_the_start_of_the_week, now=sunday_night), (
        "同一周期内读的，隔多久都还算数"
    )
    assert not reading_is_from_this_cycle(
        read_at_the_start_of_the_week, now=sunday_night + timedelta(hours=2)
    ), "跨过下一个周一 00:00 UTC 之后，同一条读数立刻作废"


def test_the_previous_cycle_batch_is_counted_apart_from_the_never_rated() -> None:
    """⚠️ **「上周期的读数」和「从没上过军力榜」必须分得开。**

    两者在第 2 步的结果上一模一样（都不在 `with_readings` 里），善后却完全不同：
    前者等军力榜再扫一轮就好，后者是这个 bot 从来没被扫到过。合成一个数的话，
    周一凌晨的日志会写着「N 个从未上榜」——一句假话，而且会把人引到
    「军力榜为什么漏了这些 bot」这条错路上。
    """
    never_seen = ScoredTarget(Coordinate(2, 406, 5), None, None)
    last_week = ScoredTarget(
        Coordinate(2, 407, 5), 9_000.0, JUST_AFTER_THE_REFRESH - timedelta(hours=2)
    )
    read_today = ScoredTarget(Coordinate(2, 408, 5), 8_000.0, JUST_AFTER_THE_REFRESH)

    choice = choose_by_military(
        [never_seen, last_week, read_today],
        now=JUST_AFTER_THE_REFRESH,
        max_age=TWO_HOURS,
        window_floor=1,
    )

    assert choice.from_previous_cycles == (last_week,), "从未上榜的被算进「上周期」那一档了"
    assert choice.with_readings == (read_today,)
    assert choice.eligible == (read_today,)


# -- 第 3 步：窗口筛选（用例 f 就在这一节） -------------------------------------


def test_a_target_outside_the_window_stays_out_however_strong_it_is() -> None:
    """⚠️ **窗口内够用时，窗口外的再强也不进。**

    这里 `2:400` 的军力是全场最高（99999），读数却是三天前的——**它不该进池**。
    窗口内还剩 3 个，够窗口门限（2 个）用，所以窗口不必放弃。

    改成「按读数时间取前 N 个」（N 是个大数）的话，`2:400` 会连同所有人一起进池
    ——这条用例因此会红。
    """
    targets = [
        _target(400, 99_999.0, scanned_at=NOW - timedelta(days=3)),  # 最强，但在窗口外
        _target(141, 9_000.0),
        _target(142, 8_000.0),
        _target(143, 7_000.0),
    ]

    choice = choose_by_military(targets, now=NOW, max_age=TWO_HOURS, window_floor=2)

    assert [item.coordinate.system for item in choice.eligible] == [141, 142, 143]
    assert not choice.widened, "窗口内够用，不该报「放宽」"


def test_the_window_floor_is_still_the_yardstick_for_step_three() -> None:
    """⚠️ **用例 (f)：`top_n`（窗口门限）仍然是第 3 步「够不够」的那把尺子。**

    它 2026-08-18 起**不再决定打谁**（军力硬截断取消了），但这一个身份必须原样
    保留：窗口内的数量 ≥ 门限就只用窗口内的，不足就放弃窗口。

    同一批目标、只改门限，结果必须翻面——把第 3 步的判据换成任何别的东西
    （固定阈值、`>= 1`、恒真、恒假）都会让这条红。
    """
    targets = [
        _target(141, 9_000.0),
        _target(142, 8_000.0),
        _target(400, 99_999.0, scanned_at=NOW - timedelta(days=3)),
    ]

    assert _eligible(targets, window_floor=2) == [141, 142], "窗口内 2 个 ≥ 门限 2，只用窗口内的"
    assert _eligible(targets, window_floor=3) == [141, 142, 400], "窗口内 2 个 < 门限 3，放弃窗口"


def test_a_short_window_gives_up_the_window_instead_of_reaching_for_the_next_newest() -> None:
    """⚠️ **不足时是「放弃窗口」，不是「按时间往下补」。**

    往下补捞到的正是**刚出窗口**那一批，而军力榜从强到弱扫，那一批恰恰是最弱的
    ——补下去等于把 PR #176 的缺陷换个地方原样复发。

    这里刻意让「更新的」和「更强的」分开站：

    | 目标 | 读数 | 军力 |
    |---|---|---|
    | `2:100` | 刚读到（窗口内） | 100 |
    | `2:200` / `2:300` | 3 小时前（刚出窗口） | 200 / 300 |
    | `2:900` / `2:800` | 3 天前 | 90000 / 80000 |

    门限 3 而窗口内只有 1 个 → 放弃窗口 → **全部有读数的目标**都进池。
    改成「按时间往下补」的话，进池的只有 `[100, 300, 200]`，两个三天前的强目标
    会被挡在外面——这条用例因此会红。
    """
    targets = [
        _target(100, 100.0),
        _target(200, 200.0, scanned_at=NOW - timedelta(hours=3)),
        _target(300, 300.0, scanned_at=NOW - timedelta(hours=3)),
        _target(900, 90_000.0, scanned_at=NOW - timedelta(days=3)),
        _target(800, 80_000.0, scanned_at=NOW - timedelta(days=3)),
    ]

    choice = choose_by_military(targets, now=NOW, max_age=TWO_HOURS, window_floor=3)

    assert {item.coordinate.system for item in choice.eligible} == {100, 200, 300, 900, 800}
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
    """窗口只筛，不排序。排序是第 4 步的事，而那一步要知道从哪颗星球出发。

    在这里顺手排一次的话，两处排序会打架，而打架的症状是「同一批目标每次挑出来
    的不是同一批」——事后拿日志对账就对不上了。
    """
    given = [_target(300, 1.0), _target(100, 2.0), _target(200, 3.0)]

    assert [
        item.coordinate.system for item in within_score_window(given, now=NOW, max_age=TWO_HOURS)
    ] == [300, 100, 200]


def test_a_window_that_keeps_everything_never_reports_a_widening() -> None:
    """⚠️ **窗口内不足门限，但库里本来就只有这些读数——这不叫「用了旧数据」。**

    放弃窗口一个目标都没多捞到，所以不该告警。判据因此是「池子里有没有窗口外的」，
    而不是「有没有走放宽那条分支」。

    少了这条，一个「`len(in_window) < window_floor` 就报警」的实现会全绿，而它会在
    库里目标本来就少的时候每轮都响——**每轮都响的告警和不响的一样没用**。
    """
    only_two = [_target(140, 9_000.0), _target(141, 8_000.0)]

    choice = choose_by_military(only_two, now=NOW, max_age=TWO_HOURS, window_floor=100)

    assert [item.coordinate.system for item in choice.eligible] == [140, 141]
    assert not choice.widened


def test_a_pool_where_everything_expired_still_attacks() -> None:
    """⚠️ **2026-08-17 那晚的复现：全部超期也不许让这一轮空手。**

    那一版把超期的整批滤掉，于是「一个新鲜分数都没有」时候选池退化成
    「军力完全不参与」，实机连续停摆 2.5 小时。窗口重新会筛，但**筛空了就放弃
    窗口**，所以那种停摆回不来。
    """
    three_days = NOW - timedelta(days=3)
    all_stale = [
        _target(140, 9_000.0, scanned_at=three_days),
        _target(141, 8_000.0, scanned_at=three_days),
        _target(142, 100.0, scanned_at=three_days),
    ]

    choice = choose_by_military(all_stale, now=NOW, max_age=TWO_HOURS, window_floor=2)

    assert [item.coordinate.system for item in choice.eligible] == [140, 141, 142]
    assert choice.widened, "一个新鲜读数都没有还照打，这件事必须说出来"
    assert choice.in_window == ()


# -- 第 4 步：得分 = 军力 ÷ 往返小时（用例 a / b / c 都在这一节）----------------


def test_the_score_beats_both_pure_distance_and_pure_military() -> None:
    """⚠️ **用例 (a)：近而弱 vs 远而强，两种旧口径给出的答案都不对。**

    这四个目标是特意排的，让三种口径的结果两两不同（从 `2:137` 出发）：

    | 目标 | 军力 | 往返小时 | 得分 |
    |---|---|---|---|
    | `2:140` | 8,000 | 0.523 | 15,284 |
    | `2:141` | 9,000 | 0.530 | 16,982 |
    | `2:400` | 30,000 | 1.371 | **21,884** |
    | `2:401` | 10,000 | 1.368 | 7,308 |

        按得分（本判据）  [400, 141, 140, 401]
        纯军力            [400, 401, 141, 140]      ← 401 冲到第 2 名
        纯就近            [140, 141, 400, 401]      ← 400 掉到第 3 名

    **`2:400` 远，但强到值得飞过去；`2:401` 一样远，却没强到那个份上。**
    这正是旧的「军力截断 + 按距离出击」两步说不清的那件事：截断线画在哪都是拍的，
    而线内又完全不看军力。

    ⚠️ 得分的分子用军力，依据是**用户口径**（2026-08-18：「已知军力和材料产出正
    相关，但是没有具体数据来拟合相关曲线」），不是实测的材料产出。资源识别修好、
    材料样本攒够之后应当重新检验（`docs/选靶数据跟踪-待办.md`）。

    把第 4 步改回纯军力或改回纯就近，这条用例都会红。
    """
    targets = [
        _target(140, 8_000.0),
        _target(141, 9_000.0),
        _target(400, 30_000.0),
        _target(401, 10_000.0),
    ]

    ordered = most_valuable_first(targets, HOME, now=NOW, window_floor=4)

    assert [item.system for item in ordered] == [400, 141, 140, 401]


def test_the_round_trip_in_the_score_is_measured_round_the_ring() -> None:
    """⚠️ **用例 (b)：得分的分母走环形距离，不是 `abs(a - b)`。**

    从 `2:137` 看 `2:499` 只有 137 步（绕过 499↔1），而线性减法会算成 362 步。
    军力一样时，往返更短的那个得分更高——所以 `2:499` 必须排在 `2:287` 前面。

    实测这两个点的单程时间是 1969 秒对 2042 秒（`domain.distance` 模块头）：
    线性模型会把它们排反，而且不报错。
    """
    same_strength = [_target(287, 9_000.0), _target(499, 9_000.0)]

    ordered = most_valuable_first(same_strength, HOME, now=NOW, window_floor=2)

    assert [item.system for item in ordered] == [499, 287]


def test_a_cross_galaxy_score_uses_the_galaxy_ring_too() -> None:
    """⚠️ **用例 (c)：跨银河那一段同样是环形的。**

    从 2 系出发，9 系是**第二近**的银河（`2→1→9`，两步），6 系要走四步。
    实测单程 5305 秒对 7502 秒。军力一样时 9 系的得分更高。

    写成 `abs(9 - 2)` 的话 9 系变成七步、被排到最后，一夜都轮不到——而且不报错。
    """
    same_strength = [
        _target(250, 9_000.0, galaxy=6),
        _target(250, 9_000.0, galaxy=9),
    ]

    ordered = most_valuable_first(same_strength, HOME, now=NOW, window_floor=2)

    assert [item.galaxy for item in ordered] == [9, 6]


def test_the_score_is_military_over_round_trip_hours() -> None:
    """得分的定义本身：分子是军力（指数 1），分母是往返小时。

    这条用例的作用是让「k = 1」这件事在测试里也说得出口。改指数、或者把分母换成
    单程/距离单位，它都会红。
    """
    target = _target(140, 8_000.0)

    value = attack_value(target, HOME)

    assert value is not None
    assert abs(value - 8_000.0 / 0.5234) < 1.0


def test_a_target_without_a_score_has_no_value_and_sorts_last() -> None:
    """⚠️ **0 分是读到的事实，None 是不知道。**

    榜单上真的有 0 分的行。把 None 当成 0 就是把「没数据」伪装成「数据是 0」——
    而这个仓有一条硬规矩：猜出来的数不许长得像量出来的。

    没有分数的目标在第 2 步就出局了，所以这条守的是 `value_key` 这个通用排序本身：
    「不知道不等于 0」在哪里都成立。
    """
    unknown = _target(140, None)
    really_zero = _target(141, 0.0)

    assert attack_value(unknown, HOME) is None
    assert sorted([unknown, really_zero], key=lambda item: value_key(item, HOME)) == [
        really_zero,
        unknown,
    ]


def test_the_order_is_the_same_every_time() -> None:
    """⚠️ 得分并列时按坐标定序。

    不定的话，同一批目标每次排出来的先后可能不一样——而那会让「上一轮打到哪了」
    无从谈起，事后拿日志对账也对不上。这里三个目标同军力、同恒星系环距
    （`137 ± 3`、`137 + 3` 的位次不同），得分完全相等。
    """
    tied = [
        ScoredTarget(Coordinate(2, 140, 9), 9_000.0, NOW),
        ScoredTarget(Coordinate(2, 140, 1), 9_000.0, NOW),
        ScoredTarget(Coordinate(2, 140, 5), 9_000.0, NOW),
    ]

    ordered = most_valuable_first(tied, HOME, now=NOW, window_floor=3)

    assert [item.position for item in ordered] == [1, 5, 9]
    assert ordered == most_valuable_first(list(reversed(tied)), HOME, now=NOW, window_floor=3)


# -- 安全线（用例 d） ----------------------------------------------------------


def test_the_cap_keeps_the_unbeatable_ones_out_of_the_pool() -> None:
    """⚠️ **用例 (d)：用户口径（2026-08-14）「军力确实要设置上限」。**

    太强的目标不是当前预设打得动的，派过去只是白烧一次配额和一趟往返。
    ⚠️ 这里刻意让超标的那个**同时是得分最高的**：安全线失效的话它会排在第一个，
    而不是消失——所以这条用例既钉「有没有被挡住」，也钉「不是只被排到后面」。
    """
    ordered = most_valuable_first(
        [_target(140, 1_773_000.0), _target(200, 9_000.0)],
        HOME,
        now=NOW,
        window_floor=2,
        max_score=100_000.0,
    )

    assert [item.system for item in ordered] == [200]


def test_no_cap_keeps_even_the_strongest() -> None:
    """默认不设上限。"""
    ordered = most_valuable_first([_target(140, 1_773_000.0)], HOME, now=NOW, window_floor=1)

    assert [item.system for item in ordered] == [140]


def test_the_cap_blocks_the_too_strong_not_the_unreadable() -> None:
    """⚠️ **上限只挡「太强」，不挡「读不出来」。**

    「不知道多强」从来不构成「一定太强」。读不出来的那一档在第 2 步就出局了，
    这里不必也不该再判一次——在安全线上顺手把 None 也扔掉，会让两个判据搅在一起，
    以后调上限就会静悄悄地改变「谁算没读数」。

    ⚠️ **它同时钉住「安全线不重排」**：出来的次序必须是传进去的次序。
    """
    kept = within_max_score(
        [_target(140, 30_000.0), _target(141, None), _target(142, 1_000.0)],
        max_score=20_000.0,
    )

    assert [item.coordinate.system for item in kept] == [141, 142]


# -- 三个默认值 ----------------------------------------------------------------


def test_the_knobs_have_the_defaults_the_user_asked_for() -> None:
    """用户口径（2026-08-18）：窗口门限 100、有效期窗口 2 小时。

    ⚠️ **`WINDOW_POOL_FLOOR` 就是从前那个 `TOP_BY_MILITARY`，身份换了、数没变。**
    它现在只是第 3 步的尺子，不再决定打谁（`domain.target_order` 模块头第 4 步）。

    ⚠️ **`MILITARY_EXPONENT` 写死 1，而且刻意不做旋钮。** 拟合它要的数据
    （派出那一刻的军力 × 读全的材料）目前一样都不够：`attack_intents` 从 PR #183
    起才开始快照派出时刻的军力，战报资源识别 34 份只读全了 5 份。没有数据时把它
    做成旋钮不是「留了余地」，是把一个说不清的数推给用户去猜。
    资源识别修好之后应当重新检验（`docs/选靶数据跟踪-待办.md`）。
    """
    assert WINDOW_POOL_FLOOR == 100
    assert DEFAULT_SCORE_MAX_AGE == timedelta(hours=2)
    assert MILITARY_EXPONENT == 1.0


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
    attacked_at = datetime(2026, 8, 17, 5, 28, tzinfo=UTC)
    stale = _target(293, 9_000.0, galaxy=4, scanned_at=datetime(2026, 8, 17, 1, 50, tzinfo=UTC))

    assert score_is_fresh(stale, now=attacked_at, max_age=timedelta(hours=1)) is False


def test_a_target_without_a_score_is_never_called_fresh() -> None:
    """没有分数就没有可排的东西，所以恒为假；它在第 2 步就已经出局了。"""
    assert score_is_fresh(_target(140, None), now=NOW, max_age=TWO_HOURS) is False
