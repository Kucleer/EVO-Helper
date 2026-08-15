"""军力攻击的分档与多出发星球指派。

这里刻意只比较距离，**不**推算飞行秒数。现有拟合系数只在一套舰队上验证过；
预设会改变舰速，把它拿来比较不同预设只会制造一串看似精确的假数据。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evo_helper.domain.distance import distance_key
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget, strongest_first


@dataclass(frozen=True)
class MilitaryTier:
    """一个军力下限及其游戏内预设标题；标题原文由用户维护。"""

    min_score: float
    preset: str


@dataclass(frozen=True)
class AttackOrigin:
    """一颗可派舰的星球及本次可占用的航线数。"""

    coordinate: Coordinate
    fleet_lines: int


@dataclass(frozen=True)
class AssignedTarget:
    coordinate: Coordinate
    origin: Coordinate
    preset: str


def tier_for(score: float | None, tiers: Sequence[MilitaryTier]) -> MilitaryTier | None:
    """按下限找档；未知军力不伪装成 0 分，故不归任何档。"""
    if score is None:
        return None
    return next((tier for tier in tiers if score >= tier.min_score), None)


def military_pool(
    targets: Iterable[ScoredTarget], *, take: int, maximum_score: float | None
) -> tuple[ScoredTarget, ...]:
    """取尚可攻击的前 N 名；未知值保留，理由同 ``target_order``。"""
    if take < 1:
        return ()
    affordable = (
        item
        for item in targets
        if maximum_score is None
        or item.military_score is None
        or item.military_score <= maximum_score
    )
    return tuple(strongest_first(affordable)[:take])


def assign_by_capacity_and_distance(
    targets: Sequence[ScoredTarget],
    origins: Sequence[AttackOrigin],
    *,
    fallback_preset: str,
    tiers: Sequence[MilitaryTier] = (),
) -> tuple[AssignedTarget, ...]:
    """把候选按航线预算分到星球，再在每组内按距离下发。

    先在整个候选池中挑离任一可用出发星球最近的 ``(目标, origin)`` 配对；每配一
    对就消耗那颗星球的一条航线。这样每颗星球优先拿自己附近的目标，而不会因为
    它的航线数较少，反过来抢走另一颗星球的近目标。

    航线预算仍是硬约束：一颗星球的航线用尽后不再参与配对；只要池中还有目标，
    每个可用 origin 都会被填满。军力排序只负责形成候选池；池内的取舍以距离为先。
    """
    remaining = {
        origin.coordinate: origin.fleet_lines for origin in origins if origin.fleet_lines > 0
    }
    assigned: list[AssignedTarget] = []
    pending = list(enumerate(targets))
    while pending:
        available = [origin for origin in origins if remaining.get(origin.coordinate, 0) > 0]
        if not available:
            break
        target_index, target, origin = min(
            (
                (target_index, target, origin)
                for target_index, target in pending
                for origin in available
            ),
            key=lambda item: (
                distance_key(item[1].coordinate, item[2].coordinate),
                item[0],
                item[2].coordinate.galaxy,
                item[2].coordinate.system,
                item[2].coordinate.position,
            ),
        )
        tier = tier_for(target.military_score, tiers)
        assigned.append(
            AssignedTarget(
                target.coordinate,
                origin.coordinate,
                fallback_preset if tier is None else tier.preset,
            )
        )
        remaining[origin.coordinate] -= 1
        pending = [item for item in pending if item[0] != target_index]
    # runner 每次只能从一颗星球出发；这里排成连续组，不能为每个目标来回切星球。
    return tuple(
        sorted(
            assigned,
            key=lambda item: (item.origin, distance_key(item.coordinate, item.origin)),
        )
    )


__all__ = [
    "AssignedTarget",
    "AttackOrigin",
    "MilitaryTier",
    "assign_by_capacity_and_distance",
    "military_pool",
    "tier_for",
]
