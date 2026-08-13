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

from evo_helper.domain.models import Coordinate, CoordinateRange

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
    ranges: tuple[ScanRangeView, ...] | None = None


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
    accepted: bool | None
    expected_report_at_utc: datetime | None
    #: 战果，来自匹配上的那份战报：`VICTORY` / `FAIL` 与双方战损总数。
    #: 还在飞、或者战报还没收，三个都是 None——**不能拿 0 顶替**，
    #: 「零损失」和「还不知道」在日志上必须看得出区别。
    outcome: str | None = None
    attacker_losses: int | None = None
    defender_losses: int | None = None


#: 攻击日志「结果」那一档的三个取值。键同 `storage.intel.DISPATCH_*`，
#: 中文标签与 chip 样式复用 `display.DISPATCH_STATE_*`。
#:
#: **没有 `NEVER`。** 情报中心筛的是「目标星球」，一颗从没被派遣过的星球才叫
#: 「从未派遣」；而攻击日志一行就是一次派遣意图，「从未」在这里不存在，摆出来
#: 只会是一档永远筛出空页的选项——而空页读起来就是「这类记录没有」。
ATTACK_LOG_RESULTS: tuple[str, ...] = ("SENT", "BLOCKED", "REJECTED")


@dataclass(frozen=True)
class AttackLogOptions:
    """攻击日志上两档快速筛选的候选值，**从库里现有的记录取**。

    预设是用户自己在游戏里维护的，写死字面量就会漏掉他新建的那一个；战果同理，
    库里存的是战斗详情页上的画面原文（`BattleReportRow.outcome`），将来多一档
    也得能筛。

    「结果」不在这里：它不是存下来的字段，而是由「有没有派遣行 + accepted」
    三选一算出来的，取值集合由表结构定死（`ATTACK_LOG_RESULTS`）。

    候选值**不跟着当前筛选走**：按预设筛完之后再看战果那一档，若候选也跟着
    收窄，用户就再也切不回别的档去了——筛选器得一直是完整的那张地图。
    """

    presets: tuple[str, ...]
    #: 含 `AWAITING`（还没收到战报的那些行），当且仅当库里真有这样一行。
    outcomes: tuple[str, ...]


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


@dataclass(frozen=True)
class MissionTaskView:
    """调度台上的一行。

    `status` 与 `detail` 分开：状态是那六七个固定词之一（页面据此上色、
    悬浮窗据此显示），`detail` 是随行的事实（今日 12/32、还剩 37 个未完成）。
    合成一句的话，页面想给状态单独上色就只能去解析字符串。
    """

    #: 任务 id。页面上一切写操作都按它寻址——同一 `kind` 可以有多行。
    task_id: int
    kind: str
    #: 展示用的名字。用户没起名时回落到链路标签。
    label: str
    enabled: bool
    priority: int
    params: dict[str, int]
    status: str
    detail: str
    #: 参数的人话回显：海盗半径实际覆盖到哪、bot 区间里有几个已记录目标。
    #: 半径 10 是多大范围用户心里没数，回显出来才看得见填错没有。
    summary: str
    disabled_reason: str | None
    #: 解析完默认值之后的出发星球，写成 `星系:恒星系:位置`。
    #: 页面回显的必须是这个而不是库里那三列——NULL 的含义是「用全局主星」，
    #: 显示成空白等于让用户以为舰队不知道从哪出发。
    origin: str = ""
    #: 解析完默认值之后的航线数（这颗星球上这个任务能占几条）。
    fleet_lines: int = 0
    #: 这两个字段有没有**自己填**过。页面据此区分「跟着全局走」与「就是这个值」
    #: ——两者显示成同一个数字的话，用户改了全局值之后会以为任务也跟着变了。
    origin_is_default: bool = True
    fleet_lines_is_default: bool = True


@dataclass(frozen=True)
class CurrentMissionView:
    task_id: int
    kind: str
    label: str
    started_at_utc: datetime
    log_path: str


@dataclass(frozen=True)
class MissionRunView:
    """`mission_runs` 里的一行，翻成页面能直接摆出来的样子。

    `command` 原样带出来：事后翻账时「那一轮到底打了谁」全靠它，
    而参数早就被用户改过好几遍了。
    """

    kind: str
    label: str
    command: str
    started_at_utc: datetime
    ended_at_utc: datetime | None
    exit_code: int | None
    #: `USER` / `SELF` / `PREEMPTED` / `SHUTDOWN` / `UNKNOWN`，还没结束时为 None。
    stopped_by: str | None
    log_path: str


