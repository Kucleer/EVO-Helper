"""数据概览页的读侧查询。**只读，一个写操作都没有。**

判据一律不在这里发明：航线占用直接 import `repository._still_holding_a_line`，
周期切分与占用时长算法在 `domain.overview`。这个模块只负责「把库里的行取出来」。

⚠️ **`_still_holding_a_line` 是从 `repository` 里拿的私名，这是刻意的。**
需求文档 8.1 写死了这一条：原型第一版自己写了个
`line_free_at_utc IS NULL AND line_released_at_utc IS NULL`，把 10–22 小时前派出、
早已超过 `UNKNOWN_LINE_HOLD` 的那些也算成「占着航线」。抄一份等价的谓词过来，
下一次有人改判据时这一处不会跟着改，而那种错静默。宁可 import 一个私名，
也不要两份判据。

⚠️ **切日不用 `func.date()`。** 那个函数在 PostgreSQL 上按会话时区换算，服务器
在 UTC+8 时整条日界会挪 8 小时（同 `repository._utc_day`）。这里一律用 Python
算好边界、以**半开区间**下推。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from evo_helper.domain.models import Coordinate
from evo_helper.domain.overview import Occupancy, RunWindow, occupancy_end
from evo_helper.storage import models as orm
from evo_helper.storage.repository import (
    SqlAlchemyRepository,
    _from_origin,
    _still_holding_a_line,
)


@dataclass(frozen=True, slots=True)
class OriginLineUsage:
    """一颗出发星球此刻的航线账。"""

    origin: Coordinate
    #: 这颗星球**配着**几条航线（`mission_task_origins.fleet_lines`）。
    #: 格子按它画，不按下面那两个数画（需求文档 8.3）。
    configured_lines: int
    #: 还占着航线的（判据 `_still_holding_a_line`），**含**时长未知那一档。
    holding: int
    #: 上一行里航线钟为 NULL、按 `hold` 兜底占着的那些。
    #:
    #: ⚠️ **必须单独显示，不许并进「在飞」。** 它是飞行时间没读出来、按
    #: `UNKNOWN_LINE_HOLD` 占着航线的那批；混在一起，页面就说不出「为什么明明
    #: 没派几发却没航线了」（需求文档 2.1）。
    unknown_duration: int
    #: 已知最早会空出来的那条航线，什么时候空。
    #:
    #: ⚠️ **只看还没到点的那些**（`line_free_at_utc > now`）。原型第一版写的是
    #: `min(line_free_at_utc)`，取到一个早就过去的时刻，页面上显示成
    #: 「23:26:48，约 21 分钟后」而当时已是次日 09:54（需求文档 8.2）。
    next_free_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class UnreadReports:
    """「未读战报」= 发出去还没回读的那些。**只统计当天（UTC+0）派出的。**

    用户口径（2026-08-19）：「未读战报只统计当天，不统计历史积压」。
    实测总积压 713 发、最老派于 08-09，混进来只会让人以为现在出了大问题——
    按当天算出来的数是个位数，那正是它该有的量级。
    """

    #: 当天派出、`expected_report_at_utc <= now` 且还没有关联战报的。**这就是它。**
    unread: int
    #: 当天派出、还没到预计战报时刻的。正常，等着就行。
    in_flight: int
    #: 当天派出、飞行时间没读出来（`expected_report_at_utc IS NULL`）的。
    unknown_eta: int
    #: 当天一共派出去几发（`accepted`）。上面三档的分母。
    dispatched_today: int
    #: 未读那一档里最老的一发，预计战报时刻是什么时候。一发都没有时为 None。
    oldest_expected_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class PeriodCounts:
    """一个周期里的计数类指标。资源另走 `resource_totals`。"""

    dispatches: int
    reports: int
    protection_hits: int
    coordinates: int


@dataclass(frozen=True, slots=True)
class ResourceTotal:
    """一个槽位在这个周期里的收获。"""

    slot: int
    amount: int
    #: 这一格里有没有近似读数（画面上是 `928K` 这样缩写显示的，真值取不回来）。
    #: 页面要标「约」——把近似值渲染得像精确读数是另一回事。
    approximate: bool


@dataclass(frozen=True, slots=True)
class GalaxyFreshness:
    galaxy: int
    fresh: int


class OverviewRepository:
    """数据概览页的读侧。**只读**：这个类里一个 `INSERT` / `UPDATE` 都没有，
    也不碰任何会触发游戏动作的路径（需求文档第七节）。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        # 「最早空出」直接问调度器自己那个查询要（`next_line_free_at`），不另写一遍。
        # 见 `line_usage` 的说明。
        self._repository = SqlAlchemyRepository(session_factory)

    # -- 此刻 ------------------------------------------------------------------

    def line_usage(
        self,
        *,
        now_utc: datetime,
        hold: timedelta,
        origins: Sequence[tuple[Coordinate, int]],
    ) -> tuple[OriginLineUsage, ...]:
        """按出发星球给出航线账。`origins` 是「坐标 + 配着几条」，由调用方从
        `MissionScheduler.configured_line_origins()` 取——这里不自己去读那张表，
        `planet_id` 与坐标快照谁优先这条规则只该有一份。

        ⚠️ **「最早空出」调 `SqlAlchemyRepository.next_line_free_at`，页面不自己
        算**（需求文档 8.2 + 8.7）。那个函数本来就是调度器用来把「等航线」锚在一个
        真会发生的事件上的，判据里带着 `line_free_at_utc > now`——原型第一版自己写
        了个 `min(line_free_at_utc)`，取到一个**早就过去**的时刻，页面上显示成
        「23:26:48，约 21 分钟后」而当时已是次日 09:54。

        两个数（占用、时长未知）另走一趟聚合，因为它们要按 `hold` 判，而
        `next_line_free_at` 不吃 `hold`（它只看有航线钟的那一档）。
        """
        holding = _still_holding_a_line(now_utc, hold)
        with self._session_factory() as session:
            counts = [self._counts_for(session, origin, holding=holding) for origin, _ in origins]
        return tuple(
            OriginLineUsage(
                origin=origin,
                configured_lines=lines,
                holding=held,
                unknown_duration=unknown,
                next_free_at_utc=self._repository.next_line_free_at(now_utc=now_utc, origin=origin),
            )
            for (origin, lines), (held, unknown) in zip(origins, counts, strict=True)
        )

    @staticmethod
    def _counts_for(
        session: Session, origin: Coordinate, *, holding: ColumnElement[bool]
    ) -> tuple[int, int]:
        """「占着几条」与「其中几条时长未知」。

        一趟查询取齐而不是查两遍：它们必须来自同一个 `now`，分两次查会让
        「占着 3 条、其中 4 条时长未知」这种自相矛盾在边界上真的发生。
        """
        row = session.execute(
            select(
                func.count().label("holding"),
                # 「时长未知」= 还占着 **且** 航线钟为 NULL。
                func.count()
                .filter(orm.AttackDispatchRow.line_free_at_utc.is_(None))
                .label("unknown"),
            )
            .select_from(orm.AttackDispatchRow)
            .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
            .where(
                _from_origin(origin),
                orm.AttackDispatchRow.accepted.is_(True),
                holding,
            )
        ).one()
        return int(row.holding or 0), int(row.unknown or 0)

    def unread_reports(self, *, now_utc: datetime, day_start_utc: datetime) -> UnreadReports:
        """当天（UTC+0）派出去、还没回读战报的那些，切成三档。

        `day_start_utc` 由调用方按 `domain.overview.day_start` 算好传进来——
        切日的口径只该有一份，这里不再算一次。

        ⚠️ **窗口是「当天派出的」，不是「当天到点的」。** 后者会把昨天派出、
        今天才到点的那些算进来，于是这个数在零点之后会先跳高再回落，而它本该
        随当天的派遣一起从 0 长起来。

        ⚠️ **不许把历史积压混进来**（用户口径 2026-08-19）：实测总积压 713 发、
        最老派于 08-09 18:27。只给 713 会让人以为现在出了大问题，而那批绝大部分
        永远读不回来了。
        """
        expected = orm.AttackDispatchRow.expected_report_at_utc
        no_report = orm.BattleReportRow.id.is_(None)
        unread_where = and_(no_report, expected <= now_utc)
        day_end_utc = day_start_utc + timedelta(days=1)
        with self._session_factory() as session:
            row = session.execute(
                select(
                    func.count().label("dispatched"),
                    func.count().filter(unread_where).label("unread"),
                    func.count().filter(and_(no_report, expected > now_utc)).label("flying"),
                    func.count().filter(and_(no_report, expected.is_(None))).label("unknown"),
                    func.min(expected).filter(unread_where).label("oldest"),
                )
                .select_from(orm.AttackDispatchRow)
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dispatched_at_utc >= day_start_utc,
                    orm.AttackDispatchRow.dispatched_at_utc < day_end_utc,
                )
            ).one()
        return UnreadReports(
            unread=int(row.unread or 0),
            in_flight=int(row.flying or 0),
            unknown_eta=int(row.unknown or 0),
            dispatched_today=int(row.dispatched or 0),
            oldest_expected_at_utc=_as_utc(row.oldest),
        )

    def galaxy_freshness(
        self, *, now_utc: datetime, window: timedelta
    ) -> tuple[GalaxyFreshness, ...]:
        """各银河在窗口内有几个新鲜的军力读数。

        直接数 `bot_targets.military_score_at_utc`，不掺任何可打性判据——这一格
        回答的是「哪个银河扫不到」，掺进保护期或重复攻击间隔就答成另一个问题了。
        """
        since = now_utc - window
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.BotTargetRow.galaxy, func.count().label("fresh"))
                .where(
                    orm.BotTargetRow.is_bot.is_(True),
                    orm.BotTargetRow.military_score_at_utc.is_not(None),
                    orm.BotTargetRow.military_score_at_utc >= since,
                )
                .group_by(orm.BotTargetRow.galaxy)
                .order_by(func.count().desc(), orm.BotTargetRow.galaxy)
            ).all()
        return tuple(GalaxyFreshness(galaxy=int(row.galaxy), fresh=int(row.fresh)) for row in rows)

    def score_age_hours_at_dispatch(self, *, since: datetime, until: datetime) -> tuple[float, ...]:
        """这段时间里每一发派出去时，目标军力读数已经多旧了（小时）。

        读的是 `attack_intents.target_military_score_at_utc` 这个**快照列**，
        绝不现取 `bot_targets.military_score_at_utc`：那一行每采一次军力榜就整行
        覆盖（生产实测 2026-08-18，同一批目标一天内从 31,756 刷到 2,616），
        现取答的是「它现在多新」，而这里要答的是「派出去那一刻多旧」。

        中位与最大由调用方算——把统计口径留在能被用例钉住的那一侧。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.AttackIntentRow.target_military_score_at_utc,
                )
                .join(
                    orm.AttackIntentRow,
                    orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                )
                .where(
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dispatched_at_utc >= since,
                    orm.AttackDispatchRow.dispatched_at_utc < until,
                    orm.AttackIntentRow.target_military_score_at_utc.is_not(None),
                )
            ).all()
        ages: list[float] = []
        for dispatched, scored in rows:
            at = _as_utc(dispatched)
            read_at = _as_utc(scored)
            if at is None or read_at is None:
                continue
            ages.append(max((at - read_at).total_seconds() / 3600.0, 0.0))
        return tuple(ages)

    # -- 周期统计 ---------------------------------------------------------------

    def period_counts(self, *, start: datetime, end: datetime) -> PeriodCounts:
        """一个周期里的计数类指标。**半开区间 `[start, end)`。**

        战报按 `reported_at_utc` 切，派遣按 `dispatched_at_utc` 切——两者是两个
        不同的时刻。拿同一列切会让「回收率」变成一个自己跟自己比的数。
        """
        with self._session_factory() as session:
            dispatches = int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.AttackDispatchRow)
                    .where(
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dispatched_at_utc >= start,
                        orm.AttackDispatchRow.dispatched_at_utc < end,
                    )
                )
                or 0
            )
            reports = int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.BattleReportRow)
                    .where(
                        orm.BattleReportRow.reported_at_utc >= start,
                        orm.BattleReportRow.reported_at_utc < end,
                    )
                )
                or 0
            )
            protection = int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.BotTargetRow)
                    .where(
                        orm.BotTargetRow.protection_seen_at_utc.is_not(None),
                        orm.BotTargetRow.protection_seen_at_utc >= start,
                        orm.BotTargetRow.protection_seen_at_utc < end,
                    )
                )
                or 0
            )
            coordinates = int(
                session.scalar(
                    select(func.count(func.distinct(_target_key())))
                    .select_from(orm.AttackDispatchRow)
                    .join(
                        orm.AttackIntentRow,
                        orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                    )
                    .where(
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dispatched_at_utc >= start,
                        orm.AttackDispatchRow.dispatched_at_utc < end,
                    )
                )
                or 0
            )
        return PeriodCounts(
            dispatches=dispatches,
            reports=reports,
            protection_hits=protection,
            coordinates=coordinates,
        )

    def resource_totals(self, *, start: datetime, end: datetime) -> tuple[ResourceTotal, ...]:
        """一个周期里 12 格各收了多少。**按战报时刻切，只覆盖已读回的战报。**

        ⚠️ **这个数是下界，而且过去某一天的数会一直涨。** 材料由游戏直接入账，
        读不读战报都拿到了——读战报只是我们知道的途径。实测 08-18 这一天，
        08-18 23:10 看到的是 33 份 / 67,594，08-19 10:10 看到的是 61 份 / 166,194
        （**隔 11 小时涨了 2.5 倍**）。所以页面上这一列**必须**和「读回战报数」
        并排显示（需求文档 8.4），否则用户明天再看同一天的数会以为出了 bug。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.BattleReportResourceRow.slot,
                    func.sum(orm.BattleReportResourceRow.amount).label("amount"),
                    func.max(cast(orm.BattleReportResourceRow.approximate, Integer)).label(
                        "approximate"
                    ),
                )
                .join(
                    orm.BattleReportRow,
                    orm.BattleReportRow.id == orm.BattleReportResourceRow.report_id,
                )
                .where(
                    orm.BattleReportRow.reported_at_utc >= start,
                    orm.BattleReportRow.reported_at_utc < end,
                )
                .group_by(orm.BattleReportResourceRow.slot)
                .order_by(orm.BattleReportResourceRow.slot)
            ).all()
        return tuple(
            ResourceTotal(
                slot=int(row.slot),
                amount=int(row.amount or 0),
                approximate=bool(row.approximate),
            )
            for row in rows
        )

    def occupancies(
        self, *, start: datetime, end: datetime, hold: timedelta, now_utc: datetime
    ) -> tuple[Occupancy, ...]:
        """与 `[start, end)` 有交集的那些航线占用段。

        每一段的结束时刻由 `domain.overview.occupancy_end` 按三档算（人工放手 →
        航线钟 → 派出 + `hold`），**与 `_still_holding_a_line` 逐条对应**。

        取行的下界放宽到 `start` 之前一段：占用段可能从窗口之前一直延伸进来
        （一发 22:30 派出、次日 00:40 回港的舰队在两天各占一段），按
        `dispatched_at_utc >= start` 取会把跨零点那一段整个漏掉。

        **一律钳到「现在」为止**：还没发生的占用不是产能。分母（`run_windows`）
        同样只算到现在，两边不同源的话，当天的利用率会在派出后立刻虚高一截。
        """
        floor = start - max(hold, timedelta(days=1))
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.AttackDispatchRow.line_free_at_utc,
                    orm.AttackDispatchRow.line_released_at_utc,
                ).where(
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dispatched_at_utc >= floor,
                    orm.AttackDispatchRow.dispatched_at_utc < end,
                )
            ).all()
        segments: list[Occupancy] = []
        for dispatched, free_at, released_at in rows:
            began = _as_utc(dispatched)
            if began is None:
                continue
            finished = min(
                occupancy_end(
                    dispatched_at_utc=began,
                    line_free_at_utc=_as_utc(free_at),
                    line_released_at_utc=_as_utc(released_at),
                    hold=hold,
                ),
                now_utc,
            )
            if finished <= began:
                continue
            segments.append(Occupancy(start=began, end=finished))
        return tuple(segments)

    def run_windows(
        self, *, start: datetime, end: datetime, now_utc: datetime, lines: int
    ) -> tuple[RunWindow, ...]:
        """与 `[start, end)` 有交集的调度器运行段，重叠的已经并好。

        `lines` 由调用方给（此刻配着的航线总数）。⚠️ **这是一个已知的近似**：
        `mission_runs` 里没有记当时的航线数，而航线数会变（道具能提升）。旧方案
        本来打算从「本轮固化记录」取，但那份记录落在控制台机器的一个 JSONL 文件
        里、不在库里，跨机读不到。所以越往前的周期这个分母越可能不准，
        页面上的「利用率」因此是趋势指标，不是精确值。

        还没结束的那一轮（`ended_at_utc IS NULL`）按「到现在为止」算，不按整段算：
        否则一轮刚起来，当天的可用航线时长就凭空多出几个小时。
        """
        floor = start - timedelta(days=31)
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.MissionRunRow.started_at_utc,
                    orm.MissionRunRow.ended_at_utc,
                ).where(
                    orm.MissionRunRow.started_at_utc >= floor,
                    orm.MissionRunRow.started_at_utc < end,
                )
            ).all()
        windows: list[RunWindow] = []
        for started, ended in rows:
            began = _as_utc(started)
            if began is None:
                continue
            finished = min(_as_utc(ended) or now_utc, now_utc)
            if finished <= began:
                continue
            windows.append(RunWindow(start=began, end=finished, lines=lines))
        return _merge_run_windows(windows, lines)

    # -- 资源图标 ---------------------------------------------------------------

    def latest_report_panel(self, *, width: int, height: int) -> bytes | None:
        """最新的一张**指定尺寸**的战报面板；库里没有就返回 None。

        `web.resource_icons` 拿它切三个稀有资源的图标——图标不进仓库，理由整段
        写在那个模块头上（公开仓库 + 游戏素材）。

        ⚠️ **尺寸必须对上才取**（标定的 520×695）。版面漂了之后旧尺寸的面板还在
        库里，随便拿一张来切等于在页面上摆三块切歪了的像素，而那种错很难看出来。

        只取一张、只在图标缓存冷启动时调一次：这一列是 blob，每次轮询都拉一张
        40KB 的 WEBP 是白烧。
        """
        with self._session_factory() as session:
            return session.scalar(
                select(orm.BattleReportScreenshotRow.image_bytes)
                .where(
                    orm.BattleReportScreenshotRow.width == width,
                    orm.BattleReportScreenshotRow.height == height,
                )
                .order_by(orm.BattleReportScreenshotRow.captured_at_utc.desc())
                .limit(1)
            )


