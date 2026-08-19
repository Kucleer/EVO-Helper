"""数据概览页读侧的四条判据，钉在真的 SQL 上。

这几条在原型第一版全都算错过（`docs/数据概览页-需求.md` 第八节），而它们的
共同点是**错得很安静**：不报错、不读空，页面上每一格都有一个像模像样的数。

派遣行**直接按 ORM 写**，不走 `record_flight_time`：这几条断言全都取决于
`line_free_at_utc` / `line_released_at_utc` / `expected_report_at_utc` 三列的
精确取值，绕一层推算等于让被测的判据和造数据的推算互相遮掩。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.overview import day_start
from evo_helper.storage.models import (
    AttackDispatchRow,
    AttackIntentRow,
    BattleReportResourceRow,
    BattleReportRow,
    BotTargetRow,
)
from evo_helper.storage.overview import OverviewRepository

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
HOLD = timedelta(minutes=90)
HOME = Coordinate(4, 277, 15)
SECOND = Coordinate(9, 250, 8)


@pytest.fixture
def overview(session_factory: sessionmaker[Session]) -> OverviewRepository:
    return OverviewRepository(session_factory)


#: `attack_intents` 上有唯一约束 `(run_id, 目标坐标, cycle_start_utc, forced_revisit)`。
#: 好几条用例要往同一个目标上种好几发，所以 `cycle_start_utc` 每次往后挪一秒。
_CYCLE = datetime(2020, 1, 1, tzinfo=UTC)


def _next_cycle() -> datetime:
    global _CYCLE
    _CYCLE += timedelta(seconds=1)
    return _CYCLE


def _dispatch(
    session_factory: sessionmaker[Session],
    run_id: UUID,
    *,
    origin: Coordinate = HOME,
    target: Coordinate = Coordinate(2, 130, 4),
    dispatched_at_utc: datetime,
    accepted: bool = True,
    line_free_at_utc: datetime | None = None,
    line_released_at_utc: datetime | None = None,
    expected_report_at_utc: datetime | None = None,
    score_at_utc: datetime | None = None,
) -> UUID:
    intent_id = uuid4()
    dispatch_id = uuid4()
    with session_factory() as session:
        session.add(
            AttackIntentRow(
                id=intent_id,
                run_id=run_id,
                origin_galaxy=origin.galaxy,
                origin_system=origin.system,
                origin_position=origin.position,
                target_galaxy=target.galaxy,
                target_system=target.system,
                target_position=target.position,
                preset_name="AAA",
                preset_signature="sig",
                cycle_start_utc=_next_cycle(),
                created_at_utc=dispatched_at_utc,
                target_kind="bot",
                target_military_score_at_utc=score_at_utc,
            )
        )
        # 两张表之间没有声明 ORM 关系，工作单元排不出插入次序；不先落盘，
        # 派遣那一行会先插进去，撞上 `attack_dispatches.intent_id` 的外键。
        session.flush()
        session.add(
            AttackDispatchRow(
                id=dispatch_id,
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at_utc,
                accepted=accepted,
                line_free_at_utc=line_free_at_utc,
                line_released_at_utc=line_released_at_utc,
                expected_report_at_utc=expected_report_at_utc,
            )
        )
        session.commit()
    return dispatch_id


def _report(
    session_factory: sessionmaker[Session],
    *,
    reported_at_utc: datetime,
    dispatch_id: UUID | None = None,
    resources: tuple[tuple[int, int], ...] = (),
) -> None:
    report_id = uuid4()
    with session_factory() as session:
        session.add(
            BattleReportRow(
                id=report_id,
                reported_at_utc=reported_at_utc,
                attacker_origin_galaxy=HOME.galaxy,
                attacker_origin_system=HOME.system,
                attacker_origin_position=HOME.position,
                defender_target_galaxy=2,
                defender_target_system=130,
                defender_target_position=4,
                dispatch_id=dispatch_id,
            )
        )
        # 同 `_dispatch`：两张表之间没有 ORM 关系，不先落盘就会撞外键。
        session.flush()
        for slot, amount in resources:
            session.add(
                BattleReportResourceRow(id=uuid4(), report_id=report_id, slot=slot, amount=amount)
            )
        session.commit()


# -- 8.1 航线占用必须用 `_still_holding_a_line` --------------------------------


def test_a_long_stale_dispatch_with_no_line_clock_no_longer_holds_a_line(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ **原型第一版就错在这里**（需求文档 8.1）。

    它自己写了 `line_free_at_utc IS NULL AND line_released_at_utc IS NULL`，
    于是 22 小时前派出、早已超过 `hold` 的那一发被算成「还占着航线」。
    正确的判据（`_still_holding_a_line`）第三档是「占到 `派出 + hold` 为止」。

    这条用例同时钉住两件事：超期的不算，而**没超期的照样算**——把「NULL 一律
    不占」搬回来的话，第二个断言会红。
    """
    _dispatch(session_factory, run_id, dispatched_at_utc=NOW - timedelta(hours=22))

    stale = overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0]
    assert stale.holding == 0
    assert stale.unknown_duration == 0

    _dispatch(session_factory, run_id, dispatched_at_utc=NOW - timedelta(minutes=10))

    fresh = overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0]
    assert fresh.holding == 1
    assert fresh.unknown_duration == 1


