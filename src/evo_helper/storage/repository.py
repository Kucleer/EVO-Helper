"""SQLAlchemy-backed RepositoryPort implementation and history queries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from evo_helper.domain.bot_round import DispatchFact
from evo_helper.domain.coordinates import next_coordinate_after
from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.pirate_round import AttackFact, PiratePhase, phase_for
from evo_helper.domain.ports import CoordinateClaim
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    CoordinateScan,
    FleetDiff,
    ReportHistoryEntry,
    ScoutReport,
    ScoutTriggerShip,
    ShipTypeDiff,
    StateEvent,
    TargetRevisit,
)
from evo_helper.domain.report_wait import (
    MAX_REPORT_AGE,
    UNKNOWN_LINE_HOLD,
    PendingReport,
    line_free_at,
)
from evo_helper.domain.scheduler import MissionKind
from evo_helper.domain.scout_verdict import verdict_of_record
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


@dataclass(frozen=True)
class DueDispatch:
    """一发**理论上已经该有战报**的攻击派遣。

    开工那一趟信箱是**由库驱动**的（用户口径 2026-08-11：「先读数据库中理论上
    已经到达的报告，然后更新数据再开始后面的任务」）：先从库里算出这张单子，
    再带着它去信箱找，而不是「翻到什么算什么」。判据见 `due_attack_dispatches`。
    """

    dispatch_id: UUID
    target: Coordinate
    dispatched_at_utc: datetime
    #: 预计战报时刻；飞行时间没读到时为 None（那一档当作「现在就该有了」）。
    expected_report_at_utc: datetime | None


@dataclass(frozen=True)
class DailyAttackStatus:
    """某条链路某个 UTC 日的攻击状态，**一行读回**，不必再翻信箱。

    用户口径（2026-08-11）：「每天的海盗次数（状态）也可以存库，这样也可以
    快速回读。」重启之后「今天打了几发、还剩几发、还有几发在等战报」要立刻答得上。
    """

    day_utc: str
    target_kind: str
    #: 信箱里数到的本链路战报份数（观测下界）。
    observed_reports: int
    #: 上面那个数是不是全天（有没有一直翻到昨天的报告）。
    complete: bool
    #: 库里当天已被游戏接受的攻击派遣数（助手自己派出去的那一侧）。
    dispatched_count: int
    #: 当天**已用配额** = 两个下界取大，且按 UTC 日只增不减。判据与
    #: `count_dispatches_since` 完全一致，这一列只是把它固化下来供回读。
    attacks_used: int
    #: 写这一刻还有几发已派出、还没有战报、且还没被判放弃。
    #: **这是瞬时状态，不是计数**，所以它可增可减——把它也做成只增不减，
    #: 舰队全回来之后那个数会永远停在最高水位，回读出来的「还在等」全是假的。
    awaiting_reports: int
    reconciled_at_utc: datetime


@dataclass(frozen=True)
class PirateProgress:
    """一个海盗目标在某段时间里走到哪一步了，供控制台一行一行显示。

    用户口径（2026-08-11）：「侦查海盗战果获得战报后，有 2 个结果，需要更新状态：
    不触发攻击 / 触发攻击 / 攻击完成（获得攻击完成战报后更新）。」

    **一列判定都不落库。** `phase` 与 `verdict` 都是查询时按现行规则现算的
    （见 `domain.scout_verdict` 与 `domain.records.ScoutReport` 的注释）：
    门槛与舰种表会变，钉进库里的结论过两天就没人说得清是按哪版算的。
    """

    target: Coordinate
    phase: PiratePhase
    #: 侦察判定，`domain.scout_verdict.VERDICT_*` 之一；侦察报告还没回就是 None。
    #: ⚠️ `UNREADABLE` **不是**「不触发攻击」，是「没看清」——两者的处置相反。
    verdict: str | None
    #: 拿来算 `verdict` 的那份侦察报告的报告时刻；没有报告就是 None。
    scout_reported_at_utc: datetime | None
    #: 本轮针对它真的派出去（且被游戏接受）的攻击发数。
    attack_count: int
    #: 其中已经收到战报的发数。`attack_count > 0 and attack_reports == attack_count`
    #: 就是「攻击完成」。
    attack_reports: int
    #: 最近一发攻击的派出时刻；一发都没派就是 None。
    latest_attack_at_utc: datetime | None


def _coordinate_sort_key(coordinate: Coordinate) -> tuple[int, int, int]:
    return (coordinate.galaxy, coordinate.system, coordinate.position)


def _pirate_progress_for(
    target: Coordinate,
    *,
    scouted: bool,
    scout: ScoutReport | None,
    attacks: tuple[AttackFact, ...],
    latest_attack_at_utc: datetime | None,
) -> PirateProgress:
    verdict = verdict_of_record(scout) if scout is not None else None
    return PirateProgress(
        target=target,
        phase=phase_for(scouted=scouted, verdict=verdict, attacks=attacks),
        verdict=verdict,
        scout_reported_at_utc=scout.reported_at_utc if scout is not None else None,
        attack_count=len(attacks),
        attack_reports=sum(1 for item in attacks if item.has_report),
        latest_attack_at_utc=latest_attack_at_utc,
    )


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
                    accepted=record.accepted,
                    evidence_artifact_id=record.evidence_artifact_id,
                    mission_kind=record.mission_kind,
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
            _link_dispatch(
                session,
                report_row,
                origin=record.attacker_origin,
                target=record.defender_target,
                reported_at=record.reported_at_utc,
            )
            session.commit()

    def rematch_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        """把库里那份**还没认领上派遣**的战报再认领一次。认上了返回 True。

        ## 为什么非有这条路不可

        `append_report` 只在**写入的那一刻**认领一次，认不上就把 `dispatch_id`
        留空、`match_status` 记 `AMBIGUOUS`，此后再没有任何代码回头看它一眼。
        于是「认领判据修好了」并不能让已经在库里的那些行自己接上——而
        `has_report_at` 那道去重又保证了它们**永远不会被重新读一遍**：
        下一趟翻信箱看到同一封，只会说一句「库里已有」然后早停。

        实机（生产库，2026-08-11）四发 AAA 全卡在这个组合里：战报确实读进来了、
        `dispatch_id` 全空、攻击日志的战果列全空、四个目标全停在「待战报」。

        所以由库驱动的那一趟（见 `due_attack_dispatches`）撞见一封「库里已有」时，
        要在这里再认一次——不重开邮件、不重读像素，只是拿现在的判据把旧行重算。

        ⚠️ **只碰 `dispatch_id` 为空的行。** 已经认领上的不重算：那会把一次判据
        变动变成一次静默的改档，而 `dispatch_id` 上有唯一约束，重算过程中一旦
        算错，原本对的那一发也一起丢了。

        ⚠️ **一行都不新建。** 这里只更新已有的战报行，绝不补 `attack_dispatches`。
        """
        _require_utc(reported_at_utc, "reported_at_utc")
        matched = False
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.BattleReportRow).where(
                    orm.BattleReportRow.defender_target_galaxy == target.galaxy,
                    orm.BattleReportRow.defender_target_system == target.system,
                    orm.BattleReportRow.defender_target_position == target.position,
                    orm.BattleReportRow.reported_at_utc == reported_at_utc,
                    orm.BattleReportRow.dispatch_id.is_(None),
                )
            ).all()
            for row in rows:
                origin = Coordinate(
                    row.attacker_origin_galaxy,
                    row.attacker_origin_system,
                    row.attacker_origin_position,
                )
                matched |= _link_dispatch(
                    session,
                    row,
                    origin=origin,
                    target=target,
                    reported_at=row.reported_at_utc,
                )
            session.commit()
        return matched

    # -- 侦察报告 -------------------------------------------------------------

    def has_scout_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        """这个目标这个**报告时刻**的侦察报告是不是已经在库里了。

        口径与 `has_report_at` 完全一致（目标 + 报告时间），理由也一样：活链路
        每一轮都会去信箱翻同样那几行，而报告时间是游戏自己写在报告上的字，
        不受本地时钟与重跑影响。没有这道去重，一份读到过的侦察报告会每趟复制一行。

        补录入口（`tools.backfill_scout_reports`）与活链路用的是同一条判据，
        所以两者可以随便交叉跑，谁先跑到都不会写重。
        """
        _require_utc(reported_at_utc, "reported_at_utc")
        with self._session_factory() as session:
            return _scout_report_exists(session, target, reported_at_utc)

    def append_scout_report(self, report: object) -> bool:
        """写一份侦察报告；同一份（目标 + 报告时间）已经在库里就不写，返回 False。

        ⚠️ **每一格原样存。** `ScoutTriggerShip.count is None` 表示这一格没读出来，
        写进去仍是 `NULL`——不许在这里补成 0。把读空补成 0 就等于把「没看清」
        记成「这里是空的」，而三值判定整个建立在这个区分上
        （见 `domain.records.ScoutTriggerShip`）。

        去重在同一个 session 里预检，是为了让正常路径不必靠 `IntegrityError` 收场；
        真正的保证是表上的 `uq_scout_reports_target_time`。
        """
        record = _require_type(report, ScoutReport, "scout report")
        _require_utc(record.reported_at_utc, "reported_at_utc")
        with self._session_factory() as session:
            if _scout_report_exists(session, record.target, record.reported_at_utc):
                return False
            session.add(
                orm.ScoutReportRow(
                    id=record.report_id,
                    reported_at_utc=record.reported_at_utc,
                    raw_time_text=record.raw_time_text,
                    origin_galaxy=record.origin.galaxy,
                    origin_system=record.origin.system,
                    origin_position=record.origin.position,
                    target_galaxy=record.target.galaxy,
                    target_system=record.target.system,
                    target_position=record.target.position,
                )
            )
            for ordinal, entry in enumerate(record.trigger_ships):
                session.add(
                    orm.ScoutTriggerShipRow(
                        report_id=record.report_id,
                        ordinal=ordinal,
                        ship_type=entry.ship_type,
                        # `None` 原样落成 NULL。**不要 `or 0`。**
                        count=entry.count,
                    )
                )
            session.commit()
        return True

    def list_scout_reports(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        target: Coordinate | None = None,
    ) -> list[ScoutReport]:
        """按报告时间升序取出侦察报告，每一格原样带回来。

        `since` / `until` 是**报告时间**上的左闭右开区间（同样是 UTC）——
        「UTC+0 的 8/11 那一天」就是 `[08-11T00:00Z, 08-12T00:00Z)`。
        """
        if since is not None:
            _require_utc(since, "since")
        if until is not None:
            _require_utc(until, "until")
        with self._session_factory() as session:
            statement = select(orm.ScoutReportRow).order_by(
                orm.ScoutReportRow.reported_at_utc, orm.ScoutReportRow.id
            )
            if since is not None:
                statement = statement.where(orm.ScoutReportRow.reported_at_utc >= since)
            if until is not None:
                statement = statement.where(orm.ScoutReportRow.reported_at_utc < until)
            if target is not None:
                statement = statement.where(
                    orm.ScoutReportRow.target_galaxy == target.galaxy,
                    orm.ScoutReportRow.target_system == target.system,
                    orm.ScoutReportRow.target_position == target.position,
                )
            rows = list(session.scalars(statement).all())
            ships: dict[UUID, list[orm.ScoutTriggerShipRow]] = {}
            if rows:
                found = session.scalars(
                    select(orm.ScoutTriggerShipRow)
                    .where(orm.ScoutTriggerShipRow.report_id.in_([row.id for row in rows]))
                    .order_by(orm.ScoutTriggerShipRow.ordinal, orm.ScoutTriggerShipRow.id)
                ).all()
                for entry in found:
                    ships.setdefault(entry.report_id, []).append(entry)
            return [
                ScoutReport(
                    report_id=row.id,
                    reported_at_utc=row.reported_at_utc,
                    raw_time_text=row.raw_time_text,
                    origin=Coordinate(row.origin_galaxy, row.origin_system, row.origin_position),
                    target=Coordinate(row.target_galaxy, row.target_system, row.target_position),
                    trigger_ships=tuple(
                        # `NULL` 原样带回成 `None`。**不要 `or 0`**：读回来时把它
                        # 补成 0，和存的时候补成 0 一样是把「没看清」记成「空的」。
                        ScoutTriggerShip(ship_type=entry.ship_type, count=entry.count)
                        for entry in ships.get(row.id, ())
                    ),
                )
                for row in rows
            ]

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
        """存下飞行时长，以及由它派生的**两个钟**。

        - `expected_report_at_utc`：出发 + 飞行时长 × 1。战报在抵达时产生。
        - `line_free_at_utc`：航线什么时候空出来。倍数按发次分岔，见
          `domain.report_wait.line_free_at`。

        两个钟一起算、一起存，是为了让「用错列」这个错误没有藏身之处：
        谁要改其中一个，另一个就在同一屏里。

        读不到飞行时间时三列都留空。等待调度器会把「未知」当成「立即尝试收取」，
        而不是无限等一个不知道何时抵达的战报；航线那一侧的 NULL 则当作不占航线。
        """
        with self._session_factory() as session:
            row = session.get(orm.AttackDispatchRow, dispatch_id)
            if row is None:
                raise ValueError(f"dispatch {dispatch_id} not found")
            if flight is None:
                row.flight_seconds = None
                row.expected_report_at_utc = None
                row.line_free_at_utc = None
            else:
                dispatched = _require_utc(dispatched_at_utc, "dispatched_at_utc")
                intent = session.get(orm.AttackIntentRow, row.intent_id)
                if intent is None:  # pragma: no cover - 外键保证不会发生
                    raise ValueError(f"dispatch {dispatch_id} has no intent")
                row.flight_seconds = int(flight.total_seconds())
                row.expected_report_at_utc = dispatched + flight
                row.line_free_at_utc = line_free_at(
                    dispatched,
                    flight,
                    mission_kind=row.mission_kind,
                    preset_name=intent.preset_name,
                )
            session.commit()

    def pending_reports(self, run_id: UUID) -> list[PendingReport]:
        """本次运行已派出的攻击，以及各自是否已闭合。

        只看被游戏**接受**的那些：被拒的没有舰队飞出去，也就永远不会有战报，
        算进来会让运行永远等不完。
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
        """某种目标在 `since` 之后真**打**出去了几发。

        海盗每天 32 次是游戏硬限制，超了会收到邮件且攻击被强制返回。
        只数被游戏**接受**的：被拒的那一发没有舰队飞出去，不消耗配额。

        **只数攻击发。** 侦察也是打向海盗的，只按 `target_kind` 过滤的话，
        一轮 4 发侦察会各吃掉一次攻击额度——当天 32 次以 4 倍速度消失，
        而且完全静默、不报任何错。侦察占的是航线，不是配额，见 `count_inflight`。

        ## 库内计数与开工对账，按 UTC 日**取大**

        这张表只知道助手自己派出去过什么。库外发生过的事它一概不知道：用户手动
        打的、上一次进程崩在写库之前的、换过库之后游戏里仍然算数的那些。数少了
        就会超额。开工对账（`tools.pirate_loop.PirateLoop.reconcile_today`）从信箱
        里数当天的战报，把观测值写进 `daily_reconciliations`，这里把它折进来。

        两边都只是下界，而且证据互相独立：

        - 库内计数知道**刚派出、战报还没到**的那几发，信箱不知道。
        - 信箱知道**库外发生过**的那几发，库不知道。

        所以谁也不能单独当答案，按 UTC 日取两者的大者。取大是能被证据支持的最紧
        的下界，只会让助手提前收手；取小或相加都会错——相加会把同一发数两遍。

        **没翻到底的那次对账照样算数。** `complete=False` 只说明「今天至少这么多」，
        而它仍然是一个真实存在的下界，而且往往比库内计数更紧。把它扔掉等于回到
        「只按库算」，也就是回到会超额的那一侧。`complete` 只作诊断用（日志里要说清
        那个数是不是全天），不作过滤条件。

        方向一律往「打得更少」倒：这个数偏大只会让助手提前收手，偏小才会白飞舰队。

        部分覆盖的那一天照样整天折进来（例如 `since` 落在当天中午）：对账的粒度
        就是 UTC 日，而配额窗口本来也是整个 UTC 日；宁可把额度算紧一点。
        """
        floor = _require_utc(since, "since")
        with self._session_factory() as session:
            dispatched = func.date(orm.AttackDispatchRow.dispatched_at_utc)
            per_day = {
                str(day): int(count)
                for day, count in session.execute(
                    select(dispatched, func.count())
                    .select_from(orm.AttackDispatchRow)
                    .join(
                        orm.AttackIntentRow,
                        orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                    )
                    .where(
                        orm.AttackIntentRow.target_kind == target_kind,
                        orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dispatched_at_utc >= floor,
                    )
                    .group_by(dispatched)
                ).all()
            }
            observed = {
                str(day): int(count)
                for day, count in session.execute(
                    select(
                        orm.DailyReconciliationRow.day_utc,
                        orm.DailyReconciliationRow.observed_reports,
                    ).where(
                        orm.DailyReconciliationRow.target_kind == target_kind,
                        orm.DailyReconciliationRow.day_utc >= floor.strftime("%Y-%m-%d"),
                    )
                ).all()
            }
        return sum(
            max(per_day.get(day, 0), observed.get(day, 0))
            for day in per_day.keys() | observed.keys()
        )

    def oldest_open_attack_at(
        self, target_kind: str, *, now_utc: datetime, max_age: timedelta
    ) -> datetime | None:
        """这种目标下，**最早那一发还在等战报**的攻击派于何时；没有就 None。

        供开工翻信箱时定下界（`tools.pirate_loop.PirateLoop._report_floor`）：
        战报不可能早于产生它的那一发，所以再往下翻就没有意义了。

        **只看攻击发。** 侦察发不产生 `battle_reports`（侦察报告走信箱里另一条路），
        算进来就是一条永远不闭合的记录，会让下界永远停在那一发上、每趟都往回
        多翻几屏。这条排除与 `pending_reports_for_kind` / `count_dispatches_since`
        同一个口径。

        **超过 `max_age` 的不算。** 那些已经被判「战报永远不会来」了
        （`_unmatched_dispatch_candidates` 与 `bot_dispatch_facts` 用的是同一个常量），
        把下界钉在一发已经放弃的派遣上，只会让每一趟都白翻到底。
        """
        floor = _require_utc(now_utc, "now_utc") - max_age
        with self._session_factory() as session:
            return session.scalar(
                select(func.min(orm.AttackDispatchRow.dispatched_at_utc))
                .select_from(orm.AttackDispatchRow)
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == target_kind,
                    orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.BattleReportRow.id.is_(None),
                    orm.AttackDispatchRow.dispatched_at_utc > floor,
                )
            )

    def due_attack_dispatches(
        self, target_kind: str, *, now_utc: datetime, max_age: timedelta
    ) -> list[DueDispatch]:
        """**这张单子**：已派出、理论上战报早该到了、库里却还没有的那些攻击发。

        开工翻信箱由它驱动（用户口径 2026-08-11：「先读数据库中理论上已经到达的
        报告，然后更新数据再开始后面的任务」）。原先那一趟是「翻到什么算什么，
        读到库里已有的就早停」——而早停假定「库里已有 ⇒ 往下都读过了」，
        这个假定在**报告已入库、却没接到该接的那一发上**时是假的
        （见 `rematch_report_at`），于是那几发永远补不回来。

        单子上还有条目时，早停就不能只凭「撞见一封已入库的」收工；单子空了
        才收工。取舍与落点写在 `tools.pirate_loop.PirateLoop._ingest_report_row`。

        判据四条，每一条都与兄弟方法同源：

        1. `target_kind` + `accepted` + `mission_kind == ATTACK`——只有攻击发会有
           战报（口径同 `pending_reports_for_kind` / `oldest_open_attack_at`）。
        2. 还没有战报认领它。
        3. **理论上已经该有了**：`expected_report_at_utc` 已过；那一列为 NULL 的
           （飞行时间没读到）当作「现在就该有」，与 `ReportWaitPlanner.plan` 里
           「未知即立即收取」同一个降级方向。
        4. **还没被判放弃**：派出超过 `max_age` 的不算。少了这一条，一发战报真的
           丢了的派遣会让单子**永远非空**，早停就此彻底失效——每一趟都要把开封
           预算烧满，而那是每封八秒。
        """
        now = _require_utc(now_utc, "now_utc")
        expected = orm.AttackDispatchRow.expected_report_at_utc
        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.AttackDispatchRow.id,
                    orm.AttackIntentRow.target_galaxy,
                    orm.AttackIntentRow.target_system,
                    orm.AttackIntentRow.target_position,
                    orm.AttackDispatchRow.dispatched_at_utc,
                    expected,
                )
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == target_kind,
                    orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.BattleReportRow.id.is_(None),
                    orm.AttackDispatchRow.dispatched_at_utc > now - max_age,
                    or_(expected.is_(None), expected <= now),
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            ).all()
        return [
            DueDispatch(
                dispatch_id=dispatch_id,
                target=Coordinate(galaxy, system, position),
                dispatched_at_utc=dispatched,
                expected_report_at_utc=expected_at,
            )
            for dispatch_id, galaxy, system, position, dispatched, expected_at in rows
        ]

    def daily_attack_status(
        self, target_kind: str, *, day_utc: datetime
    ) -> DailyAttackStatus | None:
        """把某个 UTC 日的攻击状态**一行读回来**；那天还没对过账就 None。

        用户口径（2026-08-11）：「每天的海盗次数（状态）也可以存库，这样也可以
        快速回读。」重启之后「今天打了几发、还剩几发、还有几发在等战报」要立刻
        答得上，而不是再进一趟信箱（一趟约 20 秒导航，还要抢会话）。

        写入在 `record_daily_reconciliation`，语义见 `DailyAttackStatus`。
        """
        day = _require_utc(day_utc, "day_utc").strftime("%Y-%m-%d")
        with self._session_factory() as session:
            row = session.scalar(
                select(orm.DailyReconciliationRow).where(
                    orm.DailyReconciliationRow.target_kind == target_kind,
                    orm.DailyReconciliationRow.day_utc == day,
                )
            )
            if row is None:
                return None
            return _daily_status(row)

    def record_daily_reconciliation(
        self,
        target_kind: str,
        *,
        day_utc: datetime,
        observed_reports: int,
        complete: bool,
        reconciled_at_utc: datetime,
    ) -> DailyAttackStatus:
        """记下「今天信箱里数到 N 份本链路的战报」。一天一条，**只增不减**。

        ⚠️ **绝不因此往 `attack_dispatches` 里补行。** 那张表的每一行都意味着
        「一支舰队正在外面」，凭空多一条，调度器就会以为一条航线被占着、并等一份
        永远不会来的战报，要到 `MAX_REPORT_AGE`（6 小时）才被判缺失清掉。
        这里只更新计数所依赖的那个事实，见 `count_dispatches_since`。

        ## 为什么是取大而不是覆盖

        这个数的含义是「今天信箱里**至少**有这么多份」，而一天之内战报只会变多，
        所以同一个 UTC 日里它只该往上走。

        取大是有了具体成因才改的：开工对账现在**每次开工都跑**（用户会暂停任务
        再重启，「今日 X/32」必须接得上），而每一趟能翻到多远并不一样——翻到底的
        那趟数到 20，下一趟因为面板夹住只数到 6。照覆盖写，第二趟就把配额判据
        从 20 松回 6，于是助手以为还剩 26 发可打。**计数偏小正是会超额的那一侧**，
        而超额的代价是游戏把攻击强制返回、白飞一趟舰队。

        `complete` 跟着胜出的那个数走：它说的是「那个数是不是全天」，
        接在一个已经被丢掉的数上没有意义。数一样大时取或——两趟里只要有一趟
        真的翻见了昨天，这个数就是全天的。

        ## 顺手把当天的状态固化下来

        用户口径（2026-08-11）：「每天的海盗次数（状态）也可以存库，这样也可以
        快速回读。」这张表原先**只有信箱那一侧的观测数**，答不上「今天一共算打了
        几发」——那个数要现去跑 `count_dispatches_since`；也答不上「还有几发在等
        战报」——那个数库里压根没有。于是重启之后想知道「今日 X/32、几发在飞」，
        除了再翻一趟信箱没有别的办法。

        三个数在这里一并算出来写下（判据与各自的权威查询完全一致，**不另立口径**）：

        - `dispatched_count`：当天库内已被接受的攻击派遣数（**只数攻击发**，
          口径同 `count_dispatches_since`：侦察占航线不占配额）。这是当前事实，
          照实写。
        - `attacks_used`：两个下界取大，且**按 UTC 日只增不减**。多一层只增不减
          是因为库可能被换过/清过：那时 `dispatched_count` 会掉下来，而当天在
          游戏里已经用掉的额度不会跟着退回去。偏大只让助手提前收手，偏小才白飞舰队。
        - `awaiting_reports`：`due_attack_dispatches` 的条数，**瞬时状态、可增可减**
          （理由见 `DailyAttackStatus`）。

        ⚠️ 这三个数一个都不许反过来写进 `attack_dispatches`。同上：库里多一条不
        存在的派遣，调度器就会以为一条航线被占着、等一份永远不来的战报。
        """
        day = _require_utc(day_utc, "day_utc").strftime("%Y-%m-%d")
        moment = _require_utc(reconciled_at_utc, "reconciled_at_utc")
        if observed_reports < 0:
            raise ValueError("observed_reports must not be negative")
        with self._session_factory() as session:
            row = session.scalar(
                select(orm.DailyReconciliationRow).where(
                    orm.DailyReconciliationRow.target_kind == target_kind,
                    orm.DailyReconciliationRow.day_utc == day,
                )
            )
            if row is None:
                row = orm.DailyReconciliationRow(day_utc=day, target_kind=target_kind)
                session.add(row)
                row.observed_reports = observed_reports
                row.complete = complete
            elif observed_reports > row.observed_reports:
                row.observed_reports = observed_reports
                row.complete = complete
            elif observed_reports == row.observed_reports:
                row.complete = row.complete or complete
            row.reconciled_at_utc = moment
            row.dispatched_count = _accepted_attacks_on(session, target_kind, day=day)
            row.attacks_used = max(row.attacks_used, row.dispatched_count, row.observed_reports)
            row.awaiting_reports = _awaiting_attack_reports(
                session, target_kind, now=moment, max_age=MAX_REPORT_AGE
            )
            session.commit()
            return _daily_status(row)

    def count_inflight(self, *, now_utc: datetime) -> int:
        """还占着航线的舰队有几支。**跨 kind**——航线是全局资源。

        供调度器估算空闲航线：`usable_limit − 在飞数`。这个估算不含用户自己
        派出去的舰队，因此是乐观的；`reserved_lines` 正是为这段误差留的缓冲，
        而权威闸门仍在 runner 的 `LineCapacityGate`（看屏复核）。

        **判据是 `line_free_at_utc`，不是 `expected_report_at_utc`。** 这两列是
        派出之后的两个不同的钟：战报在**抵达**时产生（1×），航线要等舰队
        **飞回来**才释放（攻击发 2×，探路发 1×，侦察发 2×）。这里问的是
        「舰队回来没有」，用错列就变成了问「战报出来没有」——于是调度器在
        航线其实还占着的时候就去派，撞上游戏的「同时派遣的舰队数量已达上限。」，
        白跑一整轮。（曾经就是这么写的。）

        **同理不看有没有战报。** 攻击发的战报在 1× 就到手，那时舰队还在往回飞；
        拿「战报已收」当作航线已空，等于把刚修掉的 1× 判据从侧门放回来。

        与 `pending_reports_for_kind` 不是同一个查询：那个按 kind 分、不带
        `> now`、也返回已闭合的行。

        **侦察发照数**：它一样占航线。它不进的是配额，见 `count_dispatches_since`。

        **飞行时间为 NULL 的照数**，占到派出时刻 + `UNKNOWN_LINE_HOLD` 为止。
        NULL 的语义是「不知道它什么时候回来」，不是「它没占航线」——被游戏接受的
        那一发舰队一定占着一条位子。此前这一档按不占记，每一发读不出飞行时间的
        派遣就凭空多出一条空闲航线，而这个错估没有任何回写路径：调度器每隔一个
        `RESTART_COOLDOWN` 就照着它再起一轮，导航几十秒、撞上限、退出、再来。
        """
        _require_utc(now_utc, "now_utc")
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.AttackDispatchRow)
                    .where(
                        orm.AttackDispatchRow.accepted.is_(True),
                        _still_holding_a_line(now_utc),
                    )
                )
                or 0
            )

    def next_line_free_at(self, *, now_utc: datetime) -> datetime | None:
        """已知最早会空出来的那条航线，什么时候空。算不出来时返回 None。

        供调度器把「等航线」锚在一个**真会发生的事件**上，而不是又一段拍脑袋的
        固定间隔。

        **只看有航线钟的那些。** 飞行时间读不到的那一档虽然照样算占位（见
        `count_inflight`），但它的 `UNKNOWN_LINE_HOLD` 是「等到这里就放弃」的
        上界，不是对返航时刻的预测；拿它当闹钟会让链路一睡 6 小时。全场只剩这种
        派遣时宁可返回 None，让调用方走自己那条有界退避。
        """
        _require_utc(now_utc, "now_utc")
        with self._session_factory() as session:
            moment: datetime | None = session.scalar(
                select(func.min(orm.AttackDispatchRow.line_free_at_utc)).where(
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.line_free_at_utc > now_utc,
                )
            )
        return moment

    def last_dispatch_at(self, target_kind: str) -> datetime | None:
        """这种目标最近一次**真的派出去**是什么时候。一次都没有则为 None。

        供调度器判「上一轮是不是空手而归」：这个时刻早于该链路上一次启动，就说明
        那一轮从头跑到尾一发都没派出去。

        **侦察发照数、被拒的那发不数**，和 `count_inflight` 同口径：侦察一样占
        航线，一轮只派了侦察不算空手；被游戏拒掉的那一发根本没飞出去，算进来就
        等于把「撞上航线上限」误读成「派成功了」——那正是要认出来的那件事。
        """
        with self._session_factory() as session:
            moment: datetime | None = session.scalar(
                select(func.max(orm.AttackDispatchRow.dispatched_at_utc))
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .where(
                    orm.AttackIntentRow.target_kind == target_kind,
                    orm.AttackDispatchRow.accepted.is_(True),
                )
            )
        return moment

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

        **侦察发一律排除。** 它不产生 `battle_reports`（侦察报告走信箱里另一条路），
        所以那一行永远不会闭合。留在结果里就是第三种「永远可收又永远不缺失」，
        和上面那条 NULL 是同一个形状：防卡死机制原样反转成卡死机制。
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
                    orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                    orm.AttackDispatchRow.accepted.is_(True),
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
        self, coordinate: Coordinate, *, since: datetime | None, now_utc: datetime | None = None
    ) -> list[DispatchFact]:
        """本轮针对这个 bot 已经**真的派出去**了哪些发、战报回来了没有。

        供 `domain.bot_round.phase_of` 判态。`since` 为空表示不限本轮
        （手工跑命令行时用）。`now_utc` 只用来判「这一发的战报还等不等得到」，
        不传就取此刻——两个调用方（调度器与 runner）都问的是「现在」。

        **这里就是 `phase_of` 那条前置条件的落实处。** 那个纯函数的 docstring
        写着「调用方必须先把已判定战报永远不会来的派遣剔除掉」，否则目标会
        静默卡死在等待态。剔除规则和兄弟方法 `pending_reports_for_kind` 同源，
        也是「现算」而不是「别处先写好的标记」：**派出超过 `MAX_REPORT_AGE`
        （6 小时）还没有战报的，就当它永远不会来了**，整条剔掉。

        为什么按 `dispatched_at_utc` 而不是 `expected_report_at_utc` 算：这条
        链路打的是同系目标，飞行按分钟计，而 `MAX_CREDIBLE_FLIGHT` 已经把简报上
        读出来的时长封在 6 小时内——比派出时刻晚 6 小时还没到的战报，只可能是丢了。

        剔干净之后这个目标会退回 `NEEDS_PROBE`（或 `NEEDS_ATTACK`），也就是
        **允许重新探路**。代价有界：每个目标每 6 小时最多重来一次；而不剔的
        代价是它这一整轮再也不动，且画面上只是「在等」。

        `accepted` 这个过滤不能省，与兄弟方法 `count_dispatches_since` /
        `pending_reports_for_kind` 同口径：被游戏拒掉的那一发没有舰队飞出去，
        也就不会产生战报，算进来就是一条「已派出且永远收不到战报」，
        该目标永远停在 `AWAITING_ATTACK_REPORT`，bot 的完成态永远达不到。

        `mission_kind` 是另一个同口径的过滤，理由一样：侦察发也收不到
        `battle_reports`。而 `phase_of` 只按预设名分探路发和攻击发，认不出
        「这一发根本不会有战报」——一条带着非探路预设名的侦察发混进来，
        就会被当成攻击发，把目标永久钉在等战报上。

        `skipped` 查的是 `target_revisits`，**按坐标+本轮**取，不是逐条派遣取：
        「分档说这个目标不值得打」是对**这一轮的这个坐标**下的判定，复查表里
        也没有指回某一条意图的列。`phase_of` 只用 `any(...)`，粒度对得上。
        """
        give_up_before = _require_utc(now_utc or datetime.now(UTC), "now_utc") - MAX_REPORT_AGE
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
                    orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                    orm.AttackIntentRow.target_galaxy == coordinate.galaxy,
                    orm.AttackIntentRow.target_system == coordinate.system,
                    orm.AttackIntentRow.target_position == coordinate.position,
                    orm.AttackDispatchRow.accepted.is_(True),
                    # 战报回来了的一律留着；没回来的只留还等得到的那些。
                    or_(
                        orm.BattleReportRow.id.is_not(None),
                        orm.AttackDispatchRow.dispatched_at_utc > give_up_before,
                    ),
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

    def pirate_progress(
        self, *, since: datetime, until: datetime | None = None
    ) -> list[PirateProgress]:
        """一段时间内每个海盗目标走到哪一步了，供控制台显示。

        窗口 `[since, until)` 落在**意图创建时刻**上，与 `daily_attack_status`
        和 `count_dispatches_since` 同口径：一天就是游戏内那一天（UTC+0）。
        `until` 省略表示不设上界。

        态由 `domain.pirate_round.phase_for` 定，判定由
        `domain.scout_verdict.verdict_of_record` **现算**——库里不存判定，
        理由见 `domain.records.ScoutReport`。所以这个方法不引入任何新表、
        任何新列：三态（不触发攻击 / 触发攻击 / 攻击完成）全是从已有的
        `attack_intents` + `attack_dispatches` + `battle_reports` + `scout_reports`
        推出来的。

        ⚠️ **侦察报告不按窗口筛，按目标取最近的一份。** 侦察发派出去到报告
        落进信箱要几分钟，跨过 UTC 零点就会出现「意图在昨天、报告在今天」；
        按窗口筛那一发会永远显示成「待侦察报告」。取该目标**不晚于 `until`**
        的最近一份，晚于窗口的不算——否则今天的报告会去解释昨天那一发。

        ⚠️ **只认 `accepted` 的派遣。** 被游戏拒掉的那一发没有舰队飞出去，
        也就永远不会有战报；算进来就是一个永久的「已触发攻击 · 待战报」。
        判据与 `bot_dispatch_facts` / `pending_reports_for_kind` 同源。
        """
        _require_utc(since, "since")
        if until is not None:
            _require_utc(until, "until")

        created = orm.AttackIntentRow.created_at_utc
        in_window: ColumnElement[bool] = (
            created >= since if until is None else and_(created >= since, created < until)
        )

        with self._session_factory() as session:
            rows = session.execute(
                select(
                    orm.AttackIntentRow.target_galaxy,
                    orm.AttackIntentRow.target_system,
                    orm.AttackIntentRow.target_position,
                    orm.AttackDispatchRow.mission_kind,
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.BattleReportRow.id,
                )
                .join(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == TARGET_KIND_PIRATE,
                    orm.AttackDispatchRow.accepted.is_(True),
                    in_window,
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            ).all()

            scouted: set[Coordinate] = set()
            attacks: dict[Coordinate, list[AttackFact]] = {}
            latest_attack: dict[Coordinate, datetime] = {}
            for galaxy, system, position, mission_kind, dispatched_at, report_id in rows:
                target = Coordinate(galaxy, system, position)
                if mission_kind == MISSION_KIND_SCOUT:
                    scouted.add(target)
                    continue
                attacks.setdefault(target, []).append(AttackFact(has_report=report_id is not None))
                latest_attack[target] = dispatched_at

            targets = sorted(scouted | set(attacks), key=_coordinate_sort_key)
            return [
                _pirate_progress_for(
                    target,
                    scouted=target in scouted,
                    scout=self._latest_scout_report(session, target, until=until),
                    attacks=tuple(attacks.get(target, ())),
                    latest_attack_at_utc=latest_attack.get(target),
                )
                for target in targets
            ]

    @staticmethod
    def _latest_scout_report(
        session: Session, target: Coordinate, *, until: datetime | None
    ) -> ScoutReport | None:
        """该目标不晚于 `until` 的最近一份侦察报告；一份都没有就 None。"""
        statement = (
            select(orm.ScoutReportRow)
            .where(
                orm.ScoutReportRow.target_galaxy == target.galaxy,
                orm.ScoutReportRow.target_system == target.system,
                orm.ScoutReportRow.target_position == target.position,
            )
            .order_by(orm.ScoutReportRow.reported_at_utc.desc(), orm.ScoutReportRow.id.desc())
            .limit(1)
        )
        if until is not None:
            statement = statement.where(orm.ScoutReportRow.reported_at_utc < until)
        row = session.scalars(statement).first()
        if row is None:
            return None
        ships = session.scalars(
            select(orm.ScoutTriggerShipRow)
            .where(orm.ScoutTriggerShipRow.report_id == row.id)
            .order_by(orm.ScoutTriggerShipRow.ordinal, orm.ScoutTriggerShipRow.id)
        ).all()
        return ScoutReport(
            report_id=row.id,
            reported_at_utc=row.reported_at_utc,
            raw_time_text=row.raw_time_text,
            origin=Coordinate(row.origin_galaxy, row.origin_system, row.origin_position),
            target=Coordinate(row.target_galaxy, row.target_system, row.target_position),
            # `NULL` 原样带回成 `None`。**不要 `or 0`**（见 `list_scout_reports`）。
            trigger_ships=tuple(
                ScoutTriggerShip(ship_type=entry.ship_type, count=entry.count) for entry in ships
            ),
        )

    def bot_report_due_at(
        self, coordinates: Sequence[Coordinate], *, since: datetime | None
    ) -> dict[Coordinate, tuple[datetime, datetime | None]]:
        """这些 bot 本轮**还没收到战报**的那一发：`(派出时刻, 预计战报时刻)`。

        两个时刻的用途完全不同，所以一起交出去而不是各查一次：

        - **派出时刻**是硬事实（本地在游戏接受「出发！」的那一刻记下的），
          用作翻信箱时的时间下界：列表按时间倒序，比它还早的报告不可能是这一发的。
        - **预计战报时刻**来自简报上的一次 OCR，只用来把日志上那句话说准
          （「还没到点」vs「到点了却没翻到」），**不当闸门**。实机上同一天同距离的
          六发读出 8 秒到 25 分钟不等——拿这么个读数去决定收不收战报，一次抖动
          就能让一个目标停摆到 `MAX_REPORT_AGE`。飞行时间是闹钟不是闸门，
          `tools.pirate_loop._read_flight_time` 的注释记着同一条。

        同一个坐标有多发未闭合时取**最早**那一发：翻信箱要的是覆盖全部候选的下界。
        """
        moments: dict[Coordinate, tuple[datetime, datetime | None]] = {}
        if not coordinates:
            return moments
        wanted = {(item.galaxy, item.system, item.position): item for item in coordinates}
        with self._session_factory() as session:
            statement = (
                select(
                    orm.AttackIntentRow.target_galaxy,
                    orm.AttackIntentRow.target_system,
                    orm.AttackIntentRow.target_position,
                    orm.AttackDispatchRow.dispatched_at_utc,
                    orm.AttackDispatchRow.expected_report_at_utc,
                )
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
                    orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.BattleReportRow.id.is_(None),
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            )
            if since is not None:
                statement = statement.where(
                    orm.AttackIntentRow.created_at_utc >= _require_utc(since, "since")
                )
            for galaxy, system, position, dispatched, expected in session.execute(statement).all():
                coordinate = wanted.get((galaxy, system, position))
                if coordinate is None or coordinate in moments:
                    continue
                moments[coordinate] = (dispatched, expected)
        return moments

    def has_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        """这个目标这个**报告时刻**的战报是不是已经在库里了。

        活链路每一趟都会去信箱翻同样那几行，而 `append_report` 认领不到派遣时
        （坐标 OCR 偏了、或者同一目标同一时段有两发分不开）只会把行写下来、
        `dispatch_id` 留空——判态那一侧仍然看不到战报，于是下一趟又读同一封。
        没有这道去重，一份读不上号的战报会每趟复制一行，越堆越多。

        判据取**报告时间**，与探索报告采集器同源（那边也是「以报告时间去重」）：
        它是游戏自己写在报告上的字，不受本地时钟与重跑影响。
        """
        _require_utc(reported_at_utc, "reported_at_utc")
        with self._session_factory() as session:
            return (
                session.scalar(
                    select(func.count())
                    .select_from(orm.BattleReportRow)
                    .where(
                        orm.BattleReportRow.defender_target_galaxy == target.galaxy,
                        orm.BattleReportRow.defender_target_system == target.system,
                        orm.BattleReportRow.defender_target_position == target.position,
                        orm.BattleReportRow.reported_at_utc == reported_at_utc,
                    )
                )
                or 0
            ) > 0

    def latest_defender_units(self, target: Coordinate, *, since: datetime) -> int | None:
        """本轮这个目标最新那份战报里守方的「单位」总数；没有就 None。

        分档要的就是这个数，而它在收报告那一趟已经读过一次了（见
        `tools.bot_loop.BotLoop._ingest_report`）。从库里取而不是再进一趟信箱，
        省的不只是十几秒 OCR：信箱里那几行**没有时间闸门**，翻到的可能是
        上一轮甚至上一天的报告，照它分档就是拿旧情报去挑舰队组合。
        `since` 把范围钉在本轮上。

        「本轮没有战报」与「有战报但没读出这个数」都返回 None——调用方两种
        情况都得退回现场读一次，分开也没有不同的处置。
        """
        _require_utc(since, "since")
        with self._session_factory() as session:
            return session.scalar(
                select(orm.BattleReportRow.defender_units)
                .where(
                    orm.BattleReportRow.defender_target_galaxy == target.galaxy,
                    orm.BattleReportRow.defender_target_system == target.system,
                    orm.BattleReportRow.defender_target_position == target.position,
                    orm.BattleReportRow.reported_at_utc >= since,
                )
                .order_by(orm.BattleReportRow.reported_at_utc.desc(), orm.BattleReportRow.id.desc())
                .limit(1)
            )

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

    def open_mission_runs(self) -> list[orm.MissionRunRow]:
        """还没闭合的子进程记录，新的在前。

        开机时必须**先读后标**：`mark_orphan_mission_runs` 会把这些行一并闭合，
        之后就再也认不出「哪一条是孤儿、它的 pid 是多少」了，而页面上的红条
        正要把那个 pid 显示给人看——让用户自己去任务管理器里核对。
        """
        with self._session_factory() as session:
            return list(
                session.scalars(
                    select(orm.MissionRunRow)
                    .where(orm.MissionRunRow.ended_at_utc.is_(None))
                    .order_by(orm.MissionRunRow.started_at_utc.desc())
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


def _scout_report_exists(session: Session, target: Coordinate, reported_at_utc: datetime) -> bool:
    """侦察报告的去重判据只有这一份：**目标 + 报告时间**。

    写侧（`append_scout_report`）与问侧（`has_scout_report_at`）共用它，
    免得哪天有人只改了一处，于是「问过不重复」和「写的时候不重复」用上两套口径。
    """
    return (
        session.scalar(
            select(func.count())
            .select_from(orm.ScoutReportRow)
            .where(
                orm.ScoutReportRow.target_galaxy == target.galaxy,
                orm.ScoutReportRow.target_system == target.system,
                orm.ScoutReportRow.target_position == target.position,
                orm.ScoutReportRow.reported_at_utc == reported_at_utc,
            )
        )
        or 0
    ) > 0


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


def _still_holding_a_line(now_utc: datetime) -> ColumnElement[bool]:
    """这一发是不是还占着一条航线。抽成具名谓词是为了让两档合在一处看得见。

    - 航线钟读到了：到点就放手。
    - 航线钟为 NULL：**照样占着**，直到派出时刻 + `UNKNOWN_LINE_HOLD`。
      NULL 的意思是「不知道它什么时候回来」，不是「它没占位」。
    """
    row = orm.AttackDispatchRow
    return or_(
        row.line_free_at_utc > now_utc,
        and_(
            row.line_free_at_utc.is_(None),
            row.dispatched_at_utc > now_utc - UNKNOWN_LINE_HOLD,
        ),
    )


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


def _daily_status(row: orm.DailyReconciliationRow) -> DailyAttackStatus:
    return DailyAttackStatus(
        day_utc=row.day_utc,
        target_kind=row.target_kind,
        observed_reports=row.observed_reports,
        complete=row.complete,
        dispatched_count=row.dispatched_count,
        attacks_used=row.attacks_used,
        awaiting_reports=row.awaiting_reports,
        reconciled_at_utc=row.reconciled_at_utc,
    )


def _accepted_attacks_on(session: Session, target_kind: str, *, day: str) -> int:
    """这条链路在这个 UTC 日**真打出去**了几发（库内一侧）。

    口径与 `count_dispatches_since` 里那半段逐字一致：`accepted` + 只数
    `MISSION_KIND_ATTACK`。侦察也是打向海盗的，不排掉的话一轮 4 发侦察就吃掉
    4 次攻击额度，当天 32 次以 4 倍速度静默消失。
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(orm.AttackDispatchRow)
            .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
            .where(
                orm.AttackIntentRow.target_kind == target_kind,
                orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                orm.AttackDispatchRow.accepted.is_(True),
                func.date(orm.AttackDispatchRow.dispatched_at_utc) == day,
            )
        )
        or 0
    )


def _awaiting_attack_reports(
    session: Session, target_kind: str, *, now: datetime, max_age: timedelta
) -> int:
    """还有几发已派出、没战报、且还没被判放弃。`due_attack_dispatches` 的计数版。

    **不加「已经到点了」那道条件**：回读的人问的是「还有几支舰队在外面没交代」，
    还在飞的那几发也算在内。到点没到点是调度那一侧的事。
    """
    return int(
        session.scalar(
            select(func.count())
            .select_from(orm.AttackDispatchRow)
            .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
            .outerjoin(
                orm.BattleReportRow,
                orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
            )
            .where(
                orm.AttackIntentRow.target_kind == target_kind,
                orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
                orm.AttackDispatchRow.accepted.is_(True),
                orm.BattleReportRow.id.is_(None),
                orm.AttackDispatchRow.dispatched_at_utc > now - max_age,
            )
        )
        or 0
    )


def _link_dispatch(
    session: Session,
    report_row: orm.BattleReportRow,
    *,
    origin: Coordinate,
    target: Coordinate,
    reported_at: datetime,
) -> bool:
    """给一行战报认领一发派遣，写回 `dispatch_id` / `match_status`。认上了返回 True。

    入库（`append_report`）与回头重认（`rematch_report_at`）共用这一段，
    **不能各写一份**：判据一分家，重认那条路就会用一套跟入库不同的规则去改
    历史行，而两套规则的差别只会在实机上以「战果列有时空着」的形式露出来。

    ⚠️ **不猜**：候选恰好一个才认领；多于一个记 `AMBIGUOUS`，一个都没有记
    `UNMATCHED`。写死这三档而不是「认不上就不动」，是为了让重认能把一行从
    `AMBIGUOUS` 改回 `UNMATCHED`——候选已经全部过期时，那才是真话。
    """
    close = [
        dispatch
        for dispatch in _unmatched_dispatch_candidates(
            session, origin=origin, target=target, reported_at=reported_at
        )
        if _close_in_time(dispatch.dispatched_at_utc, reported_at)
    ]
    if len(close) == 1:
        report_row.dispatch_id = close[0].id
        report_row.match_status = "MATCHED"
        report_row.match_confidence = 1.0
        bot_target = _bot_target_for(session, target)
        if bot_target is not None:
            bot_target.last_report_at_utc = reported_at
        return True
    report_row.match_status = "AMBIGUOUS" if len(close) > 1 else "UNMATCHED"
    return False


def _unmatched_dispatch_candidates(
    session: Session,
    *,
    origin: Coordinate,
    target: Coordinate,
    reported_at: datetime,
) -> list[orm.AttackDispatchRow]:
    """这份战报**可能**属于哪几发派遣。

    除了出发点、目标、`accepted` 与「还没被别的战报认领」，还要排掉三类：

    1. **派在这份战报之后的**。战报不可能早于产生它的那一发。
    2. **早到已经被判定「战报永远不会来」的**（派出超过 `MAX_REPORT_AGE`）。
    3. **侦察发**（`mission_kind != ATTACK`）。

    第 2 条补的是一处**两地不一致**：`bot_dispatch_facts()` 早就按 `MAX_REPORT_AGE`
    把过期派遣整条剔掉、让目标退回重新探路；可认领这一侧不认这条规则，仍把它当候选。
    于是——

        战报 2:323:10  01:35:18
        候选 A  派于 08-10 18:28（预计战报 18:53，已过期 6 小时 42 分）
        候选 B  派于 08-11 01:10（预计战报 01:35:10，差 8 秒）

    两个候选都在 12 小时容差内，`len(close) > 1` → `AMBIGUOUS` → `dispatch_id`
    留空 → `has_report` 永远为假 → 目标永远停在等战报，攻击日志也永远显示不出战果。
    **而 A 早就被另一半代码写掉了。**

    ## 第 3 条：侦察发根本产生不了攻击战报

    **侦察发不产生 `battle_reports`**——侦察报告是信箱里另一种主题，走
    `scout_reports` 那张表。所以「一个目标同一天两发」里只有攻击发有资格被认领，
    这一点是**结构上成立的**，不需要拿「时间就近」去猜哪一发更像。

    这个过滤此前只有这里没有：`count_dispatches_since`、`oldest_open_attack_at`、
    `pending_reports_for_kind`、`bot_dispatch_facts`、`bot_report_due_at` 五处
    早就写着 `mission_kind == MISSION_KIND_ATTACK`，唯独可认领这一侧漏了。
    而海盗链路的常态恰恰是「先侦察、判定值得打、再攻击」——同一个出发点、
    同一个目标、相隔几分钟。实机（生产库 2026-08-11）那天四发 AAA 攻击的战报
    **无一例外**都撞上了自己那一发侦察：

        战报 2:138:2  13:06:28   VICTORY
        候选 A  12:45:07  SCOUT   ← 探测器，永远不会有战报
        候选 B  12:51:11  ATTACK  ← 就是它

    两个候选 → `AMBIGUOUS` → 四个目标全停在「待战报」，而战报明明已经在库里。
    2:137:1 / 2:137:3 / 2:136:3 / 2:138:2 四行一模一样。加上这一条之后
    每一份都只剩一个候选，同样不需要任何猜测。

    ⚠️ 仍然**不猜**：排掉之后还多于一个候选时照旧记 `AMBIGUOUS`。这三条改的都是
    「谁有资格当候选」，不是「多个候选时挑一个」。
    """
    from evo_helper.domain.report_wait import MAX_REPORT_AGE

    linked = select(orm.BattleReportRow.dispatch_id).where(
        orm.BattleReportRow.dispatch_id.is_not(None)
    )
    rows = session.scalars(
        select(orm.AttackDispatchRow)
        .join(orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id)
        .where(
            orm.AttackIntentRow.origin_galaxy == origin.galaxy,
            orm.AttackIntentRow.origin_system == origin.system,
            orm.AttackIntentRow.origin_position == origin.position,
            orm.AttackIntentRow.target_galaxy == target.galaxy,
            orm.AttackIntentRow.target_system == target.system,
            orm.AttackIntentRow.target_position == target.position,
            orm.AttackDispatchRow.accepted.is_(True),
            orm.AttackDispatchRow.mission_kind == MISSION_KIND_ATTACK,
            orm.AttackDispatchRow.id.not_in(linked),
            orm.AttackDispatchRow.dispatched_at_utc <= reported_at,
            orm.AttackDispatchRow.dispatched_at_utc >= reported_at - MAX_REPORT_AGE,
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
