"""军力攻击的分档与多出发星球指派。

## ⚠️ 这里会用到飞行时间，而飞行时间只在**一套编组**上标过

2026-08-18 之前这个模块「刻意只比较距离、不推算飞行秒数」，理由是系数里裹着
舰速。现在第 4 步的判据是 `军力 ÷ 往返小时`，分母绕不开飞行时间，所以那条限制
没有消失，只是换了个说法：

**同一轮派遣用同一个预设，于是舰速对所有目标一样，比值是真的；绝对秒数不是。**
所以这里只拿它排序，一秒都不许写进日志或页面当成「预计到达」。原文写在
`domain.flight_time` 的模块头上。

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
`domain.target_order.has_a_military_reading`（第 2 步），整条四步流水线写在
`domain.target_order` 的模块头上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget, value_key


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
    #: **备胎**：航线预算之外多配的那些，用来顶替撞上保护期的目标。
    #:
    #: 用户口径（2026-08-24）：「如果目标是保护状态无法攻击，也需要继续根据军力
    #: 列表进行攻击。原则上这次的攻击必须发出去，并且需要是新鲜的数据」。
    #:
    #: ⚠️ **它绝不能让这一轮多派几发。** `_military_command` 的 `budget` 只数
    #: 正选（`reserve=False`），备胎只是把坐标一起交给 runner —— runner 撞上
    #: 保护期弹窗时跳过那一个、往下顶（`pirate_loop._handle_dialog`），
    #: 而 `max_dispatches` 仍旧卡住实际派出数。
    #:
    #: ⚠️ 标出来而不是「让 budget 去数 group 有多长」：`_military_command` 里
    #: `max_dispatches` 是有默认值 `None` 的，那条路上 budget 会退回 `len(group)`
    #: ——容量翻倍之后那就等于把派出数也翻倍。标记让这件事在结构上不可能发生。
    reserve: bool = False


def tier_for(score: float | None, tiers: Sequence[MilitaryTier]) -> MilitaryTier | None:
    """按下限找档；未知军力不伪装成 0 分，故不归任何档。"""
    if score is None:
        return None
    return next((tier for tier in tiers if score >= tier.min_score), None)


#: 每条航线多配几个候选。`1` = 不配备胎（就是 2026-08-24 之前的行为）。
#:
#: 用户口径（2026-08-24）：「先按 2 倍来执行」。
#:
#: 为什么需要它：保护期**只能撞上了才知道**（游戏的 8 小时保护期任何人打过都会
#: 触发）。而分配阶段原先只按航线数配同样多的目标，于是两条航线配两个目标、
#: 两个都在保护期时这一轮就空转 —— 实测 2026-08-18 20:29 那一轮当场确认
#: **四个目标全在保护期**、11.5 分钟一发未发。
#:
#: ⚠️ 备胎不是白配的：它们会占掉分配阶段的算力，而且**一旦被派出去就算这一轮
#: 走过**。所以不宜太大。2 倍的意思是「每条航线备一个」。
MILITARY_SPARE_FACTOR = 2


def assign_by_capacity_and_value(
    targets: Sequence[ScoredTarget],
    origins: Sequence[AttackOrigin],
    *,
    fallback_preset: str,
    tiers: Sequence[MilitaryTier] = (),
    spare_factor: int = 1,
) -> tuple[AssignedTarget, ...]:
    """把候选按航线预算分到星球，再在每组内按得分从高到低下发。

    **这是四步流水线的第 4 步**（`domain.target_order` 模块头）。判据只有一条：

        得分 = 军力 ÷ 往返小时          （`domain.target_order.attack_value`）

    ## ⚠️ 贪心的**键**换了，贪心的**形状**没换

    先在整个候选池中挑**得分最高**的 `(目标, origin)` 配对；每配一对就消耗那颗
    星球的一条航线。2026-08-18 之前这里挑的是**距离最近**的配对——换的只是键。

    **形状必须保持全局贪心，不能改成「先定归属再排序」。** 后者（把每个目标先
    分给离它最近的星球，再各自排序）会丢掉一个性质：航线数较少的那颗星球
    **不会反过来抢走另一颗星球的近目标**。得分本身也是按 origin 算的
    （往返时间是 (目标, 出发星球) 的函数），所以「归属」和「排序」在这里根本
    不是两件能拆开的事。

    ## 航线预算仍是硬约束

    一颗星球的航线用尽后不再参与配对；只要池中还有目标，每个可用 origin 都会
    被填满。⚠️ 传进来的 `fleet_lines` 必须是**两道闸算完之后的预算**，不是
    `mission_task_origins` 里那个原样的航线数——理由在
    `application.mission_scheduler._origin_budgets` 上。

    ## 组内也按得分排，不是按距离

    末尾按 `(origin, 得分)` 分组排序：runner 一轮只能站一颗星球，所以同一颗星球
    的目标必须排成连续的一段。**组内的先后是有后果的**——这一轮的航线预算可能
    不够把整组派完（`_military_command` 的 `budget`），排在后面的就留到下一轮。
    所以组内也必须是得分高的在前，否则「按得分出击」会在最后一步被悄悄抹掉，
    正如从前「按军力截断」被第 5 步按距离重排抹掉过一次。
    """
    if spare_factor < 1:
        raise ValueError("spare_factor 至少为 1")
    # 真实航线数（`budget` 的上限）与放行给分配的容量分开：多出来的那些是备胎。
    capacity = {
        origin.coordinate: origin.fleet_lines for origin in origins if origin.fleet_lines > 0
    }
    remaining = {origin: lines * spare_factor for origin, lines in capacity.items()}
    taken: dict[Coordinate, int] = {}
    assigned: list[tuple[Coordinate, tuple[bool, float, int, int, int], AssignedTarget]] = []
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
                value_key(item[1], item[2].coordinate),
                item[0],
                item[2].coordinate.galaxy,
                item[2].coordinate.system,
                item[2].coordinate.position,
            ),
        )
        tier = tier_for(target.military_score, tiers)
        taken[origin.coordinate] = taken.get(origin.coordinate, 0) + 1
        assigned.append(
            (
                origin.coordinate,
                value_key(target, origin.coordinate),
                AssignedTarget(
                    target.coordinate,
                    origin.coordinate,
                    fallback_preset if tier is None else tier.preset,
                    # 这颗星球上超出真实航线数的那些就是备胎。贪心是**每次取最优**，
                    # 所以先被取走的天然是得分高的 —— 备胎正好落在得分低的那一段。
                    reserve=taken[origin.coordinate] > capacity[origin.coordinate],
                ),
            )
        )
        remaining[origin.coordinate] -= 1
        pending = [item for item in pending if item[0] != target_index]
    # runner 每次只能从一颗星球出发；这里排成连续组，不能为每个目标来回切星球。
    # ⚠️ **组内先正选、后备胎**：runner 是按这个次序往下试的，备胎混在前面就等于
    # 用得分低的顶掉了得分高的。贪心本来就让正选的得分更高，显式排一次是为了让
    # 这条契约不依赖贪心的实现细节。
    return tuple(
        item for _, _, item in sorted(assigned, key=lambda row: (row[0], row[2].reserve, row[1]))
    )


__all__ = [
    "MILITARY_SPARE_FACTOR",
    "AssignedTarget",
    "AttackOrigin",
    "MilitaryTier",
    "assign_by_capacity_and_value",
    "tier_for",
]
