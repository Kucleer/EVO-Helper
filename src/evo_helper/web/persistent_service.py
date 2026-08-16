"""SQLite-backed implementation of the Web application-service seam."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, false, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.backfill import (
    BACKFILL_KINDS,
    LOG_TAIL_LINES,
    REASON_MANUAL,
    BackfillBusyError,
    BackfillPhase,
    BackfillRequest,
    BackfillState,
)
from evo_helper.application.mission_freeze import FrozenTask, MissionConfigFreeze
from evo_helper.application.mission_scheduler import MissionScheduler, SchedulerSnapshot
from evo_helper.domain.missions import (
    MissionParamError,
    bot_targets_in_range,
    pirate_systems,
    wrap_system,
)
from evo_helper.domain.models import Coordinate, CoordinateRange, RunState
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS, SYSTEMS_PER_GALAXY
from evo_helper.domain.scheduler import (
    MissionKind,
    RunningProcess,
    TaskSnapshot,
    TaskStatus,
    fills_gaps,
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

from .display import BACKFILL_KIND_LABELS, MISSION_LABELS, PARAM_LABELS
from .service import (
    AttackLogOptions,
    AttackLogView,
    AttackPlanetView,
    BackfillSummaryView,
    BackfillView,
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
    MilitaryAttackConfigView,
    MissionOriginView,
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

# 页面（2 秒）与悬浮台（1 秒）会同时读取同一份重快照。这个缓存只合并瞬时并发
# 请求，不把运行状态长期藏起来；写操作会主动失效。
SCHEDULER_VIEW_TTL_S = 0.75


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

    def list_planets(
        self,
        *,
        galaxy: int | None,
        kind: str,
        owner_query: str | None = None,
        offset: int,
        limit: int,
    ) -> PlanetPage:
        """按银河系与类型筛选星球，**在 SQL 里筛、在 SQL 里数**。

        全量扫完是 71,856 颗星球。把它们全查出来再在 Python 里过滤，既慢又会诱使
        页面拿「本页行数」冒充总数——`list_scans` 的 500 条上限就是这么变成
        「扫描停在 2:32」的假象的。
        """
        with self._session_factory() as session:
            # 空位仍是扫描证据，但不是「星球列表」的一员；否则一次全星系扫描会
            # 让 4,000+ 个空位淹没 bot / 有主星球，并把总数误读为可用目标数。
            identified = select(orm.BotTargetRow).where(
                or_(
                    orm.BotTargetRow.is_bot.is_(True),
                    orm.BotTargetRow.latest_owner_name.is_not(None),
                ),
                orm.BotTargetRow.position.not_in(PIRATE_POSITIONS),
            )
            base = identified
            if galaxy is not None:
                base = base.where(orm.BotTargetRow.galaxy == galaxy)

            counted = base.subquery()
            kind_counts = {
                "bot": 0,
                "owned": 0,
            }
            # ⚠️ 分组键是「有没有主」这个布尔量，SELECT 里也必须是同一个表达式。
            # 原来 SELECT 的是主名本身、GROUP BY 的是 `IS NULL`：SQLite 容忍选一个
            # 没进 GROUP BY 的列，PostgreSQL 直接 `GroupingError`——2026-08-16 切库
            # 之后 /planets 整页 500 就是这里，而其余页面全都正常，很容易看歪。
            #
            # 换成布尔量顺带修掉一处口径分歧：`_planet_kind_clause("owned")` 用的是
            # `IS NOT NULL`，而 `planet_kind()` 看的是真值，主名为空串时两边会算成
            # 不同的类（列表数得出来、分类计数却 KeyError）。
            owner_missing = counted.c.latest_owner_name.is_(None).label("owner_missing")
            for is_bot, missing, count in session.execute(
                select(counted.c.is_bot, owner_missing, func.count())
                .select_from(counted)
                .group_by(counted.c.is_bot, owner_missing)
            ):
                # 分类只关心主名有没有，这里的占位串只为把「有主」传给唯一那份实现。
                kind_counts[planet_kind(None if missing else "?", bool(is_bot))] += int(count)
            kind_counts["all"] = sum(kind_counts.values())

            filtered = base
            clause = _planet_kind_clause(kind)
            if clause is not None:
                filtered = filtered.where(clause)
            if owner_query and owner_query.strip():
                filtered = filtered.where(
                    orm.BotTargetRow.latest_owner_name.ilike(f"%{owner_query.strip()}%")
                )

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

            identified_counted = identified.subquery()
            galaxy_counts = {
                int(g): int(count)
                for g, count in session.execute(
                    select(identified_counted.c.galaxy, func.count())
                    .select_from(identified_counted)
                    .group_by(identified_counted.c.galaxy)
                    .order_by(identified_counted.c.galaxy)
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
            # 侦察报告回来了没有。**相关子查询而不是再加一个外连接**：一个目标
            # 一天会有好几份侦察报告（`scout_reports` 不认领派遣，见那个类），
            # 外连接会把同一发派遣乘成好几行，日志上就凭空多出几条没发生过的派遣。
            scout_back = (
                select(orm.ScoutReportRow.id)
                .where(
                    orm.ScoutReportRow.target_galaxy == orm.AttackIntentRow.target_galaxy,
                    orm.ScoutReportRow.target_system == orm.AttackIntentRow.target_system,
                    orm.ScoutReportRow.target_position == orm.AttackIntentRow.target_position,
                    orm.ScoutReportRow.reported_at_utc >= orm.AttackDispatchRow.dispatched_at_utc,
                )
                .exists()
            )
            rows = session.execute(
                statement.add_columns(scout_back)
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
                    report_received=report is not None,
                    attacker_losses=report.attacker_losses if report else None,
                    defender_losses=report.defender_losses if report else None,
                    mission_kind=dispatch.mission_kind if dispatch else None,
                    scout_report_back=bool(scouted),
                )
                for intent, dispatch, report, scouted in rows
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

    def __init__(
        self,
        repository: SqlAlchemyRepository,
        scheduler: MissionScheduler,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        scheduler_view_ttl_s: float = SCHEDULER_VIEW_TTL_S,
    ) -> None:
        self._repository = repository
        self._scheduler = scheduler
        self._monotonic = monotonic_clock
        self._scheduler_view_ttl_s = scheduler_view_ttl_s
        self._scheduler_view_lock = threading.Lock()
        self._scheduler_view_cached: SchedulerView | None = None
        self._scheduler_view_cached_at = float("-inf")
        self._scheduler_view_generation = -1
        self._scheduler_view_task_signature: tuple[object, ...] | None = None

    # -- 读 --------------------------------------------------------------------

    def scheduler_view(self) -> SchedulerView:
        """取调度快照的短 TTL 单飞缓存。

        快照会计算每个任务的随行事实；多个浏览器轮询刚好重叠时，后到的请求在
        这把小锁里复用先到者的结果，避免同时对 SQLite 发起同一轮重查询。锁只包
        这条读路径，起停仍由 ``MissionScheduler`` 自己的锁负责。
        """
        # 这条小查询既不会进入逐目标判态，也不读战报；它只避免任务被后台或其他
        # 请求线程改过时，TTL 内仍回旧行。真正昂贵的 `_facts()` 只在缓存失效时跑。
        task_signature = self._mission_task_signature()
        generation = self._scheduler.view_generation
        with self._scheduler_view_lock:
            now = self._monotonic()
            cached = self._scheduler_view_cached
            if (
                cached is not None
                and now - self._scheduler_view_cached_at < self._scheduler_view_ttl_s
                and generation == self._scheduler_view_generation
                and task_signature == self._scheduler_view_task_signature
            ):
                return cached
            view = self._view(self._scheduler.snapshot())
            self._scheduler_view_cached = view
            self._scheduler_view_cached_at = now
            self._scheduler_view_generation = self._scheduler.view_generation
            self._scheduler_view_task_signature = self._mission_task_signature()
            return view

    def _invalidate_scheduler_view(self) -> None:
        with self._scheduler_view_lock:
            self._scheduler_view_cached = None
            self._scheduler_view_cached_at = float("-inf")
            self._scheduler_view_generation = -1
            self._scheduler_view_task_signature = None

    def _mission_task_signature(self) -> tuple[object, ...]:
        """只读任务行的轻量版本号，补上调度器内存版本覆盖不到的直接写库。"""
        return tuple(
            (
                row.id,
                row.enabled,
                row.priority,
                row.name,
                row.params_json,
                row.origin_galaxy,
                row.origin_system,
                row.origin_position,
                row.fleet_lines,
                row.round_started_at_utc,
                row.quota_exhausted_until_utc,
                row.consecutive_failures,
                row.disabled_reason,
                row.updated_at_utc,
            )
            for row in self._repository.mission_tasks()
        )

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

    def start_scheduler(self, *, reconcile: bool = False) -> SchedulerView:
        """点「开始」。**默认直接开工，不先对账**（2026-08-13 实机之后改的）。

        `reconcile=True` 是页面上那个「先对账再跑任务」的复选框：人在跟前、
        想先把欠账补齐时勾上。**默认不做**，因为那一趟失败会把整夜挂机堵死
        ——理由整段写在 `web.schemas.SchedulerStartIn.reconcile` 上。

        **补录正在跑或正在排队时拒绝**（409）。理由和「起任务」那道闸门是同一
        条：一个游戏窗口、一只鼠标。区别只在这一层拦得更早——让用户点下去、
        看着调度器开着却一个任务都不起，比当场说明白糟得多。

        只拦「停 → 开」这一次跃迁：调度器**已经开着**时这一下本来就是空操作
        （`MissionScheduler.start` 自己会 return），而启动对账恰恰是它自己排出来
        的——不加这个条件，点一次「开始」之后紧接着再点一次就会被自己排的那批
        补录 409 掉。
        """
        state = self._scheduler.backfill_state()
        if not self._scheduler.enabled and state.active:
            raise ConflictError(
                f"正在{state.phase.value}（{_backfill_label(state.kind)}），"
                "补录期间不能启动调度器；等它跑完，或先点「取消补录」"
            )
        self._scheduler.start(reconcile=reconcile)
        self._invalidate_scheduler_view()
        return self.scheduler_view()

    def stop_scheduler(self) -> SchedulerView:
        self._scheduler.stop()
        self._invalidate_scheduler_view()
        return self.scheduler_view()

    # -- 战报补录 --------------------------------------------------------------

    def backfill_view(self) -> BackfillView:
        return self._backfill_view()

    def start_backfill(
        self,
        kind: str,
        since: date,
        *,
        max_pages: int | None = None,
        max_opens: int | None = None,
        exhaustive: bool = True,
    ) -> BackfillView:
        """手动补录。**不进调度**：它是修复工具，不是日常任务。

        起始日期在未来一律拒：那趟信箱翻下来必然一封都不匹配，而它要占着游戏
        窗口十几分钟，跑完还显示「补录完成」——一句看着正常的假话。

        ⚠️ **`exhaustive` 在这里默认开，在启动对账那条路上默认关。** 两个默认值
        反着来是故意的：手动这一趟基本都是来救**过期**战报的，而那些派遣早就掉出
        了 `due_attack_dispatches` 的 6 小时窗口——单子从头到尾是空的，对账模式在
        第一封「库里已有」就收工，一份都够不着。而启动对账要的正是早停。

        这个坑差点就落地了：命令契约是在 CLI 长出 `--exhaustive` 之前定的，
        于是页面上的按钮一度只会跑对账模式——在它最主要的用途上静默地什么都
        捞不回来，跑完还显示「补录完成」。
        """
        if kind not in BACKFILL_KINDS:
            raise ServiceError(f"补录链路只能是 {' / '.join(BACKFILL_KINDS)}（收到 {kind!r}）")
        today = self._scheduler.now_utc().astimezone(UTC).date()
        if since > today:
            raise ServiceError(f"起始日期在未来（{since.isoformat()} > {today.isoformat()}）")
        try:
            self._scheduler.request_backfill(
                BackfillRequest(
                    kind=kind,
                    since=since,
                    reason=REASON_MANUAL,
                    max_pages=max_pages,
                    max_opens=max_opens,
                    exhaustive=exhaustive,
                )
            )
        except BackfillBusyError as exc:
            raise ConflictError(str(exc)) from exc
        self._invalidate_scheduler_view()
        return self._backfill_view()

    def cancel_backfill(self) -> BackfillView:
        """「取消补录」：排队中的撤掉，跑着的杀掉，之后立刻放行任务。"""
        self._scheduler.cancel_backfill()
        self._invalidate_scheduler_view()
        return self._backfill_view()

    def resume_after_backfill(self) -> BackfillView:
        """「继续任务」：用户看过摘要，放任务出来。

        **补录跑完不自动放行**，所以这一下是必需的一步而不是装饰：用户要在
        放行之前看一眼「认领上了几发、几个 bot 目标不用再打了」。
        """
        self._scheduler.acknowledge_backfill()
        self._invalidate_scheduler_view()
        return self._backfill_view()

    def _backfill_view(self) -> BackfillView:
        state = self._scheduler.backfill_state()
        summary = state.summary
        return BackfillView(
            phase=state.phase.value,
            kind=state.kind,
            label=_backfill_label(state.kind),
            since="" if state.since is None else state.since.isoformat(),
            reason=state.reason,
            started_at_utc=state.started_at_utc,
            ended_at_utc=state.ended_at_utc,
            exit_code=state.exit_code,
            log_path="" if state.log_path is None else str(state.log_path),
            # 日志尾巴认的是**这一趟自己那份状态里的路径**，没有请求过就没有路径、
            # 也就没有尾巴。不认状态、按链路名去猜一个路径的话，「未在补录」旁边
            # 会摆着上一次留下的输出——一段没有主语的日志。
            log_tail=self.backfill_log_tail(),
            queued=state.queued,
            blocking=state.blocking,
            awaiting_ack=state.blocking and not state.active,
            detail=self._backfill_detail(state),
            summary=(
                None
                if summary is None
                else BackfillSummaryView(
                    reports_ingested=summary.reports_ingested,
                    dispatches_claimed=summary.dispatches_claimed,
                    bot_targets_settled=summary.bot_targets_settled,
                    bot_targets_measured=summary.bot_targets_measured,
                )
            ),
        )

    def _backfill_detail(self, state: BackfillState) -> str:
        """状态旁边那句随行的事实。

        `PENDING` 那一档必须说清楚**在等谁**：页面上只写「等任务结束」的话，
        用户看到的是一个半小时不动的状态条，而它其实完全正常——海盗那一轮就是
        要跑那么久，而硬杀它会留下一发飞出去了却没记账的舰队。
        """
        if state.phase is BackfillPhase.PENDING:
            running = self._scheduler.current
            queued = f"；后面还排着 {state.queued} 趟" if state.queued else ""
            if running is None:
                return f"窗口空着，马上开始{queued}"
            label = running.name or MISSION_LABELS.get(running.kind.value, running.kind.value)
            return f"在等「{label}」这一轮自己跑完（不硬杀：硬杀会留下没记账的派遣）{queued}"
        if state.phase is BackfillPhase.RUNNING:
            queued = f"；后面还排着 {state.queued} 趟" if state.queued else ""
            return f"正在翻信箱，这期间一个任务都不起{queued}"
        if state.blocking:
            return "任务已暂停：看过下面这几个数，点「继续任务」放行"
        return ""

    def backfill_log_tail(self) -> str:
        """补录日志的尾巴。页面上那块滚动区读它。"""
        return self._scheduler.backfill_log_tail(LOG_TAIL_LINES)

    def force_kill(self) -> SchedulerView:
        """孤儿红条上的「强制结束」。

        只停我们自己认识的那个子进程，只闭合台账里没闭合的行。**绝不按 pid 去
        杀一个不认识的进程**——pid 会被系统回收复用，那一枪可能打在别人身上。
        """
        self._scheduler.force_kill()
        self._invalidate_scheduler_view()
        return self.scheduler_view()

    # -- 改 --------------------------------------------------------------------

    def patch_mission(
        self,
        task_id: int,
        *,
        enabled: bool | None = None,
        priority: int | None = None,
        params: dict[str, object] | None = None,
        name: str | None = None,
        origin: str | None = None,
        fleet_lines: int | None = None,
    ) -> MissionTaskView:
        """改开关 / 参数 / 优先级 / 名字 / 出发星球 / 航线数。各自独立，
        `None` 表示这次不动它。

        **调度器运行中一律拒绝**（`_refuse_while_running`），只留「恢复」一个口子。
        """
        row = self._row(task_id)
        kind = MissionKind(row.kind)
        self._refuse_while_running(
            row,
            enabled=enabled,
            priority=priority,
            params=params,
            name=name,
            origin=origin,
            fleet_lines=fleet_lines,
        )
        if fills_gaps(kind) and priority is not None:
            # 领域层的排序键已经把填空隙的那几种（扫描 / 军力榜）结构性地钉在
            # 最后，所以收下这个值也
            # 不会真的改变次序——正因为如此才必须拒绝：默默收下一个不起作用的
            # 写入，页面会显示成「排序已保存」，刷新后又弹回去，用户只能得出
            # 「这个控件坏了」。理由要说出口：扫描永远有活干，排在它后面的
            # 链路就永远轮不到，当天 32 次配额会无声流失。
            raise ServiceError("扫描恒在最后一位（它永远有活干，排它后面的链路就永远轮不到）")
        if kind is MissionKind.SCAN and params:
            raise ServiceError("扫描不吃参数：它自己维护扫描计划与游标")
        if fleet_lines is not None and fleet_lines < 1:
            # 0 条航线的任务永远派不出去，而它在页面上看起来完全正常（状态是
            # 「等航线」，一句用户照着去等、等到天亮也不会动的话）。
            raise ServiceError("航线数至少是 1；填 0 等于这个任务永远派不出去")

        params_json = None if params is None else json.dumps(params, ensure_ascii=False)
        clear_origin = origin == ""
        parsed_origin = None if origin in (None, "") else _parse_origin(origin or "")
        # 用户没动出发星球时按库里现在那颗量。`clear_origin` 那一档要按「退回
        # 全局主星之后」的那颗量，所以两者都不能拿 `row` 的现值兜底。
        target_origin = (
            self._scheduler.origin if clear_origin else (parsed_origin or self._origin_of(row))
        )
        # 校验的时机有两个：动了参数，或者这一下是在**启用**它。后者不能省——
        # 先存一个空范围、再单独勾复选框，就绕过去了。只改 priority / 名字、
        # 或者要**关掉**它时不校验：参数填错了还关不掉，那就真的没退路了。
        if params is not None or enabled is True:
            raw_params = params_json or row.params_json
            try:
                military = bool(json.loads(raw_params).get("by_military", False))
            except (json.JSONDecodeError, AttributeError):
                military = False
            if kind is MissionKind.BOT and military:
                try:
                    self._scheduler.validate_military_params(raw_params)
                except MissionParamError as exc:
                    raise ServiceError(str(exc)) from exc
            else:
                self._validate(kind, raw_params, target_origin)
        # ⚠️ 这里原先还有一条 `elif origin is not None: self._check_origin(...)`：
        # 只改出发星球时单量那一项，因为「不是主星」当时会被临时闸门拒掉。
        # 闸门随「切换星球」实装删了（runner 开工会真的切过去），于是出发星球本身
        # 不再有「合不合法」这回事——写不成坐标那一档在 `_parse_origin` 就拒了。
        self._repository.update_mission_task(
            task_id,
            enabled=enabled,
            priority=priority,
            params_json=params_json,
            name=name,
            origin=parsed_origin,
            clear_origin=clear_origin,
            fleet_lines=fleet_lines,
        )
        self._invalidate_scheduler_view()
        return self._task_view_for(task_id)

    def mission_origins(self, task_id: int) -> tuple[MissionOriginView, ...]:
        """额外 origin 为空时，调用方明确知道仍回落旧单 origin。"""
        self._row(task_id)
        origins: list[MissionOriginView] = []
        for item in self._repository.mission_task_origins(task_id):
            planet = (
                None if item.planet_id is None else self._repository.attack_planet(item.planet_id)
            )
            origins.append(
                MissionOriginView(
                    planet_id=item.planet_id,
                    galaxy=item.galaxy if planet is None else planet.galaxy,
                    system=item.system if planet is None else planet.system,
                    position=item.position if planet is None else planet.position,
                    fleet_lines=item.fleet_lines,
                    enabled=item.enabled,
                )
            )
        return tuple(origins)

    def replace_mission_origins(
        self, task_id: int, origins: tuple[MissionOriginView, ...]
    ) -> tuple[MissionOriginView, ...]:
        """只允许 bot 设置多 origin，区域攻击不读、不写这张表。"""
        row = self._row(task_id)
        if MissionKind(row.kind) is not MissionKind.BOT:
            raise ServiceError("只有 bot 攻击可以配置多个出发星球")
        self._refuse_while_running(row, enabled=None, priority=None, params=None)
        planet_ids = [item.planet_id for item in origins]
        if any(planet_id is None for planet_id in planet_ids):
            raise ServiceError("请选择配置页中的出发星球")
        if len(set(planet_ids)) != len(planet_ids):
            raise ServiceError("同一颗出发星球只能配置一次")
        resolved = []
        for item in origins:
            if item.planet_id is None:  # 上面的输入校验已挡住；供类型检查器窄化。
                raise AssertionError("validated planet_id disappeared")
            resolved.append((item.planet_id, item.fleet_lines, item.enabled))
        self._repository.replace_mission_task_origins(task_id, tuple(resolved))
        self._invalidate_scheduler_view()
        return self.mission_origins(task_id)

    def attack_planets(self) -> tuple[AttackPlanetView, ...]:
        return tuple(
            AttackPlanetView(
                planet_id=row.id,
                number=row.sort_index,
                galaxy=row.galaxy,
                system=row.system,
                position=row.position,
            )
            for row in self._repository.attack_planets()
        )

    def create_attack_planet(self, coordinate: Coordinate) -> AttackPlanetView:
        self._refuse_global_config_while_running()
        try:
            row = self._repository.create_attack_planet(coordinate)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        self._invalidate_scheduler_view()
        return AttackPlanetView(row.id, row.sort_index, row.galaxy, row.system, row.position)

    def update_attack_planet(self, planet_id: int, coordinate: Coordinate) -> AttackPlanetView:
        self._refuse_global_config_while_running()
        try:
            row = self._repository.update_attack_planet(planet_id, coordinate)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        self._invalidate_scheduler_view()
        return AttackPlanetView(row.id, row.sort_index, row.galaxy, row.system, row.position)

    def delete_attack_planet(self, planet_id: int) -> None:
        self._refuse_global_config_while_running()
        try:
            self._repository.delete_attack_planet(planet_id)
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        self._invalidate_scheduler_view()

    def military_attack_config(self) -> MilitaryAttackConfigView:
        row = self._repository.military_attack_config()
        try:
            tiers = json.loads(row.tiers_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - 写侧校验
            raise ServiceError("全局军力档位配置损坏") from exc
        return MilitaryAttackConfigView(tuple(tiers))

    def replace_military_attack_tiers(
        self, tiers: tuple[dict[str, Any], ...]
    ) -> MilitaryAttackConfigView:
        self._refuse_global_config_while_running()
        normalized = [dict(tier) for tier in tiers]
        try:
            self._scheduler.validate_military_tiers(normalized)
        except MissionParamError as exc:
            raise ServiceError(str(exc)) from exc
        row = self._repository.replace_military_attack_tiers(
            json.dumps(normalized, ensure_ascii=False)
        )
        self._invalidate_scheduler_view()
        return MilitaryAttackConfigView(tuple(json.loads(row.tiers_json)))

    def create_mission(
        self,
        kind_text: str,
        *,
        name: str,
        origin: str | None = None,
        fleet_lines: int | None = None,
    ) -> MissionTaskView:
        """新建一个任务。**目前只有 bot 攻击可以有多个**（用户口径 2026-08-13）。

        新任务一律建成「不参与调度、参数为空」：它此刻既没有范围也没排优先级，
        直接参与调度等于「点了新建就开始派舰队」。用户填好范围、勾上复选框那一下
        会走 `patch_mission`，那条路上有参数校验。
        """
        kind = self._kind(kind_text)
        if self._scheduler.config_locked:
            raise ConflictError("调度器运行中，任务配置已固化，不能新建任务；点「结束」后可改")
        if kind is not MissionKind.BOT:
            existing = MISSION_LABELS.get(kind.value, kind.value)
            raise ServiceError(
                f"只有 bot 攻击可以有多个任务；「{existing}」保持一个"
                "（海盗每天 32 次是账号级配额，扫描恒在最后一位且永远有活干）"
            )
        if not name.strip():
            raise ServiceError("给这个任务起个名字，否则页面上两行长得一模一样")
        if fleet_lines is not None and fleet_lines < 1:
            raise ServiceError("航线数至少是 1；填 0 等于这个任务永远派不出去")
        parsed_origin = None if origin in (None, "") else _parse_origin(origin or "")
        rows = self._repository.mission_tasks()
        # 排在所有非扫描任务之后：新任务的优先级由用户拖，而默认插在最前面等于
        # 让一个还没配好的任务抢在正在正常工作的那些前面。
        priority = 1 + max(
            (row.priority for row in rows if row.kind != MissionKind.SCAN.value), default=-1
        )
        task_id = self._repository.create_mission_task(
            kind,
            name=name.strip(),
            priority=priority,
            params_json="{}",
            origin=parsed_origin,
            fleet_lines=fleet_lines,
            now_utc=self._scheduler.now_utc(),
        )
        self._invalidate_scheduler_view()
        return self._task_view_for(task_id)

    def delete_mission(self, task_id: int) -> None:
        """删掉一个任务。**每条链路至少留一行**，删光了页面上就再也建不回来。"""
        row = self._row(task_id)
        if self._scheduler.config_locked:
            raise ConflictError("调度器运行中，任务配置已固化，不能删除任务；点「结束」后可改")
        siblings = [item for item in self._repository.mission_tasks() if item.kind == row.kind]
        if len(siblings) <= 1:
            label = MISSION_LABELS.get(row.kind, row.kind)
            raise ServiceError(f"「{label}」只剩这一个任务了，删不得；不想让它跑就取消勾选")
        self._repository.delete_mission_task(task_id)
        self._invalidate_scheduler_view()

    def restart_bot_round(self, task_id: int) -> MissionTaskView:
        """「重开一轮」：把这个任务的 `round_started_at_utc` 推到当前。

        bot 打完一轮就退出调度，**不自动开下一轮**——自动开就等于没人看着的
        时候一直派舰队。开新一轮只能是用户按下的这一下。

        **只推这一个任务的轮**：两个 bot 任务各打各的范围，一起推等于把另一个
        还没打完的那一轮也归零。
        """
        row = self._row(task_id)
        if row.kind != MissionKind.BOT.value:
            raise ServiceError("只有 bot 攻击有「一轮」这个概念")
        self._scheduler.begin_bot_round(task_id)
        self._invalidate_scheduler_view()
        return self._task_view_for(task_id)

    # -- 内部 ------------------------------------------------------------------

    def _refuse_global_config_while_running(self) -> None:
        if self._scheduler.config_locked:
            raise ConflictError("调度器运行中，攻击配置已固化；点「结束」后可改")

    def _refuse_while_running(
        self,
        row: orm.MissionTaskRow,
        *,
        enabled: bool | None,
        priority: int | None,
        params: dict[str, object] | None,
        name: str | None = None,
        origin: str | None = None,
        fleet_lines: int | None = None,
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
        `enabled: true` 之外什么都没带**。带上 params / priority / 名字 / 出发星球 /
        航线数里的任何一样都不是恢复，是趁着恢复顺手改一笔。
        """
        if not self._scheduler.config_locked:
            return
        is_revive = (
            enabled is True
            and priority is None
            and params is None
            and name is None
            and origin is None
            and fleet_lines is None
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
            military_tiers_label=_frozen_tiers_label(record.military_tiers_json),
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

    def _validate(self, kind: MissionKind, params_json: str, origin: Coordinate) -> None:
        """用调度器自己那把尺子量一遍，量不过就 400。

        走 `command_for` 而不是在这里重写几条 if：两边一旦分家，就会出现
        「页面收下了、调度器起不来」——而调度器起不来时只会把任务自动停用，
        用户要等到下次看页面才发现。
        """
        try:
            self._scheduler.command_for(kind, params_json, origin=origin)
        except MissionParamError as exc:
            raise ServiceError(str(exc)) from exc

    def _origin_of(self, row: orm.MissionTaskRow) -> Coordinate:
        """这一行解析完默认值之后的出发星球。三列缺一就回落到全局主星。

        规则与 `MissionScheduler._origin_of` 必须一致，所以这里问的是调度器的
        `origin`，而不是再读一次 Settings。
        """
        galaxy, system, position = row.origin_galaxy, row.origin_system, row.origin_position
        if galaxy is None or system is None or position is None:
            return self._scheduler.origin
        return Coordinate(galaxy, system, position)

    def _view(self, snapshot: SchedulerSnapshot) -> SchedulerView:
        running = snapshot.running
        tasks = [task for task in snapshot.snapshots if task.kind.value in MISSION_LABELS]
        # 展示次序用领域层那把尺子，页面上排第一的就是下一个会被起的那条。
        tasks.sort(key=lambda task: (*scheduling_order(task), task.task_id))
        return SchedulerView(
            running=snapshot.enabled,
            started_at_utc=snapshot.started_at_utc,
            current=(
                None
                if running is None
                else CurrentMissionView(
                    task_id=running.task_id,
                    kind=running.kind.value,
                    label=running.name or MISSION_LABELS[running.kind.value],
                    started_at_utc=running.started_at_utc,
                    log_path=str(running.log_path),
                )
            ),
            orphan_pid=snapshot.orphan_pid,
            tasks=tuple(self._task_view(task, snapshot) for task in tasks),
            config_locked=snapshot.config_locked,
            frozen_config=(
                None
                if snapshot.frozen_config is None
                else self._current_freeze_view(snapshot.frozen_config)
            ),
        )

    def _task_view(self, task: TaskSnapshot, snapshot: SchedulerSnapshot) -> MissionTaskView:
        row = next(item for item in snapshot.tasks if item.id == task.task_id)
        running = snapshot.running
        status = status_of(
            task,
            snapshot.facts,
            running=(
                None
                if running is None
                else RunningProcess(
                    task_id=running.task_id,
                    kind=running.kind,
                    started_at_utc=running.started_at_utc,
                )
            ),
            restart_cooldown=timedelta(seconds=snapshot.config.restart_cooldown_seconds),
        )
        params = _view_params(row.params_json)
        return MissionTaskView(
            task_id=task.task_id,
            kind=task.kind.value,
            label=task.name or MISSION_LABELS[task.kind.value],
            enabled=task.enabled,
            priority=task.priority,
            params=params,
            status=status.value,
            detail=self._detail(task, status, snapshot),
            summary=self._summary(task, params),
            disabled_reason=task.disabled_reason,
            origin=str(task.origin),
            fleet_lines=task.fleet_lines,
            origin_is_default=row.origin_galaxy is None,
            fleet_lines_is_default=row.fleet_lines is None,
        )

    def _task_view_for(self, task_id: int) -> MissionTaskView:
        snapshot = self._scheduler.snapshot()
        for task in snapshot.snapshots:
            if task.task_id == task_id:
                return self._task_view(task, snapshot)
        raise NotFoundError(f"mission_tasks 里没有 id={task_id} 这一行")

    @staticmethod
    def _detail(
        task: TaskSnapshot,
        status: TaskStatus,
        snapshot: SchedulerSnapshot,
    ) -> str:
        """状态旁边那句随行的事实。

        没在参与调度的任务一律不报数字：`SchedulerFacts` 对它们填的是 0，
        照着写出来就是「今日 0/32」——一句看着正常的假话。
        """
        if status is TaskStatus.DISABLED:
            return task.disabled_reason or ""
        if status is TaskStatus.OFF:
            return ""
        facts = snapshot.facts
        if task.kind is MissionKind.PIRATE:
            used = f"今日 {facts.pirate_dispatches_today}/{facts.pirate_quota}"
            if status is TaskStatus.QUOTA_EXHAUSTED:
                # 重置点是 UTC 00:00，本地（UTC+8）就是次日早上 8 点。
                return f"{used} · 次日 08:00 恢复"
            return used
        if task.kind is MissionKind.BOT:
            remaining = facts.of(task).targets_remaining
            if remaining <= 0:
                return "本轮已全部完成"
            return f"还剩 {remaining} 个未完成"
        return "始终填空隙"

    def _summary(self, task: TaskSnapshot, params: dict[str, Any]) -> str:
        """参数与出发星球的人话回显。

        出发星球与航线数摆在最前面：多任务之后，「这一行到底从哪出发、能占几条」
        是区分两行 bot 任务的第一件事，而它俩都不在参数框里。
        """
        lines = f"{task.origin} · {task.fleet_lines} 条航线"
        if task.kind is MissionKind.PIRATE:
            return f"{lines} · {self._pirate_summary(task.origin, params)}"
        if task.kind is MissionKind.BOT:
            return f"{lines} · {self._bot_summary(params)}"
        return "—"

    def _pirate_summary(self, origin: Coordinate, params: dict[str, int]) -> str:
        """半径 10 是多大范围，用户心里没数；把实际覆盖区间回显出来。

        主星取**这个任务解析之后的那颗**，不另读一次默认值：两边各读一次的话，
        配了 `EVO_HELPER_ORIGIN` 之后页面会显示旧主星、舰队却从新主星出发，
        而用户看着「没问题」。
        """
        radius = params.get("radius")
        if radius is None:
            return "未设置半径"
        try:
            systems = pirate_systems(origin, radius)
        except MissionParamError as exc:
            return f"参数不合格：{exc}"
        # ⚠️ 首尾**不能**用 min/max：恒星系成环，半径跨过 499↔1 时
        # min 是 1、max 是 499，显示出来就成了「整个银河」，而实际只有十几个系。
        # 端点要按「主星 ± 半径」绕回来算。
        if len(systems) >= SYSTEMS_PER_GALAXY:
            return f"半径 {radius} · 整个 {origin.galaxy} 银河，{len(systems)} 个系"
        low = wrap_system(origin.system - radius)
        high = wrap_system(origin.system + radius)
        wrapped = "（跨 499↔1）" if low > high else ""
        return (
            f"半径 {radius} · {origin.galaxy}:{low} – "
            f"{origin.galaxy}:{high}{wrapped}，{len(systems)} 个系"
        )

    def _bot_summary(self, params: dict[str, Any]) -> str:
        """区间里有几个已记录的 bot。N=0 就禁止启用，所以 N 必须先看得见。"""
        if params.get("by_military") is True:
            top_n = params.get("top_n", 50)
            return f"军力前 {top_n} 名 · 统一档位 · 按出发点就近分配"
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

    def _row(self, task_id: int) -> orm.MissionTaskRow:
        row = self._repository.mission_task(task_id)
        if row is None:
            raise NotFoundError(f"mission_tasks 里没有 id={task_id} 这一行")
        return row

    @staticmethod
    def _kind(kind_text: str) -> MissionKind:
        # 大小写不敏感：规格里 `/api/missions/bot/...` 与 `/api/missions/BOT`
        # 两种写法都出现过，为这个让用户吃 404 不值得。
        try:
            return MissionKind(kind_text.upper())
        except ValueError as exc:
            raise NotFoundError(f"没有 {kind_text} 这条任务链路") from exc


def _parse_origin(text: str) -> Coordinate:
    """`星系:恒星系:位置` → 坐标。格式不对就 400。

    与 `config.Settings.origin_coordinate` 同一套格式（也是游戏里显示的那套），
    刻意不回落到主星：回落的后果是用户以为改成了 2 号星，实际舰队照旧从主星
    出发，而全程一句提示都没有。
    """
    parts = text.split(":")
    if len(parts) != 3 or not all(part.strip().isdigit() for part in parts):
        raise ServiceError(f"出发星球要写成 `星系:恒星系:位置`，收到 {text!r}")
    galaxy, system, position = (int(part) for part in parts)
    try:
        return Coordinate(galaxy, system, position)
    except ValueError as exc:
        raise ServiceError(f"出发星球 {text!r} 不是一个合法坐标：{exc}") from exc


def _backfill_label(kind: str | None) -> str:
    """补录链路的中文名。没有请求时是空串，认不出来就原样显示。

    认不出来时回落到原值而不是「未知」：宁可在页面上露出一个英文取值，也不要
    把「补的到底是哪条链路」这件事换成一句没有信息的话。
    """
    if kind is None:
        return ""
    return BACKFILL_KIND_LABELS.get(kind, kind)


def _frozen_task_view(task: FrozenTask) -> FrozenTaskView:
    params = _view_params(task.params_json)
    kind = task.kind
    return FrozenTaskView(
        kind=kind.value,
        label=task.name or MISSION_LABELS[kind.value],
        enabled=task.enabled,
        priority=task.priority,
        params=params,
        summary=_frozen_summary(kind, params),
        origin=task.origin,
        fleet_lines=task.fleet_lines,
    )


def _frozen_summary(kind: MissionKind, params: dict[str, Any]) -> str:
    """固化的那份参数念成人话。

    **只用记录里的数字，不查库。** `MissionConsoleService._summary` 会去问
    「这个范围里现在有几个 bot」——那是今天的库，而这条记录说的是上周五那一轮。
    把今天的数字贴在旧记录上，正是这份记录要防的那种走样。
    """
    if kind is MissionKind.PIRATE:
        radius = params.get("radius")
        return "未设置半径" if radius is None else f"半径 {radius}"
    if kind is MissionKind.BOT:
        if params.get("by_military") is True:
            top_n = params.get("top_n")
            return (
                "军力攻击（统一档位）"
                if not isinstance(top_n, int) or isinstance(top_n, bool)
                else f"军力前 {top_n} 名（统一档位）"
            )
        galaxy = params.get("galaxy")
        first = params.get("first_system")
        last = params.get("last_system")
        if galaxy is None or first is None or last is None:
            return "未设置系号区间"
        return f"{galaxy}:{first} – {galaxy}:{last}"
    return "不吃参数"


def _frozen_tiers_label(raw: str) -> str:
    """只从固化 JSON 读出人话档位，坏旧值不影响整张调度台。"""
    try:
        tiers = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    if not isinstance(tiers, list):
        return ""
    parts: list[str] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        score, preset = tier.get("min_score"), tier.get("preset")
        if (
            isinstance(score, bool)
            or not isinstance(score, int | float)
            or not isinstance(preset, str)
            or not preset.strip()
        ):
            continue
        parts.append(f"{preset.strip()} ≥ {score:g}")
    return "统一档位：" + " · ".join(parts) if parts else ""


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
        label = task.name or MISSION_LABELS.get(task.kind.value, task.kind.value)
        old = _matching_task(before, task)
        if old is None:
            changes.append(f"{label}：首次出现")
            continue
        if old.enabled != task.enabled:
            changes.append(f"{label}：参与调度 {_yes(old.enabled)} → {_yes(task.enabled)}")
        if old.priority != task.priority:
            changes.append(f"{label}：优先级 {old.priority} → {task.priority}")
        if old.name != task.name:
            changes.append(f"{label}：名字 {_or_dash_text(old.name)} → {_or_dash_text(task.name)}")
        if old.origin != task.origin:
            changes.append(
                f"{label}：出发星球 {_or_dash_text(old.origin)} → {_or_dash_text(task.origin)}"
            )
        if old.fleet_lines != task.fleet_lines:
            changes.append(
                f"{label}：航线数 {_or_dash(old.fleet_lines)} → {_or_dash(task.fleet_lines)}"
            )
        changes.extend(_param_changes(label, old.params_json, task.params_json))
    return tuple(changes)


def _matching_task(before: MissionConfigFreeze, task: FrozenTask) -> FrozenTask | None:
    """上一条记录里对应的那个任务。

    **先按 `task_id` 认人**：同一 `kind` 现在可以有多行，按 kind 认会把两个 bot
    任务当成同一个，于是每次「开始」都报出一串其实没发生过的改动。

    id 对不上时再按 kind 回落一次，只为读得懂**旧记录**——本轮之前写的行没有
    `task_id`，不回落的话，升级后的第一条记录会把每一条链路都说成「首次出现」，
    而那正是这份账要避免的走样。回落只认「上一条里这个 kind 恰好只有一个任务」：
    有两个的时候猜哪一个都是编的。
    """
    if task.task_id is not None:
        matched = next((item for item in before.tasks if item.task_id == task.task_id), None)
        if matched is not None:
            return matched
    same_kind = [item for item in before.tasks if item.kind is task.kind]
    if len(same_kind) == 1 and same_kind[0].task_id is None:
        return same_kind[0]
    return None


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


def _or_dash_text(value: str) -> str:
    """同上，字符串版。空串的含义是「没填 / 跟着全局走」，不是一个名字。"""
    return "—" if not value else value


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


def _view_params(raw: str) -> dict[str, Any]:
    """页面回显完整的 JSON 参数；军力档位不是整数，不能被旧的摘要过滤掉。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): value for key, value in data.items()}


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
        # 兼容直接调 service 的旧调用，但绝不把空位重新放进页面统计。
        return false()
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
