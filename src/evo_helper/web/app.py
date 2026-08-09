"""FastAPI application factory for the local EVO-Helper management UI."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Lifespan

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scan_bounds import TOTAL_GALAXIES
from evo_helper.storage.repository import SqlAlchemyRepository

from .display import LIST_SHIP_COLUMNS

# 模块级导入（而不是留在 `create_persistent_app` 里）：`register_mission_routes`
# 的签名注解要在定义时求值，FastAPI 也要拿到真实的类去解依赖。
from .persistent_service import MissionConsoleService, PersistentApplicationService
from .schemas import (
    BotTargetOut,
    CoordinateModel,
    CoordinateScanOut,
    CurrentMissionOut,
    DashboardOut,
    FleetChangeOut,
    FleetDiffOut,
    FleetEntryOut,
    FleetSnapshotOut,
    MissionTaskOut,
    MissionTaskPatch,
    RevisitIn,
    RevisitOut,
    RunStartIn,
    RunStatusOut,
    ScanPlanIn,
    ScanPlanOut,
    ScanPlanPatch,
    ScanRangeIn,
    ScanRangeOut,
    SchedulerOut,
    StateEventOut,
)
from .security import LocalSecurityMiddleware, default_local_token
from .service import (
    DEFAULT_PLANET_KIND,
    PLANET_KINDS,
    SHANGHAI,
    ApplicationService,
    BotTargetView,
    FakeApplicationService,
    FleetChangeView,
    FleetDiffView,
    FleetEntryView,
    FleetSnapshotView,
    MissionTaskView,
    NotFoundError,
    PlanPatchView,
    RevisitView,
    RunStatusView,
    ScanPlanView,
    ScanRangeView,
    SchedulerView,
    ServiceError,
    StateEventView,
    _parse_coordinate,
    _parse_window,
)

#: 星球列表的每页行数。全量扫完是 71,856 颗，整表渲染不是选项。
DEFAULT_PLANET_PAGE_SIZE = 200
MAX_PLANET_PAGE_SIZE = 1000
PLANET_PAGE_SIZES = (50, 200, 500, 1000)

#: 类型筛选的中文标签。`all` 不是一种星球，是「不过滤」。
PLANET_KIND_LABELS = (
    ("bot", "仅 bot"),
    ("owned", "有主（非 bot）"),
    ("free", "空位"),
    ("all", "全部"),
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

#: 调度器 tick 的间隔。一秒足够跟手（页面上的秒表也是一秒一跳），
#: 而每次 tick 只是几条本地 SQLite 查询，代价可以忽略。
MISSION_TICK_INTERVAL_S = 1.0

_LOGGER = logging.getLogger(__name__)


# ---- view model conversion ------------------------------------------------


def _coordinate_out(coordinate: Coordinate) -> CoordinateModel:
    return CoordinateModel(
        galaxy=coordinate.galaxy,
        system=coordinate.system,
        position=coordinate.position,
    )


def _range_out(scan_range: ScanRangeView) -> ScanRangeOut:
    return ScanRangeOut(
        start=_coordinate_out(scan_range.start),
        end=_coordinate_out(scan_range.end),
        origin=_coordinate_out(scan_range.origin),
        fleet_preset=scan_range.fleet_preset,
        fleet_preset_signature=scan_range.fleet_preset_signature,
        priority=scan_range.priority,
    )


def _plan_out(plan: ScanPlanView) -> ScanPlanOut:
    return ScanPlanOut(
        id=plan.id,
        name=plan.name,
        enabled=plan.enabled,
        window_start=plan.window_start.strftime("%H:%M"),
        window_end=plan.window_end.strftime("%H:%M"),
        dry_run=plan.dry_run,
        fleet_line_limit=plan.fleet_line_limit,
        reserved_lines=plan.reserved_lines,
        ranges=[_range_out(item) for item in plan.ranges],
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _range_view(item: ScanRangeIn) -> ScanRangeView:
    return ScanRangeView(
        start=Coordinate(item.start.galaxy, item.start.system, item.start.position),
        end=Coordinate(item.end.galaxy, item.end.system, item.end.position),
        origin=Coordinate(item.origin.galaxy, item.origin.system, item.origin.position),
        fleet_preset=item.fleet_preset,
        fleet_preset_signature=item.fleet_preset_signature,
        priority=item.priority,
    )


def _run_out(run: RunStatusView) -> RunStatusOut:
    return RunStatusOut(
        run_id=run.run_id,
        plan_id=run.plan_id,
        state=run.state.value,
        idempotency_key=run.idempotency_key,
        target_date=run.target_date.isoformat(),
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _target_out(target: BotTargetView) -> BotTargetOut:
    return BotTargetOut(
        coordinate=_coordinate_out(target.coordinate),
        latest_player=target.latest_player,
        last_scan_at=target.last_scan_at,
        last_attack_at=target.last_attack_at,
        last_dispatch_at=target.last_dispatch_at,
        last_report_at=target.last_report_at,
    )


def _fleet_entry_out(entry: FleetEntryView) -> FleetEntryOut:
    return FleetEntryOut(ship_type=entry.ship_type, quantity=entry.quantity)


def _snapshot_out(snapshot: FleetSnapshotView) -> FleetSnapshotOut:
    return FleetSnapshotOut(
        snapshot_id=snapshot.snapshot_id,
        coordinate=_coordinate_out(snapshot.coordinate),
        captured_at_utc=snapshot.captured_at_utc,
        side=snapshot.side,
        total=snapshot.total,
        is_revisit=snapshot.is_revisit,
        match_confidence=snapshot.match_confidence,
        review_status=snapshot.review_status,
        ships=[_fleet_entry_out(entry) for entry in snapshot.ships],
    )


def _change_out(change: FleetChangeView) -> FleetChangeOut:
    return FleetChangeOut(
        ship_type=change.ship_type,
        before=change.before,
        after=change.after,
        delta=change.delta,
        percent=change.percent,
    )


def _diff_out(diff: FleetDiffView) -> FleetDiffOut:
    return FleetDiffOut(
        coordinate=_coordinate_out(diff.coordinate),
        before=_snapshot_out(diff.before) if diff.before else None,
        after=_snapshot_out(diff.after),
        added=[_fleet_entry_out(entry) for entry in diff.added],
        removed=[_fleet_entry_out(entry) for entry in diff.removed],
        disappeared=list(diff.disappeared),
        first_seen=list(diff.first_seen),
        changes=[_change_out(change) for change in diff.changes],
        total_before=diff.total_before,
        total_after=diff.total_after,
    )


def _event_out(event: StateEventView) -> StateEventOut:
    return StateEventOut(
        event_id=event.event_id,
        occurred_at_utc=event.occurred_at_utc,
        aggregate=event.aggregate,
        aggregate_id=event.aggregate_id,
        event=event.event,
        from_state=event.from_state,
        to_state=event.to_state,
    )


def _revisit_out(revisit: RevisitView) -> RevisitOut:
    return RevisitOut(
        revisit_id=revisit.revisit_id,
        scope=revisit.scope,
        reason=revisit.reason,
        requested_at_utc=revisit.requested_at_utc,
        status=revisit.status,
        target_coordinate=(
            _coordinate_out(revisit.target_coordinate) if revisit.target_coordinate else None
        ),
    )


#: Run states grouped by tone for the status chips. Every chip also renders a
#: glyph and the state name, so colour is never the only signal.
_RUN_STATE_TONE = {
    "SCANNING": "ok",
    "DRAINING": "ok",
    "AWAITING_REPORT": "warn",
    "WAITING_SESSION": "warn",
    "COMPLETED": "ok",
    "ARMED": "warn",
    "WAITING_CAPACITY": "warn",
    "PAUSED": "warn",
    "FAILED": "danger",
    "EMERGENCY_STOPPED": "danger",
}

_RUN_STATE_GLYPH = {
    "SCANNING": "▶",
    "DRAINING": "▼",
    "AWAITING_REPORT": "🕗",
    "WAITING_SESSION": "🔑",
    "COMPLETED": "✓",
    "ARMED": "◷",
    "WAITING_CAPACITY": "⏸",
    "PAUSED": "⏸",
    "FAILED": "✕",
    "EMERGENCY_STOPPED": "■",
}


#: 运行状态的中文标签。界面只显示中文；英文常量仍是接口与数据库里的值。
_RUN_STATE_LABEL = {
    "DRAFT": "草稿",
    "ARMED": "待命",
    "SCANNING": "扫描中",
    "WAITING_CAPACITY": "等待航线",
    "DRAINING": "收取战报",
    "AWAITING_REPORT": "等待战报",
    "WAITING_SESSION": "等待登录",
    "COMPLETED": "已完成",
    "PAUSED": "已暂停",
    "FAILED": "已失败",
    "EMERGENCY_STOPPED": "已紧急停止",
}


#: 攻击日志一页显示多少条。日志是给人翻的，不是给人滚的。
ATTACK_LOG_LIMIT = 300


def game_time(moment: datetime | None) -> str:
    """游戏内时间。游戏一律按 UTC+0 显示（`vision.parsers.GAME_DISPLAY_ZONE`）。"""
    if moment is None:
        return "—"
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def local_time(moment: datetime | None) -> str:
    """现实时间，也就是用户的墙上时钟（UTC+8）。

    和游戏时间是**同一个瞬时的两种写法**，差 8 小时。日志上两个都写出来，
    是因为战报里的时间是游戏时间、而人回忆「我当时在干嘛」用的是现实时间——
    只给一个，另一个就得每次心算，迟早算错。
    """
    if moment is None:
        return "—"
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def _safe_back_url(back: str | None, default: str = "/planets") -> str:
    """把「返回」目标限制在本站内的相对路径。

    `back` 来自查询参数，也就是来自任何人都能构造的链接。原样塞进 href 就等于
    在本地控制台上开了一个跳转到站外的口子——`//evil.example` 和
    `https://evil.example` 都会被浏览器当成绝对地址。只放行以单个 `/` 开头的路径。
    """
    if not back:
        return default
    if not back.startswith("/") or back.startswith("//"):
        return default
    return back


def run_state_label(state: str) -> str:
    """未知状态回落到原值，宁可显示英文也不要显示空白。"""
    return _RUN_STATE_LABEL.get(state, state)


def run_state_tone(state: str) -> str:
    return _RUN_STATE_TONE.get(state, "")


def run_state_glyph(state: str) -> str:
    return _RUN_STATE_GLYPH.get(state, "•")


# ---- application factory --------------------------------------------------


def create_app(
    service: ApplicationService | None = None,
    settings: Settings | None = None,
    local_token: str | None = None,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Build the local web application.

    ``local_token`` defaults to ``EVO_HELPER_WEB_TOKEN`` or a development
    fallback; mutating requests must pass the same-origin check or this token.

    ``lifespan`` 走构造参数而不是事后往 ``app.router`` 上塞：常驻调度器的开机
    补行、每秒 tick、关机清子进程全挂在它上面，而 FastAPI 只在构造时读一次。
    """

    app = FastAPI(title="EVO-Helper", version="0.1.0", lifespan=lifespan)
    app.state.service = service or FakeApplicationService()
    app.state.settings = settings or Settings()
    token = local_token or default_local_token()
    app.add_middleware(LocalSecurityMiddleware, local_token=token)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates.env.globals["run_state_tone"] = run_state_tone
    templates.env.globals["run_state_glyph"] = run_state_glyph
    templates.env.globals["run_state_label"] = run_state_label
    templates.env.globals["game_time"] = game_time
    templates.env.globals["local_time"] = local_time

    def get_service(request: Request) -> ApplicationService:
        return cast(ApplicationService, request.app.state.service)

    def settings_for(request: Request) -> Settings:
        return cast(Settings, request.app.state.settings)

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- dashboard -------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        """The dashboard folded into 任务中心; keep the root a working entry."""
        return RedirectResponse("/missions", status_code=307)

    @app.get("/plans", include_in_schema=False)
    async def plans_page() -> RedirectResponse:
        """Plan configuration lives in 任务中心 now."""
        return RedirectResponse("/missions", status_code=307)

    @app.get("/api/dashboard", response_model=DashboardOut)
    async def api_dashboard(
        service: ApplicationService = Depends(get_service),
    ) -> DashboardOut:
        dashboard = service.dashboard()
        return DashboardOut(
            plan_count=dashboard.plan_count,
            active_run_count=dashboard.active_run_count,
            target_count=dashboard.target_count,
            pending_revisit_count=dashboard.pending_revisit_count,
        )

    # ---- plans -----------------------------------------------------------

    @app.get("/api/plans", response_model=list[ScanPlanOut])
    async def list_plans(
        service: ApplicationService = Depends(get_service),
    ) -> list[ScanPlanOut]:
        return [_plan_out(plan) for plan in service.list_plans()]

    @app.post("/api/plans", response_model=ScanPlanOut, status_code=201)
    async def create_plan(
        payload: ScanPlanIn,
        service: ApplicationService = Depends(get_service),
    ) -> ScanPlanOut:
        plan = service.create_plan(
            name=payload.name,
            enabled=payload.enabled,
            window_start=_parse_window(payload.window_start),
            window_end=_parse_window(payload.window_end),
            dry_run=payload.dry_run,
            fleet_line_limit=payload.fleet_line_limit,
            reserved_lines=payload.reserved_lines,
            ranges=tuple(_range_view(item) for item in payload.ranges),
        )
        return _plan_out(plan)

    @app.get("/api/plans/{plan_id}", response_model=ScanPlanOut)
    async def get_plan(
        plan_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> ScanPlanOut:
        plan = service.get_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"plan {plan_id} not found")
        return _plan_out(plan)

    @app.put("/api/plans/{plan_id}", response_model=ScanPlanOut)
    async def update_plan(
        plan_id: UUID,
        payload: ScanPlanPatch,
        service: ApplicationService = Depends(get_service),
    ) -> ScanPlanOut:
        patch = PlanPatchView(
            name=payload.name,
            enabled=payload.enabled,
            window_start=(_parse_window(payload.window_start) if payload.window_start else None),
            window_end=_parse_window(payload.window_end) if payload.window_end else None,
            dry_run=payload.dry_run,
            ranges=(
                tuple(_range_view(item) for item in payload.ranges)
                if payload.ranges is not None
                else None
            ),
        )
        return _plan_out(service.update_plan(plan_id, patch))

    @app.delete("/api/plans/{plan_id}", status_code=204)
    async def delete_plan(
        plan_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> None:
        service.delete_plan(plan_id)

    # ---- runs ------------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    async def runs_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="runs.html",
            context={
                "plans": [_plan_out(plan) for plan in service.list_plans()],
                "runs": [_run_out(run) for run in service.list_runs()],
            },
        )

    @app.post("/api/runs/start", response_model=RunStatusOut, status_code=201)
    async def start_run(
        payload: RunStartIn,
        service: ApplicationService = Depends(get_service),
    ) -> RunStatusOut:
        return _run_out(service.start_run(payload.plan_id, payload.idempotency_key))

    @app.get("/api/runs/{run_id}", response_model=RunStatusOut)
    async def get_run(
        run_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> RunStatusOut:
        run = service.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        return _run_out(run)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(request: Request, run_id: UUID) -> HTMLResponse:
        service = get_service(request)
        run = service.get_run(run_id)
        if run is None:
            raise NotFoundError(f"run {run_id} not found")
        return templates.TemplateResponse(
            request=request,
            name="run.html",
            context={"run": _run_out(run)},
        )

    @app.post("/api/runs/{run_id}/pause", response_model=RunStatusOut)
    async def pause_run(
        run_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> RunStatusOut:
        return _run_out(service.pause_run(run_id))

    @app.post("/api/runs/{run_id}/resume", response_model=RunStatusOut)
    async def resume_run(
        run_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> RunStatusOut:
        return _run_out(service.resume_run(run_id))

    @app.post("/api/runs/{run_id}/emergency-stop", response_model=RunStatusOut)
    async def emergency_stop_run(
        run_id: UUID,
        service: ApplicationService = Depends(get_service),
    ) -> RunStatusOut:
        return _run_out(service.emergency_stop_run(run_id))

    # ---- targets / history ----------------------------------------------

    @app.get("/missions", response_class=HTMLResponse)
    async def missions_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        dashboard = service.dashboard()
        return templates.TemplateResponse(
            request=request,
            name="missions.html",
            context={
                "active": "missions",
                "plans": [_plan_out(plan) for plan in service.list_plans()],
                "plan_count": dashboard.plan_count,
                "active_runs": dashboard.active_run_count,
                "target_count": dashboard.target_count,
                "pending_revisits": dashboard.pending_revisit_count,
                "default_preset": settings_for(request).default_fleet_preset,
                "default_preset_signature": settings_for(request).default_fleet_preset_signature,
            },
        )

    @app.get("/intel", response_class=HTMLResponse)
    async def intel_page(request: Request) -> HTMLResponse:
        """情报中心的筛选数据走 /api/intel/*，扫描结果随页面一起渲染。"""
        # 必须用 get_service(request)：外层的 service 是工厂的可选参数，
        # 直接引用会在未传入时是 None，而且类型上也不成立。
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="intel.html",
            context={
                # 坐标扫描表已经搬到 /planets：它要按银河系和类型筛选、按页取数，
                # 塞在这一页里只能整表渲染，最终必然重演「只渲染前 500 条却看着像全部」。
                "planet_total": service.count_scans(),
                "active": "intel",
                "list_ship_columns": list(LIST_SHIP_COLUMNS),
            },
        )

    @app.get("/planets", response_class=HTMLResponse)
    async def planets_page(
        request: Request,
        galaxy: int | None = None,
        kind: str = DEFAULT_PLANET_KIND,
        offset: int = 0,
        limit: int = DEFAULT_PLANET_PAGE_SIZE,
    ) -> HTMLResponse:
        """星球列表：按银河系与类型筛选，默认只看 bot。

        筛选与翻页都走查询参数，所以每种视图都有自己的可分享链接。
        取数在服务端分页——全量扫完是 71,856 颗星球，整表渲染不是选项。
        """
        service = get_service(request)
        if kind not in PLANET_KINDS:
            kind = DEFAULT_PLANET_KIND
        limit = min(max(limit, 1), MAX_PLANET_PAGE_SIZE)
        offset = max(offset, 0)
        page = service.list_planets(galaxy=galaxy, kind=kind, offset=offset, limit=limit)

        def page_url(new_offset: int) -> str:
            params = {"kind": kind, "limit": limit, "offset": new_offset}
            if galaxy is not None:
                params["galaxy"] = galaxy
            return "/planets?" + urlencode(params)

        return templates.TemplateResponse(
            request=request,
            name="planets.html",
            context={
                "page": SimpleNamespace(
                    rows=page.rows,
                    total=page.total,
                    offset=page.offset,
                    limit=page.limit,
                    kind_counts=page.kind_counts,
                    galaxy_counts=page.galaxy_counts,
                    galaxy=galaxy,
                    kind=kind,
                ),
                "all_galaxies": list(range(1, TOTAL_GALAXIES + 1)),
                "kind_labels": PLANET_KIND_LABELS,
                "page_sizes": PLANET_PAGE_SIZES,
                "default_kind": DEFAULT_PLANET_KIND,
                "back_query": quote(page_url(offset), safe=""),
                "prev_url": page_url(max(offset - limit, 0)) if offset > 0 else None,
                "next_url": page_url(offset + limit) if page.has_more else None,
                "active": "planets",
            },
        )

    @app.get("/targets", include_in_schema=False)
    async def targets_page() -> RedirectResponse:
        """The bot list became 情报中心, which can also filter by fleet."""
        return RedirectResponse("/intel", status_code=307)

    @app.get("/api/scans", response_model=list[CoordinateScanOut])
    async def list_scans(
        service: ApplicationService = Depends(get_service),
    ) -> list[CoordinateScanOut]:
        """列出坐标扫描事实，含空位与非 bot 归属。

        一次扫描的价值一半在于「这些坐标里没有 bot」，只返回 bot 会让
        空扫描看起来像什么都没发生。
        """
        return [
            CoordinateScanOut(
                coordinate=_coordinate_out(scan.coordinate),
                scanned_at_utc=scan.scanned_at_utc,
                owner_name=scan.owner_name,
                is_bot=scan.is_bot,
                confidence=scan.confidence,
            )
            for scan in service.list_scans()
        ]

    @app.get("/api/targets", response_model=list[BotTargetOut])
    async def list_targets(
        service: ApplicationService = Depends(get_service),
    ) -> list[BotTargetOut]:
        return [_target_out(target) for target in service.list_targets()]

    @app.get("/api/targets/{coordinate}/history", response_model=list[FleetSnapshotOut])
    async def get_history(
        coordinate: str,
        service: ApplicationService = Depends(get_service),
    ) -> list[FleetSnapshotOut]:
        parsed = _parse_coordinate(coordinate)
        return [_snapshot_out(s) for s in service.get_history(parsed)]

    @app.get("/api/targets/{coordinate}/diff", response_model=FleetDiffOut)
    async def get_fleet_diff(
        coordinate: str,
        service: ApplicationService = Depends(get_service),
    ) -> FleetDiffOut:
        parsed = _parse_coordinate(coordinate)
        diff = service.get_fleet_diff(parsed)
        if diff is None:
            raise NotFoundError(f"no snapshots for coordinate {coordinate}")
        return _diff_out(diff)

    @app.get("/targets/{coordinate}", response_class=HTMLResponse)
    async def target_page(
        request: Request, coordinate: str, back: str | None = None
    ) -> HTMLResponse:
        service = get_service(request)
        parsed = _parse_coordinate(coordinate)
        history = service.get_history(parsed)
        diff = service.get_fleet_diff(parsed)
        return templates.TemplateResponse(
            request=request,
            name="history.html",
            context={
                "coordinate": coordinate,
                "history": [_snapshot_out(s) for s in history],
                "diff": _diff_out(diff) if diff else None,
                "back_url": _safe_back_url(back),
            },
        )

    # ---- revisits / diagnostics -----------------------------------------

    @app.get("/api/revisits", response_model=list[RevisitOut])
    async def list_revisits(
        service: ApplicationService = Depends(get_service),
    ) -> list[RevisitOut]:
        return [_revisit_out(revisit) for revisit in service.list_revisits()]

    @app.post("/api/revisits", response_model=RevisitOut, status_code=201)
    async def request_revisit(
        payload: RevisitIn,
        service: ApplicationService = Depends(get_service),
    ) -> RevisitOut:
        target = (
            Coordinate(
                payload.target_coordinate.galaxy,
                payload.target_coordinate.system,
                payload.target_coordinate.position,
            )
            if payload.target_coordinate
            else None
        )
        return _revisit_out(service.request_revisit(payload.scope, payload.reason, target))

    @app.get("/logs", response_class=HTMLResponse)
    async def attack_log_page(request: Request, kind: str = "all") -> HTMLResponse:
        """攻击日志：每一发打出去的舰队，游戏时间与现实时间并列。

        筛选走查询参数，所以「只看海盗」有自己可分享的链接。
        """
        service = get_service(request)
        entries = service.list_attack_log(ATTACK_LOG_LIMIT)
        if kind in TARGET_KIND_LABELS:
            entries = [entry for entry in entries if entry.target_kind == kind]
        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={
                "active": "logs",
                "entries": entries,
                "kind": kind,
                "kind_labels": TARGET_KIND_LABELS,
                "limit": ATTACK_LOG_LIMIT,
            },
        )

    @app.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="diagnostics.html",
            context={
                "events": [_event_out(event) for event in service.list_events(200)],
                "revisits": [_revisit_out(r) for r in service.list_revisits()],
            },
        )

    @app.get("/api/diagnostics/events", response_model=list[StateEventOut])
    async def list_events(
        service: ApplicationService = Depends(get_service),
    ) -> list[StateEventOut]:
        return [_event_out(event) for event in service.list_events(200)]

    return app


