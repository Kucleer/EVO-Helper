"""Application service seam for the web adapter.

The real orchestration layer is owned by the root workstream and wired during
integration.  This module defines the minimal protocol the web adapter depends
on plus an in-memory fake used for tests and local demos.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from evo_helper.domain.models import Coordinate, RunState
from evo_helper.domain.state_machine import require_transition

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ServiceError(Exception):
    """Base error carrying an HTTP status code."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class NotFoundError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 404)


class ConflictError(ServiceError):
    def __init__(self, message: str) -> None:
        super().__init__(message, 409)


@dataclass(frozen=True)
class ScanRangeView:
    start: Coordinate
    end: Coordinate
    origin: Coordinate
    fleet_preset: str
    fleet_preset_signature: str
    priority: int


@dataclass(frozen=True)
class CoordinateScanView:
    """一次坐标扫描的事实。空位与海盗同样记录，不只记 bot。"""

    coordinate: Coordinate
    scanned_at_utc: datetime
    owner_name: str | None
    is_bot: bool
    confidence: float


@dataclass(frozen=True)
class ScanPlanView:
    id: UUID
    name: str
    enabled: bool
    window_start: time
    window_end: time
    dry_run: bool
    ranges: tuple[ScanRangeView, ...]
    created_at: datetime
    updated_at: datetime
    #: Fleet lines this plan may occupy at once.
    fleet_line_limit: int = 1
    #: Lines always kept free for the user's own dispatches.
    reserved_lines: int = 0


@dataclass(frozen=True)
class PlanPatchView:
    name: str | None = None
    enabled: bool | None = None
    window_start: time | None = None
    window_end: time | None = None
    dry_run: bool | None = None
    ranges: tuple[ScanRangeView, ...] | None = None


@dataclass(frozen=True)
class RunStatusView:
    run_id: UUID
    plan_id: UUID
    state: RunState
    idempotency_key: str
    target_date: date
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


#: 星球列表的筛选类型。`all` 不是一种星球，是「不过滤」。
PLANET_KINDS: tuple[str, ...] = ("bot", "owned", "free", "all")

#: 默认只看 bot：全量扫描里绝大多数坐标是空位，默认全展开没有信息量。
DEFAULT_PLANET_KIND = "bot"


def planet_kind(owner_name: str | None, is_bot: bool) -> str:
    """一颗星球归哪一类。

    分类只有这一份实现，持久化那边的 SQL 过滤条件必须与它一致——
    「同一条规则在 Fake 和持久化各写一份」的坑已经踩过一次。
    """
    if is_bot:
        return "bot"
    return "owned" if owner_name else "free"


@dataclass(frozen=True)
class PlanetRow:
    """星球列表里的一行：一颗星球的最新已知状态。"""

    coordinate: Coordinate
    owner_name: str | None
    is_bot: bool
    last_scan_at: datetime | None

    @property
    def kind(self) -> str:
        return planet_kind(self.owner_name, self.is_bot)


@dataclass(frozen=True)
class PlanetPage:
    """一页星球，外加足够让页面说清「这一页是全部还是一截」的计数。"""

    rows: tuple[PlanetRow, ...]
    #: 当前筛选下的总行数——**不是**本页行数。少了它，页面就只能拿本页行数冒充总数。
    total: int
    offset: int
    limit: int
    #: 当前银河系筛选下各类型各多少，用来标注筛选器而不必再查一次。
    kind_counts: dict[str, int]
    #: 每个银河系已扫过多少颗星球，用来填银河系下拉框。
    galaxy_counts: dict[int, int]

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.rows) < self.total


@dataclass(frozen=True)
class BotTargetView:
    coordinate: Coordinate
    latest_player: str | None
    last_scan_at: datetime | None
    last_attack_at: datetime | None
    last_dispatch_at: datetime | None
    last_report_at: datetime | None


@dataclass(frozen=True)
class FleetEntryView:
    ship_type: str
    quantity: int


