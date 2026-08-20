"""「按星球效率」的读侧，钉在真的 SQL 上。

四条判据全都**错得很安静**：不报错、不读空，页面上每一格都有一个像模像样的数。

1. 分子只加稀有三样（基础三样掺进来会让「预设大的星球」无脑领先）。
2. 归属按**派出日**，不按读回日。
3. 日界是 UTC+0 的半开区间。
4. 「数派遣」和「加资源」分两趟查，别让资源行把 `count(*)` 扇出。

派遣行**直接按 ORM 写**，不走 `record_flight_time`：断言全都取决于
`dispatched_at_utc` / `line_free_at_utc` 的精确取值，绕一层推算等于让被测的判据
和造数据的推算互相遮掩（同 `test_overview_repository`）。

⚠️ **坐标全是编造的**（公开仓库，真实出发坐标不进夹具）；**时刻全部注入**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.origin_efficiency import OriginDay
from evo_helper.domain.overview import BASIC_SLOTS, RARE_SLOTS, day_start
from evo_helper.storage.models import (
    AttackDispatchRow,
    AttackIntentRow,
    BattleReportResourceRow,
    BattleReportRow,
)
from evo_helper.storage.origin_efficiency import OriginEfficiencyRepository

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)
DAY = day_start(NOW)
DAY_END = DAY + timedelta(days=1)
HOLD = timedelta(minutes=90)

#: 编造的出发星球。位置号在 5 以上（1–4 号位是海盗位，选靶那侧另有判据挡它们）。
ALPHA = Coordinate(1, 111, 6)
BETA = Coordinate(2, 222, 7)


@pytest.fixture
def origins(session_factory: sessionmaker[Session]) -> OriginEfficiencyRepository:
    return OriginEfficiencyRepository(session_factory)


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
    origin: Coordinate,
    dispatched_at_utc: datetime,
    accepted: bool = True,
    line_free_at_utc: datetime | None = None,
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
                target_galaxy=5,
                target_system=140,
                target_position=9,
                preset_name="AAA",
                preset_signature="sig",
                cycle_start_utc=_next_cycle(),
                created_at_utc=dispatched_at_utc,
                target_kind="bot",
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
            )
        )
        session.commit()
    return dispatch_id


def _report(
    session_factory: sessionmaker[Session],
    *,
    reported_at_utc: datetime,
    dispatch_id: UUID | None,
    resources: tuple[tuple[int, int], ...] = (),
    approximate: bool = False,
    uncertainty: int = 0,
) -> None:
    report_id = uuid4()
    with session_factory() as session:
        session.add(
            BattleReportRow(
                id=report_id,
                reported_at_utc=reported_at_utc,
                attacker_origin_galaxy=ALPHA.galaxy,
                attacker_origin_system=ALPHA.system,
                attacker_origin_position=ALPHA.position,
                defender_target_galaxy=5,
                defender_target_system=140,
                defender_target_position=9,
                dispatch_id=dispatch_id,
            )
        )
        session.flush()
        for slot, amount in resources:
            session.add(
                BattleReportResourceRow(
                    id=uuid4(),
                    report_id=report_id,
                    slot=slot,
                    amount=amount,
                    approximate=approximate,
                    uncertainty=uncertainty,
                )
            )
        session.commit()


def _by_origin(
    origins: OriginEfficiencyRepository, *, start: datetime = DAY, end: datetime = DAY_END
) -> dict[Coordinate, OriginDay]:
    return {item.origin: item for item in origins.origin_days(start=start, end=end)}


# -- 分子：只算稀有三样 ---------------------------------------------------------


def test_the_numerator_adds_the_rare_slots_and_ignores_the_basic_ones(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ **基础三样绝不许进分子。**

    这一发收了三样稀有各 1,000，另外三样基础各 720,000（实测量级：那三样由我方
    货舱容量决定、与目标无关，同一预设 6 条战报的变异系数只有 0.0001）。
    分子必须是 3,000——把基础三样也加进来会变成 2,163,000，而那个数只反映
    「预设有多大」。
    """
    dispatch = _dispatch(
        session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=1)
    )
    _report(
        session_factory,
        reported_at_utc=DAY + timedelta(hours=2),
        dispatch_id=dispatch,
        resources=tuple((slot, 1_000) for slot in RARE_SLOTS)
        + tuple((slot, 720_000) for slot in BASIC_SLOTS),
    )

    row = _by_origin(origins)[ALPHA]

    assert row.rare_amount == 3_000


