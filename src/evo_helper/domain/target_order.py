"""挑今晚打谁：**先按军力取前 N 名，再把这 N 个按距离排。**

用户口径（2026-08-15）：「先取前 50 名，然后按距离排序，开始攻击，航路按配置进行攻击」。
更早一句是理由（2026-08-14）：「强的 bot = 资源多」。

## 为什么是「先截断、再排距离」，而不是按军力排整张表

按军力从头排到尾的话，相邻两个目标的军力差可能只有几十点，而距离差是
**同银河 30 分钟 vs 跨银河 2.6 小时**（实测，见 `domain.distance`）。那样排出来的
一夜航线会在银河之间来回横跳，派不了几发。

截断解决的正是这件事：前 50 名之内军力都属于「值得打」那一档，那点差别不值得
为它多飞两小时；而第 50 名与第 500 名之间才是真的量级差。所以**军力只用来决定
「谁进这 50 个」，进来之后一律按距离**。

## ⚠️ 这里刻意**没有**档位阈值

先前写过一版按军力分档（>100K / 20K–100K / 5K–20K / ≤5K），边界是从
2026-08-15 那批数据的分位数上取的。**那批数据是脏的**：30 个军力值因为
丢小数点飞到 10 万以上（`17.73K` 读成 `1773K`），又通过插值传染了 12 个。
拿它算出来的阈值不可信。

取前 N 名不需要任何阈值——**这正是它比分档结实的地方**。而且军力值每周一
UTC+0 随机刷新（用户口径 2026-08-14），任何写死的阈值下一周都要重标，
而「前 50 名」永远成立。

## 读不出军力值的排最后

榜单上没见过的 bot（`military_score is None`）排在所有已知的后面。

**不是把它们当成 0 分**：0 分是一个**读到的事实**（榜单上真的有 0 分的行），
而 None 是「不知道」。混在一起就等于把「没数据」伪装成「数据是 0」——
这个仓有一条硬规矩：猜出来的数不许长得像量出来的。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from evo_helper.domain.distance import distance_key
from evo_helper.domain.models import Coordinate

#: 一轮取多少个最强的。用户口径（2026-08-15）：前 50 名。
#:
#: 这个数与航路数是两件事：航路数（`scheduler_config.fleet_line_limit`）决定
#: **同时**在飞几发，而这个数决定**这一轮的候选池有多大**。池子要比航路数大得多，
#: 因为一发打完还要等战报回来才轮到下一个。
TOP_BY_MILITARY = 50


@dataclass(frozen=True)
class ScoredTarget:
    """一个候选目标：坐标 + 军力值（可能没有）。"""

    coordinate: Coordinate
    military_score: float | None = None
    #: 军力值读到的时刻；None 表示榜单从未见过，不伪造「新鲜」。
    military_score_at_utc: datetime | None = None


def strongest_first(targets: Iterable[ScoredTarget]) -> list[ScoredTarget]:
    """按军力从强到弱排。读不出来的排最后（理由见模块头）。

    次序是**确定**的：军力相同时按坐标定序，否则同样一批目标每次挑出来的
    前 50 个可能不一样，而那会让「上一轮打到哪了」无从谈起。
    """
    return sorted(
        targets,
        key=lambda target: (
            target.military_score is None,
            -(target.military_score or 0.0),
            target.coordinate.galaxy,
            target.coordinate.system,
            target.coordinate.position,
        ),
    )


def strongest_then_nearest(
    targets: Iterable[ScoredTarget],
    origin: Coordinate,
    *,
    take: int = TOP_BY_MILITARY,
    max_score: float | None = None,
) -> tuple[Coordinate, ...]:
    """挑出军力最高的 `take` 个，再把这几个按离 `origin` 由近到远排。

    两步的地位完全不同，别把它们合成一个排序键：

    1. **军力只用来截断**——决定谁进候选池。
    2. **池子里一律按距离**——池内那点军力差不值得为它多飞两小时。

    `max_score` 是**上限**：军力高于它的一律不进池。用户 2026-08-14 要求
    「军力确实要设置上限」——太强的目标不是当前预设打得动的，派过去只是白烧
    一次配额和一趟往返。默认 `None` = 不设上限。

    ⚠️ **上限只挡「太强」，不挡「读不出来」**：`military_score is None` 的目标
    照样留下，因为「不知道多强」不构成「一定太强」。按上限把它们一起扔掉的话，
    凡是没被榜单扫到过的 bot 就永远不会被攻击——而那正是库里最多的一批。
    """
    if take < 1:
        return ()
    affordable = [
        target
        for target in targets
        if max_score is None or target.military_score is None or target.military_score <= max_score
    ]
    pool = strongest_first(affordable)[:take]
    return tuple(
        target.coordinate
        for target in sorted(pool, key=lambda item: distance_key(item.coordinate, origin))
    )


__all__ = [
    "TOP_BY_MILITARY",
    "ScoredTarget",
    "strongest_first",
    "strongest_then_nearest",
]