def test_the_hold_comes_from_the_caller_so_a_shorter_setting_releases_sooner(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """`hold` 是用户在攻击配置页上改的那个值，**页面不许写死 90 分钟**。"""
    _dispatch(session_factory, run_id, dispatched_at_utc=NOW - timedelta(minutes=60))

    assert overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0].holding == 1
    assert (
        overview.line_usage(now_utc=NOW, hold=timedelta(minutes=45), origins=[(HOME, 5)])[0].holding
        == 0
    )


def test_a_manually_released_line_is_free_even_when_the_clock_says_otherwise(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """人工放手那一档罩住另外两档：用户在游戏里数过航线，那是观测不是推算。"""
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(minutes=10),
        line_free_at_utc=NOW + timedelta(hours=2),
        line_released_at_utc=NOW - timedelta(minutes=1),
    )

    assert overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0].holding == 0


def test_a_rejected_dispatch_never_held_a_line(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """被游戏拒掉的那一发压根没飞出去。"""
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(minutes=5),
        accepted=False,
    )

    assert overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0].holding == 0


def test_line_usage_is_reported_per_origin(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """两颗星球各配各的航线，账也各记各的。"""
    _dispatch(session_factory, run_id, origin=HOME, dispatched_at_utc=NOW - timedelta(minutes=5))
    for _ in range(3):
        _dispatch(
            session_factory, run_id, origin=SECOND, dispatched_at_utc=NOW - timedelta(minutes=5)
        )

    home, second = overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5), (SECOND, 4)])

    assert (home.origin, home.configured_lines, home.holding) == (HOME, 5, 1)
    assert (second.origin, second.configured_lines, second.holding) == (SECOND, 4, 3)


# -- 8.2 「最早空出」要过滤掉已经过去的 -----------------------------------------


def test_the_next_free_moment_ignores_line_clocks_that_are_already_in_the_past(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ **原型第一版写的是 `min(line_free_at_utc)`**（需求文档 8.2）。

    取到的是一个早就过去的时刻——页面上显示「23:26:48，约 21 分钟后」，
    而当时已经是次日 09:54。

    这里种两发：一发的航线钟已经过去（并且还没人工放手），另一发还没到。
    正确答案是**后者**；少了 `FILTER (WHERE line_free_at_utc > now)` 就会取到前者。
    """
    past = NOW - timedelta(hours=3)
    future = NOW + timedelta(minutes=64)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(hours=4),
        line_free_at_utc=past,
    )
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(minutes=10),
        line_free_at_utc=future,
    )

    usage = overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0]

    assert usage.next_free_at_utc == future
    assert usage.next_free_at_utc > NOW


def test_the_next_free_moment_is_none_when_only_unknown_duration_lines_are_held(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """`hold` 是「等到这里就放弃」的上界，不是对返航时刻的预测。

    拿它当「最早空出」摆到页面上，等于把一个兜底值说成一个预报。
    """
    _dispatch(session_factory, run_id, dispatched_at_utc=NOW - timedelta(minutes=10))

    usage = overview.line_usage(now_utc=NOW, hold=HOLD, origins=[(HOME, 5)])[0]

    assert usage.holding == 1
    assert usage.next_free_at_utc is None


# -- 第九节：未读战报只算当天 ---------------------------------------------------


def test_unread_reports_only_count_dispatches_made_today(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ 用户口径（2026-08-19）：**只统计当天，不统计历史积压。**

    实测总积压 713 发、最老派于 08-09——混进来只会让人以为现在出了大问题，
    而那批绝大部分永远读不回来了。这里种一发 10 天前的、一发昨天的、一发今天的，
    三发都到点、都没战报；答案只能是 **1**。
    """
    for age in (timedelta(days=10), timedelta(days=1)):
        moment = NOW - age
        _dispatch(
            session_factory,
            run_id,
            dispatched_at_utc=moment,
            expected_report_at_utc=moment + timedelta(minutes=40),
        )
    today = NOW - timedelta(hours=2)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=today,
        expected_report_at_utc=today + timedelta(minutes=40),
    )

    unread = overview.unread_reports(now_utc=NOW, day_start_utc=day_start(NOW))

    assert unread.unread == 1
    assert unread.dispatched_today == 1


