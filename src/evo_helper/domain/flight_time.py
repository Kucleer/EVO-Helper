"""一趟往返要多久。**这正是 `domain.distance` 刻意不提供的那个函数。**

`domain.distance` 只排序、不估时间，理由写在它的模块末尾：`26.5165` 那个系数里
**裹着舰速**，只在一套编组（速度 14.520 / 100%）上验过；把一个只在一处成立的
公式摆进通用模块，调用方不会知道这件事。

这个模块把那个函数拿出来了，所以**那条限制必须跟着搬过来，写在最显眼的地方**。

## ⚠️ 限制：这里的秒数只在**一套编组**上是真的

    实测那套编组：速度 14.520、100% 航速

换一套编组，`SECONDS_PER_ROOT_UNIT` 就是另一个数。所以：

- **不许拿它去比较不同预设**（「BBB 打这个要 40 分钟、AAA 打那个要 30 分钟」
  这种句子在本仓是编的）；
- **不许把它写进日志或页面当成「预计到达时间」**——那会造出一串看着精确的假数。

它在本仓**只有一个用途**：给同一轮里的目标排个先后。同一轮用同一个预设，
系数于是对所有目标一样，**比值是真的**，绝对值是不是真的无关紧要。

（严格说 `LAUNCH_OVERHEAD_SECONDS` 那 2 秒不随舰速缩放，所以换编组后连比值都会
差一点点；但那 2 秒对着几千秒的航程，改不动任何一对目标的先后。）

## 模型（八个实测点，全部命中）

    环形距离    galaxy_gap / system_gap        （绕哪边近算哪边）
    距离单位 D  同银河：1162 + 31.71 × 恒星系环形距离
               跨银河：20000 × 银河环形距离
    单程秒      2 + 26.5165 × √D
    往返小时    2 × 单程秒 ÷ 3600

推导与互相印证（气体消耗那把独立的尺子）整段在 `domain.distance` 的模块头上。

## 各档的实测误差（59 发攻击回测，2026-08-18）

| 银河环距 | 样本 | 误差 |
|---|---|---|
| 1 | 12 发 | **0.1%** |
| 0（同银河） | 46 发 | 5.7% |
| 2 | **1 发** | 20.9% |

⚠️ **银河环距 ≥2 只有一发样本，模型在那一档低估约 21%。** 也就是说跨两个银河的
目标**实际比这里算的更贵**，用它排序会略微高估那种目标的性价比。样本攒够之前
别去「修正」这个系数——一发样本改不动一个在 58 发上成立的模型，只会把它拧坏。
记在 `docs/选靶数据跟踪-待办.md` 第一节第 3 条。

## 往返 = 单程 × 2.00 是量出来的，不是假设的

59 发实测，三个银河距档完全一致：一次派遣占住一条航线的时长，就是单程飞行的
两倍（打完立刻返航，没有额外停留）。所以「航线小时」这个成本可以直接从飞行
时间算出来，不必再去测第二遍。
"""

from __future__ import annotations

from math import sqrt

from evo_helper.domain.distance import galaxy_gap, system_gap
from evo_helper.domain.models import Coordinate

#: 跨银河时，一格**银河环距**折算成多少「距离单位」。
#:
#: 分类：**标定常量**，不是偏好项。它由游戏的星图几何定死，改了就是错——
#: 改小会让跨银河目标看起来更便宜，于是一夜的航线全被拉去跨银河。
CROSS_GALAXY_UNITS_PER_STEP = 20_000.0

#: 同银河时的固定起步距离（即便目标就在隔壁恒星系也要飞这么远）。
#:
#: 分类：**标定常量**。
SAME_GALAXY_BASE_UNITS = 1_162.0

#: 同银河时，一格**恒星系环距**折算成多少「距离单位」。
#:
#: 分类：**标定常量**。
SAME_GALAXY_UNITS_PER_STEP = 31.71

#: 起飞降落的固定开销（秒）。四个跨银河实测点上它稳定在 2 秒。
#:
#: 分类：**标定常量**。
LAUNCH_OVERHEAD_SECONDS = 2.0

