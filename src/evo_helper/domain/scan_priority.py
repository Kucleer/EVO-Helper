"""扫描顺序：先局部、再本银河系、再其余。

用户指定的顺序是：

1. `2:001`–`2:200`（离玩家最近的一段，情报最快见效）
2. 2 系其余（`2:201`–`2:499`）
3. 银河系 1、3、4、5、6、7、8

用户的清单里没有 9 系。这里**不丢**它——未列出的银河系一律排在末尾。
「优先」只能改变顺序，不能悄悄变成「只扫」；否则某个银河系会永远不被扫到，
而界面上看不出任何异常。9 系是漏写还是有意排除，需要用户确认；
在确认之前，排在最后是唯一不会造成静默数据缺口的选择。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY, TOTAL_GALAXIES

#: 优先扫描的恒星系分段，形如 ``(银河系, 起始恒星系, 结束恒星系)``。
DEFAULT_SEGMENTS: tuple[tuple[int, int, int], ...] = ((2, 1, 200), (2, 201, 499))

#: 分段之后的银河系顺序（用户指定）。未列出的排在其后。
DEFAULT_GALAXY_ORDER: tuple[int, ...] = (1, 3, 4, 5, 6, 7, 8)


@dataclass(frozen=True)
class ScanSegment:
    """一个银河系内的连续恒星系区间。"""

    galaxy: int
    first_system: int
    last_system: int

    def __post_init__(self) -> None:
        if self.last_system < self.first_system:
            raise ValueError(
                f"last_system {self.last_system} must not precede first_system {self.first_system}"
            )

    @property
    def system_count(self) -> int:
        return self.last_system - self.first_system + 1


def scan_segments(
    *,
    segments: Sequence[tuple[int, int, int]] = DEFAULT_SEGMENTS,
    galaxy_order: Sequence[int] = DEFAULT_GALAXY_ORDER,
    total_galaxies: int = TOTAL_GALAXIES,
    systems_per_galaxy: int = SYSTEMS_PER_GALAXY,
) -> tuple[ScanSegment, ...]:
    """返回按优先级排好的扫描分段，覆盖整个宇宙。

    显式分段排在最前，然后是 ``galaxy_order`` 里的银河系，最后是任何两者都
    没提到的银河系——这样任何银河系都不会被静默漏掉。
    """
    ordered: list[ScanSegment] = []
    covered: set[int] = set()

    for galaxy, first, last in segments:
        if last > systems_per_galaxy:
            raise ValueError(
                f"segment {galaxy}:{first}-{last} exceeds systems_per_galaxy {systems_per_galaxy}"
            )
        ordered.append(ScanSegment(galaxy=galaxy, first_system=first, last_system=last))
        covered.add(galaxy)

    listed = [g for g in galaxy_order if g not in covered]
    # 两处都没提到的银河系补在最后，而不是丢掉。
    remaining = [g for g in range(1, total_galaxies + 1) if g not in covered and g not in listed]
    for galaxy in [*listed, *remaining]:
        ordered.append(ScanSegment(galaxy=galaxy, first_system=1, last_system=systems_per_galaxy))
    return tuple(ordered)
