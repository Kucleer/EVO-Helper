"""FastAPI application factory for the local EVO-Helper management UI."""

import os
from pathlib import Path
from typing import cast
from uuid import UUID

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate

from .schemas import (
    BotTargetOut,
    CoordinateModel,
    DashboardOut,
    FleetChangeOut,
    FleetDiffOut,
    FleetEntryOut,
    FleetSnapshotOut,
    RevisitIn,
    RevisitOut,
    RunStartIn,
    RunStatusOut,
    ScanPlanIn,
    ScanPlanOut,
    ScanPlanPatch,
    ScanRangeIn,
    ScanRangeOut,
    StateEventOut,
)
from .security import LocalSecurityMiddleware
from .service import (
    ApplicationService,
    BotTargetView,
    FakeApplicationService,
    FleetChangeView,
    FleetDiffView,
    FleetEntryView,
    FleetSnapshotView,
    NotFoundError,
    PlanPatchView,
    RevisitView,
    RunStatusView,
    ScanPlanView,
    ScanRangeView,
    ServiceError,
    StateEventView,
    _parse_coordinate,
    _parse_window,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"


def _default_token() -> str:
    return os.environ.get("EVO_HELPER_WEB_TOKEN", "local-evo-helper-token")


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


# ---- application factory --------------------------------------------------


def create_app(
    service: ApplicationService | None = None,
    settings: Settings | None = None,
    local_token: str | None = None,
) -> FastAPI:
    """Build the local web application.

    ``local_token`` defaults to ``EVO_HELPER_WEB_TOKEN`` or a development
    fallback; mutating requests must pass the same-origin check or this token.
    """

    app = FastAPI(title="EVO-Helper", version="0.1.0")
    app.state.service = service or FakeApplicationService()
    app.state.settings = settings or Settings()
    token = local_token or _default_token()
    app.add_middleware(LocalSecurityMiddleware, local_token=token)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

    def get_service(request: Request) -> ApplicationService:
        return cast(ApplicationService, request.app.state.service)

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ---- dashboard -------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"dashboard": service.dashboard()},
        )

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

    @app.get("/plans", response_class=HTMLResponse)
    async def plans_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="plans.html",
            context={"plans": [_plan_out(plan) for plan in service.list_plans()]},
        )

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

    @app.get("/targets", response_class=HTMLResponse)
    async def targets_page(request: Request) -> HTMLResponse:
        service = get_service(request)
        return templates.TemplateResponse(
            request=request,
            name="targets.html",
            context={"targets": [_target_out(t) for t in service.list_targets()]},
        )

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
    async def target_page(request: Request, coordinate: str) -> HTMLResponse:
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


__all__ = ["create_app"]