#: `√距离单位` → 秒 的系数。⚠️ **里面裹着舰速**（速度 14.520 / 100%）。
#:
#: 分类：**标定常量**，而且是**只在一套编组上标过**的那种。要支持别的编组，
#: 该做的是收一个 `speed` 参数、用多套编组重新标定，而不是把这个数调成
#: 「感觉差不多」——见模块头的限制。
SECONDS_PER_ROOT_UNIT = 26.5165

#: 一次派遣占住航线的时长 ÷ 单程飞行时长。59 发实测，三个银河距档完全一致。
ROUND_TRIP_MULTIPLIER = 2.0


def distance_units(target: Coordinate, origin: Coordinate) -> float:
    """两点之间的「距离单位」D。飞行时间与气体消耗都是 D 的函数。

    ⚠️ **两段都走环形距离**（`galaxy_gap` / `system_gap`），**不许写 `abs(a - b)`**。
    从 2 系去 9 系是两步不是七步；从 2:137 去 2:499 是 137 步不是 362 步。
    写成减法不会报错，只会把真正近的目标算成天涯海角——实测 `2:499` 比线性差
    只有 150 的 `2:287` 还快 73 秒（证据在 `domain.distance` 模块头）。

    跨银河时恒星系号**完全不进算式**：实测三个恒星系号各不相同的跨银河目标，
    一个只含银河环距的函数把它们全部命中在 2 秒内。
    """
    galaxy_steps = galaxy_gap(target.galaxy, origin.galaxy)
    if galaxy_steps:
        return CROSS_GALAXY_UNITS_PER_STEP * galaxy_steps
    return SAME_GALAXY_BASE_UNITS + SAME_GALAXY_UNITS_PER_STEP * system_gap(
        target.system, origin.system
    )


def one_way_seconds(
    target: Coordinate,
    origin: Coordinate,
    *,
    seconds_per_root_unit: float = SECONDS_PER_ROOT_UNIT,
) -> float:
    """单程飞行秒数。默认系数**只在标定那套编组上是真的**，见模块头。

    ⚠️ `seconds_per_root_unit` 可以传，是因为**每颗出发星球的舰速都不一样**
    （用户口径 2026-08-19：「每个球的速度都会有点不一样的」）。生产库回测
    （2026-08-19，跨银河那一档）：

        4:277:15  n=56  k = 26.5165
        9:250:8   n=19  k = 26.3327
        2:137:18  n=5   k = 26.5165

    谁来给这个 k，以及它凭什么可信，全写在
    `domain.flight_estimate.fit_seconds_per_root_unit`。**这里只负责算术**：
    这个模块不知道也不该知道那个数是从哪张表里学出来的。

    不传就是模块头上那个标定值——`domain.target_order` 用它给同一轮里的目标
    排先后，那个用途只要**同一轮用同一个系数**就够了（比值是真的），
    绝对值准不准无关紧要。
    """
    return LAUNCH_OVERHEAD_SECONDS + seconds_per_root_unit * sqrt(distance_units(target, origin))


def round_trip_hours(target: Coordinate, origin: Coordinate) -> float:
    """打这一发要占住一条航线**多少小时**。**只在标定那套编组上是真的**，见模块头。

    这是选靶那个得分的分母（`domain.target_order.attack_value`）。用「小时」而不是
    「秒」纯粹是为了让得分读起来像人话：一夜的航线预算本来就是按小时数的。

    恒大于 0（`LAUNCH_OVERHEAD_SECONDS` 保底），所以拿它当分母不必再防除零——
    最近的目标也要飞一千多个距离单位。
    """
    return ROUND_TRIP_MULTIPLIER * one_way_seconds(target, origin) / 3600.0


__all__ = [
    "CROSS_GALAXY_UNITS_PER_STEP",
    "LAUNCH_OVERHEAD_SECONDS",
    "ROUND_TRIP_MULTIPLIER",
    "SAME_GALAXY_BASE_UNITS",
    "SAME_GALAXY_UNITS_PER_STEP",
    "SECONDS_PER_ROOT_UNIT",
    "distance_units",
    "one_way_seconds",
    "round_trip_hours",
]
