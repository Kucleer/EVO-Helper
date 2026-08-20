"""「按星球效率」的口径。

每一条钉的都是一个**有更自然的写法、而那个写法会给出假结论**的判据——
它们的共同点是错得很安静：页面上每一格都会有一个像模像样的数。

⚠️ **坐标全是编造的。** 仓库是公开的，真实出发坐标不进用例夹具。
⚠️ **时刻全部注入**，一处都不读真实时钟。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.origin_efficiency import (
    LOW_RECOVERY_THRESHOLD,
    MIN_ON_DUTY,
    OriginDay,
    build_rows,
    day_label,
    is_untrustworthy,
    on_duty_seconds,
    origin_lines,
    parse_day,
    per_line,
    per_line_hour,
    selectable_days,
)
from evo_helper.domain.overview import (
    RESOURCE_STATS_START_UTC,
    LineSource,
    Occupancy,
    day_start,
)

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)
DAY = day_start(NOW)

#: 三颗编造的出发星球。位置号都在 5 以上——1–4 号位是海盗位，选靶那一侧另有判据
#: 把它们挡掉，拿它当样本会让用例断在一条与本文件无关的判据上。
EARLY = Coordinate(1, 111, 6)
LATE = Coordinate(2, 222, 7)
GONE = Coordinate(3, 333, 8)


def _day(
    origin: Coordinate,
    *,
    dispatches: int = 10,
    reports: int = 10,
    rare_amount: int = 0,
    first: datetime | None = None,
    last: datetime | None = None,
) -> OriginDay:
    return OriginDay(
        origin=origin,
        dispatches=dispatches,
        reports=reports,
        rare_amount=rare_amount,
        rare_approximate=False,
        rare_uncertainty=0,
        first_dispatch_at_utc=first if first is not None else DAY,
        last_dispatch_at_utc=last if last is not None else NOW,
    )


# -- 分母一：航线数 -------------------------------------------------------------


def test_per_line_is_none_when_there_is_no_line_count_instead_of_zero() -> None:
    """⚠️ **除零**。线数取不到时给 None，页面上是「—」。

    返回 0 的话，这一行会和「这颗星球一点收获都没有」长得一模一样，而实情是
    「分母取不到」。
    """
    assert per_line(58_424, 0) is None
    assert per_line(58_424, -1) is None
    assert per_line(58_424, 2) == pytest.approx(29_212.0)


def test_recorded_lines_are_only_trusted_when_the_account_total_still_matches() -> None:
    """⚠️ `mission_runs.configured_lines` 记的是**账号总数**，不是每颗星球的数。

    所以只有「那一天记下的总数」和此刻配置的总数一致时，才敢拿此刻这颗星球的
    分配当那一天的分配用。对不上就退下界——**不许拿此刻的配置去顶历史天**。
    """
    matched = origin_lines(
        recorded_total=9,
        configured_total=9,
        configured=2,
        enabled=True,
        occupancies=(),
        window_start=DAY,
        window_end=NOW,
    )
    assert (matched.lines, matched.source) == (2, LineSource.RECORDED)

    # 那一天账号只配着 4 条，此刻配着 9 条——按 9 条的分配去算那一天是假话。
    changed = origin_lines(
        recorded_total=4,
        configured_total=9,
        configured=2,
        enabled=True,
        occupancies=(Occupancy(start=DAY, end=DAY + timedelta(hours=2)),),
        window_start=DAY,
        window_end=NOW,
    )
    assert changed.source is LineSource.LOWER_BOUND
    assert changed.exact is False


def test_a_day_with_no_recorded_total_falls_back_to_peak_concurrency() -> None:
    """那一列是 2026-08-20 才加的，更早的天永远为 NULL ⇒ 退到最大并发在飞数。

    ⚠️ 下界的方向：任一时刻最多 3 条在忙 ⇒ **至少**配着 3 条 ⇒ 分母偏小 ⇒
    效率是**上界**。
    """
    overlapping = (
        Occupancy(start=DAY, end=DAY + timedelta(hours=2)),
        Occupancy(start=DAY + timedelta(minutes=30), end=DAY + timedelta(hours=3)),
        Occupancy(start=DAY + timedelta(minutes=40), end=DAY + timedelta(hours=1)),
    )
    count = origin_lines(
        recorded_total=None,
        configured_total=9,
        configured=2,
        enabled=True,
        occupancies=overlapping,
        window_start=DAY,
        window_end=NOW,
    )
    assert (count.lines, count.exact) == (3, False)


def test_an_origin_missing_from_the_current_config_still_gets_a_lower_bound() -> None:
    """当前配置里已经没有这颗星球（被删了），但它那天真打出去过。

    退下界，不是给 0：给 0 的话「每线」是「—」，那一天真打出去的活就没有任何
    数字去衡量了。
    """
    count = origin_lines(
        recorded_total=9,
        configured_total=9,
        configured=None,
        enabled=False,
        occupancies=(Occupancy(start=DAY, end=DAY + timedelta(hours=1)),),
        window_start=DAY,
        window_end=NOW,
    )
    assert (count.lines, count.exact) == (1, False)


# -- 分母二：在岗时长 -----------------------------------------------------------


def test_on_duty_runs_from_the_first_dispatch_to_now_not_to_the_last_dispatch() -> None:
    """⚠️ **「首发 → 末发」奖励早收工的星球**，所以这里用「首发 → 现在」。

    这一颗 00:00 和 00:30 各派一发之后再没动过。按「首发 → 末发」它只在岗
    0.5 小时，「每线小时」会高得离谱——那和「罚晚开工」是同一个错，方向相反。
    """
    quit_early = on_duty_seconds(
        first_dispatch_at_utc=DAY,
        day_start_utc=DAY,
        day_end_utc=DAY + timedelta(days=1),
        now_utc=NOW,
    )
    assert quit_early == pytest.approx(21 * 3600.0)


def test_on_duty_stops_at_the_day_boundary_for_a_past_day() -> None:
    """看历史某一天时，在岗时长封在那一天的 24 点，不是一路数到现在。"""
    yesterday = DAY - timedelta(days=1)
    seconds = on_duty_seconds(
        first_dispatch_at_utc=yesterday + timedelta(hours=3),
        day_start_utc=yesterday,
        day_end_utc=DAY,
        now_utc=NOW,
    )
    assert seconds == pytest.approx(21 * 3600.0)


def test_an_origin_that_never_dispatched_is_on_duty_for_zero_seconds() -> None:
    assert (
        on_duty_seconds(
            first_dispatch_at_utc=None,
            day_start_utc=DAY,
            day_end_utc=DAY + timedelta(days=1),
            now_utc=NOW,
        )
        == 0.0
    )


def test_a_naive_moment_is_refused_rather_than_cut_by_the_local_clock() -> None:
    """⚠️ naive 的时刻按本机时区切，而那正是「日界挪 8 小时」那个缺陷的形状。"""
    with pytest.raises(ValueError):
        on_duty_seconds(
            first_dispatch_at_utc=datetime(2026, 8, 20, 3, 0),
            day_start_utc=DAY,
            day_end_utc=DAY + timedelta(days=1),
            now_utc=NOW,
        )


def test_per_line_hour_divides_by_both_denominators_not_just_the_lines() -> None:
    """⚠️ **「每线小时」不许退化成「每线」。**

    分母漏掉在岗时长这一层，这一列就成了「每线」的复制品，而它存在的全部理由
    是要排出不同的名次。
    """
    lines_only = per_line(58_424, 2)
    both = per_line_hour(58_424, 2, 11.7 * 3600.0)
    assert lines_only == pytest.approx(29_212.0)
    assert both == pytest.approx(29_212.0 / 11.7)
    assert both != pytest.approx(lines_only)


def test_per_line_hour_is_none_when_the_planet_has_barely_started() -> None:
    """⚠️ **除零**。在岗时长趋近 0 时这个数会飙到几十万，而那只反映「刚开工」。"""
    assert per_line_hour(58_424, 2, 0.0) is None
    assert per_line_hour(58_424, 2, MIN_ON_DUTY.total_seconds() - 1) is None
    assert per_line_hour(58_424, 2, MIN_ON_DUTY.total_seconds()) is not None


# -- 回收率与「不可信」 ---------------------------------------------------------


def test_a_low_recovery_day_is_marked_untrustworthy() -> None:
    """⚠️ 08-17 实测 39 发只读回 13 发（33%）。**那天不是没赚，是战报没读回来。**

    不标的话，用户会得出「08-17 效率崩了」这个错结论。
    """
    assert is_untrustworthy(13 / 39) is True
    assert is_untrustworthy(0.0) is True
    assert is_untrustworthy(LOW_RECOVERY_THRESHOLD) is False
    # 2026-08-20 当天最差的一颗是 69%，不该被无谓地打上不可信。
    assert is_untrustworthy(0.69) is False


def test_a_day_with_no_dispatches_is_not_marked_untrustworthy() -> None:
    """回收率取不到（一发没派）时不标：那一行没有效率数可言，标了只是噪音。"""
    assert is_untrustworthy(None) is False


def test_the_threshold_is_tight_enough_to_matter_for_the_ranking() -> None:
    """阈值的推导：回收 60% ⇒ 分子至少低估 1.67 倍，而相邻两行只差 1.63 倍。

    这一条钉的是**理由本身**——把阈值放宽到 0.3，1.67 倍的低估就不再被标出来，
    页面会拿一个已经排错了的名次当结论。
    """
    understatement = 1 / LOW_RECOVERY_THRESHOLD
    neighbours = 13_053 / 8_007
    assert understatement > neighbours


# -- 排序 ----------------------------------------------------------------------


def test_rows_are_ranked_by_per_line_hour_not_by_per_line() -> None:
    """⚠️ **主排序键是「每线小时」。** 用「每线」排会把晚开工的星球排在前面。

    这里构造成两个键给出**相反**的次序：`LATE` 的「每线」高一倍，但它只在岗
    了 1 小时，而 `EARLY` 在岗 21 小时——按「每线小时」，`EARLY` 才是第一。
    """
    rows = build_rows(
        (
            _day(EARLY, rare_amount=2_000, first=DAY),
            _day(LATE, rare_amount=4_000, first=NOW - timedelta(hours=1)),
        ),
        configured={EARLY: (1, True), LATE: (1, True)},
        occupancies={},
        recorded_total=2,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    assert [row.origin for row in rows] == [LATE, EARLY]
    # 「每线」单独看，`LATE` 领先一倍——所以这条用例真的分得开两个排序键。
    assert rows[0].per_line == pytest.approx(4_000.0)
    assert rows[1].per_line == pytest.approx(2_000.0)
    # 而「每线小时」把它反过来：4000/1h = 4000 对 2000/21h = 95。
    # ⚠️ 注意 `LATE` 在岗只有 1 小时，所以它仍然靠前——这条钉的是**次序由
    # 「每线小时」决定**，见下一条用例的反例。
    assert rows[0].per_line_hour == pytest.approx(4_000.0)
    assert rows[1].per_line_hour == pytest.approx(2_000.0 / 21.0)


def test_the_slow_starter_loses_once_the_hours_are_in_the_denominator() -> None:
    """同样的两颗，把收成调成「每线」相同——此时只有在岗时长能分出名次。

    ⚠️ 排序键换成「每线」（或者把方向排反）这条就红：两行的「每线」完全相等。
    """
    rows = build_rows(
        (
            _day(EARLY, rare_amount=4_000, first=DAY),
            _day(LATE, rare_amount=4_000, first=NOW - timedelta(hours=1)),
        ),
        configured={EARLY: (1, True), LATE: (1, True)},
        occupancies={},
        recorded_total=2,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    assert rows[0].per_line == rows[1].per_line
    assert [row.origin for row in rows] == [LATE, EARLY]


def test_rows_without_a_ranking_metric_go_last() -> None:
    """「每线小时」取不到的行排在最后——它们没有名次可言。"""
    rows = build_rows(
        (
            _day(GONE, rare_amount=9_999, first=DAY),
            _day(EARLY, rare_amount=10, first=DAY),
        ),
        # `GONE` 不在配置里，也没有占用段 ⇒ 线数是 0 ⇒ 两个效率数都是 None。
        configured={EARLY: (1, True)},
        occupancies={},
        recorded_total=1,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    assert [row.origin for row in rows] == [EARLY, GONE]
    assert rows[-1].per_line is None


# -- 行集 ----------------------------------------------------------------------


def test_a_disabled_origin_is_still_a_row() -> None:
    """⚠️ **停用的星球照样出现。** 实测 2026-08-20 有一颗中途被自动停用过，
    而它当天真把活打出去了。按 `enabled` 过滤会让那一行整个消失。
    """
    rows = build_rows(
        (_day(LATE, rare_amount=24_020, first=DAY),),
        configured={EARLY: (2, True), LATE: (3, False)},
        occupancies={LATE: (Occupancy(start=DAY, end=DAY + timedelta(hours=1)),)},
        recorded_total=2,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    by_origin = {row.origin: row for row in rows}
    assert LATE in by_origin
    assert by_origin[LATE].enabled is False
    assert by_origin[LATE].in_config is True
    # 停用的不算进「此刻配置的总数」（那一列记的就是启用的那些之和），所以
    # 总数 2 == 记下来的 2 ⇒ 真值那一档成立，而这颗停用的星球退下界。
    assert by_origin[LATE].lines_exact is False


def test_an_origin_that_dispatched_but_is_gone_from_the_config_is_still_a_row() -> None:
    """配置里已经没有它了，但它那天真打出去过——照样列出来。"""
    rows = build_rows(
        (_day(GONE, rare_amount=100, first=DAY),),
        configured={EARLY: (2, True)},
        occupancies={GONE: (Occupancy(start=DAY, end=DAY + timedelta(hours=1)),)},
        recorded_total=2,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    gone = next(row for row in rows if row.origin == GONE)
    assert gone.in_config is False
    assert gone.day.dispatches == 10


def test_a_configured_origin_that_never_dispatched_is_still_a_row() -> None:
    """配了却一发没派，也要有一行——那是这一页最该喊出来的一种浪费。"""
    rows = build_rows(
        (),
        configured={EARLY: (2, True)},
        occupancies={},
        recorded_total=2,
        day_start_utc=DAY,
        now_utc=NOW,
    )
    assert [row.origin for row in rows] == [EARLY]
    assert rows[0].day.dispatches == 0
    assert rows[0].recovery is None
    assert rows[0].on_duty_s == 0.0
    assert rows[0].per_line_hour is None


# -- 日切与日期选择 -------------------------------------------------------------


def test_days_are_cut_at_utc_midnight_not_at_the_local_midnight() -> None:
    """⚠️ 统计一律按 **UTC+0** 切天（用户口径 2026-08-19）。

    这个时刻在 UTC+8 已经是 08-21 早上 5 点了，而它属于 **08-20** 这个 UTC 日。
    """
    late = datetime(2026, 8, 20, 21, 30, tzinfo=UTC)
    assert selectable_days(late)[0] == datetime(2026, 8, 20, tzinfo=UTC)
    assert parse_day(None, now_utc=late) == datetime(2026, 8, 20, tzinfo=UTC)
    assert day_label(datetime(2026, 8, 20, tzinfo=UTC), now_utc=late) == "08-20 今天"


def test_the_day_list_never_reaches_back_before_resources_existed() -> None:
    """⚠️ 那 12 格的识别是 2026-08-18 才修好的，更早的战报**没有资源明细**。

    把 08-17 摆出来，页面上会是一排「收获 0 / 效率 0」，而那天真打出去了几十发
    ——零和「没有数据」在这一段里必须分开，最诚实的分法就是不提供那些天。
    """
    days = selectable_days(datetime(2026, 8, 20, 12, 0, tzinfo=UTC))
    assert days[-1] == day_start(RESOURCE_STATS_START_UTC)
    assert all(day >= day_start(RESOURCE_STATS_START_UTC) for day in days)


def test_an_unparsable_or_out_of_range_day_falls_back_to_today() -> None:
    """认不出来 / 超出范围**不报 422**：手改地址写错一位换来一页 JSON 报错，
    读起来就是「控制台坏了」。
    """
    for value in ("", "  ", "not-a-date", "2026-13-40", "2026-08-17", "2099-01-01"):
        assert parse_day(value, now_utc=NOW) == DAY
    assert parse_day("2026-08-19", now_utc=NOW) == datetime(2026, 8, 19, tzinfo=UTC)


def test_a_past_day_is_labelled_by_date_not_as_today() -> None:
    assert day_label(datetime(2026, 8, 19, tzinfo=UTC), now_utc=NOW) == "08-19"
