"""军力攻击的分档与多出发星球指派。

这里刻意只比较距离，**不**推算飞行秒数。现有拟合系数只在一套舰队上验证过；
预设会改变舰速，把它拿来比较不同预设只会制造一串看似精确的假数据。

## ⚠️ 为什么这里不再有「补位」（2026-08-18）

这个模块从前有一个 `top_up_with_unrated`：主力（有分数的）不满前 N 个时，
用**没有军力分数**的目标按距离补齐。它的依据是一句话——

    「凡是没被榜单扫到过的 bot 就永远不会被攻击——而那正是库里最多的一批。」

**那句话依据的是一个错数**：它把非 bot 的行也算进了分母。实测生产库，
从未上过军力榜的 **bot** 有 628 个，占 bot 总数（3604）的 **17.4%**——
不是「最多的一批」。

用户 2026-08-18 据此决定：**从未上过军力榜的目标不再攻击。** 放弃这 17.4%
换来的是「军力优先」这个模式真的成立。补位那条路的问题一直都在，只是被那个
错数压住了：补位不参与按军力排序，一旦补位占了池子里相当一部分名额，这条链路
就退化成「按距离随便打」，而页面上看不出任何差别。

所以这里既不留补位函数、也不留一个永远不会被调用的空壳；判据搬到了
`domain.target_order.has_a_military_reading`（第 2 步），整条五步流水线写在
`domain.target_order` 的模块头上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evo_helper.domain.distance import distance_key
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget


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

    ⚠️ **这是五步流水线的第 5 步，用户 2026-08-18 明确要求保持现状：先打近的。**
    近目标往返 20--30 分钟、跨银河 2.6 小时（实测，见 `domain.distance`），
    同样的航线数先打近的能派十几发。把这里换成按军力排，第 4 步那道截断就白做了
    ——**两处一起按军力，等于让距离完全不参与，一夜的航线会在银河之间来回横跳**。
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
    "tier_for",
]