def _mission_task_out(task: MissionTaskView) -> MissionTaskOut:
    return MissionTaskOut(
        kind=task.kind,
        label=task.label,
        enabled=task.enabled,
        priority=task.priority,
        params=task.params,
        status=task.status,
        detail=task.detail,
        summary=task.summary,
        disabled_reason=task.disabled_reason,
    )


def _scheduler_out(view: SchedulerView) -> SchedulerOut:
    return SchedulerOut(
        running=view.running,
        started_at_utc=view.started_at_utc,
        current=(
            None
            if view.current is None
            else CurrentMissionOut(
                kind=view.current.kind,
                label=view.current.label,
                started_at_utc=view.current.started_at_utc,
                log_path=view.current.log_path,
            )
        ),
        orphan_pid=view.orphan_pid,
        tasks=[_mission_task_out(task) for task in view.tasks],
    )


def register_mission_routes(app: FastAPI) -> None:
    """调度台的一组接口。

    只在持久化 app 上注册（同 `register_intel_routes`）：它需要一台真的调度器
    和一个真的库，`FakeApplicationService` 那条路上两样都没有。

    **全部写成同步 `def`**，让 FastAPI 把它们丢进线程池。这一组里的每个动作
    都会阻塞：查库、`terminate()` 之后还要 `wait(5)`。写成 `async def` 就是在
    事件循环里等那 5 秒，整台控制台连同页面一起卡住——lifespan 里那个
    `asyncio.to_thread` 挡的正是这件事。
    """

    def get_console(request: Request) -> MissionConsoleService:
        return cast(MissionConsoleService, request.app.state.mission_console)

    @app.get("/api/scheduler", response_model=SchedulerOut)
    def scheduler_state(
        console: MissionConsoleService = Depends(get_console),
    ) -> SchedulerOut:
        return _scheduler_out(console.scheduler_view())

    @app.post("/api/scheduler/start", response_model=SchedulerOut)
    def scheduler_start(
        console: MissionConsoleService = Depends(get_console),
    ) -> SchedulerOut:
        return _scheduler_out(console.start_scheduler())

    @app.post("/api/scheduler/stop", response_model=SchedulerOut)
    def scheduler_stop(
        console: MissionConsoleService = Depends(get_console),
    ) -> SchedulerOut:
        return _scheduler_out(console.stop_scheduler())

    @app.post("/api/scheduler/force-kill", response_model=SchedulerOut)
    def scheduler_force_kill(
        console: MissionConsoleService = Depends(get_console),
    ) -> SchedulerOut:
        """孤儿红条上的「强制结束」。**不按 pid 杀不认识的进程。**"""
        return _scheduler_out(console.force_kill())

    @app.patch("/api/missions/{kind}", response_model=MissionTaskOut)
    def patch_mission(
        kind: str,
        payload: MissionTaskPatch,
        console: MissionConsoleService = Depends(get_console),
    ) -> MissionTaskOut:
        return _mission_task_out(
            console.patch_mission(
                kind,
                enabled=payload.enabled,
                priority=payload.priority,
                params=payload.params,
            )
        )

    @app.post("/api/missions/BOT/new-round", response_model=MissionTaskOut)
    def restart_bot_round(
        console: MissionConsoleService = Depends(get_console),
    ) -> MissionTaskOut:
        return _mission_task_out(console.restart_bot_round())