@dataclass(frozen=True)
class FrozenTaskView:
    """配置固化记录里的一行。

    刻意**不带 status / detail**：那两样是「现在怎么样」，而这条记录说的是
    「当时填的是什么」。混在一起，一份两天前的记录会摆出今天的状态。
    """

    kind: str
    label: str
    enabled: bool
    priority: int
    params: dict[str, int]
    #: 参数的人话回显，**只从固化的那份参数算**，不查库：
    #: 「半径 8」「2:100 – 2:200」。查库算出来的是今天的库，不是当时的。
    summary: str
    #: 当时的出发星球与航线数，同样只从记录里取。旧记录没有这两个字段，
    #: 那时显示成「—」——那不是 0，是「这条记录里没有这一项」。
    origin: str = ""
    fleet_lines: int | None = None


@dataclass(frozen=True)
class ConfigFreezeView:
    """一次「开始」固化下来的配置，翻成页面能直接摆出来的样子。"""

    frozen_at_utc: datetime
    tasks: tuple[FrozenTaskView, ...]
    #: 与**上一次「开始」**相比改了什么，每条一句人话。空元组表示没改过；
    #: 头一条记录是 `("首次记录",)`——「没改过」和「没得比」不是一回事。
    changes: tuple[str, ...]


@dataclass(frozen=True)
class SchedulerView:
    running: bool
    #: 点「开始」的时刻，供页面上那块秒表。停着时为 None。
    started_at_utc: datetime | None
    current: CurrentMissionView | None
    #: 上次没走正常关闭路径留下的进程号。**只显示给人看**，不据此杀进程。
    orphan_pid: int | None
    tasks: tuple[MissionTaskView, ...]
    #: 任务配置现在改不改得动。页面据此把输入框、复选框、拖拽把手置灰——
    #: 让用户改完才发现没生效，比一开始就不给改糟得多。
    config_locked: bool = False
    #: 本轮开始那一刻固化的配置。停着时为 None。
    frozen_config: ConfigFreezeView | None = None


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
        ranges: tuple[ScanRangeView, ...],
        fleet_line_limit: int = 1,
        reserved_lines: int = 0,
    ) -> ScanPlanView: ...
    def update_plan(self, plan_id: UUID, patch: PlanPatchView) -> ScanPlanView: ...
    def delete_plan(self, plan_id: UUID) -> None: ...
    # 运行实例这一层已经整个不在服务协议里了：`start_run` / `get_run` 是
    # `POST /api/runs/start` 与 `GET /api/runs/{run_id}` 仅有的调用点，两个接口
    # 都随「运行详情」页的关闭一起删了。库里的 `run_instances` 照旧——建与推进
    # 都在 `tools/` 的扫描与海盗链路里，读状态走 `SqlAlchemyRepository.run_state`。
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
    ) -> list[AttackLogView]: ...
    def attack_log_options(self) -> AttackLogOptions: ...
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
        """Fake 服务不模拟派遣，所以攻击日志恒为空。

        返回空列表而不是抛未实现：页面在演示服务上也要打得开，
        并且要显示「还没有攻击记录」而不是 500。
        """
        return []

    def attack_log_options(self) -> AttackLogOptions:
        """同上：没有派遣记录，也就没有预设与战果可供筛选。

        两个空元组让筛选器只剩「全部」一项，而不是摆出一串筛不出东西的档位。
        """
        return AttackLogOptions(presets=(), outcomes=())

    def dashboard(self) -> DashboardView:
        """Fake 服务里「进行中的运行」恒为 0。

        它原先数的是 `start_run` 建出来的内存运行实例，而 `start_run` 随
        `POST /api/runs/start` 一起删了——这个 Fake 现在没有任何造运行实例的入口。
        返回 0 而不是留一个永远空的字典，同 `list_attack_log` 恒空：演示服务照样
        打得开，只是没有可数的东西。真实数字由 `PersistentApplicationService`
        直接查 `run_instances` 得出。
        """
        with self._lock:
            pending = sum(1 for revisit in self._revisits if revisit.status == "pending")
            return DashboardView(
                plan_count=len(self._plans),
                active_run_count=0,
                target_count=len(self._targets),
                pending_revisit_count=pending,
            )


__all__ = [
    "ATTACK_LOG_RESULTS",
    "ApplicationService",
    "AttackLogOptions",
    "BotTargetView",
    "ConflictError",
    "CurrentMissionView",
    "DashboardView",
    "FakeApplicationService",
    "FleetChangeView",
    "FleetDiffView",
    "FleetEntryView",
    "FleetSnapshotView",
    "MissionRunView",
    "MissionTaskView",
    "NotFoundError",
    "PlanPatchView",
    "RevisitView",
    "ScanPlanView",
    "SchedulerView",
    "ScanRangeView",
    "ServiceError",
    "StateEventView",
    "_parse_coordinate",
    "_parse_window",
]