def test_the_day_boundary_for_unread_reports_is_utc_not_the_wall_clock(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """UTC 08-18 23:00 在用户的墙上时钟里是 08-19 早上 7 点。

    按 UTC+8 切天的话它会被算进「今天」——而统计口径是 UTC+0（用户口径
    2026-08-19），它属于**昨天**。
    """
    yesterday_evening = datetime(2026, 8, 18, 23, 0, tzinfo=UTC)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=yesterday_evening,
        expected_report_at_utc=yesterday_evening + timedelta(minutes=40),
    )

    unread = overview.unread_reports(now_utc=NOW, day_start_utc=day_start(NOW))

    assert unread.dispatched_today == 0
    assert unread.unread == 0


def test_unread_reports_split_into_due_flying_and_unknown(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """三档的善后完全不同，所以三个都要有。"""
    due_at = NOW - timedelta(minutes=30)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(hours=1),
        expected_report_at_utc=due_at,
    )
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(minutes=5),
        expected_report_at_utc=NOW + timedelta(minutes=35),
    )
    _dispatch(session_factory, run_id, dispatched_at_utc=NOW - timedelta(minutes=5))

    unread = overview.unread_reports(now_utc=NOW, day_start_utc=day_start(NOW))

    assert (unread.unread, unread.in_flight, unread.unknown_eta) == (1, 1, 1)
    assert unread.oldest_expected_at_utc == due_at


def test_a_dispatch_whose_report_came_back_is_no_longer_unread(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    dispatched = NOW - timedelta(hours=1)
    dispatch_id = _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=dispatched,
        expected_report_at_utc=dispatched + timedelta(minutes=20),
    )
    _report(session_factory, reported_at_utc=NOW - timedelta(minutes=20), dispatch_id=dispatch_id)

    unread = overview.unread_reports(now_utc=NOW, day_start_utc=day_start(NOW))

    assert unread.unread == 0
    assert unread.dispatched_today == 1


# -- 周期统计 -------------------------------------------------------------------


