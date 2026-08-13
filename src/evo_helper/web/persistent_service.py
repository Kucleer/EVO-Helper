"""SQLite-backed implementation of the Web application-service seam."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_freeze import FrozenTask, MissionConfigFreeze
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
from evo_helper.domain.models import Coordinate, CoordinateRange, RunState
from evo_helper.domain.scheduler import (
    MissionKind,
    RunningProcess,
    TaskStatus,
    scheduling_order,
    status_of,
)
from evo_helper.storage import models as orm
from evo_helper.storage.intel import (
    DISPATCH_BLOCKED,
    DISPATCH_REJECTED,
    DISPATCH_SENT,
    RESULT_AWAITING,
)
from evo_helper.storage.repository import SqlAlchemyRepository

from .display import MISSION_LABELS, PARAM_LABELS
from .service import (
    AttackLogOptions,
    AttackLogView,
    BotTargetView,
    ConfigFreezeView,
    ConflictError,
    CoordinateScanView,
    CurrentMissionView,
    DashboardView,
    FakeApplicationService,
    FleetDiffView,
    FleetEntryView,
    FleetSnapshotView,
    FrozenTaskView,
    MissionRunView,
    MissionTaskView,
    NotFoundError,
    PlanetPage,
    PlanetRow,
    PlanPatchView,
    RevisitView,
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

    # 建运行实例（`start_run`）与查运行实例（`get_run`）都删了：它们只有
    # `POST /api/runs/start` 与 `GET /api/runs/{run_id}` 两个调用方，而这两个接口
    # 在「运行详情」页关掉之后已经没有任何界面用得上。往 `run_instances` 写的是
    # `tools/scan_coordinates.py` 与 `tools/pirate_loop.py`（按 `PLAN_NAME` /
    # `RUN_KEY` 幂等），推状态的是 `SqlAlchemyRepository.set_run_state`，都还在跑。

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

    def list_attack_log(
        self,
        limit: int,
        *,
        day_utc: date | None = None,
        kind: str | None = None,
        target_span: CoordinateRange | None = None,
        preset: str | None = None,
        result: str | None = None,
        outcome: str | None = None,
    ) -> list[AttackLogView]:
        """攻击日志：每条意图一行，派出去的带上派遣事实。

        用 `outerjoin` 而不是 `join`：**被闸门拦下、或者读简报没通过的意图
        没有对应的派遣行**，而这些恰恰是最需要在日志里看到的——
        内连接会把它们静默滤掉，日志看起来一片干净，实际是漏了。

        按意图创建时间倒序：日志页第一眼要看的是最近发生了什么。

        战报也是 `outerjoin` 接上来的：刚派出去的那一发还没有战报，而它必须照样在列。
        战报按 `dispatch_id` 接——那是仓储层做过时间与坐标核对之后写下的匹配结果，
        在这里按坐标重新配一次，等于把同一条判据写第二份。

        **六个筛选全部下推到 SQL**，不许在取回 `limit` 条之后再挑：日志页只取最近
        若干条，在内存里筛等于「先砍掉历史再问历史」——查三天前那天、或者查某个
        坐标，会得到空页，而空页读起来和「那天/那里一发没打」一模一样。海盗每日
        32 次配额是按游戏日算的，一天的记录必须能整天取全。

        - `day_utc`：只留那一个 **UTC+0 自然日**（也就是游戏内的一天）。
        - `kind`：`bot` / `pirate`（`domain.records.TARGET_KIND_*`）。
        - `target_span`：目标坐标区间，闭区间，两端都含。
        - `preset`：舰队预设名，精确匹配 `attack_intents.preset_name`。
        - `result`：`SENT` / `BLOCKED` / `REJECTED`（`service.ATTACK_LOG_RESULTS`）。
          这一档**不是存下来的字段**，而是由「有没有派遣行 + accepted」算出来的，
          所以三个分支各自写成 SQL 条件，和页面上那一格的判据是同一套。
        - `outcome`：战报里的胜负原文；`AWAITING` 表示「还没有战果」。
          外连接下没有战报行时 `BattleReportRow.outcome` 就是 NULL，所以
          `IS NULL` 一条同时覆盖「没战报」和「战报没读出胜负」——页面上这两种
          也都显示「待战报」，两边必须是同一条判据。

        **这一行就是一次派遣，所以三个新筛选一律按这一行自己的值判**，不套情报
        中心那套「按最近一次派遣判目标星球」的口径——那一页筛的是星球，这一页
        筛的是派遣，混用会让「今天被拦下的那几发」按星球的最新状态被筛掉。
        """
        with self._session_factory() as session:
            statement = (
                select(orm.AttackIntentRow, orm.AttackDispatchRow, orm.BattleReportRow)
                .outerjoin(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
            )
            if kind is not None:
                statement = statement.where(orm.AttackIntentRow.target_kind == kind)
            if target_span is not None:
                # 打包成一个整数再比区间。逐分量比较（galaxy>= 且 system>= 且
                # position>=）会把 2:130:14 排除在 2:130:1 – 2:140:20 之外——
                # 14 > 20 不成立，那一路就走不通了。情报中心的坐标区间踩过同一个坑
                # （`storage.intel._within`）。
                packed = (
                    orm.AttackIntentRow.target_galaxy * 1_000_000
                    + orm.AttackIntentRow.target_system * 1000
                    + orm.AttackIntentRow.target_position
                )
                statement = statement.where(
                    packed.between(_pack(target_span.start), _pack(target_span.end))
                )
            if day_utc is not None:
                # 按页面第一列显示的那个瞬时切：派出去的按派遣时刻，没派出去的
                # 按意图创建时刻。半开区间 [当日 00:00, 次日 00:00)，别用
                # `date()` 之类的 SQL 函数——那是 SQLite 方言，且用不上索引。
                moment = func.coalesce(
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.AttackIntentRow.created_at_utc,
                )
                day_start = datetime.combine(day_utc, time(), tzinfo=UTC)
                statement = statement.where(
                    moment >= day_start, moment < day_start + timedelta(days=1)
                )
            if preset is not None:
                statement = statement.where(orm.AttackIntentRow.preset_name == preset)
            if result is not None:
                statement = statement.where(_dispatch_result_clause(result))
            if outcome is not None:
                statement = statement.where(
                    orm.BattleReportRow.outcome.is_(None)
                    if outcome == RESULT_AWAITING
                    else orm.BattleReportRow.outcome == outcome
                )
            rows = session.execute(
                statement.order_by(
                    orm.AttackIntentRow.created_at_utc.desc(),
                    orm.AttackIntentRow.id.desc(),
                ).limit(limit)
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

    def attack_log_options(self) -> AttackLogOptions:
        """攻击日志上「预设」「战果」两档的候选值，从库里现有的记录取。

        写死字面量会漏掉用户新建的预设——预设是他自己在游戏里维护的，助手这边
        只是读到什么记什么。战果同理：库里存的是战斗详情页上的画面原文。

        **不带任何当前筛选条件**（见 `AttackLogOptions` 的说明）：候选跟着结果
        收窄的话，筛完一档就再也切不回别的档。

        战报按 `dispatch_id is not null` 取：没配上派遣的战报根本不会出现在
        这一页上，把它的胜负摆进筛选器等于给出一个筛不出行的选项。
        """
        with self._session_factory() as session:
            presets = tuple(
                session.scalars(
                    select(orm.AttackIntentRow.preset_name)
                    .distinct()
                    .order_by(orm.AttackIntentRow.preset_name)
                ).all()
            )
            outcomes = [
                value
                for value in session.scalars(
                    select(orm.BattleReportRow.outcome)
                    .where(orm.BattleReportRow.dispatch_id.is_not(None))
                    .distinct()
                    .order_by(orm.BattleReportRow.outcome)
                ).all()
                if value is not None
            ]
            # 「待战报」只在真有这样一行时才摆出来。判据与页面上那一格、与
            # `list_attack_log(outcome=AWAITING)` 完全一致：外连接之后
            # `outcome IS NULL`——既覆盖「还没战报」，也覆盖「战报没读出胜负」。
            awaiting = session.execute(
                select(orm.AttackIntentRow.id)
                .outerjoin(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(orm.BattleReportRow.outcome.is_(None))
                .limit(1)
            ).first()
            if awaiting is not None:
                outcomes.append(RESULT_AWAITING)
        return AttackLogOptions(presets=presets, outcomes=tuple(outcomes))

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

    # `_schedule_state`（按计划时间窗定 ARMED / SCANNING）与 `_event`（往
    # `state_events` 写一条 `started`）都只有 `start_run` 用过，跟着它一起删。
    # 运行状态事件在真实链路上由 `SqlAlchemyRepository.append_state_event` 写，
    # `list_events` 照旧读得到。

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
        """改开关 / 参数 / 优先级。三样各自独立，`None` 表示这次不动它。

        **调度器运行中一律拒绝**（`_refuse_while_running`），只留「恢复」一个口子。
        """
        kind = self._kind(kind_text)
        row = self._row(kind)
        self._refuse_while_running(row, enabled=enabled, priority=priority, params=params)
        if kind is MissionKind.SCAN and priority is not None:
            # 领域层的排序键已经把扫描结构性地钉在最后一位，所以收下这个值也
            # 不会真的改变次序——正因为如此才必须拒绝：默默收下一个不起作用的
            # 写入，页面会显示成「排序已保存」，刷新后又弹回去，用户只能得出
            # 「这个控件坏了」。理由要说出口：扫描永远有活干，排在它后面的
            # 链路就永远轮不到，当天 32 次配额会无声流失。
            raise ServiceError("扫描恒在最后一位（它永远有活干，排它后面的链路就永远轮不到）")
        if kind is MissionKind.SCAN and params:
            raise ServiceError("扫描不吃参数：它自己维护扫描计划与游标")

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

    def _refuse_while_running(
        self,
        row: orm.MissionTaskRow,
        *,
        enabled: bool | None,
        priority: int | None,
        params: dict[str, int] | None,
    ) -> None:
        """调度器跑着的时候不许改配置。**「恢复」是唯一的例外。**

        为什么要拒而不是收下：调度器每秒重新读一遍库，改动会立刻生效到下一轮，
        而上一轮正拿着旧参数在飞。一轮之内两套口径，事后从日志里分不出当时用的
        是哪一套。用户口径就是「开始后无法修改，只有结束状态才可以修改」。

        为什么给「恢复」开口子：一条链路完全可能在调度器跑着的时候被自动停用
        （连崩三次，多半是「窗口抢不到前台」这类环境原因），而那正是用户最需要
        把它恢复回来的时刻——一刀切禁掉 PATCH，页面上那个「恢复」按钮就废了，
        用户只剩「点结束、恢复、再点开始」这一条路，代价是把另外两条正常的链路
        一起停掉。开这个口子不破坏固化：`enabled` 在自动停用时**本来就还是
        True**，`disabled_reason` 与失败计数是调度器自己的状态、不是用户填的
        配置，所以这一下不动固化记录里的任何一个字段。

        因此口子开得很窄：**只认「这一行确实处在已停用状态」且这次 PATCH 除了
        `enabled: true` 之外什么都没带**。带上 params 或 priority 就不是恢复，
        是趁着恢复顺手改一笔。
        """
        if not self._scheduler.config_locked:
            return
        is_revive = (
            enabled is True
            and priority is None
            and params is None
            and row.disabled_reason is not None
        )
        if is_revive:
            return
        raise ConflictError(
            "调度器运行中，任务配置已固化，不能修改；点「结束」后可改"
            "（被自动停用的链路仍可点「恢复」）"
        )

    def freeze_log_path(self) -> str | None:
        """固化记录落在磁盘上的什么地方，没有落盘时为 None。页面把它写出来。"""
        path = self._scheduler.freeze_log_path
        return None if path is None else str(path)

    def recent_config_freezes(self, *, limit: int = 20) -> list[ConfigFreezeView]:
        """历次「开始」固化下来的配置，**新的在前**。页面上那张记录表读它。

        「与上一次相比改了什么」要拿相邻两条比，所以先把整串按时间顺序算完再
        截断——先截断再比的话，最老那一条会拿不到它真正的上一条，于是每次翻页
        都多出一句凭空的「首次记录」。
        """
        records = self._scheduler.config_freezes()
        views = [
            self._freeze_view(record, records[index - 1] if index else None)
            for index, record in enumerate(records)
        ]
        views.reverse()
        return views[:limit]

    def _freeze_view(
        self, record: MissionConfigFreeze, previous: MissionConfigFreeze | None
    ) -> ConfigFreezeView:
        return ConfigFreezeView(
            frozen_at_utc=record.frozen_at_utc,
            tasks=tuple(
                _frozen_task_view(task)
                for task in record.tasks
                if task.kind.value in MISSION_LABELS
            ),
            changes=_describe_changes(previous, record),
        )

    def _current_freeze_view(self, record: MissionConfigFreeze) -> ConfigFreezeView:
        """本轮那一份，连同「与上一次开始相比改了什么」。

        上一条按**身份**去队尾找，不是无脑取倒数第二条：`records[-1]` 就是
        `record` 这件事由 `snapshot()` 保证，但那是另一个模块的实现细节，
        照着它写等于把两处绑死。
        """
        records = self._scheduler.config_freezes()
        index = next((position for position, item in enumerate(records) if item is record), None)
        previous = records[index - 1] if index else None
        return self._freeze_view(record, previous)

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
            config_locked=snapshot.config_locked,
            frozen_config=(
                None
                if snapshot.frozen_config is None
                else self._current_freeze_view(snapshot.frozen_config)
            ),
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


def _frozen_task_view(task: FrozenTask) -> FrozenTaskView:
    params = _int_params(task.params_json)
    kind = task.kind
    return FrozenTaskView(
        kind=kind.value,
        label=MISSION_LABELS[kind.value],
        enabled=task.enabled,
        priority=task.priority,
        params=params,
        summary=_frozen_summary(kind, params),
    )


def _frozen_summary(kind: MissionKind, params: dict[str, int]) -> str:
    """固化的那份参数念成人话。

    **只用记录里的数字，不查库。** `MissionConsoleService._summary` 会去问
    「这个范围里现在有几个 bot」——那是今天的库，而这条记录说的是上周五那一轮。
    把今天的数字贴在旧记录上，正是这份记录要防的那种走样。
    """
    if kind is MissionKind.PIRATE:
        radius = params.get("radius")
        return "未设置半径" if radius is None else f"半径 {radius}"
    if kind is MissionKind.BOT:
        galaxy = params.get("galaxy")
        first = params.get("first_system")
        last = params.get("last_system")
        if galaxy is None or first is None or last is None:
            return "未设置系号区间"
        return f"{galaxy}:{first} – {galaxy}:{last}"
    return "不吃参数"


def _describe_changes(
    before: MissionConfigFreeze | None, after: MissionConfigFreeze
) -> tuple[str, ...]:
    """两次「开始」之间，用户到底改了什么。

    用户口径里的「记录任务内容」有两半：这一轮用的是哪一套（`tasks`），
    以及**改了什么、什么时候改的**——后者就是这一串。一张只有参数、没有差异的
    表，翻账时要拿眼睛去逐格对，而两条记录之间往往只差一个数字。

    没有上一条时说「首次记录」而不是空：**「没改过」和「没得比」不是一回事**，
    都显示成空白的话，第一条记录看起来就像是「跟上次一样」。
    """
    if before is None:
        return ("首次记录",)
    changes: list[str] = []
    for task in after.tasks:
        label = MISSION_LABELS.get(task.kind.value, task.kind.value)
        old = before.task(task.kind)
        if old is None:
            changes.append(f"{label}：首次出现")
            continue
        if old.enabled != task.enabled:
            changes.append(f"{label}：参与调度 {_yes(old.enabled)} → {_yes(task.enabled)}")
        if old.priority != task.priority:
            changes.append(f"{label}：优先级 {old.priority} → {task.priority}")
        changes.extend(_param_changes(label, old.params_json, task.params_json))
    return tuple(changes)


def _param_changes(label: str, before_json: str, after_json: str) -> list[str]:
    """参数逐项对比。

    键的次序以**新的那份**为准，旧的那份里多出来的接在后面：改动多半是「这一项
    换了个数」，按新的次序读下来和页面上那排输入框的顺序一致。

    末尾那条兜底是给手改过库的情况：`_int_params` 只认整数，所以
    `{"radius": "8"}` 这种在两边都会被丢成空字典，逐项对比说不出改了哪一项。
    说一句「参数有改动」也比装作没改好——那种参数调度器起不来，用户迟早要来
    翻这份记录找原因。
    """
    before = _int_params(before_json)
    after = _int_params(after_json)
    changes: list[str] = []
    for key in dict.fromkeys([*after, *before]):
        old = before.get(key)
        new = after.get(key)
        if old == new:
            continue
        name = PARAM_LABELS.get(key, key)
        changes.append(f"{label}：{name} {_or_dash(old)} → {_or_dash(new)}")
    if not changes and before_json != after_json:
        changes.append(f"{label}：参数有改动（无法逐项对比）")
    return changes


def _yes(value: bool) -> str:
    return "是" if value else "否"


def _or_dash(value: int | None) -> str:
    """没有这一项时显示破折号。`0` 是个合法取值，不能和「没填」显示成一样。"""
    return "—" if value is None else str(value)


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


def _pack(coordinate: Coordinate) -> int:
    """坐标打包成一个可比大小的整数。与 `storage.intel._pack` 同一套算法。"""
    return (coordinate.galaxy * 1000 + coordinate.system) * 1000 + coordinate.position


def _dispatch_result_clause(result: str):  # type: ignore[no-untyped-def]
    """把攻击日志「结果」那一格的判据翻成 SQL 过滤条件。

    页面上那一格是这么读的：没有派遣行 → 未派出；有且 accepted → 已派出；
    有但没 accepted → 被拒。这里必须逐条对上，否则筛出来的行和它自己显示的
    结果会对不上——那种错读起来像是数据坏了。

    外连接下 `accepted` 在「没有派遣行」时是 NULL，`IS TRUE` / `IS FALSE`
    都不成立，所以两个分支各自天然排除了未派出的行。
    """
    if result == DISPATCH_SENT:
        return orm.AttackDispatchRow.accepted.is_(True)
    if result == DISPATCH_REJECTED:
        return orm.AttackDispatchRow.accepted.is_(False)
    if result == DISPATCH_BLOCKED:
        return orm.AttackDispatchRow.id.is_(None)
    # 认不出来的档不该走到这里：`attack_log_page` 只放行 `ATTACK_LOG_RESULTS`。
    raise ServiceError(f"unknown attack-log result filter: {result!r}")
