"""Pydantic request/response models for the local HTTP API."""

from datetime import date, datetime
from typing import Any
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
    #: 界面上的名字。由服务端下发，页面不自己拼。
    label: str
    enabled: bool
    priority: int
    params: dict[str, Any]
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
    #: 定时开启 / 定时关闭的时刻，**UTC**（页面自己换算成 UTC+8 显示）。
    #: 没配就是 None，含义是「这一端不限」。
    enabled_from_utc: datetime | None = None
    enabled_until_utc: datetime | None = None


class MissionTaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 每一样各自独立，`None` 一律表示「这次不动它」而不是「清空」。
    enabled: bool | None = None
    priority: int | None = None
    #: 海盗 `{"radius": N}`、bot `{"galaxy": G, "first_system": A, "last_system": B}`。
    #: 只收整数：`{"radius": true}` 这种会被当成半径 1，悄悄打出一圈不是用户
    #: 想要的范围。
    params: dict[str, Any] | None = None
    name: str | None = None
    #: 出发星球，`星系:恒星系:位置`。
    #: **空串是一个动作**：把它退回「用全局主星」。它和 `None`（这次不动它）
    #: 必须分得开，否则任何一次只改优先级的 PATCH 都会顺手把出发星球抹掉。
    origin: str | None = None
    fleet_lines: int | None = None
    #: 定时开启 / 定时关闭的时刻，ISO 8601，**必须带时区**（页面送的是
    #: `…+08:00`，服务端存 UTC）。不带时区的话「几点」这件事就没有答案，
    #: 服务端替它猜一个只会在 UTC+8 与 UTC 之间差出 8 小时。
    #:
    #: **空串是一个动作**：把这一端退回「不限」。它和 `None`（这次不动它）
    #: 必须分得开，同 `origin`——否则任何一次只改优先级的 PATCH 都会顺手
    #: 把定时窗口抹掉。两端各有各的空串，清一端不影响另一端。
    enabled_from: str | None = None
    enabled_until: str | None = None


class MissionTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: 目前只收 `BOT`：只有 bot 攻击需要多任务（用户口径 2026-08-13）。
    kind: str
    #: 必填。同类型的多个任务在页面上全靠它区分。
    name: str
    #: 留空表示跟着全局值走。
    origin: str | None = None
    fleet_lines: int | None = None


class MissionOriginIn(BaseModel):
    """军力任务选择配置页中的一颗出发星球；保存整组时原子替换。"""

    model_config = ConfigDict(extra="forbid")

    planet_id: int = Field(ge=1)
    fleet_lines: int = Field(ge=1)
    enabled: bool = True


class MissionOriginOut(CoordinateModel):
    planet_id: int | None = None
    fleet_lines: int = Field(ge=1)
    enabled: bool = True


class AttackPlanetIn(CoordinateModel):
    """全局攻击星球配置。"""


class AttackPlanetOut(AttackPlanetIn):
    planet_id: int
    number: int


class MilitaryTierIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_score: float = Field(ge=0)
    preset: str = Field(min_length=1, max_length=120)


class MilitaryAttackConfigOut(BaseModel):
    tiers: list[MilitaryTierIn]
    #: 军力榜采集开榜后先盲拖几屏。**`None` = 留空 = 按实测自动标定。**
    #:
    #: 这里只认「是不是整数」（`3.5` / `"很多"` / `true` 一律 422）；范围由
    #: `MissionScheduler.validate_blind_scrolls` 判，和调度器启动时用的是同一把
    #: 尺子——两边分家的结果是页面收下了、实机跑起来不是那个数。
    blind_scrolls: int | None = None
    #: 对账那一趟翻信箱最多往回读几个小时。**`None` = 留空 = 默认 6 小时。**
    #:
    #: 同上一项：这里只认「是不是整数」，范围由
    #: `MissionScheduler.validate_report_scan_hours` 判——两边分家的结果是页面收下
    #: 了、实机跑起来不是那个数。
    report_scan_hours: int | None = None
    #: 读不到飞行时间时，一条航线占多久（分钟）。**`None` = 留空 = 默认 90。**
    #:
    #: 与 `blind_scrolls` 同一条规矩：这里只认「是不是整数」，范围由
    #: `MissionScheduler.validate_unknown_line_hold_minutes` 判——页面和调度器
    #: 必须用同一把尺子，分家的结果是页面收下了、实机跑起来不是那个数。
    unknown_line_hold_minutes: int | None = None
    #: 两次开工翻信箱之间至少隔多久（分钟）。**`None` = 留空 = 默认 15。**
    #: `0` 是合法取值，意思是「每一轮开工都翻」，不是「关掉」。
    reconcile_cooldown_minutes: int | None = None
    #: 同一个 bot 坐标多久之内不重复打（小时）。**`None` = 留空 = 默认 24。**
    bot_revisit_hours: int | None = None
    #: 撞上保护期之后这个坐标排除多久（小时）。**`None` = 留空 = 默认 8。**
    #:
    #: ⚠️ 默认值 8 与游戏那条「保护期 8 小时」**同数不同义**：前者是我们的策略
    #: （撞上的时刻已知、保护期起点未知，宁可过度排除），后者是游戏规则。
    #: 范围由 `MissionScheduler.validate_protection_exclusion_hours` 判。
    protection_exclusion_hours: int | None = None
    # ⚠️ 这里曾经有一个 `military_time_pool`，2026-08-18 随那个错误设计一起删掉了
    # （理由在 `storage.models.MilitaryAttackConfigRow` 与 `domain.target_order`
    # 模块头第 3 步）。**别加回来**：「用多新的数据」现在由任务参数
    # `score_max_age_hours` 划线回答，不再有「取前几个」这件事。
    #: **全账号**同时在飞的舰队上限。**`None` = 留空 = 默认 9。**
    #:
    #: 它和任务上那个「航线数」是两道**同时生效**的闸（用户口径 2026-08-18：
    #: 「我的总航线数是所有星球共享的」「两者均需要约束」）。范围由
    #: `MissionScheduler.validate_account_line_limit` 判，这里只认「是不是整数」。
    account_line_limit: int | None = None
    #: 自动停用/自动恢复的日志限流窗口（秒）。**`None` = 留空 = 默认 120。**
    #: `0` 是合法取值，意思是「每一次跃迁都记」，不是「关掉日志」。
    auto_toggle_log_seconds: int | None = None


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
    params: dict[str, Any]
    #: 只从固化的那份参数算出来的人话回显：`半径 8`、`2:100 – 2:200`。
    summary: str
    #: 当时的出发星球与航线数，同样只从记录里取。旧记录没有这两项，
    #: 那时分别是空串与 None——不是 0，是「这条记录里没有这一项」。
    origin: str = ""
    fleet_lines: int | None = None


