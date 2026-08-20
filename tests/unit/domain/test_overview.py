"""数据概览页的口径。

这一份钉的全是「看起来合理、实际相反」的那几条（`docs/数据概览页-需求.md`
第八节 + 第九节的拍板结论）。每一条底下都写着它守的是哪个曾经出过的错——
少了那句话，后来的人会以为这些断言只是在复述实现。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from evo_helper.domain.overview import (
    BASIC_SLOTS,
    COUNT_STATS_START_UTC,
    RARE_SLOTS,
    RESOURCE_STATS_START_UTC,
    SLOT_FLYING,
    SLOT_FREE,
    SLOT_UNKNOWN,
    Granularity,
    LineSource,
    Occupancy,
    available_seconds,
    day_start,
    line_slots,
    max_concurrent_lines,
    month_start,
    occupancy_end,
    occupied_seconds,
    overflow_lines,
    overlap_seconds,
    parse_granularity,
    period_end,
    period_label,
    period_lines,
    period_start,
    period_starts,
    recovery_rate,
    resource_window,
    trim_empty_tail,
    utilisation,
    week_start,
)

#: 用户的墙上时钟。全站显示用它，**统计不用**。
SHANGHAI = timezone(timedelta(hours=8))


# -- UTC+0 切天 ---------------------------------------------------------------


def test_the_day_is_cut_at_utc_midnight_not_at_the_users_wall_clock() -> None:
    """⚠️ **统计按 UTC+0 切天**（用户口径 2026-08-19）。

    这一条钉的是那次「以为出了 10 倍资源计算错误」的排查：同一批战报按 UTC+8
    切是 08-19、按 UTC+0 切是 08-18，合计一样、只有日切位置不同。

    取的时刻是 UTC+8 的 08-19 早上 7 点，也就是 UTC 的 08-18 23:00——按用户的
    墙上时钟它属于 19 号，按统计口径它属于 **18 号**。
    """
    wall_clock = datetime(2026, 8, 19, 7, 0, tzinfo=SHANGHAI)

    assert day_start(wall_clock) == datetime(2026, 8, 18, tzinfo=UTC)


def test_a_utc_moment_just_before_midnight_still_belongs_to_that_day() -> None:
    assert day_start(datetime(2026, 8, 18, 23, 59, 59, tzinfo=UTC)) == datetime(
        2026, 8, 18, tzinfo=UTC
    )
    assert day_start(datetime(2026, 8, 19, 0, 0, tzinfo=UTC)) == datetime(2026, 8, 19, tzinfo=UTC)


def test_a_naive_moment_is_refused_rather_than_guessed() -> None:
    """naive 的时刻切不出 UTC 日——猜一个时区正是这条缺陷的入口。"""
    with pytest.raises(ValueError):
        day_start(datetime(2026, 8, 19, 12, 0))


# -- 周界压在周一 --------------------------------------------------------------


def test_the_week_starts_on_monday_so_the_bot_refresh_day_is_not_split() -> None:
    """周一是 bot 刷新日，全服都在打、保护期跳过大量增加。

    周界压在周日的话，那一天的异常会被劈到两周里，趋势就看不出来了。
    2026-08-17 是周一，08-23 是周日——两者必须落在同一周。
    """
    monday = datetime(2026, 8, 17, tzinfo=UTC)
    sunday = datetime(2026, 8, 23, 23, 0, tzinfo=UTC)

    assert monday.weekday() == 0
    assert week_start(monday) == monday
    assert week_start(sunday) == monday
    assert week_start(monday - timedelta(seconds=1)) == datetime(2026, 8, 10, tzinfo=UTC)


def test_the_month_starts_at_the_first_utc_midnight() -> None:
    assert month_start(datetime(2026, 8, 19, 12, tzinfo=UTC)) == datetime(2026, 8, 1, tzinfo=UTC)


# -- 两个统计起点 --------------------------------------------------------------


def test_the_two_statistics_starts_are_different_days_and_must_stay_apart() -> None:
    """⚠️ 计数类 2026-08-17、资源类 2026-08-18，**不许合并成一个常量**。

    资源识别是 08-18 才修好的（PR #191/#193），更早的战报根本没有资源明细。
    合并之后要么「合计」凭空多出一段没有明细的日子，要么 08-17 那天真打出去的
    42 发凭空消失。
    """
    assert COUNT_STATS_START_UTC == datetime(2026, 8, 17, tzinfo=UTC)
    assert RESOURCE_STATS_START_UTC == datetime(2026, 8, 18, tzinfo=UTC)
    assert COUNT_STATS_START_UTC < RESOURCE_STATS_START_UTC


def test_the_total_row_is_anchored_at_the_count_start_not_at_the_whole_database() -> None:
    """「合计」的含义是「自 2026-08-17 起的合计」，不是「库里的全部」。"""
    now = datetime(2026, 8, 19, 10, tzinfo=UTC)

    assert period_start(now, Granularity.TOTAL) == COUNT_STATS_START_UTC
    assert period_starts(now, Granularity.TOTAL) == [COUNT_STATS_START_UTC]


def test_a_window_entirely_before_the_resource_start_has_no_resource_data() -> None:
    """08-13 那天真的派了 140 发，但它一份资源明细都没有。

    返回 None（「没有资源数据」）而不是一个 0 区间：两者在页面上都显示 0，
    但语义不同——`resource_window` 是资源那一侧唯一收窄的地方，计数类不走它。
    """
    assert (
        resource_window(datetime(2026, 8, 13, tzinfo=UTC), datetime(2026, 8, 14, tzinfo=UTC))
        is None
    )


def test_a_window_straddling_the_resource_start_is_clipped_not_dropped() -> None:
    window = resource_window(datetime(2026, 8, 17, tzinfo=UTC), datetime(2026, 8, 19, tzinfo=UTC))

    assert window == (RESOURCE_STATS_START_UTC, datetime(2026, 8, 19, tzinfo=UTC))


# -- 比率：先求和再相除 ---------------------------------------------------------


def test_the_recovery_rate_sums_numerator_and_denominator_instead_of_averaging_days() -> None:
    """⚠️ **周/月的比率必须先把分子分母各自求和再相除。**

    把每天的百分比平均一遍在天数不齐、量级不齐时是**错的**：
    08-19 派 8 发全回收（100%）、08-16 派 39 发一份没回收（0%），
    平均下来 50%，而真实的周回收率是 8 ÷ 47 ≈ 17%。
    """
    summed = recovery_rate(8 + 0, 8 + 39)
    averaged = (1.0 + 0.0) / 2

    assert summed == pytest.approx(8 / 47)
    assert summed != pytest.approx(averaged)


def test_a_period_with_no_dispatches_has_no_recovery_rate_rather_than_zero_percent() -> None:
    """一发没派的那一天，「回收率 0%」是句假话。页面上显示成「—」。"""
    assert recovery_rate(0, 0) is None
    assert recovery_rate(1, 0) is None


# -- 航线格子按配置画 -----------------------------------------------------------


def test_the_line_grid_has_exactly_as_many_cells_as_the_planet_configures() -> None:
    """⚠️ **格子按配置的航线数画，不按占用数画**（需求文档 8.3）。

    原型第一版按「在飞 + 时长未知」画，于是一颗配了 4 条的星球画出了 7 格——
    那张图在说这颗星球有 7 条航线。
    """
    cells = line_slots(configured_lines=4, holding=7, unknown_duration=3)

    assert len(cells) == 4


def test_the_line_grid_shows_flying_then_unknown_then_free() -> None:
    """实测 `9:250:8`：配 4 条、占 4 条、其中 3 条是「时长未知」。"""
    assert line_slots(configured_lines=4, holding=4, unknown_duration=3) == (
        SLOT_FLYING,
        SLOT_UNKNOWN,
        SLOT_UNKNOWN,
        SLOT_UNKNOWN,
    )
    # 实测 `4:277:15`：配 5 条、占 2 条、没有时长未知的。
    assert line_slots(configured_lines=5, holding=2, unknown_duration=0) == (
        SLOT_FLYING,
        SLOT_FLYING,
        SLOT_FREE,
        SLOT_FREE,
        SLOT_FREE,
    )


def test_each_planet_gets_its_own_configured_line_count() -> None:
    """每颗星球的航线数各不相同，不许写死（实测 5 条 / 4 条）。"""
    assert len(line_slots(configured_lines=5, holding=0, unknown_duration=0)) == 5
    assert len(line_slots(configured_lines=4, holding=0, unknown_duration=0)) == 4


def test_holding_more_lines_than_configured_is_reported_instead_of_drawn() -> None:
    """在飞数可能超过配置（航线跨任务共享，海盗与 bot 抢同一批）。

    多出来的那几条要**说出来**而不是靠加格子表达——不说的话会被当成算错。
    """
    assert overflow_lines(configured_lines=4, holding=7) == 3
    assert overflow_lines(configured_lines=5, holding=2) == 0


# -- 航线占用的三档 -------------------------------------------------------------


def test_a_manual_release_beats_both_clocks() -> None:
    """⚠️ 人工放手那一档必须**罩住**另外两档，不是排在最后。

    只在有航线钟那一档上判的话，读不出飞行时间的那些（实机上最容易卡住的一批）
    按下「清理航线占用」纹丝不动。判据次序与 `_still_holding_a_line` 逐条对应。
    """
    dispatched = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    released = datetime(2026, 8, 19, 9, 30, tzinfo=UTC)

    # 航线钟说 11:00，人工放手说 9:30 —— 以人工为准。
    assert (
        occupancy_end(
            dispatched_at_utc=dispatched,
            line_free_at_utc=datetime(2026, 8, 19, 11, 0, tzinfo=UTC),
            line_released_at_utc=released,
            hold=timedelta(minutes=90),
        )
        == released
    )
    # 航线钟为 NULL 时同样以人工为准，而不是退回 `dispatched + hold`。
    assert (
        occupancy_end(
            dispatched_at_utc=dispatched,
            line_free_at_utc=None,
            line_released_at_utc=released,
            hold=timedelta(minutes=90),
        )
        == released
    )


def test_a_null_line_clock_still_occupies_until_dispatch_plus_hold() -> None:
    """NULL 的意思是「不知道它什么时候回来」，不是「它没占位」。"""
    dispatched = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)

    assert occupancy_end(
        dispatched_at_utc=dispatched,
        line_free_at_utc=None,
        line_released_at_utc=None,
        hold=timedelta(minutes=45),
    ) == dispatched + timedelta(minutes=45)


def test_the_hold_is_a_parameter_so_the_configured_value_wins_over_the_default_90() -> None:
    """`hold` 是用户在攻击配置页上能改的值。

    写死 90 分钟的话，他改成 45 之后页面会继续把一批早该放手的派遣算成「占着」。
    """
    dispatched = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
    ends = {
        minutes: occupancy_end(
            dispatched_at_utc=dispatched,
            line_free_at_utc=None,
            line_released_at_utc=None,
            hold=timedelta(minutes=minutes),
        )
        for minutes in (45, 90, 120)
    }

    assert len(set(ends.values())) == 3


# -- 占用时长按天摊 -------------------------------------------------------------


def test_an_occupancy_crossing_midnight_is_split_between_the_two_days() -> None:
    """一发 22:30 派出、次日 00:40 回港的舰队在两天各占一段。

    整段算给派出那一天的话，跨零点的那些会让当天利用率虚高、次日虚低。
    """
    began = datetime(2026, 8, 18, 22, 30, tzinfo=UTC)
    finished = datetime(2026, 8, 19, 0, 40, tzinfo=UTC)
    first_day = (datetime(2026, 8, 18, tzinfo=UTC), datetime(2026, 8, 19, tzinfo=UTC))
    second_day = (datetime(2026, 8, 19, tzinfo=UTC), datetime(2026, 8, 20, tzinfo=UTC))

    assert overlap_seconds(began, finished, *first_day) == 90 * 60
    assert overlap_seconds(began, finished, *second_day) == 40 * 60


def test_parallel_lines_are_added_up_not_merged() -> None:
    """同一时刻可以有好几条航线各占各的。

    合并区间等于把并行的航线算成一条——那正好会让「利用率」永远上不了 100%。
    """
    window = (datetime(2026, 8, 19, tzinfo=UTC), datetime(2026, 8, 19, 2, tzinfo=UTC))
    both = (
        Occupancy(
            start=datetime(2026, 8, 19, 0, tzinfo=UTC), end=datetime(2026, 8, 19, 1, tzinfo=UTC)
        ),
        Occupancy(
            start=datetime(2026, 8, 19, 0, tzinfo=UTC), end=datetime(2026, 8, 19, 1, tzinfo=UTC)
        ),
    )

    assert occupied_seconds(both, *window) == 2 * 3600


def test_available_hours_are_the_whole_period_times_the_line_count() -> None:
    """⚠️ **分母是「周期总时长 × 航线数」，不是「运行时长 × 航线数」。**

    用户口径（2026-08-20）反转了他自己 08-17 定的那条。旧分母只把调度器真的在跑
    的那几个小时算成产能，于是「关了一晚上」和「开着一整天但一发没派」在页面上
    长得一模一样（都接近 100%）。新分母把整个周期都算成产能——那段没开工的时间
    因此**显示成损失**，而「到底开没开工」由「挂机运行时长」单独回答。

    这一条按 24 小时 × 5 条断言：只要有人把分母改回「运行时长 × 线数」，
    这里就会红。
    """
    start = datetime(2026, 8, 19, tzinfo=UTC)
    end = datetime(2026, 8, 20, tzinfo=UTC)

    assert available_seconds(window_start=start, window_end=end, lines=5) == 24 * 3600 * 5


def test_a_partial_period_only_counts_the_hours_that_have_passed() -> None:
    """当前那个周期的右边界是「现在」，不是今天 24:00。

    占用段也只算到现在（`storage.overview.occupancies`）。两边不同源的话，
    早上八点打开页面会看到一个「除以 24 小时」的假低值。
    """
    start = datetime(2026, 8, 19, tzinfo=UTC)
    now = datetime(2026, 8, 19, 8, tzinfo=UTC)

    assert available_seconds(window_start=start, window_end=now, lines=4) == 8 * 3600 * 4


def test_a_period_with_no_lines_has_no_denominator_instead_of_a_zero_ratio() -> None:
    """线数为 0（当天既没有记下真值、也一发没派）时分母是 0，利用率给「—」。

    编一个正数分母出来等于凭空发明产能。
    """
    start = datetime(2026, 8, 19, tzinfo=UTC)
    end = datetime(2026, 8, 20, tzinfo=UTC)

    assert available_seconds(window_start=start, window_end=end, lines=0) == 0.0
    assert utilisation(0.0, 0.0) is None


def test_utilisation_over_one_hundred_percent_is_reported_not_truncated() -> None:
    """⚠️ **超过 100% 不许截断。**

    换成新分母之后这一条仍然会发生，只是来历变了：配置中途被改小，或者在飞数
    真的超过配置数（航线跨任务共享，海盗与 bot 抢同一批）。截成 100% 就把真信号
    抹掉了。
    """
    assert utilisation(2430.0, 1000.0) == pytest.approx(2.43)
    assert utilisation(1.0, 0.0) is None


# -- 那一天配着几条航线 ---------------------------------------------------------


def _window(*, hours: int = 24) -> tuple[datetime, datetime]:
    start = datetime(2026, 8, 15, tzinfo=UTC)
    return start, start + timedelta(hours=hours)


def _occupancy(*, start_hour: int, hours: int) -> Occupancy:
    day = datetime(2026, 8, 15, tzinfo=UTC)
    return Occupancy(
        start=day + timedelta(hours=start_hour), end=day + timedelta(hours=start_hour + hours)
    )


def test_a_recorded_line_count_is_used_as_the_truth() -> None:
    """`mission_runs.configured_lines` 有值时就用它，那是真值。"""
    start, end = _window()

    count = period_lines(
        recorded=9,
        occupancies=(_occupancy(start_hour=1, hours=2),),
        window_start=start,
        window_end=end,
    )

    assert (count.lines, count.source, count.exact) == (9, LineSource.RECORDED, True)


def test_a_missing_line_count_falls_back_to_the_peak_concurrency_not_the_current_config() -> None:
    """⚠️ **那一列为空时不许拿此刻的配置去顶。**

    用户 2026-08-20 把航线从 4 条加到 9 条。按 9 条去算 08-15（当时 4 条）会把那天
    低估到 44%——而页面上一点异样都看不出来。为空时只能用**当天观测到的最大并发
    在飞数**这个下界（用户选定的方案 C）。

    这一条同时钉住「为空不许当成 0」：当成 0 分母就是 0，那天整段变成「—」，
    真打出去的活被抹掉。
    """
    start, end = _window()
    # 同一时刻最多 4 条在飞——那一天真的配着 4 条。
    occupancies = tuple(_occupancy(start_hour=1, hours=2) for _ in range(4))

    count = period_lines(recorded=None, occupancies=occupancies, window_start=start, window_end=end)

    assert (count.lines, count.source, count.exact) == (4, LineSource.LOWER_BOUND, False)


def test_the_peak_concurrency_does_not_merge_the_overlapping_segments() -> None:
    """⚠️ **合并重叠区间等于把并行的航线算成一条。**

    合并之后最大并发恒等于 1，分母缩到 1/N，利用率被高估成好几倍。
    """
    start, end = _window()
    parallel = tuple(_occupancy(start_hour=3, hours=1) for _ in range(5))

    assert max_concurrent_lines(parallel, start, end) == 5


def test_the_peak_concurrency_counts_touching_segments_as_one_line() -> None:
    """首尾相接的两段（一条航线刚放开、下一发立刻派出）是先后两条，不是同时两条。"""
    start, end = _window()
    back_to_back = (_occupancy(start_hour=1, hours=1), _occupancy(start_hour=2, hours=1))

    assert max_concurrent_lines(back_to_back, start, end) == 1


def test_the_peak_concurrency_ignores_segments_outside_the_window() -> None:
    """窗口之外的占用段不算——它说的是别的那一天配着几条。"""
    start, end = _window(hours=4)
    outside = (_occupancy(start_hour=10, hours=2),)

    assert max_concurrent_lines(outside, start, end) == 0


def test_the_lower_bound_line_count_keeps_utilisation_at_or_below_one_hundred_percent() -> None:
    """⚠️ **这是方案 C 的自检性质：用最大并发当线数时，利用率在构造上 ≤ 100%。**

    任一时刻最多 M 条在忙 ⇒ 占用 ≤ M × 窗口长度。历史天算出 >100% 就是实现有 bug
    （最典型的写法错误正是把重叠区间合并了）。
    """
    start, end = _window()
    # 三条航线各占一段，长短与起点都不一样，还有一段跨出窗口右边界。
    occupancies = (
        _occupancy(start_hour=0, hours=6),
        _occupancy(start_hour=2, hours=20),
        _occupancy(start_hour=5, hours=3),
        _occupancy(start_hour=20, hours=2),
    )
    count = period_lines(recorded=None, occupancies=occupancies, window_start=start, window_end=end)

    occupied = occupied_seconds(occupancies, start, end)
    ratio = utilisation(
        occupied, available_seconds(window_start=start, window_end=end, lines=count.lines)
    )

    assert ratio is not None
    assert ratio <= 1.0


# -- 表格行 ---------------------------------------------------------------------


def test_the_day_view_gives_at_most_seven_rows() -> None:
    """用户口径（2026-08-19）：「按天最多 7 行」。"""
    now = datetime(2026, 8, 19, 10, tzinfo=UTC)

    starts = period_starts(now, Granularity.DAY)

    assert len(starts) == 7
    assert starts[0] == datetime(2026, 8, 19, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 8, 13, tzinfo=UTC)


def test_the_day_view_is_not_clipped_at_the_statistics_start() -> None:
    """起点只管「合计」那一行。

    每一档都截到 08-17 的话，页面就再也看不见 08-15~08-16 那段战报一份没读回来
    的故障——而让那种故障第一天就显眼，正是这一页存在的理由。
    """
    starts = period_starts(datetime(2026, 8, 19, 10, tzinfo=UTC), Granularity.DAY)

    assert min(starts) < COUNT_STATS_START_UTC


def test_period_ends_are_half_open_and_the_total_ends_at_now() -> None:
    now = datetime(2026, 8, 19, 10, tzinfo=UTC)

    assert period_end(datetime(2026, 8, 19, tzinfo=UTC), Granularity.DAY, now=now) == datetime(
        2026, 8, 20, tzinfo=UTC
    )
    assert period_end(datetime(2026, 8, 17, tzinfo=UTC), Granularity.WEEK, now=now) == datetime(
        2026, 8, 24, tzinfo=UTC
    )
    assert period_end(datetime(2026, 12, 1, tzinfo=UTC), Granularity.MONTH, now=now) == datetime(
        2027, 1, 1, tzinfo=UTC
    )
    assert period_end(COUNT_STATS_START_UTC, Granularity.TOTAL, now=now) == now


def test_only_the_trailing_empty_rows_are_dropped() -> None:
    """中间那些空行是有信息量的（08-14 一发没派）。

    砍掉之后趋势看起来是连续的，而它其实断过。
    """
    rows = ["a", "", "b", "", ""]

    assert trim_empty_tail(rows, lambda row: row == "") == ["a", "", "b"]
    assert trim_empty_tail(["", ""], lambda row: row == "") == [""]


def test_period_labels_mark_the_current_bucket() -> None:
    now = datetime(2026, 8, 19, 10, tzinfo=UTC)

    assert period_label(datetime(2026, 8, 19, tzinfo=UTC), Granularity.DAY, now=now) == "08-19 今天"
    assert period_label(datetime(2026, 8, 18, tzinfo=UTC), Granularity.DAY, now=now) == "08-18"
    assert (
        period_label(datetime(2026, 8, 17, tzinfo=UTC), Granularity.WEEK, now=now)
        == "本周 08-17~08-23"
    )
    assert period_label(COUNT_STATS_START_UTC, Granularity.TOTAL, now=now) == "合计 自 08-17"


def test_an_unknown_granularity_falls_back_to_day_instead_of_failing() -> None:
    """档位切换是四个链接；手改地址写错一个字母换来一页 JSON 报错，
    读起来就是「控制台坏了」。
    """
    assert parse_granularity(None) is Granularity.DAY
    assert parse_granularity("weeek") is Granularity.DAY
    assert parse_granularity("WEEK") is Granularity.WEEK


def test_the_rare_slots_are_the_three_the_user_watches() -> None:
    """5 = 合金碎片、8 = 泰坦立方、9 = 收割者碎片（`SLOT_LABELS`）。"""
    from evo_helper.domain.battle_resources import slot_label

    assert RARE_SLOTS == (5, 8, 9)
    assert [slot_label(slot) for slot in RARE_SLOTS] == ["合金碎片", "泰坦立方", "收割者碎片"]


def test_the_basic_slots_are_the_three_regular_resources() -> None:
    """⚠️ 「今天收益」第四张卡上那三样，**槽位号只有这一份**。

    0 = 金属、1 = 晶体、2 = 气体（`SLOT_LABELS`）。这条钉的是「有没有抄错、
    有没有少一样」：`SLOT_LABELS` 的顺序与游戏「太空舱」页并不一致，另抄一份
    `(0, 1, 2)` 出去，日后对不上的症状是「数字全对、只是安在了别的资源名下」，
    页面上一点异样都没有。

    ⚠️ 三样**都要**：用户口径是「金属/晶体/气体」，少一样就不是他要的那张卡。
    """
    from evo_helper.domain.battle_resources import slot_label

    assert BASIC_SLOTS == (0, 1, 2)
    assert [slot_label(slot) for slot in BASIC_SLOTS] == ["金属", "晶体", "气体"]
    # 和稀有那三样不许重叠：一格同时出现在两张卡上就等于被数了两遍。
    assert not set(BASIC_SLOTS) & set(RARE_SLOTS)
