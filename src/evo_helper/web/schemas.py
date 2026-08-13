"""Pydantic request/response models for the local HTTP API."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CoordinateModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    galaxy: int = Field(ge=1)
    system: int = Field(ge=1)
    position: int = Field(ge=1)


class ScanRangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: CoordinateModel
    end: CoordinateModel
    origin: CoordinateModel
    fleet_preset: str = Field(min_length=1, max_length=64)
    fleet_preset_signature: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=0, ge=0, le=100)


class ScanPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    window_start: str = Field(pattern=r"^\d{2}:\d{2}$")
    window_end: str = Field(pattern=r"^\d{2}:\d{2}$")
    #: Fleet lines this plan may occupy, and how many stay free for the user.
    fleet_line_limit: int = Field(default=1, ge=1, le=99)
    reserved_lines: int = Field(default=0, ge=0, le=99)
    ranges: list[ScanRangeIn] = Field(min_length=1)


class ScanPlanPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    window_start: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    window_end: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    fleet_line_limit: int | None = Field(default=None, ge=1, le=99)
    reserved_lines: int | None = Field(default=None, ge=0, le=99)
    ranges: list[ScanRangeIn] | None = None


class ScanRangeOut(ScanRangeIn):
    pass


class CoordinateScanOut(BaseModel):
    coordinate: CoordinateModel
    scanned_at_utc: datetime
    owner_name: str | None
    is_bot: bool
    confidence: float


class ScanPlanOut(BaseModel):
    id: UUID
    name: str
    enabled: bool
    window_start: str
    window_end: str
    fleet_line_limit: int
    reserved_lines: int
    ranges: list[ScanRangeOut]
    created_at: datetime
    updated_at: datetime


class RevisitIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str = Field(pattern=r"^(target|plan|range)$")
    reason: str = Field(min_length=1, max_length=256)
    target_coordinate: CoordinateModel | None = None


class RevisitOut(BaseModel):
    revisit_id: UUID
    scope: str
    reason: str
    requested_at_utc: datetime
    status: str
    target_coordinate: CoordinateModel | None = None


class BotTargetOut(BaseModel):
    coordinate: CoordinateModel
    latest_player: str | None = None
    last_scan_at: datetime | None = None
    last_attack_at: datetime | None = None
    last_dispatch_at: datetime | None = None
    last_report_at: datetime | None = None


class FleetEntryOut(BaseModel):
    ship_type: str
    quantity: int


class FleetSnapshotOut(BaseModel):
    snapshot_id: UUID
    coordinate: CoordinateModel
    captured_at_utc: datetime
    side: str
    total: int
    is_revisit: bool
    match_confidence: float
    review_status: str
    ships: list[FleetEntryOut]


class FleetChangeOut(BaseModel):
    ship_type: str
    before: int
    after: int
    delta: int
    percent: float


class FleetDiffOut(BaseModel):
    coordinate: CoordinateModel
    before: FleetSnapshotOut | None = None
    after: FleetSnapshotOut
    added: list[FleetEntryOut]
    removed: list[FleetEntryOut]
    disappeared: list[str]
    first_seen: list[str]
    changes: list[FleetChangeOut]
    total_before: int
    total_after: int


class StateEventOut(BaseModel):
    event_id: UUID
    occurred_at_utc: datetime
    aggregate: str
    aggregate_id: UUID
    event: str
    from_state: str | None = None
    to_state: str | None = None


class MissionTaskOut(BaseModel):
    #: 任务 id。**页面上一切写操作都按它寻址**——同一 `kind` 可以有多行
    #: （多个 bot 攻击任务），按 kind 寻址会打到不确定的那一行上。
    task_id: int
    kind: str
    #: 界面上的名字。桌面悬浮窗是个瘦客户端，它只认接口给的这个字符串。
    label: str
    enabled: bool
    priority: int
    params: dict[str, int]
    #: `运行中` / `待命` / `等航线` / `冷却中` / `配额用尽` / `已完成` /
    #: `已停用` / `未启用`，由 `domain.scheduler.status_of` 判定。
    status: str
    #: 状态旁边那句随行的事实：`今日 12/32`、`还剩 37 个未完成`。
    detail: str
    #: 参数的人话回显：出发星球、航线数、半径实际覆盖到哪、区间里有几个 bot。
    summary: str
    disabled_reason: str | None = None
    #: 解析完默认值之后的出发星球（`星系:恒星系:位置`）与航线数。
    #: 回显的是**解析后**的值：库里那三列为 NULL 时含义是「用全局主星」，
    #: 显示成空白等于让用户以为舰队不知道从哪出发。
    origin: str = ""
    fleet_lines: int = 0
    #: 这两样是不是还跟着全局值走（也就是任务自己没填过）。
    origin_is_default: bool = True
    fleet_lines_is_default: bool = True


class MissionTaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 每一样各自独立，`None` 一律表示「这次不动它」而不是「清空」。
    enabled: bool | None = None
    priority: int | None = None
    #: 海盗 `{"radius": N}`、bot `{"galaxy": G, "first_system": A, "last_system": B}`。
    #: 只收整数：`{"radius": true}` 这种会被当成半径 1，悄悄打出一圈不是用户
    #: 想要的范围。
    params: dict[str, int] | None = None
    name: str | None = None
    #: 出发星球，`星系:恒星系:位置`。
    #: **空串是一个动作**：把它退回「用全局主星」。它和 `None`（这次不动它）
    #: 必须分得开，否则任何一次只改优先级的 PATCH 都会顺手把出发星球抹掉。
    origin: str | None = None
    fleet_lines: int | None = None


class MissionTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 目前只收 `BOT`：只有 bot 攻击需要多任务（用户口径 2026-08-13）。
    kind: str
    #: 必填。同类型的多个任务在页面上全靠它区分。
    name: str
    #: 留空表示跟着全局值走。
    origin: str | None = None
    fleet_lines: int | None = None


class CurrentMissionOut(BaseModel):
    task_id: int
    kind: str
    label: str
    started_at_utc: datetime
    log_path: str


class FrozenTaskOut(BaseModel):
    """配置固化记录里的一行：**当时填的是什么**，不含当时的状态。"""

    kind: str
    label: str
    enabled: bool
    priority: int
    params: dict[str, int]
    #: 只从固化的那份参数算出来的人话回显：`半径 8`、`2:100 – 2:200`。
    summary: str
    #: 当时的出发星球与航线数，同样只从记录里取。旧记录没有这两项，
    #: 那时分别是空串与 None——不是 0，是「这条记录里没有这一项」。
    origin: str = ""
    fleet_lines: int | None = None


class ConfigFreezeOut(BaseModel):
    frozen_at_utc: datetime
    tasks: list[FrozenTaskOut]
    #: 与上一次「开始」相比改了什么。空表示没改；`["首次记录"]` 表示没得比。
    changes: list[str]


class SchedulerOut(BaseModel):
    running: bool
    #: 点「开始」的时刻，供页面与悬浮窗上那块秒表。停着时为 None。
    started_at_utc: datetime | None = None
    current: CurrentMissionOut | None = None
    #: 上次没走正常关闭路径留下的进程号。**只显示给人看**，不据此杀进程——
    #: pid 会被系统回收复用。
    orphan_pid: int | None = None
    tasks: list[MissionTaskOut]
    #: 任务配置现在改不改得动。运行中为真，页面据此把输入框、复选框、拖拽
    #: 把手一并置灰——让用户改完才发现 409，比一开始就不给改糟得多。
    config_locked: bool = False
    #: 本轮开始那一刻固化下来的配置。停着时为 None。
    frozen_config: ConfigFreezeOut | None = None


class SchedulerStartIn(BaseModel):
    """POST /api/scheduler/start 的请求体。**整个可以省略。**

    省略时按默认值走，也就是「先对账再放行任务」——桌面悬浮窗
    （`tools/scan_console.py`）和 4.3 节里那条 curl 都是不带体打的，
    它们不该因为多了这个字段而换一种行为。
    """

    model_config = ConfigDict(extra="forbid")

    #: 点「开始」之后先跑一趟战报对账（海盗、bot 各一趟），跑完才起任务。
    #: **默认为真**：默认跳过等于把这条修复关掉，而它防的是「拿不全的数据
    #: 决定要不要再打一遍」，代价是白送一支舰队。
    reconcile: bool = True


class BackfillStartIn(BaseModel):
    """POST /api/backfill 的请求体。"""

    model_config = ConfigDict(extra="forbid")

    #: `pirate` / `bot`。两条链路的信箱主题不同，一趟只读得了一种。
    kind: str
    #: 起始日期（**UTC 日**，`YYYY-MM-DD`）。和战报上写的时间同一套口径——
    #: 游戏一律按 UTC+0 显示。
    since: date
    #: 翻几页信箱、最多开几封。不填就用 CLI 自己的默认值：**两处各写一份默认
    #: 值，改了一边就是另一边悄悄按旧值跑**。页面上不给这两个框。
    max_pages: int | None = None
    max_opens: int | None = None
    #: 补录模式：一直翻到 `since` 为止，不因为单子空了就收工。
    #:
    #: **默认 True，而点「开始」时的启动对账默认 False**——两个默认值反着来是
    #: 故意的，因为两条路要的东西相反：手动这一趟基本都是来救**过期**战报的
    #: （那些派遣早掉出了 6 小时单子，对账模式在第一封「库里已有」就收工，
    #: 一份都够不着），而启动对账要的正是早停，没有欠账时几十秒走完。
    #:
    #: 默认成 False 会让这个按钮在最常见的用法上静默地什么都捞不回来——
    #: 而它跑完还会显示「补录完成」。
    exhaustive: bool = True


class BackfillSummaryOut(BaseModel):
    """一批补录改了什么。跑完摆在页面上，用户看过才放行任务。"""

    reports_ingested: int
    #: 认领上了几发派遣。**认领上了才会影响任务决策**——一份挂在那里没认领的
    #: 战报，`domain.bot_round.phase_of` 根本看不见。
    dispatches_claimed: int
    #: 几个 bot 目标从「本轮还要打」变成了「本轮已完成」。这就是省下来的重复攻击。
    bot_targets_settled: int
    #: 一共量了几个 bot 目标。0 表示没量（没有参与调度的 bot 任务），
    #: 和「量了但一个都没变」不是一回事。
    bot_targets_measured: int


class BackfillOut(BaseModel):
    phase: str
    kind: str | None = None
    label: str = ""
    since: str = ""
    reason: str = ""
    started_at_utc: datetime | None = None
    ended_at_utc: datetime | None = None
    exit_code: int | None = None
    log_path: str = ""
    #: 日志的最后几十行。补录跑十几分钟，这是页面上唯一的进度来源。
    log_tail: str = ""
    queued: int = 0
    #: 补录此刻扣不扣着游戏窗口。为真时调度器一个任务都不起。
    blocking: bool = False
    #: 跑完了但还没确认放行。页面据此显示「继续任务」按钮。
    awaiting_ack: bool = False
    detail: str = ""
    summary: BackfillSummaryOut | None = None


class DashboardOut(BaseModel):
    plan_count: int
    active_run_count: int
    target_count: int
    pending_revisit_count: int


__all__ = [
    "BackfillOut",
    "BackfillStartIn",
    "BackfillSummaryOut",
    "BotTargetOut",
    "ConfigFreezeOut",
    "CoordinateModel",
    "CurrentMissionOut",
    "DashboardOut",
    "FleetChangeOut",
    "FleetDiffOut",
    "FleetEntryOut",
    "FleetSnapshotOut",
    "FrozenTaskOut",
    "MissionTaskCreate",
    "MissionTaskOut",
    "MissionTaskPatch",
    "RevisitIn",
    "RevisitOut",
    "ScanPlanIn",
    "ScanPlanOut",
    "ScanPlanPatch",
    "ScanRangeIn",
    "ScanRangeOut",
    "SchedulerOut",
    "SchedulerStartIn",
    "StateEventOut",
]