def test_period_counts_cut_on_a_half_open_utc_interval(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """半开区间：日界那一刻属于**后面**那一天，不是两天各算一次。"""
    boundary = datetime(2026, 8, 19, tzinfo=UTC)
    _dispatch(session_factory, run_id, dispatched_at_utc=boundary - timedelta(seconds=1))
    _dispatch(session_factory, run_id, dispatched_at_utc=boundary)

    yesterday = overview.period_counts(start=boundary - timedelta(days=1), end=boundary)
    today = overview.period_counts(start=boundary, end=boundary + timedelta(days=1))

    assert (yesterday.dispatches, today.dispatches) == (1, 1)


def test_reports_are_cut_by_their_own_moment_not_by_the_dispatch_moment(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """派遣按 `dispatched_at_utc` 切、战报按 `reported_at_utc` 切。

    拿同一列切的话，「回收率」就变成一个自己跟自己比的数——恒等于 100%。
    """
    dispatched = datetime(2026, 8, 18, 23, 30, tzinfo=UTC)
    dispatch_id = _dispatch(session_factory, run_id, dispatched_at_utc=dispatched)
    _report(
        session_factory,
        reported_at_utc=datetime(2026, 8, 19, 0, 10, tzinfo=UTC),
        dispatch_id=dispatch_id,
    )

    day18 = overview.period_counts(
        start=datetime(2026, 8, 18, tzinfo=UTC), end=datetime(2026, 8, 19, tzinfo=UTC)
    )
    day19 = overview.period_counts(
        start=datetime(2026, 8, 19, tzinfo=UTC), end=datetime(2026, 8, 20, tzinfo=UTC)
    )

    assert (day18.dispatches, day18.reports) == (1, 0)
    assert (day19.dispatches, day19.reports) == (0, 1)


def test_covered_coordinates_count_distinct_targets(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """与派遣数的差就是当天的重复度。"""
    for target in (Coordinate(2, 130, 4), Coordinate(2, 130, 4), Coordinate(3, 141, 9)):
        _dispatch(
            session_factory, run_id, target=target, dispatched_at_utc=NOW - timedelta(minutes=5)
        )

    counts = overview.period_counts(start=day_start(NOW), end=NOW)

    assert (counts.dispatches, counts.coordinates) == (3, 2)


def test_protection_hits_are_counted_in_their_own_window(
    overview: OverviewRepository, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as session:
        session.add(
            BotTargetRow(
                galaxy=4,
                system=100,
                position=1,
                is_bot=True,
                protection_seen_at_utc=NOW - timedelta(hours=1),
            )
        )
        session.add(
            BotTargetRow(
                galaxy=4,
                system=100,
                position=2,
                is_bot=True,
                protection_seen_at_utc=NOW - timedelta(days=3),
            )
        )
        session.commit()

    counts = overview.period_counts(start=day_start(NOW), end=NOW)

    assert counts.protection_hits == 1


def test_resource_totals_only_cover_reports_that_came_back(
    overview: OverviewRepository, session_factory: sessionmaker[Session]
) -> None:
    """⚠️ 资源列是**下界**，而且过去某一天的数会一直涨（需求文档 8.4）。

    实测 08-18 这一天，隔 11 小时从 33 份 / 67,594 变成 61 份 / 166,194。
    这条用例演的就是那件事：同一个窗口，补进第二份战报之后总数跟着长。
    """
    window = (datetime(2026, 8, 18, tzinfo=UTC), datetime(2026, 8, 19, tzinfo=UTC))
    _report(
        session_factory,
        reported_at_utc=datetime(2026, 8, 18, 12, tzinfo=UTC),
        resources=((5, 67_594),),
    )

    before = overview.resource_totals(start=window[0], end=window[1])
    assert [(item.slot, item.amount) for item in before] == [(5, 67_594)]

    _report(
        session_factory,
        reported_at_utc=datetime(2026, 8, 18, 20, tzinfo=UTC),
        resources=((5, 98_600), (8, 7_807)),
    )

    after = overview.resource_totals(start=window[0], end=window[1])
    assert [(item.slot, item.amount) for item in after] == [(5, 166_194), (8, 7_807)]


def test_occupancy_segments_follow_the_three_tier_rule(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """三档各出一发，段的长度必须各不相同。

    把三档合并成任意一档（比如「一律按 hold 算」），这三个断言里至少两个会红。
    """
    began = NOW - timedelta(hours=2)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=began,
        line_released_at_utc=began + timedelta(minutes=20),
        line_free_at_utc=began + timedelta(minutes=50),
    )
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=began,
        line_free_at_utc=began + timedelta(minutes=50),
    )
    _dispatch(session_factory, run_id, dispatched_at_utc=began)

    segments = overview.occupancies(
        start=day_start(NOW), end=NOW, hold=timedelta(minutes=90), now_utc=NOW
    )

    assert sorted(int((item.end - item.start).total_seconds() / 60) for item in segments) == [
        20,
        50,
        90,
    ]


def test_occupancy_is_never_counted_beyond_now(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """还没发生的占用不是产能。分母也只算到现在，两边必须同源。"""
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=NOW - timedelta(minutes=10),
        line_free_at_utc=NOW + timedelta(hours=3),
    )

    segments = overview.occupancies(
        start=day_start(NOW), end=NOW + timedelta(hours=14), hold=HOLD, now_utc=NOW
    )

    assert [item.end for item in segments] == [NOW]


def test_score_age_at_dispatch_reads_the_snapshot_column(
    overview: OverviewRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """读的是 `attack_intents` 上那个**快照**，不是 `bot_targets` 的现值。

    现值每采一次军力榜就整行覆盖，现取答的是「它现在多新」。
    """
    dispatched = NOW - timedelta(hours=1)
    _dispatch(
        session_factory,
        run_id,
        dispatched_at_utc=dispatched,
        score_at_utc=dispatched - timedelta(hours=3),
    )
    _dispatch(session_factory, run_id, dispatched_at_utc=dispatched)

    ages = overview.score_age_hours_at_dispatch(since=day_start(NOW), until=NOW)

    assert ages == (pytest.approx(3.0),)
