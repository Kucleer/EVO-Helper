"""SQLite-backed implementation of the Web application-service seam."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_scheduler import (
    MissionScheduler,
    SchedulerSnapshot,
    task_snapshot,
)
from evo_helper.domain.missions import (
    MissionParamError,
    bot_targets_in_range,
    pirate_systems,
)
from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.scheduler import (
    MissionKind,
    RunningProcess,
    TaskStatus,
    scheduling_order,
    status_of,
)
from evo_helper.domain.state_machine import require_transition
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

from .display import MISSION_LABELS
from .service import (
    SHANGHAI,
    AttackLogView,
    BotTargetView,
    ConflictError,
    CoordinateScanView,
    CurrentMissionView,
    DashboardView,
    FakeApplicationService,
    FleetDiffView,
    FleetEntryView,
    FleetSnapshotView,
    MissionRunView,
    MissionTaskView,
    NotFoundError,
    PlanetPage,
    PlanetRow,
    PlanPatchView,
    RevisitView,
    RunStatusView,
    ScanPlanView,
    ScanRangeView,
    SchedulerView,
    ServiceError,
    StateEventView,
    planet_kind,
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

    def list_planets(self, *, galaxy: int | None, kind: str, offset: int, limit: int) -> PlanetPage:
        """按银河系与类型筛选星球，**在 SQL 里筛、在 SQL 里数**。

        全量扫完是 71,856 颗星球。把它们全查出来再在 Python 里过滤，既慢又会诱使
        页面拿「本页行数」冒充总数——`list_scans` 的 500 条上限就是这么变成
        「扫描停在 2:32」的假象的。
        """
        with self._session_factory() as session:
            base = select(orm.BotTargetRow)
            if galaxy is not None:
                base = base.where(orm.BotTargetRow.galaxy == galaxy)

            counted = base.subquery()
            kind_counts = {
                "bot": 0,
                "owned": 0,
                "free": 0,
            }
            for is_bot, owner, count in session.execute(
                select(counted.c.is_bot, counted.c.latest_owner_name, func.count())
                .select_from(counted)
                .group_by(counted.c.is_bot, counted.c.latest_owner_name.is_(None))
            ):
                kind_counts[planet_kind(owner, bool(is_bot))] += int(count)
            kind_counts["all"] = sum(kind_counts.values())

            filtered = base
            clause = _planet_kind_clause(kind)
            if clause is not None:
                filtered = filtered.where(clause)

            total = int(session.scalar(select(func.count()).select_from(filtered.subquery())) or 0)
            rows = session.scalars(
                filtered.order_by(
                    orm.BotTargetRow.galaxy,
                    orm.BotTargetRow.system,
                    orm.BotTargetRow.position,
                )
                .offset(offset)
                .limit(limit)
            ).all()

            galaxy_counts = {
                int(g): int(count)
                for g, count in session.execute(
                    select(orm.BotTargetRow.galaxy, func.count())
                    .group_by(orm.BotTargetRow.galaxy)
                    .order_by(orm.BotTargetRow.galaxy)
                )
            }

        return PlanetPage(
            rows=tuple(
                PlanetRow(
                    coordinate=Coordinate(row.galaxy, row.system, row.position),
                    owner_name=row.latest_owner_name,
                    is_bot=bool(row.is_bot),
                    last_scan_at=row.last_scanned_at_utc,
                )
                for row in rows
            ),
            total=total,
            offset=offset,
            limit=limit,
            kind_counts=kind_counts,
            galaxy_counts=galaxy_counts,
        )

    def count_scans(self) -> int:
        """库里一共有多少条扫描事实。

        `list_scans` 有上限，页面必须能说出「显示的是全部还是一截」——
        只渲染前 500 条却不声明，看上去就像扫描停在了第 500 个坐标上。
        """
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(orm.CoordinateScanRow)) or 0)

    def list_scans(self, limit: int = 500) -> list[CoordinateScanView]:
        """按坐标顺序列出扫描事实。

        这里**不**过滤 bot：一次扫描的价值一半在于「这些坐标里没有 bot」，
        只显示 bot 会让空扫描看起来像什么都没发生。
        """
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.CoordinateScanRow)
                .order_by(
                    orm.CoordinateScanRow.galaxy,
                    orm.CoordinateScanRow.system,
                    orm.CoordinateScanRow.position,
                )
                .limit(limit)
            ).all()
            return [
                CoordinateScanView(
                    coordinate=Coordinate(row.galaxy, row.system, row.position),
                    scanned_at_utc=row.scanned_at_utc,
                    owner_name=row.owner_name,
                    is_bot=row.is_bot,
                    confidence=row.confidence,
                )
                for row in rows
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

    def list_attack_log(self, limit: int) -> list[AttackLogView]:
        """攻击日志：每条意图一行，派出去的带上派遣事实。

        用 `outerjoin` 而不是 `join`：**被闸门拦下、或者读简报没通过的意图
        没有对应的派遣行**，而这些恰恰是最需要在日志里看到的——
        内连接会把它们静默滤掉，日志看起来一片干净，实际是漏了。

        按意图创建时间倒序：日志页第一眼要看的是最近发生了什么。

        战报也是 `outerjoin` 接上来的：刚派出去的那一发还没有战报，而它必须照样在列。
        战报按 `dispatch_id` 接——那是仓储层做过时间与坐标核对之后写下的匹配结果，
        在这里按坐标重新配一次，等于把同一条判据写第二份。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.AttackIntentRow, orm.AttackDispatchRow, orm.BattleReportRow)
                .outerjoin(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .order_by(
                    orm.AttackIntentRow.created_at_utc.desc(),
                    orm.AttackIntentRow.id.desc(),
                )
                .limit(limit)
            ).all()
            return [
                AttackLogView(
                    intent_id=intent.id,
                    target=Coordinate(
                        intent.target_galaxy, intent.target_system, intent.target_position
                    ),
                    origin=Coordinate(
                        intent.origin_galaxy, intent.origin_system, intent.origin_position
                    ),
                    target_kind=intent.target_kind,
                    preset_name=intent.preset_name,
                    preset_signature=intent.preset_signature,
                    guard_status=intent.guard_status,
                    created_at_utc=intent.created_at_utc,
                    dispatched_at_utc=dispatch.dispatched_at_utc if dispatch else None,
                    accepted=dispatch.accepted if dispatch else None,
                    expected_report_at_utc=dispatch.expected_report_at_utc if dispatch else None,
                    outcome=report.outcome if report else None,
                    attacker_losses=report.attacker_losses if report else None,
                    defender_losses=report.defender_losses if report else None,
                )
                for intent, dispatch, report in rows
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
                orm.FleetSnapshotRow.report_id == report.id,
                orm.FleetSnapshotRow.side == "defender",
                # 只要战前的参战舰队。带 `round_no` 的是逐回合的剩余战舰，
                # 混进来会把同一个舰种数两遍：实测 2:137:14 的详情弹窗显示
                # 「合计 157」（= 参战 81 + 第1回合 76）、`重型战斗机` 出现两次，
                # 而列表页用的 `_defender_counts` 一直是对的（8 种 / 81）。
                # 同一条判据在两个地方各写一份，就会出现这种「列表对、详情错」。
                orm.FleetSnapshotRow.round_no.is_(None),
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


class MissionConsoleService:
    """调度台的读写口。页面和桌面悬浮窗都只跟它说话。

    它**不含任何调度判据**：起谁、为什么不起，全部问 `domain.scheduler`；
    参数合不合格，全部问调度器自己那段换算（`MissionScheduler.command_for`）。
    这一层只做三件事：把事实翻成人话、把 `MissionParamError` 翻成 400、
    把用户的按钮翻成调度器的方法调用。

    判据在这里抄一份的代价不是「重复代码」，是「页面说的和调度器做的不是
    一回事」——那种错静默、且只有在舰队白飞一趟之后才看得见。
    """

    def __init__(self, repository: SqlAlchemyRepository, scheduler: MissionScheduler) -> None:
        self._repository = repository
        self._scheduler = scheduler

    # -- 读 --------------------------------------------------------------------

    def scheduler_view(self) -> SchedulerView:
        return self._view(self._scheduler.snapshot())

    def recent_runs(self, *, limit: int = 50) -> list[MissionRunView]:
        """`mission_runs` 的近况，新的在前。

        `label` 在这里就翻好，页面不必再维护第二份 `kind → 中文名` 的表。
        `kind` 认不出来时回落到原值：宁可显示英文，也不要让一条脏数据把
        整张历史表打成 500。
        """
        return [
            MissionRunView(
                kind=row.kind,
                label=MISSION_LABELS.get(row.kind, row.kind),
                command=row.command,
                started_at_utc=row.started_at_utc,
                ended_at_utc=row.ended_at_utc,
                exit_code=row.exit_code,
                stopped_by=row.stopped_by,
                log_path=row.log_path,
            )
            for row in self._repository.mission_runs(limit=limit)
        ]

    # -- 开关 ------------------------------------------------------------------

    def start_scheduler(self) -> SchedulerView:
        self._scheduler.start()
        return self.scheduler_view()

    def stop_scheduler(self) -> SchedulerView:
        self._scheduler.stop()
        return self.scheduler_view()

    def force_kill(self) -> SchedulerView:
        """孤儿红条上的「强制结束」。

        只停我们自己认识的那个子进程，只闭合台账里没闭合的行。**绝不按 pid 去
        杀一个不认识的进程**——pid 会被系统回收复用，那一枪可能打在别人身上。
        """
        self._scheduler.force_kill()
        return self.scheduler_view()

    # -- 改 --------------------------------------------------------------------

    def patch_mission(
        self,
        kind_text: str,
        *,
        enabled: bool | None = None,
        priority: int | None = None,
        params: dict[str, int] | None = None,
    ) -> MissionTaskView:
        """改开关 / 参数 / 优先级。三样各自独立，`None` 表示这次不动它。"""
        kind = self._kind(kind_text)
        if kind is MissionKind.SCAN and priority is not None:
            # 领域层的排序键已经把扫描结构性地钉在最后一位，所以收下这个值也
            # 不会真的改变次序——正因为如此才必须拒绝：默默收下一个不起作用的
            # 写入，页面会显示成「排序已保存」，刷新后又弹回去，用户只能得出
            # 「这个控件坏了」。理由要说出口：扫描永远有活干，排在它后面的
            # 链路就永远轮不到，当天 32 次配额会无声流失。
            raise ServiceError("扫描恒在最后一位（它永远有活干，排它后面的链路就永远轮不到）")
        if kind is MissionKind.SCAN and params:
            raise ServiceError("扫描不吃参数：它自己维护扫描计划与游标")

        row = self._row(kind)
        params_json = None if params is None else json.dumps(params, ensure_ascii=False)
        # 校验的时机有两个：动了参数，或者这一下是在**启用**它。
        # 后者不能省——先存一个空范围、再单独勾复选框，就绕过去了。
        # 只改 priority、或者要**关掉**它时不校验：参数填错了还关不掉，
        # 那就真的没退路了。
        if params is not None or enabled is True:
            self._validate(kind, params_json or row.params_json)
        self._repository.update_mission_task(
            kind, enabled=enabled, priority=priority, params_json=params_json
        )
        return self._task_view_for(kind)

    def restart_bot_round(self) -> MissionTaskView:
        """「重开一轮」：把 `round_started_at_utc` 推到当前。

        bot 打完一轮就退出调度，**不自动开下一轮**——自动开就等于没人看着的
        时候一直派舰队。开新一轮只能是用户按下的这一下。
        """
        self._scheduler.begin_bot_round()
        return self._task_view_for(MissionKind.BOT)

    # -- 内部 ------------------------------------------------------------------

    def _validate(self, kind: MissionKind, params_json: str) -> None:
        """用调度器自己那把尺子量一遍，量不过就 400。

        走 `command_for` 而不是在这里重写几条 if：两边一旦分家，就会出现
        「页面收下了、调度器起不来」——而调度器起不来时只会把任务自动停用，
        用户要等到下次看页面才发现。
        """
        try:
            self._scheduler.command_for(kind, params_json)
        except MissionParamError as exc:
            raise ServiceError(str(exc)) from exc

    def _view(self, snapshot: SchedulerSnapshot) -> SchedulerView:
        running = snapshot.running
        tasks = [row for row in snapshot.tasks if row.kind in MISSION_LABELS]
        # 展示次序用领域层那把尺子，页面上排第一的就是下一个会被起的那条。
        tasks.sort(key=lambda row: (*scheduling_order(task_snapshot(row)), row.id))
        return SchedulerView(
            running=snapshot.enabled,
            started_at_utc=snapshot.started_at_utc,
            current=(
                None
                if running is None
                else CurrentMissionView(
                    kind=running.kind.value,
                    label=MISSION_LABELS[running.kind.value],
                    started_at_utc=running.started_at_utc,
                    log_path=str(running.log_path),
                )
            ),
            orphan_pid=snapshot.orphan_pid,
            tasks=tuple(self._task_view(row, snapshot) for row in tasks),
        )

    def _task_view(self, row: orm.MissionTaskRow, snapshot: SchedulerSnapshot) -> MissionTaskView:
        kind = MissionKind(row.kind)
        running = snapshot.running
        status = status_of(
            task_snapshot(row),
            snapshot.facts,
            running=(
                None
                if running is None
                else RunningProcess(kind=running.kind, started_at_utc=running.started_at_utc)
            ),
            restart_cooldown=timedelta(seconds=snapshot.config.restart_cooldown_seconds),
        )
        params = _int_params(row.params_json)
        return MissionTaskView(
            kind=row.kind,
            label=MISSION_LABELS[row.kind],
            enabled=row.enabled,
            priority=row.priority,
            params=params,
            status=status.value,
            detail=self._detail(kind, status, snapshot, row),
            summary=self._summary(kind, params),
            disabled_reason=row.disabled_reason,
        )

    def _task_view_for(self, kind: MissionKind) -> MissionTaskView:
        snapshot = self._scheduler.snapshot()
        for row in snapshot.tasks:
            if row.kind == kind.value:
                return self._task_view(row, snapshot)
        raise NotFoundError(f"mission_tasks 里没有 {kind.value} 这一行")

    @staticmethod
    def _detail(
        kind: MissionKind,
        status: TaskStatus,
        snapshot: SchedulerSnapshot,
        row: orm.MissionTaskRow,
    ) -> str:
        """状态旁边那句随行的事实。

        没在参与调度的链路一律不报数字：`SchedulerFacts` 对它们填的是 0，
        照着写出来就是「今日 0/32」——一句看着正常的假话。
        """
        if status is TaskStatus.DISABLED:
            return row.disabled_reason or ""
        if status is TaskStatus.OFF:
            return ""
        facts = snapshot.facts
        if kind is MissionKind.PIRATE:
            used = f"今日 {facts.pirate_dispatches_today}/{facts.pirate_quota}"
            if status is TaskStatus.QUOTA_EXHAUSTED:
                # 重置点是 UTC 00:00，本地（UTC+8）就是次日早上 8 点。
                return f"{used} · 次日 08:00 恢复"
            return used
        if kind is MissionKind.BOT:
            remaining = facts.bot_targets_remaining
            if remaining <= 0:
                return "本轮已全部完成"
            return f"还剩 {remaining} 个未完成"
        return "始终填空隙"

    def _summary(self, kind: MissionKind, params: dict[str, int]) -> str:
        if kind is MissionKind.PIRATE:
            return self._pirate_summary(params)
        if kind is MissionKind.BOT:
            return self._bot_summary(params)
        return "—"

    def _pirate_summary(self, params: dict[str, int]) -> str:
        """半径 10 是多大范围，用户心里没数；把实际覆盖区间回显出来。

        主星取**调度器认定的那个**，不另读一次默认值：两边各读一次的话，
        配了 `EVO_HELPER_ORIGIN` 之后页面会显示旧主星、舰队却从新主星出发，
        而用户看着「没问题」。
        """
        radius = params.get("radius")
        if radius is None:
            return "未设置半径"
        origin = self._scheduler.origin
        try:
            systems = pirate_systems(origin, radius)
        except MissionParamError as exc:
            return f"参数不合格：{exc}"
        # `pirate_systems` 按离主星的距离排，不是按系号排，所以取首尾要先排序。
        numbers = sorted(system for _, system in systems)
        return (
            f"半径 {radius} · {origin.galaxy}:{numbers[0]} – "
            f"{origin.galaxy}:{numbers[-1]}，{len(numbers)} 个系"
        )

    def _bot_summary(self, params: dict[str, int]) -> str:
        """区间里有几个已记录的 bot。N=0 就禁止启用，所以 N 必须先看得见。"""
        galaxy = params.get("galaxy")
        first = params.get("first_system")
        last = params.get("last_system")
        if galaxy is None or first is None or last is None:
            return "未设置系号区间"
        try:
            targets = bot_targets_in_range(
                self._bot_coordinates(), galaxy=galaxy, first_system=first, last_system=last
            )
        except MissionParamError as exc:
            return f"参数不合格：{exc}"
        return f"{galaxy}:{first} – {galaxy}:{last} · 该范围内已记录 bot：{len(targets)} 个"

    def _bot_coordinates(self) -> list[Coordinate]:
        return [
            Coordinate(row.galaxy, row.system, row.position)
            for row in self._repository.list_bot_targets()
        ]

    def _row(self, kind: MissionKind) -> orm.MissionTaskRow:
        for row in self._repository.mission_tasks():
            if row.kind == kind.value:
                return row
        raise NotFoundError(f"mission_tasks 里没有 {kind.value} 这一行")

    @staticmethod
    def _kind(kind_text: str) -> MissionKind:
        # 大小写不敏感：规格里 `/api/missions/bot/...` 与 `/api/missions/BOT`
        # 两种写法都出现过，为这个让用户吃 404 不值得。
        try:
            return MissionKind(kind_text.upper())
        except ValueError as exc:
            raise NotFoundError(f"没有 {kind_text} 这条任务链路") from exc


def _int_params(raw: str) -> dict[str, int]:
    """`params_json` → 整数参数表。

    坏值一律丢掉而不是抛：这一列可能被人手改过，而一条读不懂的参数不该让
    整个调度台打不开。填错的后果照样看得见——`summary` 会说「未设置」，
    启用时也会被 `command_for` 拦下。
    """
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    # `bool` 是 `int` 的子类，得单独排掉：`{"radius": true}` 会变成半径 1。
    return {
        str(key): value
        for key, value in data.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _validate_lines(fleet_line_limit: int, reserved_lines: int) -> None:
    """Reserving every line would make the plan unable to dispatch anything."""
    if reserved_lines >= fleet_line_limit:
        raise ServiceError(
            f"reserved_lines ({reserved_lines}) must be fewer than "
            f"fleet_line_limit ({fleet_line_limit}); the plan would never dispatch"
        )


def _planet_kind_clause(kind: str):  # type: ignore[no-untyped-def]
    """把 `planet_kind()` 的分类翻成 SQL 过滤条件。

    两边必须一致。有用例拿同一批数据分别走这里和 `planet_kind()` 对答案，
    改了一处忘了另一处会当场红。
    """
    if kind == "bot":
        return orm.BotTargetRow.is_bot.is_(True)
    if kind == "owned":
        return and_(
            orm.BotTargetRow.is_bot.is_(False),
            orm.BotTargetRow.latest_owner_name.is_not(None),
        )
    if kind == "free":
        return orm.BotTargetRow.latest_owner_name.is_(None)
    return None