def test_a_resource_row_does_not_inflate_the_dispatch_count(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ **扇出**。把「数派遣」和「加资源」写进同一个 `GROUP BY`，一发派了三种
    资源就会被数成三发，回收率随之变成 300%。
    """
    dispatch = _dispatch(
        session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=1)
    )
    _report(
        session_factory,
        reported_at_utc=DAY + timedelta(hours=2),
        dispatch_id=dispatch,
        resources=tuple((slot, 100) for slot in RARE_SLOTS),
    )

    row = _by_origin(origins)[ALPHA]

    assert (row.dispatches, row.reports) == (1, 1)


def test_an_approximate_reading_is_carried_out_with_its_error_budget(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """近似读数要标「约」，误差**逐份战报相加**（同 `storage.overview.ResourceTotal`）。"""
    for hour in (1, 2):
        dispatch = _dispatch(
            session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=hour)
        )
        _report(
            session_factory,
            reported_at_utc=DAY + timedelta(hours=hour, minutes=30),
            dispatch_id=dispatch,
            resources=((RARE_SLOTS[0], 928_000),),
            approximate=True,
            uncertainty=500,
        )

    row = _by_origin(origins)[ALPHA]

    assert row.rare_approximate is True
    assert row.rare_uncertainty == 1_000


# -- 归属：按派出日 -------------------------------------------------------------


def test_a_report_read_the_next_day_still_counts_for_the_dispatch_day(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ **按派出日归属，不按读回日。**

    这一发是当天 23:30 派出的，战报次日 01:00 才读回来。它的收获记在**派出的
    那一天**；按读回日归属的话，「今天效率高」可能只是「今天补读了昨天的战报」。
    """
    dispatch = _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=DAY + timedelta(hours=23, minutes=30),
    )
    _report(
        session_factory,
        reported_at_utc=DAY_END + timedelta(hours=1),
        dispatch_id=dispatch,
        resources=((RARE_SLOTS[1], 4_242),),
    )

    today = _by_origin(origins)
    tomorrow = _by_origin(origins, start=DAY_END, end=DAY_END + timedelta(days=1))

    assert today[ALPHA].rare_amount == 4_242
    assert tomorrow == {}


def test_a_report_with_no_dispatch_link_is_not_attributed_to_anyone(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """配不上派遣的战报既说不出是哪颗星球派的，也说不出是哪天派的。

    硬塞进某一行只会让那一行凭空多出一笔收获。它的可观察后果是回收率偏低——
    而那正是该被看见的事。
    """
    _dispatch(session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=1))
    _report(
        session_factory,
        reported_at_utc=DAY + timedelta(hours=2),
        dispatch_id=None,
        resources=((RARE_SLOTS[0], 99_999),),
    )

    row = _by_origin(origins)[ALPHA]

    assert row.rare_amount == 0
    assert (row.dispatches, row.reports) == (1, 0)


def test_the_report_count_asks_whether_this_dispatch_came_back(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ 「读回」数的是「这一发有没有战报」，**不是「当天读回了几份战报」**。

    昨天派出、今天读回的那一发不进今天的分子；不这样的话，回收率变成一个自己
    跟自己比的数（分子分母切的是两个不同的时刻）。
    """
    yesterday = _dispatch(
        session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY - timedelta(hours=2)
    )
    _report(session_factory, reported_at_utc=DAY + timedelta(hours=1), dispatch_id=yesterday)
    _dispatch(session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=3))

    row = _by_origin(origins)[ALPHA]

    assert (row.dispatches, row.reports) == (1, 0)


# -- 日界与筛选 -----------------------------------------------------------------


def test_the_day_window_is_half_open_at_utc_midnight(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ 日界是 **UTC+0** 的半开区间 `[00:00, 次日 00:00)`。

    23:59:59 属于今天、次日 00:00:00 不属于。切日按会话时区（`func.date()`）
    或者按本机时区，整条日界会挪 8 小时，而页面上看不出任何异样。
    """
    _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=DAY + timedelta(hours=23, minutes=59, seconds=59),
    )
    _dispatch(session_factory, run_id, origin=BETA, dispatched_at_utc=DAY_END)

    today = _by_origin(origins)

    assert set(today) == {ALPHA}