async def _mission_tick_loop(scheduler: MissionScheduler, interval: float) -> None:
    """每秒问一次调度器该干什么。

    收退出码不能只在页面轮询时做——没人开着页面时，那条记录会一直挂在
    「运行中」，连续失败也就永远数不到三。

    `to_thread`：tick 会查 SQLite、起进程，停子进程时还要 `wait(5)`。放在事件
    循环里跑，那 5 秒会把整个控制台连同页面一起卡住。

    一次 tick 抛异常只记日志、不退出循环：这条循环一停，整台调度器就静默地
    再也不动了，而页面上仍然显示「运行中」——比多一行报错糟得多。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(scheduler.tick)
        except Exception:
            _LOGGER.exception("调度器 tick 失败，本轮跳过")


def create_persistent_app(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings | None = None,
    local_token: str | None = None,
    mission_scheduler: MissionScheduler | None = None,
    tick_interval_s: float = MISSION_TICK_INTERVAL_S,
) -> FastAPI:
    """Build the local Web UI against the SQLite-backed management service."""
    from .intel_routes import register_intel_routes

    scheduler = mission_scheduler or MissionScheduler(
        SqlAlchemyRepository(session_factory), MissionSupervisor()
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 开机：补齐三行任务与单行配置，并把上次没走正常关闭路径的行标成
        # UNKNOWN。**不按 pid 自动杀**——pid 会被系统回收复用，照着一个可能
        # 已经换了主人的号码开枪比留个警告更糟；页面上给红条和「强制结束」。
        app.state.mission_orphans = await asyncio.to_thread(scheduler.prepare)
        task = asyncio.create_task(_mission_tick_loop(scheduler, tick_interval_s))
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            # 主动清子进程，覆盖「正常重启」这条最常见的路径。不清的话，控制台
            # 关了，一个还在点鼠标的 runner 留在后台。
            await asyncio.to_thread(scheduler.shutdown)

    app = create_app(
        service=PersistentApplicationService(session_factory),
        settings=settings,
        local_token=local_token,
        lifespan=lifespan,
    )
    # 调度器的开关**不持久化**：这个对象每次建进程都是新的，一律停在「已停止」。
    app.state.mission_scheduler = scheduler
    app.state.mission_console = MissionConsoleService(
        SqlAlchemyRepository(session_factory), scheduler
    )
    register_mission_routes(app)
    # Intel search reads fleet snapshots straight from SQL, so it takes the
    # session factory rather than going through the application service.
    register_intel_routes(app, session_factory)
    return app


__all__ = ["create_app", "create_persistent_app"]
