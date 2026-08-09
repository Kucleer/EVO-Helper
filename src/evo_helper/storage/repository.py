"""SQLAlchemy-backed RepositoryPort implementation and history queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.bot_round import DispatchFact
from evo_helper.domain.coordinates import next_coordinate_after
from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.ports import CoordinateClaim
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
    FleetDiff,
    ReportHistoryEntry,
    ShipTypeDiff,
    StateEvent,
    TargetRevisit,
)
from evo_helper.domain.report_wait import PendingReport
from evo_helper.domain.scheduler import MissionKind
from evo_helper.domain.state_machine import require_transition

from . import models as orm

#: How far a report's timestamp may deviate from the dispatch time and still
#: count as the same dispatch under the strict origin/target/time match rule.
MATCH_TIME_TOLERANCE = timedelta(hours=12)

#: 分档判定「不值得打」的目标，记在 `target_revisits` 上（其语义正是「需要复查的
#: 目标」），用独立 scope 与「战报缺失」那批分开。
#:
#: **不写 `attack_intents.guard_status`。** 那一列被 `application/workflow.py` 用
#: `ALLOWED` / `REFUSED` 占着，`logs.html` 把它渲染成「未派出 · {guard_status}」；
#: 塞第三套词汇进去，一发确实飞出去了的攻击会在日志页显示成「未派出」。
REVISIT_SCOPE_TIER_NEGLIGIBLE = "BOT_TIER_NEGLIGIBLE"

#: 孤儿行的 `stopped_by`：控制台重启时发现的、上次没走正常关闭路径的子进程。
STOPPED_BY_UNKNOWN = "UNKNOWN"

#: 三行任务的初始值：`(kind, enabled, priority, params_json)`。
#:
#: **扫描排最后**，它永远有活干，排在谁前面谁就永远轮不到。
#:
#: **只有扫描默认开着**：它不派遣、全程只读。两条攻击链路默认关着，理由和
#: `evo_bot.AUTO_ENABLED` 默认 False 一样——装好就会派舰队不是好默认。bot 的
#: 系号区间也没法猜，留空等页面上填。
_MISSION_SEEDS: tuple[tuple[MissionKind, bool, int, str], ...] = (
    (MissionKind.PIRATE, False, 0, '{"radius": 10}'),
    (MissionKind.BOT, False, 1, "{}"),
    (MissionKind.SCAN, True, 2, "{}"),
)

#: 这些复查行写 `DONE` 而不是默认的 `PENDING`。`persistent_service` 数的是
#: PENDING 的条数、missions 页显示成「待复查」——分档说不值得打是一个已经
#: 下完的判定，不是等人去做的活，用 PENDING 会凭空撑起那个计数。
REVISIT_STATUS_DONE = "DONE"


class StorageConflictError(ValueError):
    """Raised when an append would violate a uniqueness or close-once rule."""


class SqlAlchemyRepository:
    """Repository over one session factory; each method opens its own session."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # -- frozen RepositoryPort -------------------------------------------------

    def claim_next_coordinate(self, run_id: UUID) -> CoordinateClaim | None:
        with self._session_factory() as session:
            run = session.get(orm.RunInstance, run_id)
            if run is None:
                raise ValueError(f"unknown run instance: {run_id}")
            pending = _pending_from_run(run)
            if pending is not None:
                return CoordinateClaim(coordinate=pending)
            cursor = _cursor_from_run(run)
            ranges = session.scalars(
                select(orm.ScanRangeRow)
                .where(orm.ScanRangeRow.plan_id == run.plan_id)
                .order_by(
                    orm.ScanRangeRow.priority,
                    orm.ScanRangeRow.start_galaxy,
                    orm.ScanRangeRow.start_system,
                    orm.ScanRangeRow.start_position,
                )
            ).all()
            for range_row in ranges:
                start = _range_start(range_row)
                end = _range_end(range_row)
                if cursor is not None and cursor > end:
                    continue
                if cursor is None or cursor < start:
                    next_coordinate: Coordinate | None = start
                else:
                    # 位数窗口取自区间本身（例如 5–20），不是 POSITION_LIMIT。
                    # 后者是每银河系的**恒星系数** 499，拿它当位数上限会让游标
                    # 在每个恒星系里空转 479 个不存在的行星位。
                    next_coordinate = next_coordinate_after(
                        cursor,
                        end,
                        position_limit=range_row.end_position,
                        first_position=range_row.start_position,
                    )
                if next_coordinate is None:
                    continue
                _set_run_pending(run, next_coordinate)
                session.commit()
                return CoordinateClaim(coordinate=next_coordinate)
            return None

    def complete_coordinate(self, run_id: UUID, coordinate: Coordinate) -> None:
        """Commit a claimed coordinate only after its workflow step completed."""
        with self._session_factory() as session:
            run = session.get(orm.RunInstance, run_id)
            if run is None:
                raise ValueError(f"unknown run instance: {run_id}")
            if _pending_from_run(run) != coordinate:
                raise StorageConflictError(
                    "cannot complete a coordinate that is not currently pending"
                )
            _set_run_cursor(run, coordinate)
            _clear_run_pending(run)
            session.commit()

    def run_state(self, run_id: UUID) -> RunState:
        with self._session_factory() as session:
            run = session.get(orm.RunInstance, run_id)
            if run is None:
                raise ValueError(f"unknown run instance: {run_id}")
            return RunState(run.state)

    def set_run_state(self, run_id: UUID, target: RunState) -> None:
        """Persist a transition whose audit event was just appended by the workflow."""
        with self._session_factory() as session:
            run = session.get(orm.RunInstance, run_id)
            if run is None:
                raise ValueError(f"unknown run instance: {run_id}")
            current = RunState(run.state)
            require_transition(current, target)
            run.state = target.value
            if target in {RunState.COMPLETED, RunState.EMERGENCY_STOPPED}:
                run.finished_at_utc = datetime.now(run.created_at_utc.tzinfo)
            session.commit()

    def save_scan(self, scan: object) -> None:
        record = _require_type(scan, CoordinateScan, "scan")
        _require_utc(record.scanned_at_utc, "scanned_at_utc")
        with self._session_factory() as session:
            session.add(
                orm.CoordinateScanRow(
                    run_id=record.run_id,
                    galaxy=record.coordinate.galaxy,
                    system=record.coordinate.system,
                    position=record.coordinate.position,
                    scanned_at_utc=record.scanned_at_utc,
                    owner_name=record.owner_name,
                    is_bot=record.is_bot,
                    confidence=record.confidence,
                    evidence_artifact_id=record.evidence_artifact_id,
                )
            )
            target = _bot_target_for(session, record.coordinate)
            if target is None:
                session.add(
                    orm.BotTargetRow(
                        galaxy=record.coordinate.galaxy,
                        system=record.coordinate.system,
                        position=record.coordinate.position,
                        is_bot=record.is_bot,
                        latest_owner_name=record.owner_name,
                        last_scanned_at_utc=record.scanned_at_utc,
                    )
                )
            else:
                target.latest_owner_name = record.owner_name
                target.last_scanned_at_utc = record.scanned_at_utc
                target.is_bot = record.is_bot or target.is_bot
            session.commit()

    def save_attack_intent(self, intent: object) -> None:
        record = _require_type(intent, AttackIntent, "attack intent")
        _require_utc(record.cycle_start_utc, "cycle_start_utc")
        _require_utc(record.created_at_utc, "created_at_utc")
        with self._session_factory() as session:
            existing = session.scalar(
                select(orm.AttackIntentRow).where(
                    orm.AttackIntentRow.run_id == record.run_id,
                    orm.AttackIntentRow.target_galaxy == record.target.galaxy,
                    orm.AttackIntentRow.target_system == record.target.system,
                    orm.AttackIntentRow.target_position == record.target.position,
                    orm.AttackIntentRow.cycle_start_utc == record.cycle_start_utc,
                    orm.AttackIntentRow.forced_revisit == record.forced_revisit,
                )
            )
            if existing is not None:
                raise StorageConflictError("duplicate attack intent for run/target/cycle")
            session.add(
                orm.AttackIntentRow(
                    id=record.intent_id,
                    run_id=record.run_id,
                    origin_galaxy=record.origin.galaxy,
                    origin_system=record.origin.system,
                    origin_position=record.origin.position,
                    target_galaxy=record.target.galaxy,
                    target_system=record.target.system,
                    target_position=record.target.position,
                    preset_name=record.preset.name,
                    preset_signature=record.preset.signature,
                    cycle_start_utc=record.cycle_start_utc,
                    guard_status=record.guard_status,
                    forced_revisit=record.forced_revisit,
                    created_at_utc=record.created_at_utc,
                    target_kind=record.target_kind,
                )
            )
            session.commit()

    def save_dispatch(self, dispatch: object) -> None:
        record = _require_type(dispatch, AttackDispatch, "dispatch")
        _require_utc(record.dispatched_at_utc, "dispatched_at_utc")
        with self._session_factory() as session:
            intent = session.get(orm.AttackIntentRow, record.intent_id)
            if intent is None:
                raise ValueError(f"unknown attack intent: {record.intent_id}")
            session.add(
                orm.AttackDispatchRow(
                    id=record.dispatch_id,
                    intent_id=record.intent_id,
                    dispatched_at_utc=record.dispatched_at_utc,
                    dry_run=record.dry_run,
                    accepted=record.accepted,
                    evidence_artifact_id=record.evidence_artifact_id,
                )
            )
            target = _bot_target_for(
                session,
                Coordinate(intent.target_galaxy, intent.target_system, intent.target_position),
            )
            if target is not None:
                target.last_dispatch_at_utc = record.dispatched_at_utc
            session.commit()

    def append_report(self, report: object) -> None:
        record = _require_type(report, BattleReport, "report")
        _require_utc(record.reported_at_utc, "reported_at_utc")
        with self._session_factory() as session:
            report_row = orm.BattleReportRow(
                id=record.report_id,
                reported_at_utc=record.reported_at_utc,
                raw_time_text=record.raw_time_text,
                attacker_origin_galaxy=record.attacker_origin.galaxy,
                attacker_origin_system=record.attacker_origin.system,
                attacker_origin_position=record.attacker_origin.position,
                defender_target_galaxy=record.defender_target.galaxy,
                defender_target_system=record.defender_target.system,
                defender_target_position=record.defender_target.position,
                match_confidence=record.match_confidence,
                manual_review_status=record.manual_review_status,
                is_from_revisit=record.is_from_revisit,
                ui_version=record.ui_version,
                attacker_units=record.attacker_units,
                defender_units=record.defender_units,
                outcome=record.outcome,
                attacker_losses=record.attacker_losses,
                defender_losses=record.defender_losses,
            )
            session.add(report_row)
            for entry in record.fleet:
                session.add(
                    orm.FleetSnapshotRow(
                        report_id=record.report_id,
                        side=entry.side,
                        ship_type=entry.ship_type,
                        count=entry.count,
                        round_no=entry.round_no,
                        uncertain=entry.uncertain,
                    )
                )
            close = [
                dispatch
                for dispatch in _unmatched_dispatch_candidates(session, record)
                if _close_in_time(dispatch.dispatched_at_utc, record.reported_at_utc)
            ]
            if len(close) == 1:
                report_row.dispatch_id = close[0].id
                report_row.match_status = "MATCHED"
                report_row.match_confidence = 1.0
                target = _bot_target_for(session, record.defender_target)
                if target is not None:
                    target.last_report_at_utc = record.reported_at_utc
            elif len(close) > 1:
                report_row.match_status = "AMBIGUOUS"
            session.commit()

    # -- query helpers ---------------------------------------------------------

    def list_bot_targets(self) -> list[orm.BotTargetRow]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.BotTargetRow)
                .where(orm.BotTargetRow.is_bot)
                .order_by(
                    orm.BotTargetRow.galaxy,
                    orm.BotTargetRow.system,
                    orm.BotTargetRow.position,
                )
            ).all()
            return list(rows)

    def history_for_coordinate(self, coordinate: Coordinate) -> list[ReportHistoryEntry]:
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.BattleReportRow, orm.FleetSnapshotRow)
                .join(
                    orm.FleetSnapshotRow, orm.FleetSnapshotRow.report_id == orm.BattleReportRow.id
                )
                .where(
                    orm.BattleReportRow.defender_target_galaxy == coordinate.galaxy,
                    orm.BattleReportRow.defender_target_system == coordinate.system,
                    orm.BattleReportRow.defender_target_position == coordinate.position,
                )
                .order_by(
                    orm.BattleReportRow.reported_at_utc,
                    orm.FleetSnapshotRow.side,
                    orm.FleetSnapshotRow.ship_type,
                )
            ).all()
            return [
                ReportHistoryEntry(
                    report_id=report.id,
                    reported_at_utc=report.reported_at_utc,
                    side=snapshot.side,
                    ship_type=snapshot.ship_type,
                    count=snapshot.count,
                    is_from_revisit=report.is_from_revisit,
                    match_confidence=report.match_confidence,
                    manual_review_status=report.manual_review_status,
                )
                for report, snapshot in rows
            ]

    def fleet_diff(
        self,
        after_report_id: UUID,
        before_report_id: UUID | None = None,
        side: str = "defender",
    ) -> FleetDiff:
        """Compute the fleet composition diff between two reports (plan 8.2)."""
        with self._session_factory() as session:
            after = session.get(orm.BattleReportRow, after_report_id)
            if after is None:
                raise ValueError(f"unknown report: {after_report_id}")
            after_counts = _snapshot_counts(session, after_report_id, side)
            if before_report_id is None:
                before_counts: dict[str, int] = {}
            else:
                before = session.get(orm.BattleReportRow, before_report_id)
                if before is None:
                    raise ValueError(f"unknown report: {before_report_id}")
                before_counts = _snapshot_counts(session, before_report_id, side)
            earlier_ship_types = _earlier_ship_types(session, after, side)
            ships = _build_ship_diffs(before_counts, after_counts, earlier_ship_types)
            total_before = sum(before_counts.values())
            total_after = sum(after_counts.values())
            return FleetDiff(
                before_report_id=before_report_id,
                after_report_id=after_report_id,
                side=side,
                total_before=total_before,
                total_after=total_after,
                total_change=total_after - total_before,
                ships=ships,
                is_from_revisit=after.is_from_revisit,
                match_confidence=after.match_confidence,
                manual_review_status=after.manual_review_status,
            )

    def append_state_event(self, event: StateEvent) -> None:
        _require_utc(event.occurred_at_utc, "occurred_at_utc")
        with self._session_factory() as session:
            session.add(
                orm.StateEventRow(
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    event=event.event,
                    before_state=event.before_state,
                    after_state=event.after_state,
                    occurred_at_utc=event.occurred_at_utc,
                )
            )
            session.commit()

    def state_events_for(self, aggregate_type: str, aggregate_id: UUID) -> list[StateEvent]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.StateEventRow)
                .where(
                    orm.StateEventRow.aggregate_type == aggregate_type,
                    orm.StateEventRow.aggregate_id == aggregate_id,
                )
                .order_by(orm.StateEventRow.occurred_at_utc, orm.StateEventRow.id)
            ).all()
            return [
                StateEvent(
                    aggregate_type=row.aggregate_type,
                    aggregate_id=row.aggregate_id,
                    event=row.event,
                    before_state=row.before_state,
                    after_state=row.after_state,
                    occurred_at_utc=row.occurred_at_utc,
                )
                for row in rows
            ]

    def add_target_revisit(self, revisit: TargetRevisit) -> None:
        _require_utc(revisit.requested_at_utc, "requested_at_utc")
        if revisit.executed_at_utc is not None:
            _require_utc(revisit.executed_at_utc, "executed_at_utc")
        with self._session_factory() as session:
            session.add(
                orm.TargetRevisitRow(
                    id=revisit.revisit_id,
                    scope=revisit.scope,
                    reason=revisit.reason,
                    target_galaxy=revisit.target.galaxy if revisit.target is not None else None,
                    target_system=revisit.target.system if revisit.target is not None else None,
                    target_position=revisit.target.position if revisit.target is not None else None,
                    requested_at_utc=revisit.requested_at_utc,
                    executed_at_utc=revisit.executed_at_utc,
                    status=revisit.status,
                )
            )
            session.commit()

    # -- 派出后的松手等待 ------------------------------------------------------

    def record_flight_time(
        self, dispatch_id: UUID, flight: timedelta | None, dispatched_at_utc: datetime
    ) -> None:
        """存下飞行时长与预计战报时间。

        读不到飞行时间时两列都留空。等待调度器会把「未知」当成「立即尝试收取」，
        而不是无限等一个不知道何时抵达的战报。
        """
        with self._session_factory() as session:
            row = session.get(orm.AttackDispatchRow, dispatch_id)
            if row is None:
                raise ValueError(f"dispatch {dispatch_id} not found")
            if flight is None:
                row.flight_seconds = None
                row.expected_report_at_utc = None
            else:
                row.flight_seconds = int(flight.total_seconds())
                row.expected_report_at_utc = (
                    _require_utc(dispatched_at_utc, "dispatched_at_utc") + flight
                )
            session.commit()

    def pending_reports(self, run_id: UUID) -> list[PendingReport]:
        """本次运行已派出的攻击，以及各自是否已闭合。

        只看**真实**派遣：演习模式的记录不会产生战报，把它们算进来会让运行永远等不完。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.AttackDispatchRow, orm.BattleReportRow.id)
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.run_id == run_id,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dry_run.is_(False),
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            ).all()
            return [
                PendingReport(
                    dispatch_id=str(dispatch.id),
                    expected_report_at_utc=dispatch.expected_report_at_utc,
                    closed=report_id is not None,
                )
                for dispatch, report_id in rows
            ]

    # -- 调度器要问的事 --------------------------------------------------------

    def count_dispatches_since(self, target_kind: str, *, since: datetime) -> int:
        """某种目标在 `since` 之后真派出去了几发。

        海盗每天 32 次是游戏硬限制，超了会收到邮件且攻击被强制返回。
        只数**真实**派遣：演习记录不会消耗配额。
        """
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.AttackDispatchRow)
                    .join(
                        orm.AttackIntentRow,
                        orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                    )
                    .where(
                        orm.AttackIntentRow.target_kind == target_kind,
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dry_run.is_(False),
                        orm.AttackDispatchRow.dispatched_at_utc >= _require_utc(since, "since"),
                    )
                )
                or 0
            )

    def count_inflight(self, *, now_utc: datetime) -> int:
        """还在天上飞的舰队有几支。**跨 kind**——航线是全局资源。

        供调度器估算空闲航线：`usable_limit − 在飞数`。这个估算不含用户自己
        派出去的舰队，因此是乐观的；`reserved_lines` 正是为这段误差留的缓冲，
        而权威闸门仍在 runner 的 `LineCapacityGate`（看屏复核）。

        与 `pending_reports_for_kind` 不是同一个查询：那个按 kind 分、不带
        `> now`、也返回已闭合的行。这边问的是「舰队回来没有」，那边问的是
        「战报收了没有」——预计时间已过的那条已经不占航线，却正是最该去收的。

        飞行时间为 NULL 的不计入：读不到就当它不占位，宁可估高。估高了 runner
        起来空跑一轮，估低了则是航线空着不派——前者有闸门兜底，后者没有。
        """
        _require_utc(now_utc, "now_utc")
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.AttackDispatchRow)
                    .outerjoin(
                        orm.BattleReportRow,
                        orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                    )
                    .where(
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dry_run.is_(False),
                        orm.AttackDispatchRow.expected_report_at_utc > now_utc,
                        orm.BattleReportRow.id.is_(None),
                    )
                )
                or 0
            )

    def pending_reports_for_kind(
        self,
        target_kind: str,
        *,
        now_utc: datetime,
        grace: timedelta,
        max_age: timedelta,
    ) -> list[PendingReport]:
        """某种目标下尚未放弃的派遣，供 `ReportWaitPlanner` 判「该等还是该收」。

        按 `target_kind` 分开：混在一起，一条链路会替另一条判「该回去收了」。

        **「已放弃」是这里现算的规则，不是别处先写好的标记。** 写标记要有人在
        每次判缺失时去写，而那个人（调度器）还不存在；先落地标记再依赖它，
        中间这段时间查询会一条都排不掉。规则两条：

        1. 有预计时间的：过了预计时间再加 `grace` 还没战报 → 判缺失。
        2. 预计时间为 NULL 的：按 `dispatched_at_utc` 算，超过 `max_age` → 判缺失。

        第 2 条不能省。`plan()` 见到任何一条 NULL 就无条件返回 `COLLECT`，而库里
        现存的派遣**全是 NULL**——只写第 1 条，NULL 的派遣既永远「可收」又永远不
        被判缺失，海盗的「有活干」右半边被钉死为真，扫描永远抢不到空隙。
        """
        _require_utc(now_utc, "now_utc")
        expected = orm.AttackDispatchRow.expected_report_at_utc
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.AttackDispatchRow, orm.BattleReportRow.id)
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == target_kind,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dry_run.is_(False),
                    or_(
                        expected.is_(None),
                        expected > now_utc - grace,
                    ),
                    or_(
                        expected.is_not(None),
                        orm.AttackDispatchRow.dispatched_at_utc > now_utc - max_age,
                    ),
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            ).all()
            return [
                PendingReport(
                    dispatch_id=str(dispatch.id),
                    expected_report_at_utc=dispatch.expected_report_at_utc,
                    closed=report_id is not None,
                )
                for dispatch, report_id in rows
            ]

    def bot_dispatch_facts(
        self, coordinate: Coordinate, *, since: datetime | None
    ) -> list[DispatchFact]:
        """本轮针对这个 bot 已经**真的派出去**了哪些发、战报回来了没有。

        供 `domain.bot_round.phase_of` 判态。`since` 为空表示不限本轮
        （手工跑命令行时用）。

        `accepted` / `dry_run` 两个过滤缺一不可，与兄弟方法
        `count_dispatches_since` / `pending_reports_for_kind` 同口径：被游戏拒掉
        的和演习的都不会产生战报，算进来就是一条「已派出且永远收不到战报」，
        该目标永远停在 `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到。

        `skipped` 查的是 `target_revisits`，**按坐标+本轮**取，不是逐条派遣取：
        「分档说这个目标不值得打」是对**这一轮的这个坐标**下的判定，复查表里
        也没有指回某一条意图的列。`phase_of` 只用 `any(...)`，粒度对得上。
        """
        with self._session_factory() as session:
            statement = (
                select(orm.AttackIntentRow.preset_name, orm.BattleReportRow.id)
                .join(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == TARGET_KIND_BOT,
                    orm.AttackIntentRow.target_galaxy == coordinate.galaxy,
                    orm.AttackIntentRow.target_system == coordinate.system,
                    orm.AttackIntentRow.target_position == coordinate.position,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dry_run.is_(False),
                )
            )
            if since is not None:
                statement = statement.where(
                    orm.AttackIntentRow.created_at_utc >= _require_utc(since, "since")
                )
            skipped = _tier_negligible(session, coordinate, since=since)
            return [
                DispatchFact(
                    preset_name=preset_name,
                    has_report=report_id is not None,
                    skipped=skipped,
                )
                for preset_name, report_id in session.execute(statement).all()
            ]

    def mark_bot_target_skipped(self, coordinate: Coordinate, *, since: datetime) -> None:
        """把「分档说不值得打」记成本轮的一条 `target_revisits`。

        不记的话，下一趟又会重新分一次档、重新读一次战报，而结论不会变。

        **`since` 必填，且本轮真的探过路才写。** 分档结论是对刚读到的那份战报
        下的，本轮没有依据就没有判定可记。原先 `since` 可空，而 `None` 在查询侧
        的含义是「不限时间范围」：手工跑一次 `--probe --attack`，只要有一个目标
        被判成「不值得打」，这个坐标历史上每一轮的记录就全被刷掉。

        一轮只写一条：同轮里探了两次、或者这一趟重跑了，都不该越堆越多。
        """
        _require_utc(since, "since")
        with self._session_factory() as session:
            if _tier_negligible(session, coordinate, since=since):
                return
            if _latest_bot_intent_at(session, coordinate, since=since) is None:
                return
            session.add(
                orm.TargetRevisitRow(
                    id=uuid4(),
                    scope=REVISIT_SCOPE_TIER_NEGLIGIBLE,
                    reason="分档判定不值得打",
                    target_galaxy=coordinate.galaxy,
                    target_system=coordinate.system,
                    target_position=coordinate.position,
                    requested_at_utc=datetime.now(UTC),
                    status=REVISIT_STATUS_DONE,
                )
            )
            session.commit()

    def set_resume_at(self, run_id: UUID, resume_at_utc: datetime | None) -> None:
        with self._session_factory() as session:
            row = session.get(orm.RunInstance, run_id)
            if row is None:
                raise ValueError(f"run {run_id} not found")
            row.resume_at_utc = (
                _require_utc(resume_at_utc, "resume_at_utc") if resume_at_utc else None
            )
            session.commit()

    def note_session_attempt(self, run_id: UUID, *, succeeded: bool) -> int:
        """记一次登录尝试，返回当前连续失败次数。成功则归零。"""
        with self._session_factory() as session:
            row = session.get(orm.RunInstance, run_id)
            if row is None:
                raise ValueError(f"run {run_id} not found")
            row.session_attempts = 0 if succeeded else row.session_attempts + 1
            attempts = row.session_attempts
            session.commit()
            return attempts

    # -- 调度器的三行任务、单行配置与子进程台账 --------------------------------

    def ensure_mission_rows(self, *, now_utc: datetime) -> None:
        """补齐三行任务和单行配置，缺什么补什么。

        迁移里没有 `bulk_insert`：种子数据放在迁移里，改一次默认值就得再写一条
        迁移，而且老库和新库会各拿到一份不同的默认值。放这里则是每次开机对一遍。

        **只补不改。** 第二遍要是覆盖，用户拖出来的优先级、填好的参数每次重启
        都会被抹掉。
        """
        _require_utc(now_utc, "now_utc")
        with self._session_factory() as session:
            existing = set(session.scalars(select(orm.MissionTaskRow.kind)).all())
            for kind, enabled, priority, params in _MISSION_SEEDS:
                if kind.value in existing:
                    continue
                session.add(
                    orm.MissionTaskRow(
                        kind=kind.value,
                        enabled=enabled,
                        priority=priority,
                        params_json=params,
                        consecutive_failures=0,
                        created_at_utc=now_utc,
                        updated_at_utc=now_utc,
                    )
                )
            if session.get(orm.SchedulerConfigRow, 1) is None:
                session.add(orm.SchedulerConfigRow(id=1))
            session.commit()

    def mission_tasks(self) -> list[orm.MissionTaskRow]:
        """三条链路的当前配置，按 (priority, id) 升序。

        并列的 priority 之间用 id 决出胜负：`priority` 列没有唯一约束，而
        `decide()` 的排序是稳定排序——输入次序不确定，谁先起就成了随机的。
        """
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(orm.MissionTaskRow).order_by(
                        orm.MissionTaskRow.priority, orm.MissionTaskRow.id
                    )
                ).all()
            )

    def scheduler_config(self) -> orm.SchedulerConfigRow:
        with self._session_factory() as session:
            row = session.get(orm.SchedulerConfigRow, 1)
            if row is None:
                raise ValueError("scheduler_config 还没初始化；先调 ensure_mission_rows()")
            return row

    def update_mission_task(
        self,
        kind: MissionKind,
        *,
        enabled: bool | None = None,
        priority: int | None = None,
        params_json: str | None = None,
    ) -> None:
        """页面上改开关、拖顺序、编参数走这一个入口。

        `None` 一律表示「这次不动它」，而不是「清空」：三样东西各自独立，
        改一样就把另外两样重置回默认是页面上最容易出的那种错。

        改任何一样都清掉 `disabled_reason`：自动停用是对**旧配置**下的判定，
        用户既然动手改了，就该给它一次重新开始的机会——否则参数填错一次，
        修好了也永远起不来。
        """
        with self._session_factory() as session:
            row = _mission_task(session, kind)
            if enabled is not None:
                row.enabled = enabled
            if priority is not None:
                row.priority = priority
            if params_json is not None:
                row.params_json = params_json
            row.disabled_reason = None
            row.consecutive_failures = 0
            row.updated_at_utc = datetime.now(UTC)
            session.commit()

    def begin_bot_round(self, *, now_utc: datetime) -> None:
        """「重开一轮」：把 `round_started_at_utc` 推到当前。

        上一轮的战报据此被排除在完成判据之外——不推的话，昨天打完的那批目标
        今天仍然算「已完成」，新的一轮永远开不起来。
        """
        _require_utc(now_utc, "now_utc")
        with self._session_factory() as session:
            row = _mission_task(session, MissionKind.BOT)
            row.round_started_at_utc = now_utc
            row.updated_at_utc = now_utc
            session.commit()

    def begin_mission_run(
        self,
        kind: MissionKind,
        *,
        command: Sequence[str],
        pid: int | None,
        started_at_utc: datetime,
        log_path: str,
    ) -> UUID:
        """起了一个子进程，记一行。返回的 id 用来在它结束时回填。"""
        _require_utc(started_at_utc, "started_at_utc")
        run_id = uuid4()
        with self._session_factory() as session:
            session.add(
                orm.MissionRunRow(
                    id=run_id,
                    kind=kind.value,
                    # 存成一行是给人看的。argv 列表在页面上排不开，而这一列的
                    # 唯一用途就是事后翻账「那一轮到底打了谁」。
                    command=" ".join(command),
                    pid=pid,
                    started_at_utc=started_at_utc,
                    log_path=log_path,
                )
            )
            session.commit()
        return run_id

    def finish_mission_run(
        self, run_id: UUID, *, ended_at_utc: datetime, exit_code: int | None, stopped_by: str
    ) -> None:
        _require_utc(ended_at_utc, "ended_at_utc")
        with self._session_factory() as session:
            row = session.get(orm.MissionRunRow, run_id)
            if row is None:
                raise ValueError(f"mission run {run_id} not found")
            row.ended_at_utc = ended_at_utc
            row.exit_code = exit_code
            row.stopped_by = stopped_by
            session.commit()

    def mission_runs(self, *, limit: int) -> list[orm.MissionRunRow]:
        """最近的子进程记录，新的在前。"""
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(orm.MissionRunRow)
                    .order_by(orm.MissionRunRow.started_at_utc.desc())
                    .limit(limit)
                ).all()
            )

    def mark_orphan_mission_runs(self, *, ended_at_utc: datetime) -> int:
        """开机时把「上次没走正常关闭路径」的行标出来，返回条数。

        **不按 pid 自动杀。** pid 会被系统回收复用，照着一个可能已经换了主人的
        号码开枪，比留个警告糟得多。这里只标记，处置交给页面上的红条和
        「强制结束」按钮。

        `ended_at_utc` 也一并补上：留空的话这一行会永远显示成「运行中」，
        而它恰恰是「我们已经不知道它死活了」的意思。
        """
        _require_utc(ended_at_utc, "ended_at_utc")
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(orm.MissionRunRow).where(orm.MissionRunRow.ended_at_utc.is_(None))
                ).all()
            )
            for row in rows:
                row.ended_at_utc = ended_at_utc
                row.stopped_by = STOPPED_BY_UNKNOWN
            session.commit()
            return len(rows)

    def last_mission_starts(self) -> dict[MissionKind, datetime]:
        """每条链路上一次**启动**的时刻，供重启冷却判据用。

        取启动而不是结束：一个刚起来就秒退的 runner 正是最该被节流的那种，
        按结束时刻算等于对它完全不设防。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.MissionRunRow.kind, func.max(orm.MissionRunRow.started_at_utc)).group_by(
                    orm.MissionRunRow.kind
                )
            ).all()
        result: dict[MissionKind, datetime] = {}
        for kind, started_at in rows:
            try:
                result[MissionKind(kind)] = started_at
            except ValueError:
                # 库里出现不认识的 kind（手改或旧版本留下的）不该让调度器崩掉：
                # 它只是没有冷却记录而已。
                continue
        return result

    def record_mission_failure(
        self, kind: MissionKind, *, exit_code: int | None, limit: int
    ) -> int:
        """记一次异常退出，返回当前连续次数；到 `limit` 就自动停用。

        没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。
        失败多半是「窗口抢不到前台」或「甩鼠标触发 FAILSAFE」，重启只会再来一遍。
        """
        with self._session_factory() as session:
            row = _mission_task(session, kind)
            row.consecutive_failures += 1
            if row.consecutive_failures >= limit and row.disabled_reason is None:
                row.disabled_reason = (
                    f"连续 {row.consecutive_failures} 次异常退出（退出码 {exit_code}）"
                )
            failures = row.consecutive_failures
            row.updated_at_utc = datetime.now(UTC)
            session.commit()
            return failures

    def clear_mission_failures(self, kind: MissionKind) -> None:
        """跑完一轮且退出码为 0。「连续」是连续，中间成功过就重新数。"""
        with self._session_factory() as session:
            row = _mission_task(session, kind)
            row.consecutive_failures = 0
            row.updated_at_utc = datetime.now(UTC)
            session.commit()

    def disable_mission_task(self, kind: MissionKind, reason: str) -> None:
        """参数不合格之类的配置问题：重试一万次也一样，直接停用并写清原因。"""
        with self._session_factory() as session:
            row = _mission_task(session, kind)
            row.disabled_reason = reason
            row.updated_at_utc = datetime.now(UTC)
            session.commit()


def _mission_task(session: Session, kind: MissionKind) -> orm.MissionTaskRow:
    row = session.scalar(select(orm.MissionTaskRow).where(orm.MissionTaskRow.kind == kind.value))
    if row is None:
        raise ValueError(f"mission_tasks 里没有 {kind.value} 这一行；先调 ensure_mission_rows()")
    return row


def _require_type[T](value: object, expected: type[T], label: str) -> T:
    if not isinstance(value, expected):
        raise TypeError(f"{label} must be {expected.__name__}, got {type(value).__name__}")
    return value


def _latest_bot_intent_at(
    session: Session, coordinate: Coordinate, *, since: datetime
) -> datetime | None:
    """本轮针对这个 bot 最新那条意图是什么时候建的；本轮没有则 None。

    只取最新一条而不是「有没有」，是为了让「那一条」这个意思留在代码里：
    分档结论是对**最近那份战报**下的，不是对这个坐标的全部历史下的。
    """
    return session.scalar(
        select(orm.AttackIntentRow.created_at_utc)
        .where(
            orm.AttackIntentRow.target_kind == TARGET_KIND_BOT,
            orm.AttackIntentRow.target_galaxy == coordinate.galaxy,
            orm.AttackIntentRow.target_system == coordinate.system,
            orm.AttackIntentRow.target_position == coordinate.position,
            orm.AttackIntentRow.created_at_utc >= since,
        )
        .order_by(orm.AttackIntentRow.created_at_utc.desc())
        .limit(1)
    )


def _tier_negligible(session: Session, coordinate: Coordinate, *, since: datetime | None) -> bool:
    """本轮这个坐标有没有被分档判成「不值得打」。

    读写两侧共用，判据才不会分叉：写的时候拿它去重（一轮写一条就够），
    读的时候拿它填 `DispatchFact.skipped`。
    """
    statement = (
        select(func.count())
        .select_from(orm.TargetRevisitRow)
        .where(
            orm.TargetRevisitRow.scope == REVISIT_SCOPE_TIER_NEGLIGIBLE,
            orm.TargetRevisitRow.target_galaxy == coordinate.galaxy,
            orm.TargetRevisitRow.target_system == coordinate.system,
            orm.TargetRevisitRow.target_position == coordinate.position,
        )
    )
    if since is not None:
        statement = statement.where(orm.TargetRevisitRow.requested_at_utc >= since)
    return bool(session.scalar(statement) or 0)


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


def _cursor_from_run(run: orm.RunInstance) -> Coordinate | None:
    if run.cursor_galaxy is None or run.cursor_system is None or run.cursor_position is None:
        return None
    return Coordinate(run.cursor_galaxy, run.cursor_system, run.cursor_position)


def _set_run_cursor(run: orm.RunInstance, coordinate: Coordinate) -> None:
    run.cursor_galaxy = coordinate.galaxy
    run.cursor_system = coordinate.system
    run.cursor_position = coordinate.position


def _pending_from_run(run: orm.RunInstance) -> Coordinate | None:
    if run.pending_galaxy is None or run.pending_system is None or run.pending_position is None:
        return None
    return Coordinate(run.pending_galaxy, run.pending_system, run.pending_position)


def _set_run_pending(run: orm.RunInstance, coordinate: Coordinate) -> None:
    run.pending_galaxy = coordinate.galaxy
    run.pending_system = coordinate.system
    run.pending_position = coordinate.position


def _clear_run_pending(run: orm.RunInstance) -> None:
    run.pending_galaxy = None
    run.pending_system = None
    run.pending_position = None


def _range_start(row: orm.ScanRangeRow) -> Coordinate:
    return Coordinate(row.start_galaxy, row.start_system, row.start_position)


def _range_end(row: orm.ScanRangeRow) -> Coordinate:
    return Coordinate(row.end_galaxy, row.end_system, row.end_position)


def _bot_target_for(session: Session, coordinate: Coordinate) -> orm.BotTargetRow | None:
    return session.scalar(
        select(orm.BotTargetRow).where(
            orm.BotTargetRow.galaxy == coordinate.galaxy,
            orm.BotTargetRow.system == coordinate.system,
            orm.BotTargetRow.position == coordinate.position,
        )
    )


def _unmatched_dispatch_candidates(
    session: Session,
    report: BattleReport,
) -> list[orm.AttackDispatchRow]:
    linked = select(orm.BattleReportRow.dispatch_id).where(
        orm.BattleReportRow.dispatch_id.is_not(None)
    )
    rows = session.scalars(
        select(orm.AttackDispatchRow)
        .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
        .where(
            orm.AttackIntentRow.origin_galaxy == report.attacker_origin.galaxy,
            orm.AttackIntentRow.origin_system == report.attacker_origin.system,
            orm.AttackIntentRow.origin_position == report.attacker_origin.position,
            orm.AttackIntentRow.target_galaxy == report.defender_target.galaxy,
            orm.AttackIntentRow.target_system == report.defender_target.system,
            orm.AttackIntentRow.target_position == report.defender_target.position,
            orm.AttackDispatchRow.accepted.is_(True),
            orm.AttackDispatchRow.dry_run.is_(False),
            orm.AttackDispatchRow.id.not_in(linked),
        )
        .order_by(orm.AttackDispatchRow.dispatched_at_utc)
    ).all()
    return list(rows)


def _close_in_time(dispatch_at: datetime | None, reported_at: datetime) -> bool:
    if dispatch_at is None:
        return False
    return abs(dispatch_at - reported_at) <= MATCH_TIME_TOLERANCE


def _snapshot_counts(session: Session, report_id: UUID, side: str) -> dict[str, int]:
    rows = session.scalars(
        select(orm.FleetSnapshotRow).where(
            orm.FleetSnapshotRow.report_id == report_id,
            orm.FleetSnapshotRow.side == side,
        )
    ).all()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.ship_type] = counts.get(row.ship_type, 0) + row.count
    return counts


def _earlier_ship_types(
    session: Session,
    after: orm.BattleReportRow,
    side: str,
) -> set[str]:
    rows = session.scalars(
        select(orm.FleetSnapshotRow.ship_type)
        .join(orm.BattleReportRow, orm.BattleReportRow.id == orm.FleetSnapshotRow.report_id)
        .where(
            orm.BattleReportRow.defender_target_galaxy == after.defender_target_galaxy,
            orm.BattleReportRow.defender_target_system == after.defender_target_system,
            orm.BattleReportRow.defender_target_position == after.defender_target_position,
            orm.FleetSnapshotRow.side == side,
            orm.BattleReportRow.reported_at_utc < after.reported_at_utc,
        )
        .distinct()
    ).all()
    return set(rows)


def _build_ship_diffs(
    before_counts: dict[str, int],
    after_counts: dict[str, int],
    earlier_ship_types: set[str],
) -> tuple[ShipTypeDiff, ...]:
    diffs: list[ShipTypeDiff] = []
    for ship_type in sorted(set(before_counts) | set(after_counts)):
        before = before_counts.get(ship_type, 0)
        after = after_counts.get(ship_type, 0)
        change = after - before
        percent_change = None if before == 0 else (change / before) * 100.0
        if before == 0 and after > 0:
            status = "ADDED"
        elif before > 0 and after == 0:
            status = "REMOVED"
        elif change > 0:
            status = "INCREASED"
        elif change < 0:
            status = "REDUCED"
        else:
            status = "UNCHANGED"
        diffs.append(
            ShipTypeDiff(
                ship_type=ship_type,
                before_count=before,
                after_count=after,
                absolute_change=change,
                percent_change=percent_change,
                status=status,
                first_seen=after > 0 and ship_type not in earlier_ship_types,
            )
        )
    return tuple(diffs)