@dataclass(frozen=True)
class FleetSnapshotView:
    snapshot_id: UUID
    coordinate: Coordinate
    captured_at_utc: datetime
    side: str
    total: int
    is_revisit: bool
    match_confidence: float
    review_status: str
    ships: tuple[FleetEntryView, ...]


@dataclass(frozen=True)
class FleetChangeView:
    ship_type: str
    before: int
    after: int
    delta: int
    percent: float


@dataclass(frozen=True)
class FleetDiffView:
    coordinate: Coordinate
    before: FleetSnapshotView | None
    after: FleetSnapshotView
    added: tuple[FleetEntryView, ...]
    removed: tuple[FleetEntryView, ...]
    disappeared: tuple[str, ...]
    first_seen: tuple[str, ...]
    changes: tuple[FleetChangeView, ...]
    total_before: int
    total_after: int


@dataclass(frozen=True)
class StateEventView:
    event_id: UUID
    occurred_at_utc: datetime
    aggregate: str
    aggregate_id: UUID
    event: str
    from_state: str | None
    to_state: str | None


@dataclass(frozen=True)
class AttackLogView:
    """攻击日志的一行：一次派遣，或一个还没派出去的意图。

    只存 `dispatched_at_utc` 一个瞬时，不存两份时间。游戏内时间是 UTC+0
    （`vision.parsers.GAME_DISPLAY_ZONE`），现实时间是 UTC+8
    （`domain.scheduling.SHANGHAI_OFFSET`）——同一个瞬时的两种写法，
    存两遍只会让它们迟早对不上。渲染时各显示一次，都标明时区。
    """

    intent_id: UUID
    target: Coordinate
    origin: Coordinate
    #: `bot` 或 `pirate`。
    target_kind: str
    preset_name: str
    preset_signature: str
    guard_status: str
    created_at_utc: datetime
    #: 真正点下「出发！」的时刻。意图被闸门拦下或还没派出时为 None。
    dispatched_at_utc: datetime | None
    dry_run: bool | None
    accepted: bool | None
    expected_report_at_utc: datetime | None
    #: 战果，来自匹配上的那份战报：`VICTORY` / `FAIL` 与双方战损总数。
    #: 还在飞、或者战报还没收，三个都是 None——**不能拿 0 顶替**，
    #: 「零损失」和「还不知道」在日志上必须看得出区别。
    outcome: str | None = None
    attacker_losses: int | None = None
    defender_losses: int | None = None


@dataclass(frozen=True)
class RevisitView:
    revisit_id: UUID
    scope: str
    reason: str
    requested_at_utc: datetime
    status: str
    target_coordinate: Coordinate | None = None


@dataclass(frozen=True)
class DashboardView:
    plan_count: int
    active_run_count: int
    target_count: int
    pending_revisit_count: int


class ApplicationService(Protocol):
    def list_plans(self) -> list[ScanPlanView]: ...
    def get_plan(self, plan_id: UUID) -> ScanPlanView | None: ...
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
    ) -> ScanPlanView: ...
    def update_plan(self, plan_id: UUID, patch: PlanPatchView) -> ScanPlanView: ...
    def delete_plan(self, plan_id: UUID) -> None: ...
    def start_run(self, plan_id: UUID, idempotency_key: str) -> RunStatusView: ...
    def get_run(self, run_id: UUID) -> RunStatusView | None: ...
    def list_runs(self) -> list[RunStatusView]: ...
    def pause_run(self, run_id: UUID) -> RunStatusView: ...
    def resume_run(self, run_id: UUID) -> RunStatusView: ...
    def emergency_stop_run(self, run_id: UUID) -> RunStatusView: ...
    def list_targets(self) -> list[BotTargetView]: ...
    def list_scans(self, limit: int = 500) -> list[CoordinateScanView]: ...
    def count_scans(self) -> int: ...
    def list_planets(
        self, *, galaxy: int | None, kind: str, offset: int, limit: int
    ) -> PlanetPage: ...
    def get_history(self, coordinate: Coordinate) -> list[FleetSnapshotView]: ...
    def get_fleet_diff(self, coordinate: Coordinate) -> FleetDiffView | None: ...
    def list_events(self, limit: int) -> list[StateEventView]: ...
    def request_revisit(
        self, scope: str, reason: str, target_coordinate: Coordinate | None
    ) -> RevisitView: ...
    def list_revisits(self) -> list[RevisitView]: ...
    def list_attack_log(self, limit: int) -> list[AttackLogView]: ...
    def dashboard(self) -> DashboardView: ...


