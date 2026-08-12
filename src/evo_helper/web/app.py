"""FastAPI application factory for the local EVO-Helper management UI."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast
from urllib.parse import quote, urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Lifespan

from evo_helper.application.mission_freeze import DEFAULT_FREEZE_LOG, MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.config import Settings
from evo_helper.domain.intel_query import InvalidQueryError, parse_coordinate_span
from evo_helper.domain.models import Coordinate, CoordinateRange
from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scan_bounds import TOTAL_GALAXIES
from evo_helper.storage.repository import SqlAlchemyRepository

from .display import (
    BATTLE_RESULT_GLYPHS,
    BATTLE_RESULT_LABELS,
    BATTLE_RESULT_TONES,
    DISPATCH_STATE_GLYPHS,
    DISPATCH_STATE_LABELS,
    DISPATCH_STATE_TONES,
    LIST_SHIP_COLUMNS,
    MISSION_LABELS,
    STATUS_GLYPHS,
    STATUS_TONES,
    TARGET_KIND_GLYPHS,
    TARGET_KIND_TONES,
)

# 模块级导入（而不是留在 `create_persistent_app` 里）：`register_mission_routes`
# 的签名注解要在定义时求值，FastAPI 也要拿到真实的类去解依赖。
from .persistent_service import MissionConsoleService, PersistentApplicationService
from .schemas import (
    BotTargetOut,
    ConfigFreezeOut,
    CoordinateModel,
    CoordinateScanOut,
    CurrentMissionOut,
    DashboardOut,
    FleetChangeOut,
    FleetDiffOut,
    FleetEntryOut,
    FleetSnapshotOut,
    FrozenTaskOut,
    MissionTaskOut,
    MissionTaskPatch,
    RevisitIn,
    RevisitOut,
    ScanPlanIn,
    ScanPlanOut,
    ScanPlanPatch,
    ScanRangeIn,
    ScanRangeOut,
    SchedulerOut,
    StateEventOut,
    TierBandOut,
    TierThresholdsOut,
    TierThresholdsPatch,
)
from .security import LocalSecurityMiddleware, default_local_token
from .service import (
    ATTACK_LOG_RESULTS,
    DEFAULT_PLANET_KIND,
    PLANET_KINDS,
    SHANGHAI,
    ApplicationService,
    BotTargetView,
    ConfigFreezeView,
    FakeApplicationService,
    FleetChangeView,
    FleetDiffView,
    FleetEntryView,
    FleetSnapshotView,
    FrozenTaskView,
    MissionTaskView,
    NotFoundError,
    PlanPatchView,
    RevisitView,
    ScanPlanView,
    ScanRangeView,
    SchedulerView,
    ServiceError,
    StateEventView,
    TierThresholdsView,
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


def _blank_to_none(value: Any) -> Any:
    """把 `?galaxy=` 这种空串当成「没给这个参数」。

    星球列表页那个下拉框里，「全部银河系」那一项的 `value` 就是空串——浏览器提交
    表单时必然带上 `galaxy=`。而参数声明成 `int | None` 时 FastAPI 会拿空串去解析
    整数，直接 422：

        {"detail":[{"type":"int_parsing","loc":["query","galaxy"],
                    "msg":"Input should be a valid integer, ...","input":""}]}

    也就是说**这一页自带的默认筛选项点下去就报错**，还是 JSON 报错页，不是页面。
    翻页链接自己不会带空串（`page_url` 只在非 None 时才拼 `galaxy`），所以这个坑
    只有走表单才踩得到——而那正是用户会走的那条路。

    `offset` / `limit` 一并按这条处理：它们现在是下拉框，不会提交空串，但共享出去
    的链接被人手改成 `?limit=` 是同一个 422，而这类修补一处漏一处的成本比统一处理高。
    """
    return None if value == "" else value


#: 允许空串的整数查询参数。空串→None，由处理函数各自决定落回什么默认值。
BlankableInt = Annotated[int | None, BeforeValidator(_blank_to_none)]

#: 允许空串的日期查询参数（`YYYY-MM-DD`）。空串→None，也就是「不按日期筛」。
#:
#: 和 `BlankableInt` 同一个坑：攻击日志页的日期框清空之后，浏览器照样提交
#: `date=`，声明成 `date | None` 会当场 422。日期框天生就有「清空」这个动作，
#: 不像下拉框还能塞一个 `value=""` 的选项绕开，所以这一条是必须的。
BlankableDate = Annotated[date | None, BeforeValidator(_blank_to_none)]

#: 允许空串的文本查询参数。空串→None，也就是「这一格没填」。
#:
#: `str | None` 本身不会 422，但空串会一路走到解析函数那里变成一条错误提示，
#: 而用户看到的只是「我没填这一格」。攻击日志的坐标框是两个 `<input>`，
#: 提交表单必然带上 `target_start=&target_end=`。
BlankableStr = Annotated[str | None, BeforeValidator(_blank_to_none)]


def _target_span(start: str | None, end: str | None) -> CoordinateRange | None:
    """把攻击日志上那两个坐标框读成一个闭区间。

    只填一端时另一端跟着它走：填 `2:130` 就是「只看 2:130 这个星系」，而不是
    「从 2:130 到宇宙尽头」——一个人只填了起点，想的是那一个位置，不是半个宇宙。
    简写补位由 `parse_coordinate_span` 负责：起点补 1，终点补最后一位，所以
    `2:130` – `2:140` 覆盖 2:130:1 到 2:140:999，两端都含。
    """
    first = start or end
    last = end or start
    if first is None or last is None:
        return None
    return parse_coordinate_span(first, last)


def _ordered_outcomes(values: tuple[str, ...]) -> list[str]:
    """战果候选值排成人看得懂的顺序：胜、负、平、待战报，其余按原文排在后面。

    库里的 `DISTINCT` 只能给出字母序（`AWAITING` 会排到 `VICTORY` 前面），而
    这几档在用户心里是有固定次序的。认不出来的取值**照样列出来**，不丢：
    库里存的是画面原文，将来多一档的话，能不能筛得到比排得好看重要。
    """
    known = [value for value in BATTLE_RESULT_LABELS if value in values]
    return known + sorted(value for value in values if value not in BATTLE_RESULT_LABELS)


def _span_label(span: CoordinateRange | None) -> str:
    """把补位之后的区间写全给用户看。

    输入 `2:130` 补成 `2:130:1`，页面上必须显示补完的那一对——否则「为什么
    2:130:14 也在里面」这件事，用户只能从结果里反推。
    """
    return "" if span is None else f"{span.start} – {span.end}"


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
    # `tojson` 默认 `ensure_ascii=True`，中文会变成 `运行中`。
    # 调度台把八档状态文案当 JSON 传给页面脚本，转义之后既没法在浏览器里
    # 一眼看懂，也没法在测试里对着那八个词断言。Jinja 的 `tojson` 仍会转义
    # `<` `>` `&` `'`，放进 `<script>` 依然是安全的。
    templates.env.policies["json.dumps_kwargs"] = {"sort_keys": True, "ensure_ascii": False}
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates.env.globals["game_time"] = game_time
    templates.env.globals["local_time"] = local_time

    def get_service(request: Request) -> ApplicationService:
        return cast(ApplicationService, request.app.state.service)

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

    @app.get("/runs", include_in_schema=False)
    async def runs_page() -> RedirectResponse:
        """「运行详情」这一页已经关掉，起停与记账都在任务中心。

        它操作的是更早的 `run_instances` / `scan_plans` 那条路径：页面上「启动
        运行」建出来的运行实例没有任何人推进，三条 2026-08-07/09 的记录就一直
        卡在「扫描中」。真正在跑的是常驻调度器——起停走 `/api/scheduler`，
        每一轮记在 `mission_runs`，都在任务中心那一页上。

        留 307 而不是直接删路由：同 `/targets → /intel` 的先例，旧链接与书签
        不该变成 404。**`run_instances` / `scan_plans` 两张表照旧**——扫描链路
        （`tools/scan_coordinates.py`、`tools/pirate_loop.py` 的 `PLAN_NAME` /
        `RUN_KEY`）还在往里写，这次关掉的只是这一页。
        """
        return RedirectResponse("/missions", status_code=307)

    @app.get("/runs/{run_id}", include_in_schema=False)
    async def run_page(run_id: UUID) -> RedirectResponse:
        """运行实例的详情页只从上面那张表点进来，跟着一起关掉。"""
        return RedirectResponse("/missions", status_code=307)

    # `/api/runs` 底下已经一个接口都不剩了：
    #
    # - 「暂停 / 恢复 / 紧急停止」跟着 `run.html` 上那三个按钮一起删了（PR #101）。
    # - `POST /api/runs/start` 与 `GET /api/runs/{run_id}` 这次一起删。前者上一轮
    #   留着是因为「运行实例记账」，可它在页面关掉之后就没有调用方了，只剩测试
    #   拿它给 `attack_intents.run_id` 造外键；后者是它的读侧，`start` 一走，
    #   HTTP 客户端连 run_id 从哪来都没有了（列表接口 PR #101 已删），等于不可达。
    #
    # **表和记账机制照旧**：`run_instances` / `scan_plans` 仍由扫描与海盗链路
    # （`tools/scan_coordinates.py`、`tools/pirate_loop.py` 的 `PLAN_NAME` /
    # `RUN_KEY` 幂等键）建与推进，运行状态走 `SqlAlchemyRepository.set_run_state`，
    # 首页那块「进行中的运行」也照旧数 `run_instances`。这次删的只是没人调的 HTTP 口。

    # ---- targets / history ----------------------------------------------

    @app.get("/missions", response_class=HTMLResponse)
    async def missions_page(request: Request) -> HTMLResponse:
        """调度台。

        三行任务只在这里渲染出**壳**——名字、参数框、状态槽位。里面的每一个
        字（状态、随行事实、参数回显）都由 `/api/scheduler` 下发并由页面上那段
        轮询填进去。这不是偷懒：判据在页面上抄一份，就会出现「页面说的和调度器
        做的不是一回事」，而那种错静默、且只有在舰队白飞一趟之后才看得见。

        `mission_console` 用 `getattr` 取：它只挂在常驻 app 上
        （`create_persistent_app`），假服务那条路上没有库也没有调度器。取不到就
        渲染一张空的历史表，页面其余部分照常可用。
        """
        console = getattr(request.app.state, "mission_console", None)
        return templates.TemplateResponse(
            request=request,
            name="missions.html",
            context={
                "active": "missions",
                "mission_labels": MISSION_LABELS,
                "status_tones": STATUS_TONES,
                "status_glyphs": STATUS_GLYPHS,
                "runs": [] if console is None else console.recent_runs(limit=50),
                # 历次「开始」固化下来的配置。**本轮**那一份不在这里，它由
                # /api/scheduler 随状态一起下发——刚点完「开始」就要看得见，
                # 而这一段只有刷新整页才会变。
                "freezes": [] if console is None else console.recent_config_freezes(limit=20),
                # 那份记录落在磁盘上的什么地方。写出来是为了让「能查」不依赖
                # 控制台还开着——出事的时候用记事本也打得开。
                "freeze_log_path": None if console is None else console.freeze_log_path(),
            },
        )

    @app.get("/tiers", response_class=HTMLResponse)
    async def tiers_page(request: Request) -> HTMLResponse:
        """分档阈值。

        三个数在这里**渲染出初值**（不像调度台那样全靠轮询）：这一页没有任何
        随时间变的东西，一次 GET 就够，多一条 2 秒一次的轮询只会让用户正在输入
        的数字被顶掉。「现在改不改得动」是唯一会变的，页面上那个「保存」按钮
        撞上 409 时会把话说明白。

        `mission_console` 用 `getattr` 取（同 `missions_page`）：假服务那条路上
        没有库也没有调度器，取不到就渲染一张只读的默认值表。
        """
        console = getattr(request.app.state, "mission_console", None)
        view = None if console is None else console.tier_thresholds()
        return templates.TemplateResponse(
            request=request,
            name="tiers.html",
            context={"active": "tiers", "thresholds": view},
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
                # 标签与 chip 样式由服务端下发，页面脚本不再自己写一份中文——
                # 抄一份就会有一天页面上的档位和库里的取值对不上。
                "kind_labels": TARGET_KIND_LABELS,
                "kind_tones": TARGET_KIND_TONES,
                "kind_glyphs": TARGET_KIND_GLYPHS,
                "dispatch_labels": DISPATCH_STATE_LABELS,
                "dispatch_tones": DISPATCH_STATE_TONES,
                "dispatch_glyphs": DISPATCH_STATE_GLYPHS,
                "result_labels": BATTLE_RESULT_LABELS,
                "result_tones": BATTLE_RESULT_TONES,
                "result_glyphs": BATTLE_RESULT_GLYPHS,
            },
        )

    @app.get("/planets", response_class=HTMLResponse)
    async def planets_page(
        request: Request,
        galaxy: BlankableInt = None,
        kind: str = DEFAULT_PLANET_KIND,
        offset: BlankableInt = 0,
        limit: BlankableInt = DEFAULT_PLANET_PAGE_SIZE,
    ) -> HTMLResponse:
        """星球列表：按银河系与类型筛选，默认只看 bot。

        筛选与翻页都走查询参数，所以每种视图都有自己的可分享链接。
        取数在服务端分页——全量扫完是 71,856 颗星球，整表渲染不是选项。
        """
        service = get_service(request)
        if kind not in PLANET_KINDS:
            kind = DEFAULT_PLANET_KIND
        # 空串已经在 `BlankableInt` 那一层变成了 None，这里落回各自的默认值。
        limit = min(
            max(DEFAULT_PLANET_PAGE_SIZE if limit is None else limit, 1), MAX_PLANET_PAGE_SIZE
        )
        offset = max(offset or 0, 0)
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
    async def attack_log_page(
        request: Request,
        kind: str = "all",
        date: BlankableDate = None,
        target_start: BlankableStr = None,
        target_end: BlankableStr = None,
        preset: BlankableStr = None,
        result: BlankableStr = None,
        outcome: BlankableStr = None,
    ) -> HTMLResponse:
        """攻击日志：每一发打出去的舰队，游戏时间与现实时间并列。

        筛选走查询参数，所以「只看海盗」「只看 8 月 9 日」「只看 2:130–2:140」
        都有自己可分享的链接。

        `date` 按**游戏时间 UTC+0** 的自然日切，和表格第一列同一口径。拿现实时间
        UTC+8 的日期去切会把每天最早的八小时划到前一天——而那八小时正好压着
        海盗每日 32 次配额的边界，切错就等于把配额记到别的日子上。

        默认不按日期筛。默认「今天」在这一页是反的：UTC+0 的今天要到现实时间
        08:00 才开始，早上打开日志会看见一页空白，而空白读起来就是「昨晚一发没打」。

        **六个筛选一律下推到 SQL**（`list_attack_log` 的参数），不在取回
        `ATTACK_LOG_LIMIT` 条之后再挑：那样等于先砍掉历史再问历史，查一个旧坐标
        必得空页，而空页读起来就是「那个坐标没打过」。事件类型原先正是在内存里
        筛的，一并搬下去。

        顶上那三档快速筛选（预设 / 结果 / 战果）**按每一行自己的值判**。
        情报中心那三个同名的档不是一回事：那一页一行是一颗目标星球，所以才有
        「按最近一次派遣判」那套口径；这一页一行就是一次派遣，行里就写着答案。

        坐标解析失败**不返回 422**：这是一张 HTML 页，一页 JSON 报错读起来就是
        「控制台坏了」。改为照常渲染、在顶上挂一条红字说明「这一页没有按坐标筛」——
        默默地不筛才是最坏的一种，用户会以为下面那些行就是筛出来的结果。

        `preset` / `result` / `outcome` 的**空串一律当「不筛」**（`BlankableStr`）：
        三个下拉框的「全部」那一项 value 就是空串，浏览器提交表单必然带上
        `preset=&result=&outcome=`，声明成会解析的类型就是当场 422（PR #74）。
        """
        service = get_service(request)
        span: CoordinateRange | None = None
        span_error: str | None = None
        try:
            span = _target_span(target_start, target_end)
        except InvalidQueryError as exc:
            span_error = str(exc)
        # 认不出来的档当成没筛（同 `kind` 那一条）：这三个参数只有手改链接才
        # 可能写错，而报 422 会把一整页记录换成一页 JSON。当前生效的筛选写在
        # 筛选栏右侧那句话里，所以「没按它筛」不会是悄悄发生的。
        if result not in ATTACK_LOG_RESULTS:
            result = None
        options = service.attack_log_options()
        entries = service.list_attack_log(
            ATTACK_LOG_LIMIT,
            day_utc=date,
            kind=kind if kind in TARGET_KIND_LABELS else None,
            target_span=span,
            preset=preset,
            result=result,
            outcome=outcome,
        )

        def keep(**overrides: str) -> str:
            """带着当前的其余筛选拼链接——切换任何一档都不该把别的甩掉。"""
            params: dict[str, str] = {"kind": kind}
            if date is not None:
                params["date"] = date.isoformat()
            if target_start:
                params["target_start"] = target_start
            if target_end:
                params["target_end"] = target_end
            if preset:
                params["preset"] = preset
            if result:
                params["result"] = result
            if outcome:
                params["outcome"] = outcome
            params.update(overrides)
            return "/logs?" + urlencode({k: v for k, v in params.items() if v})

        chosen: list[str] = []
        if preset:
            chosen.append(f"预设 {preset}")
        if result:
            chosen.append(f"结果 {DISPATCH_STATE_LABELS.get(result, result)}")
        if outcome:
            chosen.append(f"战果 {BATTLE_RESULT_LABELS.get(outcome, outcome)}")

        return templates.TemplateResponse(
            request=request,
            name="logs.html",
            context={
                "active": "logs",
                "entries": entries,
                "kind": kind,
                "kind_labels": TARGET_KIND_LABELS,
                # bot 与海盗的 chip 样式**复用情报中心那一套**（PR #96）：
                # 同一个概念在两页上用两种色，比不上色更糟。
                "kind_tones": TARGET_KIND_TONES,
                "kind_glyphs": TARGET_KIND_GLYPHS,
                "limit": ATTACK_LOG_LIMIT,
                "day_value": date.isoformat() if date is not None else "",
                "target_start_value": target_start or "",
                "target_end_value": target_end or "",
                "span_label": _span_label(span),
                "span_error": span_error,
                "preset_value": preset or "",
                "result_value": result or "",
                "outcome_value": outcome or "",
                "preset_options": options.presets,
                "result_options": ATTACK_LOG_RESULTS,
                "outcome_options": _ordered_outcomes(options.outcomes),
                "quick_label": " · ".join(chosen),
                # 「结果」「战果」两列与两个下拉框共用同一套标签、色调、字形，
                # 页面上不再各写一份中文。
                "dispatch_labels": DISPATCH_STATE_LABELS,
                "dispatch_tones": DISPATCH_STATE_TONES,
                "dispatch_glyphs": DISPATCH_STATE_GLYPHS,
                "result_labels": BATTLE_RESULT_LABELS,
                "result_tones": BATTLE_RESULT_TONES,
                "result_glyphs": BATTLE_RESULT_GLYPHS,
                "kind_url": lambda value: keep(kind=value),
                "clear_date_url": keep(date=""),
                "clear_target_url": keep(target_start="", target_end=""),
                "clear_quick_url": keep(preset="", result="", outcome=""),
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


def _frozen_task_out(task: FrozenTaskView) -> FrozenTaskOut:
    return FrozenTaskOut(
        kind=task.kind,
        label=task.label,
        enabled=task.enabled,
        priority=task.priority,
        params=task.params,
        summary=task.summary,
    )


def _config_freeze_out(freeze: ConfigFreezeView) -> ConfigFreezeOut:
    return ConfigFreezeOut(
        frozen_at_utc=freeze.frozen_at_utc,
        tasks=[_frozen_task_out(task) for task in freeze.tasks],
        changes=list(freeze.changes),
        tier_thresholds=freeze.tier_thresholds,
    )


def _tier_thresholds_out(view: TierThresholdsView) -> TierThresholdsOut:
    return TierThresholdsOut(
        alpha_from=view.alpha_from,
        beta_from=view.beta_from,
        gamma_from=view.gamma_from,
        bands=[TierBandOut(preset=band.preset, span=band.span) for band in view.bands],
        locked=view.locked,
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
        config_locked=view.config_locked,
        frozen_config=(
            None if view.frozen_config is None else _config_freeze_out(view.frozen_config)
        ),
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

    @app.get("/api/tier-thresholds", response_model=TierThresholdsOut)
    def tier_thresholds(
        console: MissionConsoleService = Depends(get_console),
    ) -> TierThresholdsOut:
        return _tier_thresholds_out(console.tier_thresholds())

    @app.patch("/api/tier-thresholds", response_model=TierThresholdsOut)
    def patch_tier_thresholds(
        payload: TierThresholdsPatch,
        console: MissionConsoleService = Depends(get_console),
    ) -> TierThresholdsOut:
        """三个数一次全给。运行中 409，不递增 400。见 `patch_tier_thresholds`。"""
        return _tier_thresholds_out(
            console.patch_tier_thresholds(
                alpha_from=payload.alpha_from,
                beta_from=payload.beta_from,
                gamma_from=payload.gamma_from,
            )
        )


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

    # 主星在这里从 Settings 解析、往下注入：`domain` 不许 import `config`，
    # 所以 `domain.missions.ORIGIN` 只是默认值，真正的取值由这个组装点决定。
    # 解析失败要在建 app 时就炸——比舰队从错的星球飞出去之后再发现好得多。
    resolved = settings or Settings()
    scheduler = mission_scheduler or MissionScheduler(
        SqlAlchemyRepository(session_factory),
        MissionSupervisor(),
        origin=resolved.origin_coordinate,
        # 只有这里给固化记录一个真的文件。默认（`MissionScheduler` 自己建的
        # 那个）只留在内存里——往仓库里写文件必须是组装点明确决定的事，
        # 否则每一次跑测试都会在工作区里落下一个文件。
        freeze_log=MissionFreezeLog(DEFAULT_FREEZE_LOG),
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
        settings=resolved,
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