class ConfigFreezeOut(BaseModel):
    frozen_at_utc: datetime
    tasks: list[FrozenTaskOut]
    military_tiers_label: str = ""
    #: 与上一次「开始」相比改了什么。空表示没改；`["首次记录"]` 表示没得比。
    changes: list[str]


class SchedulerOut(BaseModel):
    running: bool
    #: 点「开始」的时刻，供页面上那块秒表。停着时为 None。
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


class LineReleaseOut(BaseModel):
    """POST /api/attack-lines/release 的回执。

    `released` 是**这一下真的放开了几条**。页面把它写出来：这个按钮唯一的可见
    后果是若干个任务从「等航线」变回「待命」，而那要等下一轮轮询才看得见——
    中间这段空白里，这个数字是用户判断「点到了没有」的唯一凭据。
    """

    released: int
    released_at_utc: datetime


class SchedulerStartIn(BaseModel):
    """POST /api/scheduler/start 的请求体。**整个可以省略。**

    省略时按默认值走——4.3 节里那条 curl 就是不带体打的，它不该因为多了这个
    字段而换一种行为。
    """

    model_config = ConfigDict(extra="forbid")

    #: 点「开始」之后先跑一趟战报对账（海盗、bot 各一趟），跑完才起任务。
    #:
    #: ⚠️ **默认关（2026-08-13 实机之后改的），因为它会把整夜挂机堵死。**
    #:
    #: 那一趟一旦失败，`application.backfill.BackfillState.blocking` 把
    #: `FAILED` 也算成「扣着窗口」，要等人点确认才放行——用户口径本来是
    #: 「看过摘要再放行」，而那条口径默认了**有人在看**。无人值守时它变成：
    #: 凌晨崩一次，之后一整夜一个任务都不起，而页面上只显示「补录中」。
    #:
    #: 实机 2026-08-13 22:56 就是这样：启动对账那一趟倒在
    #: 「游戏窗口抢不到前台」（那是个已知条件，见 `domain.scheduler`
    #: `EXIT_ENVIRONMENT_BUSY`），而它在 CLI 里是未捕获的异常、退出码不是 75。
    #:
    #: **关掉它损失不大**：每一轮开工本来就有 `reconcile_today` 翻一趟信箱，
    #: 启动对账只是额外多一趟、翻得更深。真正要救过期战报时走页面上那个手动
    #: 按钮——那时人在跟前，确认也就有人点。
    #:
    #: ⚠️ 上面这句「本来就有」在 2026-08-15 到 08-17 之间**是假的**，而这道闸门
    #: 就是靠它才敢关的：那两天里 `LoopOptions.reconcile_on_start` 默认 False，
    #: runner 一趟信箱都不翻，于是攻击照派、战报一份没读。两道闸门各自以「另一道
    #: 还开着」为理由，谁也没开着。整段前因后果在 `domain.reconcile_cooldown`。
    #:
    #: 那一侧现在改成了冷却判据（默认必翻，只是不每轮都翻），所以这句话重新成立。
    #:
    #: **但这道闸门仍然保持默认关**：它关掉的理由（对账失败会让
    #: `BackfillState.blocking` 扣住窗口、整夜一个任务都不起）今天依然成立。
    #: 要恢复默认开，原本要解决两件事，现在还剩一件：
    #:
    #: - ~~CLI 把「抢不到前台」按 75 收场~~ —— 已经做了：
    #:   `game.game_window.ForegroundUnavailable` +
    #:   `tools.scan_coordinates.run_with_foreground_guard`。
    #: - `blocking` 对 `FAILED` 那一档仍然没有无人值守的出路。
    #:
    #: 剩下这一件没解决之前单独重开它，只会把故障从「不读战报」换成「整夜不起任务」。
    reconcile: bool = False


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
    "LineReleaseOut",
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