def _parse_window(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _parse_coordinate(value: str) -> Coordinate:
    parts = value.split(":")
    if len(parts) != 3:
        raise NotFoundError(f"invalid coordinate: {value!r}")
    try:
        galaxy, system, position = (int(part) for part in parts)
    except ValueError as exc:
        raise NotFoundError(f"invalid coordinate: {value!r}") from exc
    try:
        return Coordinate(galaxy, system, position)
    except ValueError as exc:
        raise NotFoundError(str(exc)) from exc


def _fleet_total(ships: tuple[FleetEntryView, ...]) -> int:
    return sum(ship.quantity for ship in ships)


class FakeApplicationService:
    """Thread-safe in-memory implementation of :class:`ApplicationService`."""

    def __init__(self, now_utc: Callable[[], datetime] | None = None) -> None:
        self._now = now_utc or (lambda: datetime.now(UTC))
        self._lock = Lock()
        self._plans: dict[UUID, ScanPlanView] = {}
        self._runs: dict[UUID, RunStatusView] = {}
        self._runs_by_key: dict[str, UUID] = {}
        self._targets: dict[Coordinate, BotTargetView] = {}
        self._snapshots: dict[Coordinate, list[FleetSnapshotView]] = {}
        self._events: list[StateEventView] = []
        self._revisits: list[RevisitView] = []
        self._scans: list[CoordinateScanView] = []
        self._planets: list[PlanetRow] = []

    # ---- plans -----------------------------------------------------------

    def list_plans(self) -> list[ScanPlanView]:
        with self._lock:
            return sorted(self._plans.values(), key=lambda plan: plan.name)

    def get_plan(self, plan_id: UUID) -> ScanPlanView | None:
        with self._lock:
            return self._plans.get(plan_id)

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
        self._validate_window(window_start, window_end)
        for scan_range in ranges:
            self._validate_range(scan_range)
        self._validate_lines(fleet_line_limit, reserved_lines)
        now = self._now()
        plan = ScanPlanView(
            id=uuid4(),
            name=name,
            enabled=enabled,
            window_start=window_start,
            window_end=window_end,
            dry_run=dry_run,
            ranges=ranges,
            created_at=now,
            updated_at=now,
            fleet_line_limit=fleet_line_limit,
            reserved_lines=reserved_lines,
        )
        with self._lock:
            self._plans[plan.id] = plan
        return plan

    def update_plan(self, plan_id: UUID, patch: PlanPatchView) -> ScanPlanView:
        with self._lock:
            current = self._plans.get(plan_id)
            if current is None:
                raise NotFoundError(f"plan {plan_id} not found")
            window_start = patch.window_start or current.window_start
            window_end = patch.window_end or current.window_end
            self._validate_window(window_start, window_end)
            ranges = patch.ranges or current.ranges
            for scan_range in ranges:
                self._validate_range(scan_range)
            updated = ScanPlanView(
                id=current.id,
                name=patch.name or current.name,
                enabled=patch.enabled if patch.enabled is not None else current.enabled,
                window_start=window_start,
                window_end=window_end,
                dry_run=patch.dry_run if patch.dry_run is not None else current.dry_run,
                ranges=ranges,
                created_at=current.created_at,
                updated_at=self._now(),
            )
            self._plans[plan_id] = updated
            return updated

    def delete_plan(self, plan_id: UUID) -> None:
        with self._lock:
            if plan_id not in self._plans:
                raise NotFoundError(f"plan {plan_id} not found")
            del self._plans[plan_id]

    @staticmethod
    def _validate_window(window_start: time, window_end: time) -> None:
        if window_start > window_end:
            raise ServiceError("window_start must not be after window_end")

    @staticmethod
    def _validate_lines(fleet_line_limit: int, reserved_lines: int) -> None:
        """Reserving every line would make the plan unable to dispatch anything."""
        if reserved_lines >= fleet_line_limit:
            raise ServiceError(
                f"reserved_lines ({reserved_lines}) must be fewer than "
                f"fleet_line_limit ({fleet_line_limit}); the plan would never dispatch"
            )

    @staticmethod
    def _validate_range(scan_range: ScanRangeView) -> None:
        if scan_range.end < scan_range.start:
            raise ServiceError("range end must not precede its start")
        # The origin is deliberately not required to fall inside the range. It
        # is the player's own planet, which normally sits well outside the
        # coordinates being scanned.

    # ---- runs ------------------------------------------------------------

    def start_run(self, plan_id: UUID, idempotency_key: str) -> RunStatusView:
        with self._lock:
            if idempotency_key in self._runs_by_key:
                existing_id = self._runs_by_key[idempotency_key]
                existing = self._runs[existing_id]
                raise ConflictError(f"idempotency_key already used by run {existing.run_id}")
            plan = self._plans.get(plan_id)
            if plan is None:
                raise NotFoundError(f"plan {plan_id} not found")
            if not plan.enabled:
                raise ServiceError(f"plan {plan.name} is disabled")
            now = self._now()
            state, target_date = self._schedule_state(plan, now)
            run = RunStatusView(
                run_id=uuid4(),
                plan_id=plan.id,
                state=state,
                idempotency_key=idempotency_key,
                target_date=target_date,
                created_at=now,
            )
            self._runs[run.run_id] = run
            self._runs_by_key[idempotency_key] = run.run_id
            self._append_event(run.run_id, "run", "started", None, state.value)
            return run

    def get_run(self, run_id: UUID) -> RunStatusView | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(self) -> list[RunStatusView]:
        with self._lock:
            return sorted(self._runs.values(), key=lambda run: run.created_at)

    def pause_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.PAUSED, "paused")

    def resume_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.ARMED, "resumed")

    def emergency_stop_run(self, run_id: UUID) -> RunStatusView:
        return self._transition(run_id, RunState.EMERGENCY_STOPPED, "emergency_stopped")

    def _transition(self, run_id: UUID, target: RunState, event: str) -> RunStatusView:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise NotFoundError(f"run {run_id} not found")
            try:
                require_transition(run.state, target)
            except ValueError as exc:
                raise ConflictError(str(exc)) from exc
            updated = RunStatusView(
                run_id=run.run_id,
                plan_id=run.plan_id,
                state=target,
                idempotency_key=run.idempotency_key,
                target_date=run.target_date,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=self._now() if target is RunState.EMERGENCY_STOPPED else None,
            )
            self._runs[run_id] = updated
            self._append_event(run_id, "run", event, run.state.value, target.value)
            return updated

    def _schedule_state(self, plan: ScanPlanView, now: datetime) -> tuple[RunState, date]:
        shanghai_now = now.astimezone(SHANGHAI)
        today = shanghai_now.date()
        current = shanghai_now.time()
        if current < plan.window_start:
            return RunState.ARMED, today
        if current <= plan.window_end:
            return RunState.SCANNING, today
        return RunState.ARMED, today + timedelta(days=1)

    def _append_event(
        self,
        aggregate_id: UUID,
        aggregate: str,
        event: str,
        from_state: str | None,
        to_state: str | None,
    ) -> None:
        self._events.append(
            StateEventView(
                event_id=uuid4(),
                occurred_at_utc=self._now(),
                aggregate=aggregate,
                aggregate_id=aggregate_id,
                event=event,
                from_state=from_state,
                to_state=to_state,
            )
        )

    # ---- targets / history ----------------------------------------------

    def list_targets(self) -> list[BotTargetView]:
        with self._lock:
            return sorted(self._targets.values(), key=lambda target: str(target.coordinate))

    def list_scans(self, limit: int = 500) -> list[CoordinateScanView]:
        with self._lock:
            return sorted(
                self._scans[:limit],
                key=lambda s: (s.coordinate.galaxy, s.coordinate.system, s.coordinate.position),
            )

    def count_scans(self) -> int:
        with self._lock:
            return len(self._scans)

    def list_planets(self, *, galaxy: int | None, kind: str, offset: int, limit: int) -> PlanetPage:
        with self._lock:
            planets = sorted(
                self._planets,
                key=lambda r: (r.coordinate.galaxy, r.coordinate.system, r.coordinate.position),
            )
        galaxy_counts: dict[int, int] = {}
        for row in planets:
            galaxy_counts[row.coordinate.galaxy] = galaxy_counts.get(row.coordinate.galaxy, 0) + 1
        in_galaxy = [r for r in planets if galaxy is None or r.coordinate.galaxy == galaxy]
        kind_counts = {k: 0 for k in PLANET_KINDS if k != "all"}
        for row in in_galaxy:
            kind_counts[row.kind] += 1
        kind_counts["all"] = len(in_galaxy)
        matched = [r for r in in_galaxy if kind == "all" or r.kind == kind]
        return PlanetPage(
            rows=tuple(matched[offset : offset + limit]),
            total=len(matched),
            offset=offset,
            limit=limit,
            kind_counts=kind_counts,
            galaxy_counts=galaxy_counts,
        )

    def get_history(self, coordinate: Coordinate) -> list[FleetSnapshotView]:
        with self._lock:
            return list(self._snapshots.get(coordinate, []))

    def get_fleet_diff(self, coordinate: Coordinate) -> FleetDiffView | None:
        with self._lock:
            history = list(self._snapshots.get(coordinate, []))
            if not history:
                return None
            before = history[-2] if len(history) > 1 else None
            after = history[-1]
            return self._compute_diff(coordinate, before, after)

    def _compute_diff(
        self,
        coordinate: Coordinate,
        before: FleetSnapshotView | None,
        after: FleetSnapshotView,
    ) -> FleetDiffView:
        before_ships = {entry.ship_type: entry.quantity for entry in before.ships} if before else {}
        after_ships = {entry.ship_type: entry.quantity for entry in after.ships}
        all_types = sorted(set(before_ships) | set(after_ships))
        added: list[FleetEntryView] = []
        removed: list[FleetEntryView] = []
        disappeared: list[str] = []
        first_seen: list[str] = []
        changes: list[FleetChangeView] = []
        for ship_type in all_types:
            before_qty = before_ships.get(ship_type, 0)
            after_qty = after_ships.get(ship_type, 0)
            if before_qty == 0 and after_qty > 0:
                first_seen.append(ship_type)
                added.append(FleetEntryView(ship_type, after_qty))
            elif before_qty > 0 and after_qty == 0:
                disappeared.append(ship_type)
                removed.append(FleetEntryView(ship_type, before_qty))
            elif after_qty > before_qty:
                delta = after_qty - before_qty
                percent = (delta / before_qty * 100.0) if before_qty else 100.0
                changes.append(FleetChangeView(ship_type, before_qty, after_qty, delta, percent))
            elif after_qty < before_qty:
                delta = after_qty - before_qty
                percent = (delta / before_qty * 100.0) if before_qty else -100.0
                changes.append(FleetChangeView(ship_type, before_qty, after_qty, delta, percent))
        return FleetDiffView(
            coordinate=coordinate,
            before=before,
            after=after,
            added=tuple(added),
            removed=tuple(removed),
            disappeared=tuple(disappeared),
            first_seen=tuple(first_seen),
            changes=tuple(changes),
            total_before=_fleet_total(before.ships) if before else 0,
            total_after=_fleet_total(after.ships),
        )

    def add_snapshot(
        self,
        coordinate: Coordinate,
        side: str,
        ships: tuple[FleetEntryView, ...],
        *,
        is_revisit: bool = False,
        match_confidence: float = 1.0,
        review_status: str = "pending",
        captured_at_utc: datetime | None = None,
    ) -> FleetSnapshotView:
        snapshot = FleetSnapshotView(
            snapshot_id=uuid4(),
            coordinate=coordinate,
            captured_at_utc=captured_at_utc or self._now(),
            side=side,
            total=_fleet_total(ships),
            is_revisit=is_revisit,
            match_confidence=match_confidence,
            review_status=review_status,
            ships=ships,
        )
        with self._lock:
            self._snapshots.setdefault(coordinate, []).append(snapshot)
            target = self._targets.get(coordinate)
            if target is None:
                target = BotTargetView(
                    coordinate=coordinate,
                    latest_player=None,
                    last_scan_at=None,
                    last_attack_at=None,
                    last_dispatch_at=None,
                    last_report_at=snapshot.captured_at_utc,
                )
            else:
                target = BotTargetView(
                    coordinate=coordinate,
                    latest_player=target.latest_player,
                    last_scan_at=target.last_scan_at,
                    last_attack_at=target.last_attack_at,
                    last_dispatch_at=target.last_dispatch_at,
                    last_report_at=snapshot.captured_at_utc,
                )
            self._targets[coordinate] = target
        return snapshot

    def upsert_target(self, target: BotTargetView) -> None:
        with self._lock:
            self._targets[target.coordinate] = target

    # ---- revisits / diagnostics -----------------------------------------

    def request_revisit(
        self, scope: str, reason: str, target_coordinate: Coordinate | None
    ) -> RevisitView:
        if scope == "target" and target_coordinate is None:
            raise ServiceError("target revisit requires target_coordinate")
        revisit = RevisitView(
            revisit_id=uuid4(),
            scope=scope,
            reason=reason,
            requested_at_utc=self._now(),
            status="pending",
            target_coordinate=target_coordinate,
        )
        with self._lock:
            self._revisits.append(revisit)
        return revisit

    def list_revisits(self) -> list[RevisitView]:
        with self._lock:
            return list(self._revisits)

    def list_events(self, limit: int) -> list[StateEventView]:
        with self._lock:
            return list(self._events[-limit:])

    def list_attack_log(self, limit: int) -> list[AttackLogView]:
        """Fake 服务不模拟派遣，所以攻击日志恒为空。

        返回空列表而不是抛未实现：页面在演示服务上也要打得开，
        并且要显示「还没有攻击记录」而不是 500。
        """
        return []

    def dashboard(self) -> DashboardView:
        with self._lock:
            active = sum(
                1
                for run in self._runs.values()
                if run.state
                in {
                    RunState.ARMED,
                    RunState.SCANNING,
                    RunState.WAITING_CAPACITY,
                    RunState.DRAINING,
                }
            )
            pending = sum(1 for revisit in self._revisits if revisit.status == "pending")
            return DashboardView(
                plan_count=len(self._plans),
                active_run_count=active,
                target_count=len(self._targets),
                pending_revisit_count=pending,
            )


__all__ = [
    "ApplicationService",
    "BotTargetView",
    "ConflictError",
    "DashboardView",
    "FakeApplicationService",
    "FleetChangeView",
    "FleetDiffView",
    "FleetEntryView",
    "FleetSnapshotView",
    "NotFoundError",
    "PlanPatchView",
    "RevisitView",
    "RunStatusView",
    "ScanPlanView",
    "ScanRangeView",
    "ServiceError",
    "StateEventView",
    "_parse_coordinate",
    "_parse_window",
]
