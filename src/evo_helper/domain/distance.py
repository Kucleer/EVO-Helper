"""哪个目标离出发星球更近。**只排序，不估时间。**

## 判据：先比银河，再比恒星系

用户口径（2026-08-14）：「银河系（第一个坐标）数值越接近出发星球的，距离越近。
同一个银河系，恒星系（第二个坐标）更接近的距离更近。」

也就是字典序 `(|Δ银河|, |Δ恒星系|)`。而**实机数据支持它，余量很大**——同一套舰队
编组（速度 14.520）在简报页上读到的：

    2:499:18   同银河、系差 362    飞行 32分49秒 = 1969 秒
    3:303:18   跨一个银河、系差 166  飞行 1时2分32秒 = 3752 秒

跨一个银河比**同银河内最远的那一头**还贵近一倍。所以银河这一位永远压过恒星系，
不存在「同银河的远目标其实比隔壁银河更远」那种翻转。

## 为什么绝不把距离存成列

距离是 **(目标, 出发星球)** 的函数，不是目标自己的属性。

用户 2026-08-14 明确要求兼容「以后可能会多星球发出攻击，并且会配置航线」。存成列的
那一天，第二颗出发星球上的任务会拿着按主星算的距离排序——**而且完全不报错**，
只是打的顺序莫名其妙。仓里已有的 `count_inflight(origin=...)`、`fleet_lines`
都是按 origin 分账的，这条跟它们同一个形状。

## 位次只用来定序，不代表距离

同一个恒星系里的两颗星球，飞行时间差别可以忽略。把 `position` 放进排序键**只是
为了让结果确定**：不放的话，同距离目标的先后取决于库里的返回顺序，而那个顺序
在换一次查询、换一次索引之后就会变，事后对不上账。

## 这里**不**算飞行时间

拟合飞行时间曲线是另一件事（用户 2026-08-14 定为长期低优先级）。那需要按编组
速度分组、需要跨银河样本，而且形状八成是 `a + b·√距离`（三个同银河点拟合下来
误差 <1%）。在那之前，**排序不需要知道要飞多久，只需要知道谁更近**——
这两件事分开，排序就不会被一条还没验过的曲线拖住。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from evo_helper.domain.models import Coordinate


def distance_key(target: Coordinate, origin: Coordinate) -> tuple[int, int, int]:
    """按「离 `origin` 由近到远」排序时用的键。小的在前。

    三段的地位完全不同，别把它们看成同一个量：

    1. `|Δ银河|` —— **压倒性的**，实机上跨一个银河比同银河内最远端还贵近一倍。
    2. `|Δ恒星系|` —— 同银河内的真实远近。
    3. `position` —— **只为定序**，不代表距离（同系内的差别可忽略）。
    """
    return (
        abs(target.galaxy - origin.galaxy),
        abs(target.system - origin.system),
        target.position,
    )


def nearest_first(targets: Iterable[Coordinate], origin: Coordinate) -> tuple[Coordinate, ...]:
    """把目标按「离 `origin` 近的排前面」重排。

    ⚠️ **每个任务用它自己的 `origin` 现算**，不许缓存、不许存列——理由见模块头。

    用途（用户 2026-08-14）：一夜的航线是有限的，而近目标的往返时间比远目标短
    一个量级（同银河近距离约 20–30 分钟，跨银河约 2.6 小时）。同样 6 条航线，
    先打近的能派十几发，先打远的只能派两三发。

    排序是**稳定**的：`sorted` 保证同键的原有先后不变，而第三段 `position` 又
    把同一恒星系内的目标定死了顺序。两条加起来，同一批目标每次都排成同一个样子
    ——事后拿日志对账时这一点很要紧。
    """
    return tuple(sorted(targets, key=lambda target: distance_key(target, origin)))


def within(
    targets: Sequence[Coordinate], origin: Coordinate, *, systems: int
) -> tuple[Coordinate, ...]:
    """只留下**同银河**且恒星系差不超过 `systems` 的那些，仍按由近到远排。

    跨银河的一律不要：不是「远一点」，是贵一个量级（见模块头那两个实测数）。
    要打跨银河的目标时，把它做成另一个任务、配它自己的出发星球，而不是把半径调大。
    """
    near = [
        target
        for target in targets
        if target.galaxy == origin.galaxy and abs(target.system - origin.system) <= systems
    ]
    return nearest_first(near, origin)


__all__ = ["distance_key", "nearest_first", "within"]
