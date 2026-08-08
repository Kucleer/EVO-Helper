"""舰队数量的读取判据：合计对不上就不算数。

战斗详情页独立给出双方的「单位」总数，回放页给出逐舰种的数量。
**两者必须对上**——这是个单向校验，和坐标那边「请求 vs 面板读回」同构：
读错凑不出正确的合计，而合计一旦对上，逐行的数就没有可疑的余地了。

为什么需要这条判据：这个游戏的数字字体会把相邻笔画粘在一起，
`117` 读成 `17`、`11` 读成 `1`、`39` 读成 `33`。没有校验的话，
这些错误会一路"成功"入库——实测过一次，守方合计 247 存成了 144，
全程零报错，是靠人肉比对才发现的。

怎么读到对为止（用户给的办法，实测有效）：
背景那层半透明水印（`TOTAL CREW`、`personnel`）是主要干扰源，
**轻微拖动页面会改变它与文字的叠合关系**，等于换一个独立样本。
实测同一份战报拖动六次，`39` 有三次读对；但 `11` 六次全错，
所以光靠拖动不收敛——还要同时换 OCR 配方。两个轴一起扫。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

#: 数量列的配方阶梯：放大倍数与重采样。最近邻保住相邻笔画之间的缝，
#: LANCZOS 会把它插值糊掉；两种都留，因为它们读对的样本并不重合。
COUNT_RECIPES: tuple[tuple[int, str], ...] = (
    (4, "lanczos"),
    (3, "lanczos"),
    (5, "lanczos"),
    (4, "nearest"),
    (3, "nearest"),
)

#: 一份战报最多重拍几次。拖动改变的是背景叠合，不是内容，所以多拍无害；
#: 但也不能无限拍——读不出来要让人知道，而不是卡在那里。
MAX_RECAPTURES = 8


@dataclass(frozen=True)
class FleetReading:
    """一次读数，以及它与期望总数是否吻合。"""

    counts: tuple[int, ...]
    expected_total: int
    recipe: tuple[int, str]
    attempt: int

    @property
    def total(self) -> int:
        return sum(self.counts)

    @property
    def confirmed(self) -> bool:
        """合计对上才算数。空读数不算——0 == 0 不是证据。"""
        return bool(self.counts) and self.total == self.expected_total


class FleetCountsUnresolved(RuntimeError):
    """拍了几轮、换了几套配方都没对上合计。

    **不要退而求其次存一个最接近的**：数量是舰队时间线做差异的依据，
    存一个差不多的比不存更坏——它看起来像数据，不会有人再去核。
    """


def read_until_total(
    *,
    sample: Callable[[tuple[int, str]], Sequence[int]],
    expected_total: int,
    nudge: Callable[[int], None],
    recipes: Sequence[tuple[int, str]] = COUNT_RECIPES,
    max_recaptures: int = MAX_RECAPTURES,
) -> FleetReading:
    """反复读，直到某一次的合计等于 `expected_total`。

    每一轮把所有配方都试一遍；都不中就 `nudge` 一下换个背景再来。
    `nudge` 收到的是本轮的位移量——正负交替、幅度不同，
    连续同向拖会把内容推出可视区。
    """
    if expected_total <= 0:
        raise ValueError(f"期望总数必须为正，收到 {expected_total}")
    seen: list[FleetReading] = []
    for attempt in range(max_recaptures):
        if attempt:
            nudge(nudge_offset(attempt))
        for recipe in recipes:
            reading = FleetReading(
                counts=tuple(sample(recipe)),
                expected_total=expected_total,
                recipe=recipe,
                attempt=attempt,
            )
            if reading.confirmed:
                return reading
            seen.append(reading)
    best = max(seen, key=lambda r: -abs(r.total - expected_total), default=None)
    raise FleetCountsUnresolved(
        f"读了 {len(seen)} 次都没对上合计 {expected_total}；"
        f"最接近的一次是 {best.total if best else '（无）'}"
    )


def nudge_offset(attempt: int) -> int:
    """第 `attempt` 轮的拖动位移：正负交替，幅度小步变化。

    一直朝一个方向拖会把内容推出可视区；幅度也要变，
    同样的位移大概率复现同样的叠合、也就复现同样的错误。
    """
    magnitude = 14 + (attempt % 3) * 6
    return magnitude if attempt % 2 else -magnitude
