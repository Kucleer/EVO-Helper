"""「数据概览」页：一眼看全此刻的状态、今天的收益、以及周期统计。

## 这一页的三条硬性质

1. **只读。** 一个写操作都没有，也不触发任何游戏动作（需求文档第七节）。
2. **不依赖 runner。** 调度器停着时照常打开——它读的全是库里已有的行。
3. **不自己造判据**（需求文档 8.7）。航线占用问
   `storage.overview`（那边直接用 `repository._still_holding_a_line`）、
   `hold` 问 `MissionScheduler.unknown_line_hold()`、航线数问
   `MissionScheduler.configured_line_origins()`、候选池问
   `MissionScheduler.military_candidate_pool()`。这一页凡是自己算过一遍的地方，
   原型评审时都算错过。

## 航线利用率与挂机运行时长是一对

分母自 2026-08-20 起是「**周期总时长 × 航线数**」（用户口径反转了 08-17 那条
「任务实际运行时间 × 航线数」）。于是分母不再替用户解释「为什么低」，
**「那段时间到底开没开工」改由「挂机运行时长」直说**（`domain.uptime`）。
两个数一起看才读得懂，所以它们在同一个 `_capacity` 里算出来、在页面上挨着摆。

⚠️ **线数分「真值」和「下界推算」两种，页面必须区分。** 真值来自
`mission_runs.configured_lines`（2026-08-20 才加的列）；那一列为空的历史天用
「当天最大并发在飞数」当下界，于是**分母偏小、利用率偏高**，页面上给那种格子
标一个「≤」并在悬停里说明方向。宁可说「这个数不准」，也不能让它假装准。

## 时区

**统计按 UTC+0 切天，页面上的时刻按 UTC+8 显示**（用户口径 2026-08-19）。
这会让某一天的数和用户按 `AT TIME ZONE 'Asia/Shanghai'` 手查的对不上——
合计一样、只有日切位置不同，**不是 bug**。页首写明了，不再额外解释。

## 刷新

「此刻」与「今天收益」几秒一轮，周期统计那张表每分钟一轮，两块各取各的片段
（`?fragment=`），而不是整页重取：整页重取会把周期统计那几趟聚合查询也按 5 秒
一次地跑起来。轮询的纪律（页面不可见就停表、上一次没回来就不发下一次）抄的是
`base.html` 里 `autoRefresh` 那一段，理由同样写在那里。
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_scheduler import ConfiguredOrigin, MissionScheduler
from evo_helper.domain.battle_resources import slot_label
from evo_helper.domain.flight_time import round_trip_hours
from evo_helper.domain.models import Coordinate
from evo_helper.domain.overview import (
    BASIC_SLOTS,
    COUNT_STATS_START_UTC,
    RARE_SLOTS,
    RESOURCE_STATS_START_UTC,
    Granularity,
    LineCount,
    available_seconds,
    day_start,
    line_slots,
    occupied_seconds,
    overflow_lines,
    parse_granularity,
    period_end,
    period_label,
    period_lines,
    period_starts,
    recovery_rate,
    resource_window,
    trim_empty_tail,
    utilisation,
)
from evo_helper.domain.records import BattleResourceEntry
from evo_helper.domain.target_order import ScoredTarget
from evo_helper.domain.uptime import partially_observed, uptime_seconds
from evo_helper.infrastructure.system_log import record_system_log
from evo_helper.storage.overview import (
    GalaxyFreshness,
    OriginLineUsage,
    OverviewRepository,
    ResourceTotal,
    UnreadReports,
)
from evo_helper.web.display import resource_amount_text, resource_precision_hint
from evo_helper.web.persistent_service import MissionConsoleService
from evo_helper.web.resource_icons import PANEL_SIZE, ResourceIconCache

_LOGGER = logging.getLogger(__name__)

#: ⚠️ **「新鲜」的窗口不在这里定，它读攻击配置里的有效期。**
#:
#: 从前这里是 `FRESHNESS_WINDOW = timedelta(hours=6)`，一个只有这一页认的数。
#: 用户口径（2026-08-26）：「统一为读取攻击配置，跟着配置走。我这里要看的就是
#: 动态数据来让我决策的」。走 `MissionScheduler.score_max_age()`
#: （`military_attack_config.score_max_age_hours`，页面上叫**有效期**）。
#:
#: 「此刻」区那三格（候选池按星系、星系质量、各银河新鲜读数）**共用这一个窗口**：
#: 它们摆在同一行给人横着比，窗口不一致的话比出来的结论是假的。

#: 候选池按往返时长分的档（**小时**上界，最后一档是「以上」）。
#:
#: 分档而不是给一个平均数：2026-08-19 发现候选池里 514/518 个都是跨银河，
#: 而平均数完全看不出这件事——近处不是被打空，是**没扫到**。
POOL_BUCKETS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)

#: 查询失败时往 `system_log` 写痕迹的最小间隔（秒）。
#:
#: ⚠️ **这一页会轮询**（此刻 5 秒一轮），库一断就是每 5 秒一条。PR #188 修过一次
#: 同形状的事故——当时两条日志占了 `system_log` 全表的 44%。先例是
#: `record_unrecognised_screen` 的 120 秒。
FAILURE_LOG_INTERVAL_S = 120.0


@dataclass(frozen=True, slots=True)
class SchedulerCard:
    """「此刻」区第一位（用户口径 2026-08-19）。"""

    running: bool
    started_at_utc: datetime | None
    current_label: str
    current_detail: str
    current_started_at_utc: datetime | None
    last_run_label: str
    last_run_ended_at_utc: datetime | None
    last_run_outcome: str


@dataclass(frozen=True, slots=True)
class LineCard:
    origin: str
    configured_lines: int
    holding: int
    unknown_duration: int
    next_free_at_utc: datetime | None
    slots: tuple[str, ...]
    overflow: int

    @property
    def full(self) -> bool:
        return self.holding >= self.configured_lines > 0


@dataclass(frozen=True, slots=True)
class LineTotals:
    """各星球那几张航线卡的合计（用户口径 2026-08-26：「增加总航线卡片显示合计情况」）。

    ⚠️ **从卡片本身加，不另走一趟查询。** 另查一份的话，「合计」与它下面那几张卡
    会在同一页上对不起来——而读这一页的人正是拿这两处判「此刻还能不能派」，
    一个和明细矛盾的合计比根本不显示合计更糟。同理它只加**页面真的画出来的**那
    几张卡：停用的那些在 `build_now_view` 就没进来，所以合计里也不会有
    （用户口径 2026-08-26 的第一条与第二条必须同口径）。
    """

    #: 参与合计的出发星球数——也就是下面画了几张卡。
    origins: int
    configured_lines: int
    holding: int
    #: 各星球「最早空出」里最早的那一个；一个都算不出时 None，页面写「—」。
    next_free_at_utc: datetime | None
    #: **每一颗星球都满**才算满。
    #:
    #: ⚠️ **不按「占用合计 ≥ 配置合计」判。** 那个式子在「一颗超配、另一颗全空」
    #: 时也成立，而那种时候明明还派得出去——航线是按星球各占各的，一颗星球上超
    #: 出来的占用填不平另一颗的空位（`configured_line_origins` 的合并语义）。
    all_full: bool

    @classmethod
    def of(cls, cards: Sequence[LineCard]) -> LineTotals:
        """把页面这一轮真的要画的那几张卡加起来。**入参就是模板遍历的那个元组。**"""
        moments = [card.next_free_at_utc for card in cards if card.next_free_at_utc is not None]
        return cls(
            origins=len(cards),
            configured_lines=sum(card.configured_lines for card in cards),
            holding=sum(card.holding for card in cards),
            next_free_at_utc=min(moments) if moments else None,
            all_full=bool(cards) and all(card.full for card in cards),
        )


@dataclass(frozen=True, slots=True)
class ResourceCell:
    """一个槽位在这个周期里的收获，以及它在页面上怎么写。

    ⚠️ **「约」和误差范围不在模板里现写。** 两句话都问
    `display.resource_amount_text` / `display.resource_precision_hint` 要——
    攻击日志页那一列也是它们渲染的，同一个概念在两页上写成两种样子，
    比两页都不标更让人犯迷糊（`logs.html` 那一段的理由）。
    """

    slot: int
    label: str
    amount: int
    approximate: bool
    #: 最大绝对误差（逐份战报相加，见 `storage.overview.ResourceTotal`）。
    uncertainty: int = 0

    @property
    def text(self) -> str:
        """页面上的那个数。近似值带「约」。"""
        return resource_amount_text(self._entry)

    @property
    def hint(self) -> str:
        """鼠标停上去那句：这个数准到什么程度。"""
        return resource_precision_hint(self._entry)

    @property
    def _entry(self) -> BattleResourceEntry:
        return BattleResourceEntry(
            slot=self.slot,
            amount=self.amount,
            approximate=self.approximate,
            uncertainty=self.uncertainty,
        )


@dataclass(frozen=True, slots=True)
class PoolBucket:
    label: str
    count: int


@dataclass(frozen=True, slots=True)
class GalaxyPool:
    """候选池在**一个银河**里的样子：按往返时长分档 + 一个成色系数。

    用户口径（2026-08-26）：「我关心的是每个星系内 0-30 分钟有多少、30-60 分钟有
    多少……如果近距离航线太少了，我会考虑减少航线数量」，以及后来那句
    「候选池 · 按星系 这里的数据范围也要新鲜度一致」。

    ⚠️ **只数有效期内有读数的目标。** 池子里还有几千个读数早就过期的，选靶那一步
    根本不认它们——把它们算进来，这一格会报出一个攻击链此刻用不上的数。
    """

    galaxy: int
    #: 这个银河配了几条航线；没配星球时 0。
    lines: int
    configured: bool
    enabled: bool
    #: 各档个数，与 `POOL_BUCKETS` 对齐（最后一项是「以上」那一档）。
    bands: tuple[int, ...]
    #: 成色：**每个 bot 各自**「军力 ÷ 往返小时」，再取均值。
    #:
    #: ⚠️ 用户口径（2026-08-26）：「记得你是需要按各个 bot 星球去单独做除法，
    #: 再合计。而不是反过来」——即 `mean(军力ᵢ ÷ 往返ᵢ)`，不是
    #: `总军力 ÷ 总往返`。后者会被一个又远又肥的目标拉偏，而选靶真正按的是前者
    #: （`domain.target_order` 里那条 `军力 ÷ 往返小时`）。
    quality: float | None
    #: 参与算成色的个数（取前 `window_floor` 个，不足就是全部）。
    counted: int


@dataclass(frozen=True, slots=True)
class TodayCard:
    rare: tuple[ResourceCell, ...]
    yesterday: dict[int, int]
    #: 金属 / 晶体 / 气体（`BASIC_SLOTS`），三样合在第四张卡里。
    basics: tuple[ResourceCell, ...]
    #: 今天这个窗口里**到底有没有收获记录**。
    #:
    #: ⚠️ 这个布尔值就是「0」和「不知道」的分界，不许省掉：入库是全有或全无
    #: （12 格但凡一格读不出，那份战报一行都不写），所以「有若干行、偏偏缺某一格」
    #: 只有一种解释——那一格读到了，是 0；而**一条收获记录都没有**时，
    #: 「全 0」与「这条链路根本没读过资源」在库里分不开，那时候写 0 就是拿
    #: 「不知道」冒充 0（判据与措辞同 `logs.html` 摘要那一段，PR #217 定的）。
    resources_seen: bool
    dispatches: int
    reports: int
    recovery: float | None
    protection_hits: int
    utilisation: float | None
    occupied_hours: float
    available_hours: float
    #: 分母按几条航线算，以及那个数是真值还是下界（见 `_Capacity`）。
    lines: int
    lines_exact: bool
    #: 挂机运行时长（小时）。**None = 这一段没有心跳观测**，页面写「—」而不是 0。
    uptime_hours: float | None
    #: 这一段只有一部分有心跳观测，因此挂机时长是下界。
    uptime_partial: bool
    score_age_median: float | None
    score_age_max: float | None


@dataclass(frozen=True, slots=True)
class PeriodRow:
    label: str
    dispatches: int
    reports: int
    recovery: float | None
    rare: tuple[ResourceCell, ...]
    utilisation: float | None
    #: 分母按几条航线算。
    lines: int
    #: ⚠️ **那个线数是真值还是下界。** 页面必须把两种天分开标：下界那些天的利用率
    #: 是**上界**（线数偏小 ⇒ 分母偏小 ⇒ 比值偏大），混在一起就是让一个不准的数
    #: 假装准。
    lines_exact: bool
    #: 挂机运行时长（小时）。**None = 那一段没有心跳观测**，写「—」不写 0。
    uptime_hours: float | None
    uptime_partial: bool
    is_total: bool

    @property
    def empty(self) -> bool:
        """整行没有任何事实。末尾连着的空行会被砍掉（`trim_empty_tail`）——
        数据只有几天时，「按月」那一档不该在页面上挂五行零。
        """
        return (
            self.dispatches == 0
            and self.reports == 0
            and all(cell.amount == 0 for cell in self.rare)
        )


@dataclass(frozen=True, slots=True)
class NowView:
    now_utc: datetime
    scheduler: SchedulerCard
    lines: tuple[LineCard, ...]
    #: 上面那几张卡的合计，摆在它们前面（用户口径 2026-08-26）。
    line_totals: LineTotals
    unread: UnreadReports
    pool_total: int
    pool_buckets: tuple[PoolBucket, ...]
    #: 候选池按银河拆开的那张卡。
    pool_galaxies: tuple[GalaxyPool, ...]
    #: 上面那张卡里参与统计的总数（有效期内有读数的），不是 `pool_total`。
    pool_fresh_total: int
    #: 各档的表头，与 `GalaxyPool.bands` 一一对应。
    pool_band_labels: tuple[str, ...]
    #: 那三格共用的窗口，页面照抄不解释——来自攻击配置的有效期。
    score_max_age_hours: float
    pool_unscored: int
    galaxies: tuple[GalaxyFreshness, ...]
    today: TodayCard
    #: 这一趟有没有查失败。失败时页面上给一条红条，而不是把 0 当成事实摆出来。
    failed: bool = False


class _FailureLog:
    """查询失败的限流留痕。**每 tick 可能触发的日志必须限流**（CLAUDE.md）。

    只在「状态发生变化」或者过了 `FAILURE_LOG_INTERVAL_S` 才写；恢复正常时补
    一条 INFO，好让排障的人在库里看得出这段红是什么时候开始、什么时候结束的。
    """

    def __init__(self, *, interval_s: float = FAILURE_LOG_INTERVAL_S) -> None:
        self._interval_s = interval_s
        self._failing = False
        self._last_logged_at: datetime | None = None

    def failed(self, *, where: str, error: BaseException, now: datetime) -> None:
        due = (
            self._last_logged_at is None
            or (now - self._last_logged_at).total_seconds() >= self._interval_s
        )
        if not self._failing or due:
            record_system_log(
                "ERROR",
                __name__,
                f"数据概览页查询失败：{where}",
                payload={
                    "where": where,
                    "error": repr(error),
                    # 出事时能不能只靠库里的日志定位，判据是「当时看到了什么」，
                    # 所以把这一趟用的时刻也记下来——按 UTC 切天的口径全靠它。
                    "now_utc": now.isoformat(),
                },
            )
            self._last_logged_at = now
        self._failing = True

    def recovered(self, *, now: datetime) -> None:
        if not self._failing:
            return
        record_system_log(
            "INFO",
            __name__,
            "数据概览页查询恢复正常",
            payload={"now_utc": now.isoformat()},
        )
        self._failing = False
        self._last_logged_at = None


def register_overview_routes(app: FastAPI, session_factory: sessionmaker[Session]) -> None:
    """把「数据概览」挂上去。**只在持久化 app 上注册**（同 `register_system_log_routes`）：
    它要读真的调度器（`hold`、航线配置、候选池），而假服务上没有那个对象。
    """
    repository = OverviewRepository(session_factory)
    templates: Jinja2Templates = app.state.templates
    icons = ResourceIconCache(
        lambda: repository.latest_report_panel(width=PANEL_SIZE[0], height=PANEL_SIZE[1])
    )
    app.state.overview_icons = icons
    now_failures = _FailureLog()
    period_failures = _FailureLog()
    router = APIRouter(tags=["overview"])

    def _scheduler(request: Request) -> MissionScheduler:
        return cast(MissionScheduler, request.app.state.mission_scheduler)

    def _console(request: Request) -> MissionConsoleService:
        return cast(MissionConsoleService, request.app.state.mission_console)

    def _now(request: Request) -> datetime:
        """这一页认的「现在」，**取自调度器**（`MissionScheduler.now_utc`）。

        另取一次 `datetime.now()` 的话，页面上的「还占着 3 条」和调度器下一步
        据以行动的判据会用两个差着一点的时刻——而航线占用、「最早空出」、
        UTC 日切三件事全都压在这个时刻上。同 `now_utc` 那一段的理由。
        """
        return _scheduler(request).now_utc()

    def _now_view(request: Request) -> NowView:
        now = _now(request)
        try:
            view = build_now_view(
                repository,
                scheduler=_scheduler(request),
                console=_console(request),
                now_utc=now,
            )
        except Exception as error:  # noqa: BLE001 - 只读页面不许把控制台弄死
            now_failures.failed(where="此刻", error=error, now=now)
            _LOGGER.exception("数据概览「此刻」查询失败")
            return _empty_now_view(now)
        now_failures.recovered(now=now)
        return view

    def _period_rows(request: Request, granularity: Granularity) -> tuple[list[PeriodRow], bool]:
        now = _now(request)
        try:
            rows = build_period_rows(
                repository,
                scheduler=_scheduler(request),
                granularity=granularity,
                now_utc=now,
            )
        except Exception as error:  # noqa: BLE001 - 同上
            period_failures.failed(where="周期统计", error=error, now=now)
            _LOGGER.exception("数据概览「周期统计」查询失败")
            return [], True
        period_failures.recovered(now=now)
        return rows, False

    @router.get("/overview", response_class=HTMLResponse, include_in_schema=False)
    async def overview_page(
        request: Request,
        granularity: str | None = None,
        fragment: str | None = None,
    ) -> HTMLResponse:
        """页面本体，以及两块可单独重取的片段。

        `granularity` 认不出来时静默回落到「按天」，**不 422**：档位切换是四个
        链接，手改地址写错一个字母换来一页 JSON 报错，读起来就是「控制台坏了」。
        """
        chosen = parse_granularity(granularity)
        if fragment == "now":
            return templates.TemplateResponse(
                request=request,
                name="_overview_now.html",
                context={"now": _now_view(request), "granularity": chosen},
            )
        rows: list[PeriodRow] = []
        failed = False
        if fragment != "now":
            rows, failed = _period_rows(request, chosen)
        if fragment == "periods":
            return templates.TemplateResponse(
                request=request,
                name="_overview_periods.html",
                context={"rows": rows, "granularity": chosen, "periods_failed": failed},
            )
        return templates.TemplateResponse(
            request=request,
            name="overview.html",
            context={
                "active": "overview",
                "now": _now_view(request),
                "rows": rows,
                "granularity": chosen,
                "periods_failed": failed,
                "granularities": tuple(Granularity),
                "count_start": COUNT_STATS_START_UTC,
                "resource_start": RESOURCE_STATS_START_UTC,
            },
        )

    @router.get("/api/overview/resource-icon/{slot}", include_in_schema=False)
    async def resource_icon(slot: int) -> Response:
        """一个稀有资源的图标，**运行时从库里的战报面板上切出来**。

        切不出来（没装 Pillow、库里还没有战报面板、版面漂了）返回 204，
        页面上那个 `<img>` 自己消失——图标是装饰，缺了不该变成一个破图标。
        """
        icon = icons.icon(slot)
        if icon is None:
            return Response(status_code=204)
        return Response(
            content=icon.image_bytes,
            media_type=icon.media_type,
            # 图标每个进程只切一次，值不会在进程内变；缓存一小时省掉每次开页
            # 的三次往返。不用 `immutable`：重启控制台之后它可能换成一张更新的
            # 面板切出来的，而那时候浏览器该重新问一次。
            headers={"Cache-Control": "private, max-age=3600"},
        )

    app.include_router(router)


def build_now_view(
    repository: OverviewRepository,
    *,
    scheduler: MissionScheduler,
    console: MissionConsoleService,
    now_utc: datetime,
) -> NowView:
    """组装「此刻」与「今天收益」。

    ⚠️ `hold` 一律问调度器要（`unknown_line_hold()`），**不许写死 90 分钟**：
    那是用户在攻击配置页上能改的值，写死之后他改成 45 分钟，页面会继续把一批
    早该放手的派遣画成「占着」（需求文档 8.1）。
    """
    hold = scheduler.unknown_line_hold()
    # ⚠️ **两级「停用」各挡一次**（用户口径 2026-08-26：「未启用的任务，不应显示在
    # 数据概览」）：`enabled_tasks_only` 挡的是任务自己那个复选框
    # （`mission_tasks.enabled`，只有调度器那一侧看得见那一行），`item.enabled`
    # 挡的是出发星球那个勾。少了前一道闸的症状用户截图过：整个军力任务的勾去掉
    # 之后，页面照旧给那几颗星球画着「0 / 1 条占用」的卡片——链路停着，页面上却
    # 像是活着只是这会儿没派。
    origins = [
        (item.coordinate, item.fleet_lines)
        for item in scheduler.configured_line_origins(enabled_tasks_only=True)
        if item.enabled
    ]
    usage = repository.line_usage(now_utc=now_utc, hold=hold, origins=origins)
    today_start = day_start(now_utc)
    # ⚠️⚠️ **候选池那张卡**故意用**没过滤**的那一份出发星球，和上面的航线卡片
    # **不同口径**。用户口径 2026-08-26：
    #
    #     「候选池不用跟着走，我就是根据候选池的情况来调整攻击航路以达到最大化」
    #
    # 也就是说这张卡是**决策用的**，不是「此刻在跑什么」的镜子：要决定「9 系那条
    # 航路值不值得开」，就得先看得见「开了之后有多少目标落进 30–60 分那一档」。
    # 跟着过滤的话，停用的星球一从分母里消失，那个问题就再也问不出来了——
    # 而那恰恰是用户停用它、又回来看这一页的原因。
    #
    # ⚠️ 所以下面这一行**不许**改成 `origins`。两处口径不同是有意的，不是漏改。
    reachable = [
        (item.coordinate, item.fleet_lines) for item in scheduler.configured_line_origins()
    ]
    pool = scheduler.military_candidate_pool()
    max_age = scheduler.score_max_age()
    pool_total, pool_buckets, pool_unscored = _pool_view(pool, reachable)
    # ⚠️ 「此刻」区那三格（候选池按星系、星系质量、各银河新鲜读数）**共用这一份**：
    # 它们摆在同一行给人横着比，各筛各的话比出来的结论是假的。
    fresh = _fresh_enough(pool, now_utc=now_utc, window=max_age)
    galaxies = _pool_by_galaxy(
        fresh,
        configured=scheduler.configured_line_origins(),
        enabled_tasks=scheduler.configured_line_origins(enabled_tasks_only=True),
        floor=scheduler.window_floor(),
    )
    cards = tuple(_line_card(item) for item in usage)
    return NowView(
        now_utc=now_utc,
        scheduler=_scheduler_card(console),
        lines=cards,
        line_totals=LineTotals.of(cards),
        unread=repository.unread_reports(now_utc=now_utc, day_start_utc=today_start),
        pool_total=pool_total,
        pool_buckets=pool_buckets,
        pool_unscored=pool_unscored,
        pool_galaxies=galaxies,
        pool_fresh_total=len(fresh),
        pool_band_labels=_bucket_labels(),
        score_max_age_hours=round(max_age.total_seconds() / 3600, 1),
        galaxies=_galaxy_freshness(fresh),
        today=_today_card(repository, now_utc=now_utc, today_start=today_start, hold=hold),
    )


def _line_card(usage: OriginLineUsage) -> LineCard:
    return LineCard(
        origin=str(usage.origin),
        configured_lines=usage.configured_lines,
        holding=usage.holding,
        unknown_duration=usage.unknown_duration,
        next_free_at_utc=usage.next_free_at_utc,
        slots=line_slots(
            configured_lines=usage.configured_lines,
            holding=usage.holding,
            unknown_duration=usage.unknown_duration,
        ),
        overflow=overflow_lines(configured_lines=usage.configured_lines, holding=usage.holding),
    )


def _scheduler_card(console: MissionConsoleService) -> SchedulerCard:
    """调度器那张卡。走 `scheduler_view()` 与 `recent_runs()`，两个都是现成的读口
    ——状态那句话由 `domain.scheduler.status_of` 判，这一页不自己解释任何事。
    """
    view = console.scheduler_view()
    current = view.current
    detail = ""
    if current is not None:
        for task in view.tasks:
            if task.task_id == current.task_id:
                detail = task.detail
                break
    runs = [run for run in console.recent_runs(limit=20) if run.ended_at_utc is not None]
    last = runs[0] if runs else None
    return SchedulerCard(
        running=view.running,
        started_at_utc=view.started_at_utc,
        current_label="" if current is None else current.label,
        current_detail=detail,
        current_started_at_utc=None if current is None else current.started_at_utc,
        last_run_label="" if last is None else last.label,
        last_run_ended_at_utc=None if last is None else last.ended_at_utc,
        last_run_outcome="" if last is None else _run_outcome(last.exit_code, last.stopped_by),
    )


def _run_outcome(exit_code: int | None, stopped_by: str | None) -> str:
    """上一轮是怎么结束的，一句人话。

    `exit_code` 为 None 而 `stopped_by` 有值时说「被停掉」——那两种结束在排障时
    完全不是一回事：非零退出是脚本自己栽了，被抢占是调度器换了任务。
    """
    if exit_code == 0:
        return "正常"
    if exit_code is not None:
        return f"退出码 {exit_code}"
    return {"USER": "手动结束", "PREEMPTED": "被抢占", "SHUTDOWN": "控制台关闭"}.get(
        stopped_by or "", "未知"
    )


def _fresh_enough(
    pool: Sequence[ScoredTarget], *, now_utc: datetime, window: timedelta
) -> tuple[ScoredTarget, ...]:
    """池子里**读数还在有效期内**的那些。

    ⚠️ 从同一个元组上分，**不是各查一遍库**。抄一份筛选出来，下次有人在攻击配置页
    加一条排除规则，那几格就又分岔了——而这种分岔是**看不出来的**：2026-08-26 实测
    「候选池」与「各银河新鲜读数」两格数字碰巧一模一样（891 = 891），因为刚扫到的
    bot 恰好都还在池子里。巧合不是保证。
    """
    since = now_utc - window
    return tuple(
        target
        for target in pool
        if target.military_score is not None
        and target.military_score_at_utc is not None
        and target.military_score_at_utc >= since
    )


def _pool_by_galaxy(
    fresh: Sequence[ScoredTarget],
    *,
    configured: Sequence[ConfiguredOrigin],
    enabled_tasks: Sequence[ConfiguredOrigin],
    floor: int,
) -> tuple[GalaxyPool, ...]:
    """候选池按银河拆开：各档个数 + 成色。

    ## 往返时长从哪颗星球算

    有本银河的出发星球就用它；**没配星球的银河按最近的那颗算**（同 `_pool_view`
    的口径）。那种银河的目标会整体落进最后一档——那正是实情：要跨银河飞过去。
    3 系、6 系今天就是这样，池子最大却一个近的都没有。

    ## ⚠️ 停用的银河照样列出来

    用户口径（2026-08-26）：「候选池不用跟着走，我就是根据候选池的情况来调整攻击
    航路以达到最大化」。要判断「9 系那条航路值不值得开」，就得先看得见它开了之后
    有多少目标、成色如何——跟着「未启用不显示」走的话，那个问题再也问不出来。
    """
    homes = {item.coordinate.galaxy: item for item in configured}
    on = {item.coordinate.galaxy for item in enabled_tasks if item.enabled}
    anywhere = [item.coordinate for item in configured]
    grouped: dict[int, list[ScoredTarget]] = {}
    for target in fresh:
        grouped.setdefault(target.coordinate.galaxy, []).append(target)

    def hours(target: ScoredTarget, galaxy: int) -> float:
        home = homes.get(galaxy)
        if home is not None:
            return round_trip_hours(target.coordinate, home.coordinate)
        return min(round_trip_hours(target.coordinate, other) for other in anywhere)

    out: list[GalaxyPool] = []
    for galaxy, targets in grouped.items():
        if not anywhere:
            # 一颗星球都没配时算不出往返时长，这一格整块交空——同 `_pool_view`。
            return ()
        bands = [0] * (len(POOL_BUCKETS) + 1)
        values: list[float] = []
        for target in targets:
            spent = hours(target, galaxy)
            bands[
                next(
                    (slot for slot, ceiling in enumerate(POOL_BUCKETS) if spent < ceiling),
                    len(POOL_BUCKETS),
                )
            ] += 1
            # ⚠️ **逐个 bot 各自相除**，不是总军力 ÷ 总往返。见 `GalaxyPool.quality`。
            values.append(cast(float, target.military_score) / spent)
        values.sort(reverse=True)
        taken = values[:floor] if floor > 0 else values
        out.append(
            GalaxyPool(
                galaxy=galaxy,
                lines=homes[galaxy].fleet_lines if galaxy in homes else 0,
                configured=galaxy in homes,
                enabled=galaxy in on,
                bands=tuple(bands),
                quality=statistics.mean(taken) if taken else None,
                counted=len(taken),
            )
        )
    # 按成色降序：这一格是拿来挑「该给谁加线」的，最肥的排最上面。
    # 并列按银河号，免得次序随字典序抖。
    return tuple(sorted(out, key=lambda item: (-(item.quality or 0.0), item.galaxy)))


def _galaxy_freshness(fresh: Sequence[ScoredTarget]) -> tuple[GalaxyFreshness, ...]:
    """各银河有几个新鲜读数。**从上面那一份分**，不另查一遍库。"""
    tally = Counter(target.coordinate.galaxy for target in fresh)
    return tuple(
        GalaxyFreshness(galaxy=galaxy, fresh=count)
        for galaxy, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _pool_view(
    pool: Sequence[ScoredTarget], origins: list[tuple[Coordinate, int]]
) -> tuple[int, tuple[PoolBucket, ...], int]:
    """候选池按**往返时长**分档。

    ⚠️ 池子本身问 `MissionScheduler.military_candidate_pool()`，页面不自己筛
    （需求文档 8.7）：排除近期打过的、刚撞过保护期的，这两条都是能在攻击配置页
    上改的策略，另算一份的话页面显示的池子和调度器下一轮真的会挑的不是同一个。

    往返时长按**最近的那颗出发星球**算：一个目标只要有一颗星球够得着就够得着，
    拿最远的那颗去分档会把它整体推到「跨银河」那一档里。
    """
    if not origins:
        return len(pool), (), sum(1 for item in pool if item.military_score is None)
    buckets = [0] * (len(POOL_BUCKETS) + 1)
    for target in pool:
        hours = min(round_trip_hours(target.coordinate, origin) for origin, _ in origins)
        index = next(
            (slot for slot, ceiling in enumerate(POOL_BUCKETS) if hours < ceiling),
            len(POOL_BUCKETS),
        )
        buckets[index] += 1
    labels = _bucket_labels()
    return (
        len(pool),
        tuple(PoolBucket(label=label, count=count) for label, count in zip(labels, buckets)),
        sum(1 for item in pool if item.military_score is None),
    )


def _bucket_labels() -> tuple[str, ...]:
    labels: list[str] = []
    previous = 0.0
    for ceiling in POOL_BUCKETS:
        labels.append(f"{_minutes(previous)}–{_minutes(ceiling)} 分")
        previous = ceiling
    labels.append(f"≥ {_minutes(previous)} 分")
    return tuple(labels)


def _minutes(hours: float) -> int:
    return int(round(hours * 60))


@dataclass(frozen=True, slots=True)
class _Capacity:
    """一个周期的航线产能账：占用多久、可用多久、按几条线算、以及机器开了多久。

    ⚠️ **这四样必须一起算、一起摆。** 分母自 2026-08-20 起是「周期总时长 × 航线
    数」（`domain.overview.available_seconds`），于是它不再替用户解释「为什么低」；
    「开没开工」改由挂机时长直说。少了后者，低利用率就分不出是「航线闲着」还是
    「机器根本没开」——而那正是旧口径拿运行时长当分母想要避免的事。
    """

    occupied_hours: float
    available_hours: float
    lines: LineCount
    utilisation: float | None
    uptime_hours: float | None
    uptime_partial: bool


def _capacity(
    repository: OverviewRepository,
    *,
    start: datetime,
    end: datetime,
    hold: timedelta,
    now_utc: datetime,
    observed_since: datetime | None,
) -> _Capacity:
    """一个周期的产能账。**「今天」那张卡和周期统计那张表走同一份实现。**

    ⚠️ 两处各算一遍的话，页面上会出现「今天 43%、按天那一行 91%」这种自相矛盾
    ——同一天、同一个名字、两个数（需求文档 8.7 那条「页面不许自己造判据」正是
    为这种事写的）。
    """
    occupancies = repository.occupancies(start=start, end=end, hold=hold, now_utc=now_utc)
    occupied = occupied_seconds(occupancies, start, end)
    lines = period_lines(
        # ⚠️ 真值只认 `mission_runs.configured_lines`；那一列为空时**不许**拿此刻
        # 配着的条数去顶（用户当天把 4 条加到 9 条），退回下界推算。
        recorded=repository.recorded_lines(start=start, end=end),
        occupancies=occupancies,
        window_start=start,
        window_end=end,
    )
    available = available_seconds(window_start=start, window_end=end, lines=lines.lines)
    uptime = uptime_seconds(
        repository.uptime_segments(start=start, end=end),
        observed_since=observed_since,
        window_start=start,
        window_end=end,
    )
    return _Capacity(
        occupied_hours=occupied / 3600.0,
        available_hours=available / 3600.0,
        lines=lines,
        utilisation=utilisation(occupied, available),
        uptime_hours=None if uptime is None else uptime / 3600.0,
        uptime_partial=partially_observed(observed_since=observed_since, window_start=start),
    )


def _today_card(
    repository: OverviewRepository,
    *,
    now_utc: datetime,
    today_start: datetime,
    hold: timedelta,
) -> TodayCard:
    counts = repository.period_counts(start=today_start, end=now_utc)
    haul = _haul(repository, start=today_start, end=now_utc)
    yesterday_start = today_start - timedelta(days=1)
    yesterday = _haul(repository, start=yesterday_start, end=today_start)
    capacity = _capacity(
        repository,
        start=today_start,
        # 右边界钳到「现在」，不是今天 24:00：占用段也只算到现在
        # （`storage.overview.occupancies`），两边不同源的话，早上八点打开页面
        # 会看到一个「除以 24 小时」的假低值。
        end=now_utc,
        hold=hold,
        now_utc=now_utc,
        observed_since=repository.first_uptime_beat(),
    )
    ages = repository.score_age_hours_at_dispatch(since=today_start, until=now_utc)
    return TodayCard(
        rare=haul.cells(RARE_SLOTS),
        yesterday={cell.slot: cell.amount for cell in yesterday.cells(RARE_SLOTS)},
        basics=haul.cells(BASIC_SLOTS),
        resources_seen=haul.seen,
        dispatches=counts.dispatches,
        reports=counts.reports,
        recovery=recovery_rate(counts.reports, counts.dispatches),
        protection_hits=counts.protection_hits,
        utilisation=capacity.utilisation,
        occupied_hours=capacity.occupied_hours,
        available_hours=capacity.available_hours,
        lines=capacity.lines.lines,
        lines_exact=capacity.lines.exact,
        uptime_hours=capacity.uptime_hours,
        uptime_partial=capacity.uptime_partial,
        score_age_median=statistics.median(ages) if ages else None,
        score_age_max=max(ages) if ages else None,
    )


@dataclass(frozen=True, slots=True)
class _Haul:
    """一个周期里 12 格的收获，**按槽位存**（名字是渲染时才翻译的解释）。"""

    totals: dict[int, ResourceTotal]

    @property
    def seen(self) -> bool:
        """这个周期里到底有没有收获记录。

        ⚠️ **这不等于「收成是 0」。** 库里只存非零的格子，所以一行都没有既可能是
        12 格全 0，也可能是这些战报根本没读过资源（存量战报全是后者），库里分不开
        （`storage.models.BattleReportResourceRow` 的注释）。反过来，只要有一行，
        「缺哪一格」就是确凿的 0——12 格是一起读的，读全了才入库。
        """
        return bool(self.totals)

    def cells(self, slots: tuple[int, ...]) -> tuple[ResourceCell, ...]:
        """挑出这几格。缺的那一格给 0——`seen` 为假时页面不该把这个 0 摆出来。"""
        return tuple(
            ResourceCell(
                slot=slot,
                label=slot_label(slot),
                amount=total.amount if total else 0,
                approximate=bool(total and total.approximate),
                uncertainty=total.uncertainty if total else 0,
            )
            for slot, total in ((slot, self.totals.get(slot)) for slot in slots)
        )


def _haul(repository: OverviewRepository, *, start: datetime, end: datetime) -> _Haul:
    """这个周期收了些什么。

    ⚠️ **资源类走自己那个起点**（`RESOURCE_STATS_START_UTC`，2026-08-18），
    与计数类的 2026-08-17 是两个日期、不许合并成一个常量：那 12 格的识别是
    08-18 才修好的，更早的战报根本没有资源明细。窗口整段落在起点之前时
    `resource_window` 返回 None，这里一行都取不到（`seen` 为假）——而同一个周期的
    **派遣数照常显示**，那正是两个起点分开的可观察后果。
    """
    window = resource_window(start, end)
    if window is None:
        return _Haul(totals={})
    rows = repository.resource_totals(start=window[0], end=window[1])
    return _Haul(totals={item.slot: item for item in rows})


def build_period_rows(
    repository: OverviewRepository,
    *,
    scheduler: MissionScheduler,
    granularity: Granularity,
    now_utc: datetime,
) -> list[PeriodRow]:
    """周期统计那张表。**比率一律先把分子分母各自求和再相除**（`recovery_rate`）。

    ⚠️ 利用率与「今天」那张卡走**同一个** `_capacity`：口径改了一处没改另一处，
    页面上就会出现「今天 43%、按天第一行 91%」这种自相矛盾。
    """
    hold = scheduler.unknown_line_hold()
    # 「心跳从什么时候起才有」是全库唯一的一条线，每行再问一次纯属白付。
    observed_since = repository.first_uptime_beat()
    rows: list[PeriodRow] = []
    for start in period_starts(now_utc, granularity):
        end = min(period_end(start, granularity, now=now_utc), now_utc)
        if end <= start:
            continue
        counts = repository.period_counts(start=start, end=end)
        rare = _haul(repository, start=start, end=end).cells(RARE_SLOTS)
        capacity = _capacity(
            repository,
            start=start,
            end=end,
            hold=hold,
            now_utc=now_utc,
            observed_since=observed_since,
        )
        rows.append(
            PeriodRow(
                label=period_label(start, granularity, now=now_utc),
                dispatches=counts.dispatches,
                reports=counts.reports,
                recovery=recovery_rate(counts.reports, counts.dispatches),
                rare=rare,
                utilisation=capacity.utilisation,
                lines=capacity.lines.lines,
                lines_exact=capacity.lines.exact,
                uptime_hours=capacity.uptime_hours,
                uptime_partial=capacity.uptime_partial,
                is_total=granularity is Granularity.TOTAL,
            )
        )
    return trim_empty_tail(rows, lambda row: row.empty)


def _empty_now_view(now: datetime) -> NowView:
    """查询失败时摆出来的空视图。**页面上会同时显示一条红条**——把 0 当成事实
    摆出来，比显示一句「读不到」糟得多：这一页的读者正是拿它判断「现在积压多少」。
    """
    return NowView(
        now_utc=now,
        scheduler=SchedulerCard(
            running=False,
            started_at_utc=None,
            current_label="",
            current_detail="",
            current_started_at_utc=None,
            last_run_label="",
            last_run_ended_at_utc=None,
            last_run_outcome="",
        ),
        lines=(),
        # 一张卡都没有，合计自然是空的；页面在没有卡片时也不画那张合计卡。
        line_totals=LineTotals.of(()),
        unread=UnreadReports(
            unread=0,
            in_flight=0,
            unknown_eta=0,
            dispatched_today=0,
            oldest_expected_at_utc=None,
        ),
        pool_total=0,
        pool_buckets=(),
        pool_unscored=0,
        # 查询整趟失败时这三格一个字都不写：宁可空着，也不许摆一个看着正常的 0。
        pool_galaxies=(),
        pool_fresh_total=0,
        pool_band_labels=(),
        score_max_age_hours=0.0,
        galaxies=(),
        today=TodayCard(
            rare=(),
            yesterday={},
            # 三样的名字照摆、数写「—」：这一趟根本没查到东西，
            # `resources_seen` 为假就是「不知道」，绝不是「今天一样没收着」。
            basics=_Haul(totals={}).cells(BASIC_SLOTS),
            resources_seen=False,
            dispatches=0,
            reports=0,
            recovery=None,
            protection_hits=0,
            utilisation=None,
            occupied_hours=0.0,
            available_hours=0.0,
            lines=0,
            # 这一趟什么都没查到，所以线数「不是真值」——页面照旧标它是推算的，
            # 而不是摆出一个像真值的 0。
            lines_exact=False,
            # ⚠️ None 而不是 0.0：0 小时的意思是「那段时间没开机」，而这里的事实
            # 是「这一趟没读到」。
            uptime_hours=None,
            uptime_partial=False,
            score_age_median=None,
            score_age_max=None,
        ),
        failed=True,
    )


__all__ = [
    "POOL_BUCKETS",
    "GalaxyPool",
    "LineCard",
    "LineTotals",
    "NowView",
    "PeriodRow",
    "ResourceCell",
    "SchedulerCard",
    "TodayCard",
    "build_now_view",
    "build_period_rows",
    "register_overview_routes",
]
