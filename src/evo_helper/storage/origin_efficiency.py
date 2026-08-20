"""「按天 × 按星球效率」的读侧查询。**只读，一个写操作都没有。**

判据一律不在这里发明：槽位取 `domain.overview.RARE_SLOTS`、日界由调用方按
`domain.overview.day_start` 算好以**半开区间**下推、出发星球判据用
`repository._from_origin` 的那三个分量。

⚠️ **切日不用 `func.date()`**（同 `storage.overview` 那一段）：那个函数在
PostgreSQL 上按**会话时区**换算，服务器在 UTC+8 时整条日界会挪 8 小时。

## ⚠️ 为什么是两趟查询而不是一趟

一发派遣可能带出好几行资源明细（12 格里非零的那几格）。把「数派遣」和
「加资源」写进同一个 `GROUP BY`，`count(*)` 会被资源行**扇出**放大——一发派了
3 种资源就被数成 3 发，回收率随之变成 300%。所以计数走 `_counts`、资源走
`_rare`，在 Python 里按坐标合。

## ⚠️ 归属：按**派出日**，靠 `battle_reports.dispatch_id` 反查

资源挂在战报上，而战报的 `reported_at_utc` 是**读回**时刻。这一段要的是
「这一发是哪天派出去的」，所以两趟查询都从 `attack_dispatches` 出发、
按 `dispatched_at_utc` 切窗口，资源那一趟再顺着 `dispatch_id` 走回来。

代价是**没配上派遣的战报（`dispatch_id IS NULL`）一律不算**。那是刻意的：
一份配不上派遣的战报既说不出是哪颗星球派的，也说不出是哪天派的，硬塞进某一行
只会让那一行凭空多出一笔收获。它的可观察后果是「回收率」——配不上的那些
在页面上表现为回收率偏低，而那正是该被看见的事。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.origin_efficiency import OriginDay
from evo_helper.domain.overview import RARE_SLOTS, Occupancy, occupancy_end
from evo_helper.storage import models as orm


@dataclass(frozen=True, slots=True)
class _Rare:
    amount: int
    approximate: bool
    uncertainty: int


class OriginEfficiencyRepository:
    """按天、按出发星球的效率读侧。**只读**：这个类里一个 `INSERT` / `UPDATE`
    都没有，也不碰任何会触发游戏动作的路径。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def origin_days(self, *, start: datetime, end: datetime) -> tuple[OriginDay, ...]:
        """`[start, end)` 这一天里，每颗**真派出过**的星球一行。

        行集只含派出过的；「配了却一发没派」那些由
        `domain.origin_efficiency.build_rows` 用当前配置补上——库里查不出一个
        从未出现过的坐标。
        """
        with self._session_factory() as session:
            counts = self._counts(session, start=start, end=end)
            rare = self._rare(session, start=start, end=end)
        empty = _Rare(amount=0, approximate=False, uncertainty=0)
        return tuple(
            OriginDay(
                origin=origin,
                dispatches=dispatches,
                reports=reports,
                rare_amount=(haul := rare.get(origin, empty)).amount,
                rare_approximate=haul.approximate,
                rare_uncertainty=haul.uncertainty,
                first_dispatch_at_utc=first,
                last_dispatch_at_utc=last,
            )
            for origin, (dispatches, reports, first, last) in counts.items()
        )

    def origin_occupancies(
        self, *, start: datetime, end: datetime, hold: timedelta, now_utc: datetime
    ) -> dict[Coordinate, tuple[Occupancy, ...]]:
        """与 `[start, end)` 有交集的航线占用段，**按出发星球分开**。

        线数没有真值的那些天靠它推下界（`domain.origin_efficiency.origin_lines`
        → `domain.overview.max_concurrent_lines`）。

        ⚠️ **每一段的结束时刻问 `domain.overview.occupancy_end` 要**（人工放手 →
        航线钟 → 派出 + `hold` 三档），不在这里另写一遍：那三档与
        `repository._still_holding_a_line` 逐条对应，抄一份出去下一次改判据这一处
        不会跟着改，而那种错静默。

        ⚠️ **取行的下界要放宽到 `start` 之前一段**：占用段可能从窗口之前一直延伸
        进来（22:30 派出、次日 00:40 回港的那些在两天各占一段），按
        `dispatched_at_utc >= start` 取会把跨零点那一段整个漏掉，于是当天的最大
        并发在飞数偏小、下界更松（同 `storage.overview.occupancies`）。

        ⚠️ **一律钳到「现在」为止**：还没发生的占用不是产能。
        """
        floor = start - max(hold, timedelta(days=1))
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.AttackIntentRow.origin_galaxy,
                    orm.AttackIntentRow.origin_system,
                    orm.AttackIntentRow.origin_position,
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.AttackDispatchRow.line_free_at_utc,
                    orm.AttackDispatchRow.line_released_at_utc,
                )
                .select_from(orm.AttackDispatchRow)
                .join(
                    orm.AttackIntentRow,
                    orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                )
                .where(
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dispatched_at_utc >= floor,
                    orm.AttackDispatchRow.dispatched_at_utc < end,
                )
            ).all()
        segments: dict[Coordinate, list[Occupancy]] = defaultdict(list)
        for row in rows:
            began = _as_utc(row.dispatched_at_utc)
            if began is None:
                continue
            finished = min(
                occupancy_end(
                    dispatched_at_utc=began,
                    line_free_at_utc=_as_utc(row.line_free_at_utc),
                    line_released_at_utc=_as_utc(row.line_released_at_utc),
                    hold=hold,
                ),
                now_utc,
            )
            if finished <= began:
                continue
            origin = Coordinate(
                int(row.origin_galaxy), int(row.origin_system), int(row.origin_position)
            )
            segments[origin].append(Occupancy(start=began, end=finished))
        return {origin: tuple(items) for origin, items in segments.items()}

    @staticmethod
    def _counts(
        session: Session, *, start: datetime, end: datetime
    ) -> dict[Coordinate, tuple[int, int, datetime | None, datetime | None]]:
        """每颗星球当天派了几发、其中几发已读回战报、首发与末发是什么时候。

        ⚠️ **「已读回」数的是「这一发有没有战报」，不是「当天读回了几份战报」。**
        后者会把昨天派出、今天读回的算进来，于是回收率变成一个自己跟自己比的数
        （分子分母切的是两个不同的时刻）。

        `battle_reports.dispatch_id` 上有唯一约束，所以这个 `LEFT JOIN` 不会扇出。
        """
        rows = session.execute(
            select(
                orm.AttackIntentRow.origin_galaxy,
                orm.AttackIntentRow.origin_system,
                orm.AttackIntentRow.origin_position,
                func.count().label("dispatches"),
                func.count().filter(orm.BattleReportRow.id.is_not(None)).label("reports"),
                func.min(orm.AttackDispatchRow.dispatched_at_utc).label("first"),
                func.max(orm.AttackDispatchRow.dispatched_at_utc).label("last"),
            )
            .select_from(orm.AttackDispatchRow)
            .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
            .outerjoin(
                orm.BattleReportRow,
                orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
            )
            .where(
                orm.AttackDispatchRow.accepted.is_(True),
                orm.AttackDispatchRow.dispatched_at_utc >= start,
                orm.AttackDispatchRow.dispatched_at_utc < end,
            )
            .group_by(
                orm.AttackIntentRow.origin_galaxy,
                orm.AttackIntentRow.origin_system,
                orm.AttackIntentRow.origin_position,
            )
        ).all()
        return {
            Coordinate(int(row.origin_galaxy), int(row.origin_system), int(row.origin_position)): (
                int(row.dispatches or 0),
                int(row.reports or 0),
                _as_utc(row.first),
                _as_utc(row.last),
            )
            for row in rows
        }

    @staticmethod
    def _rare(session: Session, *, start: datetime, end: datetime) -> dict[Coordinate, _Rare]:
        """每颗星球当天派出去的那些发次，一共收回来多少稀有三样。

        ⚠️ **槽位来自 `domain.overview.RARE_SLOTS`，这里不许再写一份号码。**
        那张对照表的顺序与游戏「太空舱」页不一致（`domain.battle_resources.SLOT_LABELS`
        上写着是哪两格对调），抄第二份出去，对不上的症状是「数字全对、只是安在了
        别的资源名下」——页面上一点异样都没有。

        ⚠️ **基础三样绝不许进来**（`domain.overview.BASIC_SLOTS` 那三格）。实测
        2026-08-20：它们由我方货舱容量决定、与目标无关（同一预设 6 条战报的变异
        系数 0.0001），掺进来会让「预设大的星球」无脑领先。
        """
        rows = session.execute(
            select(
                orm.AttackIntentRow.origin_galaxy,
                orm.AttackIntentRow.origin_system,
                orm.AttackIntentRow.origin_position,
                func.sum(orm.BattleReportResourceRow.amount).label("amount"),
                func.max(cast(orm.BattleReportResourceRow.approximate, Integer)).label(
                    "approximate"
                ),
                func.sum(orm.BattleReportResourceRow.uncertainty).label("uncertainty"),
            )
            .select_from(orm.BattleReportResourceRow)
            .join(
                orm.BattleReportRow,
                orm.BattleReportRow.id == orm.BattleReportResourceRow.report_id,
            )
            .join(
                orm.AttackDispatchRow,
                orm.AttackDispatchRow.id == orm.BattleReportRow.dispatch_id,
            )
            .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
            .where(
                orm.BattleReportResourceRow.slot.in_(RARE_SLOTS),
                orm.AttackDispatchRow.accepted.is_(True),
                orm.AttackDispatchRow.dispatched_at_utc >= start,
                orm.AttackDispatchRow.dispatched_at_utc < end,
            )
            .group_by(
                orm.AttackIntentRow.origin_galaxy,
                orm.AttackIntentRow.origin_system,
                orm.AttackIntentRow.origin_position,
            )
        ).all()
        return {
            Coordinate(
                int(row.origin_galaxy), int(row.origin_system), int(row.origin_position)
            ): _Rare(
                amount=int(row.amount or 0),
                approximate=bool(row.approximate),
                uncertainty=int(row.uncertainty or 0),
            )
            for row in rows
        }


def _as_utc(value: datetime | None) -> datetime | None:
    """把库里读出来的时刻钉成 aware 的 UTC。

    `UTCDateTime` 保证读出来是 aware 的，但聚合函数（`min` / `max`）绕过那一层的
    类型装饰，在 SQLite 上会交出 naive 的值——naive 与 aware 一比就是
    `TypeError`，而那在页面上表现为 500（同 `storage.overview._as_utc`）。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["OriginEfficiencyRepository"]
