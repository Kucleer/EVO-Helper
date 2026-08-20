"""「数据概览」页上的「按星球效率」那一段：今天哪个出发星球效率最高。

用户口径（2026-08-20）：

> 比如我想知道今天哪个星球的效率最高，用按天、按星球的资源收益去除他的航线数。

## 这一段的性质，和整页一样

1. **只读。** 一个写操作都没有，也不触发任何游戏动作。
2. **不依赖 runner。** 调度器停着时照常打开。
3. **不自己造判据。** 口径在 `domain.origin_efficiency`、SQL 在
   `storage.origin_efficiency`、航线数问 `MissionScheduler.configured_line_origins()`。
   这个模块只负责把它们拼起来交给模板。

## ⚠️ 为什么自己开一条路由，而不是挂进 `overview_routes` 的 `?fragment=`

这一段和「利用率 / 挂机时长」那一块在同一页上**同时开发**。挤进同一个函数的
话，两边的合并冲突会落在**判据**上（分子分母各自该取什么），而那是这一页最不
该被合并搞乱的地方。所以：自己的 domain 模块、自己的 SQL、自己的路由、
自己的模板片段；`overview.html` 那边只多一个容器和一次 fetch。

## 时区

**统计按 UTC+0 切天，页面上的时刻按 UTC+8 显示**（同整页的口径）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.battle_resources import slot_label
from evo_helper.domain.models import Coordinate
from evo_helper.domain.origin_efficiency import (
    LOW_RECOVERY_THRESHOLD,
    OriginEfficiency,
    build_rows,
    day_label,
    parse_day,
    selectable_days,
)
from evo_helper.domain.overview import BASIC_SLOTS, RARE_SLOTS
from evo_helper.domain.records import BattleResourceEntry
from evo_helper.storage.origin_efficiency import OriginEfficiencyRepository
from evo_helper.storage.overview import OverviewRepository
from evo_helper.web.display import resource_amount_text, resource_precision_hint

# ⚠️ 限流留痕那一套**直接 import 私名**，不抄一份等价的过来。这一页会轮询，
# 库一断就是每 N 秒一条日志（PR #188 修过一次同形状的事故：两条日志占了
# `system_log` 全表的 44%）。抄一份的话，下一次有人改限流间隔这一处不会跟着改。
from evo_helper.web.overview_routes import _FailureLog

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class OriginRow:
    """一行「按星球效率」在页面上长什么样。**模板里一个算术都不做。**"""

    origin: str
    #: 那一天这颗星球按几条航线算。
    lines: int
    #: 上面那个数是真值还是下界。假 ⇒ 分母偏小 ⇒ 两个效率数是**上界**，页面带「≤」。
    lines_exact: bool
    #: 当前配置里还有没有它。没有的（被删掉了）照样列出来——它当天真打出去过。
    in_config: bool
    #: 当前配置里是不是启用状态。**停用的照样出现在表里**：它当天真打出去过。
    enabled: bool
    dispatches: int
    reports: int
    recovery: float | None
    #: 稀有三样合计在页面上的写法（近似值带「约」）。
    rare_text: str
    rare_hint: str
    rare_amount: int
    on_duty_hours: float
    per_line: float | None
    per_line_hour: float | None
    #: 回收率低到足以让排序翻转（`domain.origin_efficiency.LOW_RECOVERY_THRESHOLD`）。
    untrustworthy: bool
    first_dispatch_at_utc: datetime | None
    last_dispatch_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class DayChoice:
    """日期选择器上的一格。"""

    #: `?origin_day=` 的取值。
    value: str
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class OriginEfficiencyView:
    day_start_utc: datetime
    day_label: str
    days: tuple[DayChoice, ...]
    rows: tuple[OriginRow, ...]
    #: 三样稀有资源的名字，**由 `slot_label` 翻译**——模板里不许另抄一份。
    rare_labels: tuple[str, ...]
    #: 三样基础资源的名字，给页脚那句「它们不进这个指标」用。同样由 `slot_label`
    #: 翻译：写死在模板里的话，改名那天页面上会念着两个不同的资源名。
    basic_labels: tuple[str, ...]
    #: 低回收率的门槛，给页面上那句说明用。
    low_recovery_threshold: float
    #: 这一趟有没有查失败。失败时给一条红条，而不是把 0 当成事实摆出来。
    failed: bool = False


def register_origin_efficiency_routes(app: FastAPI, session_factory: sessionmaker[Session]) -> None:
    """把「按星球效率」那一段挂上去。**只在持久化 app 上注册**（同
    `register_overview_routes`）：它要问真的调度器要航线配置，而假服务上没有那个对象。
    """
    repository = OriginEfficiencyRepository(session_factory)
    # ⚠️ 「那一天记下来的账号航线总数」直接问 `OverviewRepository.recorded_lines`，
    # **不在自己这边再写一遍那趟查询**：那个函数里压着好几条判据（NULL 是「不知道」、
    # 取一天里的最大值、孤儿轮次要连 `started_at_utc` 一起卡住下界），抄一份出去，
    # 下一次有人改判据这一处不会跟着改。
    overview = OverviewRepository(session_factory)
    templates: Jinja2Templates = app.state.templates
    failures = _FailureLog()
    router = APIRouter(tags=["overview"])

    def _scheduler(request: Request) -> MissionScheduler:
        return cast(MissionScheduler, request.app.state.mission_scheduler)

    @router.get("/overview/origin-efficiency", response_class=HTMLResponse, include_in_schema=False)
    async def origin_efficiency_fragment(
        request: Request, origin_day: str | None = None
    ) -> HTMLResponse:
        """这一段的片段。`origin_day` 认不出来时静默回落到今天，**不 422**：
        日期是几个链接，手改地址写错一位换来一页 JSON 报错，读起来就是
        「控制台坏了」（同 `domain.overview.parse_granularity` 那一段的理由）。
        """
        scheduler = _scheduler(request)
        # 「现在」取自调度器，不另取一次 `datetime.now()`：UTC 日切、在岗时长
        # 两件事都压在这个时刻上，两个差着一点的时刻会让页面和调度器量两把尺子。
        now = scheduler.now_utc()
        try:
            view = build_view(
                repository,
                overview=overview,
                scheduler=scheduler,
                now_utc=now,
                day=origin_day,
            )
        except Exception as error:  # noqa: BLE001 - 只读页面不许把控制台弄死
            failures.failed(where="按星球效率", error=error, now=now)
            _LOGGER.exception("数据概览「按星球效率」查询失败")
            view = _empty_view(now_utc=now, day=origin_day)
        else:
            failures.recovered(now=now)
        return templates.TemplateResponse(
            request=request,
            name="_overview_origins.html",
            context={"origins": view},
        )

    app.include_router(router)


def build_view(
    repository: OriginEfficiencyRepository,
    *,
    overview: OverviewRepository,
    scheduler: MissionScheduler,
    now_utc: datetime,
    day: str | None,
) -> OriginEfficiencyView:
    """组装这一段。

    ⚠️ **配置问 `configured_line_origins()`，含停用的那些**。停用的星球当天真把活
    打出去了（实测 2026-08-20 有一颗中途被自动停用），按 `enabled` 过滤会让那一行
    整个消失——而它恰恰是这张表最该解释的一行。

    ⚠️ **`hold` 一律问调度器要**（`unknown_line_hold()`），不许写死 90 分钟：
    那是用户在攻击配置页上能改的值，而线数的下界推算要靠它算占用段的末端。

    ⚠️ **「那一天这颗星球配着几条」这个数库里没有**，只能分两档给，判据在
    `domain.origin_efficiency.origin_lines`——那里也写着为什么不能拿此刻的配置
    去顶一个总数对不上的历史天。
    """
    day_start_utc = parse_day(day, now_utc=now_utc)
    day_end_utc = day_start_utc + timedelta(days=1)
    window_end = min(day_end_utc, now_utc)
    hold = scheduler.unknown_line_hold()
    configured: dict[Coordinate, tuple[int, bool]] = {
        item.coordinate: (item.fleet_lines, item.enabled)
        for item in scheduler.configured_line_origins()
    }
    facts = repository.origin_days(start=day_start_utc, end=window_end)
    rows = build_rows(
        facts,
        configured=configured,
        occupancies=repository.origin_occupancies(
            start=day_start_utc, end=window_end, hold=hold, now_utc=now_utc
        ),
        recorded_total=overview.recorded_lines(start=day_start_utc, end=window_end),
        day_start_utc=day_start_utc,
        now_utc=now_utc,
    )
    return OriginEfficiencyView(
        day_start_utc=day_start_utc,
        day_label=day_label(day_start_utc, now_utc=now_utc),
        days=_day_choices(now_utc=now_utc, selected=day_start_utc),
        rows=tuple(_row(item) for item in rows),
        rare_labels=tuple(slot_label(slot) for slot in RARE_SLOTS),
        basic_labels=tuple(slot_label(slot) for slot in BASIC_SLOTS),
        low_recovery_threshold=LOW_RECOVERY_THRESHOLD,
    )


def _row(item: OriginEfficiency) -> OriginRow:
    entry = BattleResourceEntry(
        # 「约」与误差范围两句话问 `display` 要，不在模板里现写（同
        # `overview_routes.ResourceCell`）。槽位随便取三样里的第一个——
        # 这一格是三样的**合计**，`slot` 只影响不了这两句话的措辞。
        slot=RARE_SLOTS[0],
        amount=item.day.rare_amount,
        approximate=item.day.rare_approximate,
        uncertainty=item.day.rare_uncertainty,
    )
    return OriginRow(
        origin=str(item.origin),
        lines=item.lines,
        lines_exact=item.lines_exact,
        in_config=item.in_config,
        enabled=item.enabled,
        dispatches=item.day.dispatches,
        reports=item.day.reports,
        recovery=item.recovery,
        rare_text=resource_amount_text(entry),
        rare_hint=resource_precision_hint(entry),
        rare_amount=item.day.rare_amount,
        on_duty_hours=item.on_duty_hours,
        per_line=item.per_line,
        per_line_hour=item.per_line_hour,
        untrustworthy=item.untrustworthy,
        first_dispatch_at_utc=item.day.first_dispatch_at_utc,
        last_dispatch_at_utc=item.day.last_dispatch_at_utc,
    )


def _day_choices(*, now_utc: datetime, selected: datetime) -> tuple[DayChoice, ...]:
    return tuple(
        DayChoice(
            value=f"{start:%Y-%m-%d}",
            label=day_label(start, now_utc=now_utc),
            selected=start == selected,
        )
        for start in selectable_days(now_utc)
    )


def _empty_view(*, now_utc: datetime, day: str | None) -> OriginEfficiencyView:
    """查询失败时摆出来的空视图，**页面上同时给一条红条**。

    把 0 当成事实摆出来比显示一句「读不到」糟得多：这一段的读者正是拿它判断
    「哪颗星球该关掉」。
    """
    day_start_utc = parse_day(day, now_utc=now_utc)
    return OriginEfficiencyView(
        day_start_utc=day_start_utc,
        day_label=day_label(day_start_utc, now_utc=now_utc),
        days=_day_choices(now_utc=now_utc, selected=day_start_utc),
        rows=(),
        rare_labels=tuple(slot_label(slot) for slot in RARE_SLOTS),
        basic_labels=tuple(slot_label(slot) for slot in BASIC_SLOTS),
        low_recovery_threshold=LOW_RECOVERY_THRESHOLD,
        failed=True,
    )


__all__ = [
    "DayChoice",
    "OriginEfficiencyView",
    "OriginRow",
    "build_view",
    "register_origin_efficiency_routes",
]