def test_a_rejected_dispatch_is_not_counted(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """被游戏拒掉的那些没有舰队飞出去，也就永远不会有战报。

    算进来的话，回收率会被一批注定读不回来的发次压低。
    """
    _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=DAY + timedelta(hours=1),
        accepted=False,
    )

    assert _by_origin(origins) == {}


def test_each_origin_gets_its_own_row(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """分组按出发坐标的三个分量，漏一个分量的症状是两颗星球被合成一行。"""
    alpha = _dispatch(
        session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=1)
    )
    _report(
        session_factory,
        reported_at_utc=DAY + timedelta(hours=2),
        dispatch_id=alpha,
        resources=((RARE_SLOTS[0], 1_000),),
    )
    _dispatch(session_factory, run_id, origin=BETA, dispatched_at_utc=DAY + timedelta(hours=3))

    rows = _by_origin(origins)

    assert set(rows) == {ALPHA, BETA}
    assert rows[ALPHA].rare_amount == 1_000
    assert rows[BETA].rare_amount == 0


def test_first_and_last_dispatch_come_back_as_aware_utc(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ 聚合函数（`min` / `max`）绕过 `UTCDateTime` 的类型装饰，在 SQLite 上会
    交出 **naive** 的值——naive 与 aware 一比就是 `TypeError`，页面上表现为 500。
    """
    for hour in (2, 9):
        _dispatch(
            session_factory, run_id, origin=ALPHA, dispatched_at_utc=DAY + timedelta(hours=hour)
        )

    row = _by_origin(origins)[ALPHA]

    assert row.first_dispatch_at_utc == DAY + timedelta(hours=2)
    assert row.last_dispatch_at_utc == DAY + timedelta(hours=9)
    assert row.first_dispatch_at_utc.tzinfo is not None


# -- 占用段（线数没有真值时的下界） ---------------------------------------------


def test_occupancies_are_split_per_origin(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """线数的下界按**每颗星球**的最大并发算，所以占用段必须按星球分开。

    合在一起的话，两颗星球各占 1 条会被算成某一颗占了 2 条，那颗星球的分母
    凭空翻倍。
    """
    _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=DAY + timedelta(hours=1),
        line_free_at_utc=DAY + timedelta(hours=3),
    )
    _dispatch(
        session_factory,
        run_id,
        origin=BETA,
        dispatched_at_utc=DAY + timedelta(hours=1, minutes=30),
        line_free_at_utc=DAY + timedelta(hours=4),
    )

    segments = origins.origin_occupancies(start=DAY, end=NOW, hold=HOLD, now_utc=NOW)

    assert len(segments[ALPHA]) == 1
    assert len(segments[BETA]) == 1


def test_an_occupancy_that_started_before_midnight_is_still_returned(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """⚠️ 跨零点那一段不能漏：按 `dispatched_at_utc >= start` 取会把它整个丢掉，
    于是当天的最大并发在飞数偏小、线数的下界更松（同 `storage.overview.occupancies`）。
    """
    _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=DAY - timedelta(minutes=30),
        line_free_at_utc=DAY + timedelta(hours=2),
    )

    segments = origins.origin_occupancies(start=DAY, end=NOW, hold=HOLD, now_utc=NOW)

    assert len(segments[ALPHA]) == 1


def test_an_occupancy_is_clamped_to_now(
    origins: OriginEfficiencyRepository, session_factory: sessionmaker[Session], run_id: UUID
) -> None:
    """还没发生的占用不是产能：末端一律钳到「现在」。"""
    _dispatch(
        session_factory,
        run_id,
        origin=ALPHA,
        dispatched_at_utc=NOW - timedelta(minutes=10),
        line_free_at_utc=NOW + timedelta(hours=5),
    )

    segments = origins.origin_occupancies(start=DAY, end=NOW, hold=HOLD, now_utc=NOW)

    assert segments[ALPHA][0].end == NOW
