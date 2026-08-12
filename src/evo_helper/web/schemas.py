"""Pydantic request/response models for the local HTTP API."""

from datetime import datetime
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
    #: 参数的人话回显：半径实际覆盖到哪、区间里有几个已记录的 bot。
    summary: str
    disabled_reason: str | None = None


class MissionTaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 三样各自独立，`None` 一律表示「这次不动它」而不是「清空」。
    enabled: bool | None = None
    priority: int | None = None
    #: 海盗 `{"radius": N}`、bot `{"galaxy": G, "first_system": A, "last_system": B}`。
    #: 只收整数：`{"radius": true}` 这种会被当成半径 1，悄悄打出一圈不是用户
    #: 想要的范围。
    params: dict[str, int] | None = None


class CurrentMissionOut(BaseModel):
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


class ConfigFreezeOut(BaseModel):
    frozen_at_utc: datetime
    tasks: list[FrozenTaskOut]
    #: 与上一次「开始」相比改了什么。空表示没改；`["首次记录"]` 表示没得比。
    changes: list[str]
    #: 那一轮的分档阈值，`2000 / 4000 / 8000`。这个字段是后加的，历史记录里
    #: 没有——那时为 None，页面写「未记录」，**不回填一个默认值**。
    tier_thresholds: str | None = None


class TierBandOut(BaseModel):
    """一档一行：这个区间的守方，用哪套预设。"""

    #: `AAA` / `BBB` / `CCC`，最低那一档是 `（不派）`。
    preset: str
    #: `2K 以下` / `2K–4K` / `8K+`。
    span: str


class TierThresholdsOut(BaseModel):
    """分档阈值页的全部内容。"""

    alpha_from: int
    beta_from: int
    gamma_from: int
    #: 四行区间回显，从低到高。服务端算好送出去，页面不自己拼——
    #: 两边各拼一次就会对同一套阈值说出两种区间。
    bands: list[TierBandOut]
    #: 调度器运行中为真。页面据此把三个框和「保存」置灰。
    locked: bool = False


class TierThresholdsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 三个数一次全给。**不做成各自可选**：阈值的合法性是三个数之间的关系
    #: （严格递增），单独改一个必须拿另外两个一起量，那还不如让调用方把三个
    #: 都摆出来——页面上本来就是三个框一起保存。
    #:
    #: `ge=1` 只挡住负数和零这种一眼看得出的；严格递增由
    #: `domain.fleet_tier.TierThresholds` 判，错误信息要说清哪一档会变成死区。
    alpha_from: int = Field(ge=1)
    beta_from: int = Field(ge=1)
    gamma_from: int = Field(ge=1)


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


class DashboardOut(BaseModel):
    plan_count: int
    active_run_count: int
    target_count: int
    pending_revisit_count: int


__all__ = [
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
    "StateEventOut",
    "TierBandOut",
    "TierThresholdsOut",
    "TierThresholdsPatch",
]
