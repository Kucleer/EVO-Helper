"""SQLite-backed implementation of the Web application-service seam."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.state_machine import require_transition
from evo_helper.storage import models as orm

from .service import (
    SHANGHAI,
    BotTargetView,
    ConflictError,
    DashboardView,
    FakeApplicationService,
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
)


class PersistentApplicationService:
    """Persist all Web management state through SQLAlchemy sessions."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        now_utc: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._now = now_utc or (lambda: datetime.now(UTC))

    def list_plans(self) -> list[ScanPlanView]:
        with self._session_factory() as session:
            rows = session.scalars(select(orm.ScanPlan).order_by(orm.ScanPlan.name)).all()
            return [self._plan_view(session, row) for row in rows]

    def get_plan(self, plan_id: UUID) -> ScanPlanView | None:
        with self._session_factory() as session:
            row = self._plan_row(session, plan_id)
            return self._plan_view(session, row) if row else None

    def create_plan(
        self,
        *,
        name: str,
        enabled: bool,
        window_start: time,
        window_end: time,
        dry_run: bool,
        ranges: tuple[ScanRangeView, ...],
        fleet_line_limit: int = 1,
        reserved_lines: int = 0,
    ) -> ScanPlanView:
        self._validate_plan(window_start, window_end, ranges)
        _validate_lines(fleet_line_limit, reserved_lines)
        now = self._now()
        with self._session_factory() as session:
            row = orm.ScanPlan(
                name=name,
                enabled=enabled,
                time_window_start=window_start.strftime("%H:%M"),
                time_window_end=window_end.strftime("%H:%M"),
                timezone_name="Asia/Shanghai",
                dry_run=dry_run,
                fleet_line_limit=fleet_line_limit,
                reserved_lines=reserved_lines,
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise ConflictError(f"plan named {name!r} already exists") from exc
            self._replace_ranges(session, row.id, ranges)
            session.commit()
            return self._plan_view(session, row)

    def update_plan(self, plan_id: UUID, patch: PlanPatchView) -> ScanPlanView:
        with self._session_factory() as session:
            row = self._required_plan(session, plan_id)
            current = self._plan_view(session, row)
            ranges = patch.ranges if patch.ranges is not None else current.ranges
            start = patch.window_start or current.window_start
            end = patch.window_end or current.window_end
            self._validate_plan(start, end, ranges)
            if patch.name is not None:
                row.name = patch.name
            if patch.enabled is not None:
                row.enabled = patch.enabled
            row.time_window_start = start.strftime("%H:%M")
            row.time_window_end = end.strftime("%H:%M")
            if patch.dry_run is not None:
                row.dry_run = patch.dry_run
            if patch.ranges is not None:
                self._replace_ranges(session, row.id, patch.ranges)
            row.updated_at_utc = self._now()
            try:
                session.commit()
            except IntegrityError as exc:
                raise ConflictError(f"plan named {row.name!r} already exists") from exc
            return self._plan_view(session, row)

    def delete_plan(self, plan_id: UUID) -> None:
        with self._session_factory() as session:
            row = self._required_plan(session, plan_id)
            if session.scalar(
                select(func.count())
                .select_from(orm.RunInstance)
                .where(orm.RunInstance.plan_id == row.id)
            ):
                raise ConflictError("cannot delete a plan that has run history")
            session.execute(delete(orm.ScanRangeRow).where(orm.ScanRangeRow.plan_id == row.id))
            session.delete(row)
            session.commit()

    def start_run(self, plan_id: UUID, idempotency_key: str) -> RunStatusView:
        with self._session_factory() as session:
            if session.scalar(
                select(orm.RunInstance).where(orm.RunInstance.idempotency_key == idempotency_key)
            ):
                raise ConflictError(f"idempotency_key already used: {idempotency_key}")
            plan = self._required_plan(session, plan_id)
            if not plan.enabled:
                raise ServiceError(f"plan {plan.name} is disabled")
            now = self._now()
            plan_view = self._plan_view(session, plan)
            state, target_date = self._schedule_state(plan_view, now)
            run = orm.RunInstance(
                id=uuid4(),
                plan_id=plan.id,
                idempotency_key=idempotency_key,
                target_date=datetime.combine(target_date, time.min, tzinfo=UTC),
                state=state.value,
                created_at_utc=now,
                started_at_utc=now if state is RunState.SCANNING else None,
            )
            session.add(run)
            session.flush()
            self._event(session, run.id, "started", None, state.value, now)
            session.commit()
            return self._run_view(session, run, plan.public_id)

    def get_run(self, run_id: UUID) -> RunStatusView | None:
        with self._session_factory() as session:
            row = session.get(orm.RunInstance, run_id)
            return self._run_view(session, row) if row else None

    def list_runs(self) -> list[RunStatusView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.RunInstance).order_by(orm.RunInstance.created_at_utc)
            ).all()
            return [self._run_view(session, row) for row in rows]

    def pause_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.PAUSED, "paused")

    def resume_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.ARMED, "resumed")

    def emergency_stop_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.EMERGENCY_STOPPED, "emergency_stopped")

    def list_targets(self) -> list[BotTargetView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.BotTargetRow)
                .where(orm.BotTargetRow.is_bot)
                .order_by(
                    orm.BotTargetRow.galaxy, orm.BotTargetRow.system, orm.BotTargetRow.position
                )
            ).all()
            return [
                BotTargetView(
                    Coordinate(row.galaxy, row.system, row.position),
                    row.latest_owner_name,
                    row.last_scanned_at_utc,
                    row.last_attack_at_utc,
                    row.last_dispatch_at_utc,
                    row.last_report_at_utc,
                )
                for row in rows
            ]

    def get_history(self, coordinate: Coordinate) -> list[FleetSnapshotView]:
        with self._session_factory() as session:
            reports = session.scalars(
                select(orm.BattleReportRow)
                .where(
                    orm.BattleReportRow.defender_target_galaxy == coordinate.galaxy,
                    orm.BattleReportRow.defender_target_system == coordinate.system,
                    orm.BattleReportRow.defender_target_position == coordinate.position,
                )
                .order_by(orm.BattleReportRow.reported_at_utc, orm.BattleReportRow.id)
            ).all()
            return [
                view
                for report in reports
                if (view := self._report_view(session, coordinate, report))
            ]

    def get_fleet_diff(self, coordinate: Coordinate) -> FleetDiffView | None:
        history = self.get_history(coordinate)
        if not history:
            return None
        before = history[-2] if len(history) > 1 else None
        return FakeApplicationService()._compute_diff(coordinate, before, history[-1])

    def list_events(self, limit: int) -> list[StateEventView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.StateEventRow)
                .order_by(orm.StateEventRow.occurred_at_utc.desc(), orm.StateEventRow.id.desc())
                .limit(limit)
            ).all()
            return [
                StateEventView(
                    row.id,
                    row.occurred_at_utc,
                    row.aggregate_type,
                    row.aggregate_id,
                    row.event,
                    row.before_state,
                    row.after_state,
                )
                for row in reversed(rows)
            ]

    def request_revisit(
        self, scope: str, reason: str, target_coordinate: Coordinate | None
    ) -> RevisitView:
        if scope == "target" and target_coordinate is None:
            raise ServiceError("target revisit requires target_coordinate")
        now = self._now()
        row = orm.TargetRevisitRow(
            id=uuid4(),
            scope=scope,
            reason=reason,
            target_galaxy=target_coordinate.galaxy if target_coordinate else None,
            target_system=target_coordinate.system if target_coordinate else None,
            target_position=target_coordinate.position if target_coordinate else None,
            requested_at_utc=now,
            status="PENDING",
        )
        with self._session_factory() as session:
            session.add(row)
            session.commit()
            return self._revisit_view(row)

    def list_revisits(self) -> list[RevisitView]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.TargetRevisitRow).order_by(orm.TargetRevisitRow.requested_at_utc)
            ).all()
            return [self._revisit_view(row) for row in rows]

    def dashboard(self) -> DashboardView:
        active = [
            state.value
            for state in (
                RunState.ARMED,
                RunState.SCANNING,
                RunState.WAITING_CAPACITY,
                RunState.DRAINING,
            )
        ]
        with self._session_factory() as session:
            return DashboardView(
                plan_count=session.scalar(select(func.count()).select_from(orm.ScanPlan)) or 0,
                active_run_count=session.scalar(
                    select(func.count())
                    .select_from(orm.RunInstance)
                    .where(orm.RunInstance.state.in_(active))
                )
                or 0,
                target_count=session.scalar(
                    select(func.count())
                    .select_from(orm.BotTargetRow)
                    .where(orm.BotTargetRow.is_bot)
                )
                or 0,
                pending_revisit_count=session.scalar(
                    select(func.count())
                    .select_from(orm.TargetRevisitRow)
                    .where(orm.TargetRevisitRow.status == "PENDING")
                )
                or 0,
            )

    def _transition(self, run_id: UUID, target: RunState, event: str) -> RunStatusView:
        with self._session_factory() as session:
            row = session.get(orm.RunInstance, run_id)
            if row is None:
                raise NotFoundError(f"run {run_id} not found")
            current = RunState(row.state)
            try:
                require_transition(current, target)
            except ValueError as exc:
                raise ConflictError(str(exc)) from exc
            now = self._now()
            row.state = target.value
            if target is RunState.EMERGENCY_STOPPED:
                row.finished_at_utc = now
            self._event(session, row.id, event, current.value, target.value, now)
            session.commit()
            return self._run_view(session, row)

    def _required_plan(self, session: Session, public_id: UUID) -> orm.ScanPlan:
        row = self._plan_row(session, public_id)
        if row is None:
            raise NotFoundError(f"plan {public_id} not found")
        return row

    @staticmethod
    def _plan_row(session: Session, public_id: UUID) -> orm.ScanPlan | None:
        return session.scalar(select(orm.ScanPlan).where(orm.ScanPlan.public_id == public_id))

    def _plan_view(self, session: Session, row: orm.ScanPlan) -> ScanPlanView:
        ranges = session.scalars(
            select(orm.ScanRangeRow)
            .where(orm.ScanRangeRow.plan_id == row.id)
            .order_by(orm.ScanRangeRow.priority, orm.ScanRangeRow.id)
        ).all()
        return ScanPlanView(
            row.public_id,
            row.name,
            row.enabled,
            time.fromisoformat(row.time_window_start),
            time.fromisoformat(row.time_window_end),
            row.dry_run,
            tuple(
                ScanRangeView(
                    Coordinate(item.start_galaxy, item.start_system, item.start_position),
                    Coordinate(item.end_galaxy, item.end_system, item.end_position),
                    Coordinate(item.origin_galaxy, item.origin_system, item.origin_position),
                    item.fleet_preset_name,
                    item.fleet_preset_signature,
                    item.priority,
                )
                for item in ranges
            ),
            row.created_at_utc,
            row.updated_at_utc,
            row.fleet_line_limit,
            row.reserved_lines,
        )

    def _run_view(
        self, session: Session, row: orm.RunInstance, public_id: UUID | None = None
    ) -> RunStatusView:
        plan = session.get(orm.ScanPlan, row.plan_id)
        if plan is None:  # pragma: no cover - database foreign key invariant
            raise NotFoundError(f"plan {row.plan_id} for run {row.id} not found")
        plan_id = public_id or plan.public_id
        return RunStatusView(
            row.id,
            plan_id,
            RunState(row.state),
            row.idempotency_key,
            row.target_date.date() if row.target_date else row.created_at_utc.date(),
            row.created_at_utc,
            row.started_at_utc,
            row.finished_at_utc,
        )

    @staticmethod
    def _validate_plan(start: time, end: time, ranges: tuple[ScanRangeView, ...]) -> None:
        if start > end:
            raise ServiceError("window_start must not be after window_end")
        for item in ranges:
            if item.end < item.start:
                raise ServiceError("range end must not precede its start")
            # The origin is deliberately not required to fall inside the range.
            # It is the player's own planet, which normally sits well outside
            # the coordinates being scanned.

    @staticmethod
    def _replace_ranges(session: Session, plan_id: int, ranges: tuple[ScanRangeView, ...]) -> None:
        session.execute(delete(orm.ScanRangeRow).where(orm.ScanRangeRow.plan_id == plan_id))
        for item in ranges:
            session.add(
                orm.ScanRangeRow(
                    plan_id=plan_id,
                    start_galaxy=item.start.galaxy,
                    start_system=item.start.system,
                    start_position=item.start.position,
                    end_galaxy=item.end.galaxy,
                    end_system=item.end.system,
                    end_position=item.end.position,
                    origin_galaxy=item.origin.galaxy,
                    origin_system=item.origin.system,
                    origin_position=item.origin.position,
                    fleet_preset_name=item.fleet_preset,
                    fleet_preset_signature=item.fleet_preset_signature,
                    priority=item.priority,
                )
            )

    @staticmethod
    def _schedule_state(plan: ScanPlanView, now: datetime) -> tuple[RunState, date]:
        local = now.astimezone(SHANGHAI)
        if local.time() < plan.window_start:
            return RunState.ARMED, local.date()
        if local.time() <= plan.window_end:
            return RunState.SCANNING, local.date()
        return RunState.ARMED, local.date() + timedelta(days=1)

    @staticmethod
    def _event(
        session: Session,
        run_id: UUID,
        event: str,
        before: str | None,
        after: str | None,
        now: datetime,
    ) -> None:
        session.add(
            orm.StateEventRow(
                aggregate_type="run",
                aggregate_id=run_id,
                event=event,
                before_state=before,
                after_state=after,
                occurred_at_utc=now,
            )
        )

    @staticmethod
    def _revisit_view(row: orm.TargetRevisitRow) -> RevisitView:
        coordinate = (
            Coordinate(row.target_galaxy, row.target_system, row.target_position)
            if row.target_galaxy is not None
            and row.target_system is not None
            and row.target_position is not None
            else None
        )
        return RevisitView(
            row.id, row.scope, row.reason, row.requested_at_utc, row.status, coordinate
        )

    @staticmethod
    def _report_view(
        session: Session, coordinate: Coordinate, report: orm.BattleReportRow
    ) -> FleetSnapshotView | None:
        rows = session.scalars(
            select(orm.FleetSnapshotRow)
            .where(
                orm.FleetSnapshotRow.report_id == report.id, orm.FleetSnapshotRow.side == "defender"
            )
            .order_by(orm.FleetSnapshotRow.ship_type)
        ).all()
        if not rows:
            return None
        ships = tuple(FleetEntryView(row.ship_type, row.count) for row in rows)
        return FleetSnapshotView(
            report.id,
            coordinate,
            report.reported_at_utc,
            "defender",
            sum(ship.quantity for ship in ships),
            report.is_from_revisit,
            report.match_confidence,
            report.manual_review_status,
            ships,
        )


def _validate_lines(fleet_line_limit: int, reserved_lines: int) -> None:
    """Reserving every line would make the plan unable to dispatch anything."""
    if reserved_lines >= fleet_line_limit:
        raise ServiceError(
            f"reserved_lines ({reserved_lines}) must be fewer than "
            f"fleet_line_limit ({fleet_line_limit}); the plan would never dispatch"
        )
