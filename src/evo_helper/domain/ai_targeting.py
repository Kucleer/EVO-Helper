"""AI 选靶一期的**纯判据**：picks 的硬校验、软核对、以及领域记录。

这里不碰网络、不碰库、不碰线程。prompt 组装与 LLM 调用在
`application.ai_targeting`，那层把事实查好传进来，本层只做「AI 有没有守规则、
有没有编数字」这两件事——它们必须能脱离游戏和模型被钉住。

## 硬校验 vs 软核对

- **硬校验**不过就整份作废（`schema_violation`），不落 `ai_picks_json`。
  判据见 `需求文档 5.2`：picks 数必须恰好等于预算、target/origin/preset 必须
  来自给过的集合、同一坐标不许出现两次、每个 origin 分到的发数不许超预算。
- **软核对**只记录、不作废。两类：**数字自洽**（AI 报的军力 / 龄 / 往返与
  我们的数据差多少，容差内算过）和**规则遵守**（有没有推荐撞在保护期里的、
  距我方上次攻击不足 8 小时的）。

⚠️ **编数字是 LLM 最危险的失败模式。** 让它把 `military` / `reading_age_hours` /
`round_trip_minutes` 报回结构化字段，就是为了把「靠人眼比对」变成「靠断言」。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import GAME_PROTECTION_HOURS

#: 军力读数「差多少算编数」。军力是库里逐行存的值，AI 原样报回来就该分毫不差。
#: 留一个极小容差只为吸收 JSON 数字往返的浮点噪声，**不是给编数留余地**。
MILITARY_EQUALITY_TOLERANCE = 1e-6

#: 读数龄（小时）允许的偏差。需求文档 5.3：±0.1h。
READING_AGE_TOLERANCE_HOURS = 0.1

#: 往返分钟允许的偏差。需求文档 5.3：±1 分钟。
ROUND_TRIP_TOLERANCE_MINUTES = 1.0


class AiDecisionStatus(StrEnum):
    """这一轮记录的状态。失败那一档记进 `status`，**调度照常派遣**。"""

    OK = "ok"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    INVALID_JSON = "invalid_json"
    SCHEMA_VIOLATION = "schema_violation"


@dataclass(frozen=True)
class InflightLine:
    """一条还占着航线的在飞派遣，供 prompt 的航线预算那一块用。

    `line_free_at_utc` 为 None = **时长未知**：飞行时间没读出来，按兜底
    （`report_wait.UNKNOWN_LINE_HOLD`，可配置）占着，实际可能早就回来了。
    给 AI 时这条要单独标出来，不然它会把猜测当事实（方案 2.1）。
    """

    dispatched_at_utc: datetime
    line_free_at_utc: datetime | None


@dataclass(frozen=True)
class AiPick:
    """AI 返回的 `picks` 里的一项，解析后的结构。"""

    target: Coordinate
    origin: Coordinate
    preset: str
    rank: int | None = None
    military: float | None = None
    reading_age_hours: float | None = None
    round_trip_minutes: float | None = None
    reason: str = ""


@dataclass(frozen=True)
class PickVocabulary:
    """硬校验要用的「输入给过 AI 的集合」。**只许从喂进去的东西里取。**"""

    targets: frozenset[Coordinate]
    origins: frozenset[Coordinate]
    presets: frozenset[str]
    #: 每个 origin 分到的可派发数（两道闸算完之后的真实预算）。
    budget_by_origin: Mapping[Coordinate, int]
    #: 全轮可派发数 = 各 origin 预算之和。**picks 必须恰好等于它，不多不少。**
    total_budget: int


@dataclass(frozen=True)
class SoftReference:
    """软核对用的我方事实：AI 报回来的三个数与这几份比。

    `now` 是产生决策的时刻（`MilitaryPoolReading.now`），不是核对时刻——
    核对发生在后台线程里，可能晚几秒，而保护期 / 攻击间隔判据用的是决策那一刻
    的事实。
    """

    #: target -> 军力（真实读数）。
    military: Mapping[Coordinate, float]
    #: target -> 读数龄（小时）。
    reading_age_hours: Mapping[Coordinate, float]
    #: target -> origin -> 往返分钟（`domain.flight_time.round_trip_hours` 换算）。
    round_trip_minutes: Mapping[Coordinate, Mapping[Coordinate, float]]
    #: target -> 我方上次攻击时刻。None = 从没打过。
    last_attack_at: Mapping[Coordinate, datetime | None]
    #: target -> 保护期到什么时候（`protection_seen_at_utc` + 8 小时）。None = 没撞过。
    protected_until: Mapping[Coordinate, datetime | None]
    now: datetime
    #: 已知**根本没有军力读数**的候选。
    #:
    #: ⚠️ **「查不到」和「已知没有」必须分开。** 喂给 AI 的是全池
    #: （`MilitaryPoolReading.candidates`），里面本来就有一批军力榜从没见过的坐标。
    #: 只让它们在 `military` 里缺席的话，AI 给它们编一个军力数会被当成
    #: 「无从核对」放过去——而那恰恰是最该抓的一种编数字。
    targets_without_reading: frozenset[Coordinate] = frozenset()


@dataclass(frozen=True)
class AiTargetDecision:
    """一期记录的一条。`response` 与 `picks` 在失败那几档为 None。"""

    decided_at_utc: datetime
    task_id: int | None
    run_id: UUID | None
    cycle_start_utc: datetime | None
    budget: int
    algorithm_picks_json: str
    prompt_text: str
    status: str
    model: str | None = None
    ai_picks_json: str | None = None
    overlap: int | None = None
    response_text: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    violations_json: str | None = None


def _coordinate_text(value: Coordinate) -> str:
    return f"{value.galaxy}:{value.system}:{value.position}"


def parse_pick(item: object) -> AiPick | None:
    """把 AI 返回的 JSON 里的一项解析成 `AiPick`；形状不对返回 None。

    `target` / `origin` / `preset` 三样缺一不可（形状校验在硬校验那层），
    三个可空数字字段宽松处理——它们本来就是软核对的输入，缺了只是那项没法
    核对，不是整份作废的理由（整份作废只发生在 `schema_violation` 那几档）。
    """
    if not isinstance(item, dict):
        return None
    try:
        target = _parse_coord(item["target"])
        origin = _parse_coord(item["origin"])
    except (KeyError, TypeError, ValueError):
        return None
    preset = item.get("preset")
    if not isinstance(preset, str) or not preset:
        return None
    rank = item.get("rank")
    military = item.get("military")
    age = item.get("reading_age_hours")
    minutes = item.get("round_trip_minutes")
    reason = item.get("reason")
    return AiPick(
        target=target,
        origin=origin,
        preset=preset,
        rank=_optional_float_to_int(rank),
        military=_optional_float(military),
        reading_age_hours=_optional_float(age),
        round_trip_minutes=_optional_float(minutes),
        reason=str(reason) if reason is not None else "",
    )


def _parse_coord(text: object) -> Coordinate:
    if not isinstance(text, str):
        raise ValueError("coordinate must be a string")
    galaxy, system, position = (int(part.strip()) for part in text.split(":"))
    return Coordinate(galaxy, system, position)


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _optional_float_to_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def validate_picks(
    picks: Sequence[AiPick],
    vocabulary: PickVocabulary,
    algorithm_targets: set[Coordinate] | frozenset[Coordinate] = frozenset(),
) -> tuple[list[dict[str, object]], int]:
    """**硬校验**。返回 `(violations, overlap)`；violations 非空 = 整份作废。

    ⚠️ **picks 数必须恰好等于 `total_budget`。** 「留一条线等下一轮」是错的：
    情报每小时贬值（收益曲线里的衰减项），等出来的不可能比现在好
    （需求文档第三节）。不满或超出都作废。

    `algorithm_targets` 是现有算法这一轮选中的坐标，供算 `overlap`。
    `overlap` 按 target 算——**它不是好坏判据**（重合率高不代表好、低不代表坏），
    只是诊断。
    """
    violations: list[dict[str, object]] = []

    def add(code: str, detail: str) -> None:
        violations.append({"code": code, "detail": detail})

    if len(picks) != vocabulary.total_budget:
        add(
            "budget_mismatch",
            f"picks 应恰好等于 {vocabulary.total_budget} 个（两道闸算完的预算），"
            f"实际 {len(picks)} 个",
        )

    assigned: dict[Coordinate, int] = {}
    seen: set[Coordinate] = set()
    for pick in picks:
        if pick.target not in vocabulary.targets:
            add("unknown_target", f"{_coordinate_text(pick.target)} 不在给过的候选集合里")
        if pick.origin not in vocabulary.origins:
            add("unknown_origin", f"出发星球 {_coordinate_text(pick.origin)} 不在给过的集合里")
        if pick.preset not in vocabulary.presets:
            add("unknown_preset", f"预设 {pick.preset!r} 不在给过的集合里")
        if pick.target in seen:
            add("duplicate_target", f"{_coordinate_text(pick.target)} 在同一份 picks 里出现了两次")
        seen.add(pick.target)
        assigned[pick.origin] = assigned.get(pick.origin, 0) + 1

    for origin, used in assigned.items():
        budget = vocabulary.budget_by_origin.get(origin)
        if budget is not None and used > budget:
            add(
                "origin_budget_exceeded",
                f"出发星球 {_coordinate_text(origin)} 分到 {used} 发，超过预算 {budget}",
            )

    overlap = 0
    if not violations:
        overlap = len({pick.target for pick in picks} & set(algorithm_targets))
    return violations, overlap


@dataclass(frozen=True)
class SoftCheckResult:
    """软核对的结果：每一条记录为 `(severity, code, detail)`。"""

    violations: tuple[dict[str, object], ...]


def soft_check_picks(
    picks: Sequence[AiPick], reference: SoftReference
) -> tuple[dict[str, object], ...]:
    """**软核对**：数字自洽 + 规则遵守。只记录，不作废。

    返回的每条 `{"code": ..., "detail": ...}` 原样进 `violations_json`。
    `code` 前缀区分两类：`self_consistency_*`（数字对不上）与 `rule_*`（规则）。
    """
    found: list[dict[str, object]] = []

    def add(code: str, detail: str) -> None:
        found.append({"code": code, "detail": detail})

    for pick in picks:
        expected_military = reference.military.get(pick.target)
        if (
            pick.military is not None
            and expected_military is not None
            and abs(pick.military - expected_military) > MILITARY_EQUALITY_TOLERANCE
        ):
            add(
                "self_consistency_military",
                f"{_coordinate_text(pick.target)}：AI 报军力 {pick.military:,.0f}，"
                f"我方读数 {expected_military:,.0f}（军力必须精确相等）",
            )
        if (
            pick.military is not None
            and expected_military is None
            and pick.target in reference.targets_without_reading
        ):
            add(
                "self_consistency_military",
                f"{_coordinate_text(pick.target)}：AI 报军力 {pick.military:,.0f}，"
                f"而我方对它**没有任何军力读数**（prompt 里这一行写的就是「无读数」）",
            )
        expected_age = reference.reading_age_hours.get(pick.target)
        if (
            pick.reading_age_hours is not None
            and expected_age is not None
            and abs(pick.reading_age_hours - expected_age) > READING_AGE_TOLERANCE_HOURS
        ):
            add(
                "self_consistency_age",
                f"{_coordinate_text(pick.target)}：AI 报读数龄 {pick.reading_age_hours:.2f}h，"
                f"我方 {expected_age:.2f}h（容差 ±{READING_AGE_TOLERANCE_HOURS}h）",
            )
        expected_minutes = reference.round_trip_minutes.get(pick.target, {}).get(pick.origin)
        if (
            pick.round_trip_minutes is not None
            and expected_minutes is not None
            and abs(pick.round_trip_minutes - expected_minutes) > ROUND_TRIP_TOLERANCE_MINUTES
        ):
            add(
                "self_consistency_round_trip",
                f"{_coordinate_text(pick.target)}：AI 报往返 {pick.round_trip_minutes:.0f} 分钟，"
                f"我方 {expected_minutes:.0f} 分钟（容差 ±{ROUND_TRIP_TOLERANCE_MINUTES} 分钟）",
            )
        protected_until = reference.protected_until.get(pick.target)
        if protected_until is not None and protected_until > reference.now:
            add(
                "rule_in_protection",
                f"{_coordinate_text(pick.target)}：保护期到 {protected_until:%Y-%m-%d %H:%M} UTC，"
                f"此刻（{reference.now:%Y-%m-%d %H:%M} UTC）还没过，撞在保护期里",
            )
        last_attack = reference.last_attack_at.get(pick.target)
        if last_attack is not None and reference.now - last_attack < timedelta(
            hours=GAME_PROTECTION_HOURS
        ):
            add(
                "rule_attacked_too_recently",
                f"{_coordinate_text(pick.target)}：我方 {last_attack:%Y-%m-%d %H:%M} UTC "
                f"才打过（不足游戏规则 8 小时）",
            )
    return tuple(found)


__all__ = [
    "AiDecisionStatus",
    "AiPick",
    "AiTargetDecision",
    "InflightLine",
    "MILITARY_EQUALITY_TOLERANCE",
    "READING_AGE_TOLERANCE_HOURS",
    "ROUND_TRIP_TOLERANCE_MINUTES",
    "PickVocabulary",
    "SoftReference",
    "parse_pick",
    "soft_check_picks",
    "validate_picks",
]