def _target_key() -> ColumnElement[int]:
    """把目标坐标压成一个整数，好让 `count(distinct …)` 在两种方言上都能跑。

    `count(distinct (a, b, c))` 是 PostgreSQL 的写法，SQLite 上直接语法错误；
    而这个仓的用例在两种方言上都跑（`tests/support/database.py` 头上写着为什么）。
    位置最多三位、恒星系最多三位，乘出来不会串位。
    """
    return (
        orm.AttackIntentRow.target_galaxy * 1_000_000
        + orm.AttackIntentRow.target_system * 1_000
        + orm.AttackIntentRow.target_position
    )


def _merge_run_windows(windows: list[RunWindow], lines: int) -> tuple[RunWindow, ...]:
    """把重叠的运行段并起来。

    ⚠️ **不并的话分母会翻倍。** 同一时刻可以有好几行 `mission_runs`（军力榜扫描
    与 bot 攻击并存，抢占切换时前后两轮还会短暂重叠），而「可用航线时长」问的是
    **这台机器开着工的那段时间**乘航线数——同一分钟被两轮各算一次，产能就凭空
    多出一倍，利用率随之腰斩。
    """
    if not windows:
        return ()
    ordered = sorted(windows, key=lambda item: item.start)
    merged: list[RunWindow] = [ordered[0]]
    for window in ordered[1:]:
        last = merged[-1]
        if window.start <= last.end:
            if window.end > last.end:
                merged[-1] = RunWindow(start=last.start, end=window.end, lines=lines)
            continue
        merged.append(window)
    return tuple(merged)


def _as_utc(value: datetime | None) -> datetime | None:
    """把库里读出来的时刻钉成 aware 的 UTC。

    `UTCDateTime` 保证读出来是 aware 的（见 `storage/database.py`），但聚合函数
    （`min` / `max`）绕过那一层的类型装饰，在 SQLite 上会交出 naive 的值。
    naive 与 aware 一比就是 `TypeError`，而那在页面上表现为 500。
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "GalaxyFreshness",
    "OriginLineUsage",
    "OverviewRepository",
    "PeriodCounts",
    "ResourceTotal",
    "UnreadReports",
]
