"""Concrete domain records exchanged with the repository.

The frozen RepositoryPort accepts ``object`` payloads; these records give that
contract deterministic, framework-free shapes for scans, intents, dispatches,
reports, events, revisits, and fleet-diff results. All timestamps are
timezone-aware UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from .models import Coordinate, FleetPresetRef

#: 攻击目标的两类。字符串常量而不是枚举：它要原样进数据库、也要原样进接口，
#: 而枚举在这两处都得来回转换，转换点越多越容易两边写成不同的字面量。
TARGET_KIND_BOT = "bot"
TARGET_KIND_PIRATE = "pirate"

#: 界面上的中文标签。日志页只显示中文，库里存的仍是上面的英文常量。
TARGET_KIND_LABELS = {TARGET_KIND_BOT: "bot", TARGET_KIND_PIRATE: "海盗"}

#: 一次派遣的**性质**：真打出去的一发，还是一发侦察探测器。
#:
#: 与 `TARGET_KIND_*` 是两个正交的维度，缺一个就会算错两笔账：侦察也是打向海盗的，
#: 只按 `target_kind` 分的话，日配额查询会把每一发侦察都当成一次攻击——一轮 4 发，
#: 当天 32 次额度以 4 倍速度消失，而且完全静默。反过来，侦察**确实占航线**，
#: 所以在飞数两者都要数。
#:
#: 同样是字符串常量而不是枚举，理由与 `TARGET_KIND_*` 一致：原样进库、原样出接口。
MISSION_KIND_ATTACK = "ATTACK"
MISSION_KIND_SCOUT = "SCOUT"


@dataclass(frozen=True)
class CoordinateScan:
    run_id: UUID
    coordinate: Coordinate
    scanned_at_utc: datetime
    owner_name: str | None = None
    is_bot: bool = False
    confidence: float = 0.0
    evidence_artifact_id: UUID | None = None


@dataclass(frozen=True)
class AttackIntent:
    intent_id: UUID
    run_id: UUID
    origin: Coordinate
    target: Coordinate
    preset: FleetPresetRef
    cycle_start_utc: datetime
    created_at_utc: datetime
    guard_status: str = "PENDING"
    forced_revisit: bool = False
    #: 打的是 bot 还是海盗。两种目标的判定链路、预设、收益都不一样，
    #: 事后翻日志时「这一发是打谁的」是第一个要回答的问题。
    #: 默认 `bot`：这个字段加进来之前的存量意图全部来自 bot 攻击链路。
    target_kind: str = TARGET_KIND_BOT


@dataclass(frozen=True)
class AttackDispatch:
    dispatch_id: UUID
    intent_id: UUID
    dispatched_at_utc: datetime
    accepted: bool
    evidence_artifact_id: UUID | None = None
    #: 这一发是攻击还是侦察（见 `MISSION_KIND_*`）。默认 `ATTACK`：这个字段
    #: 加进来之前入库的派遣全部来自攻击链路，侦察那时压根没有记录。
    mission_kind: str = MISSION_KIND_ATTACK


@dataclass(frozen=True)
class FleetSnapshotEntry:
    side: str
    ship_type: str
    count: int
    round_no: int | None = None
    #: 这一行的数没有把握，界面上要标出来。
    uncertain: bool = False


@dataclass(frozen=True)
class BattleReport:
    report_id: UUID
    reported_at_utc: datetime
    attacker_origin: Coordinate
    defender_target: Coordinate
    raw_time_text: str | None = None
    ui_version: str | None = None
    match_confidence: float = 0.0
    manual_review_status: str = "PENDING"
    is_from_revisit: bool = False
    fleet: tuple[FleetSnapshotEntry, ...] = ()
    #: 战斗详情页的「单位」总数，与 `fleet` 是两个独立来源；
    #: 大舰队的逐行数量是四舍五入显示，相加凑不出这个数。
    attacker_units: int | None = None
    defender_units: int | None = None
    #: 详情页那行大字：`VICTORY` / `FAIL`。存游戏画面上的原文，不翻译——
    #: 界面要显示中文是渲染层的事，库里只存读到的那个词。
    #: 可空：这个字段加进来之前入库的战报没读过胜负，填 `FAIL` 会凭空造出败仗。
    outcome: str | None = None
    #: 详情页的「损失单位」总数，双方各一。**海盗战报只记这个和 `outcome`**
    #: （用户口径 2026-08-09，为省性能）：明细要进回放页，一份报告多花两三秒，
    #: 而海盗全是同一个预设打的，逐舰种没有分析价值。
    attacker_losses: int | None = None
    defender_losses: int | None = None


@dataclass(frozen=True)
class StateEvent:
    aggregate_type: str
    aggregate_id: UUID
    event: str
    occurred_at_utc: datetime
    before_state: str | None = None
    after_state: str | None = None


@dataclass(frozen=True)
class UiObservation:
    observation_id: UUID
    screen: str
    ui_version: str | None
    detection_result: str | None
    confidence: float
    observed_at_utc: datetime
    evidence_artifact_id: UUID | None = None


@dataclass(frozen=True)
class TargetRevisit:
    revisit_id: UUID
    scope: str
    reason: str
    requested_at_utc: datetime
    target: Coordinate | None = None
    status: str = "PENDING"
    executed_at_utc: datetime | None = None


@dataclass(frozen=True)
class ReportHistoryEntry:
    report_id: UUID
    reported_at_utc: datetime
    side: str
    ship_type: str
    count: int
    is_from_revisit: bool
    match_confidence: float
    manual_review_status: str


@dataclass(frozen=True)
class ShipTypeDiff:
    ship_type: str
    before_count: int
    after_count: int
    absolute_change: int
    percent_change: float | None
    status: str
    first_seen: bool


@dataclass(frozen=True)
class FleetDiff:
    before_report_id: UUID | None
    after_report_id: UUID
    side: str
    total_before: int
    total_after: int
    total_change: int
    ships: tuple[ShipTypeDiff, ...]
    is_from_revisit: bool
    match_confidence: float
    manual_review_status: str
