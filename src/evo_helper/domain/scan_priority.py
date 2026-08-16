"""扫描顺序：先已配置星球周边、再扫完整个宇宙。

配置了攻击星球时，按星球列表的顺序逐颗处理其所在恒星系，再向两边展开
100 个恒星系。之后才接上全宇宙的剩余部分。这样扫描到的 bot 最快就能给
对应出发点的攻击任务使用；优先不意味着漏扫，所有未覆盖的恒星系仍会补上。

没有任何攻击星球时保留历史默认顺序，兼容首次部署和旧库。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evo_helper.domain.models import Coordinate
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
    priority_planets: Sequence[Coordinate] = (),
    priority_radius: int = 100,
    total_galaxies: int = TOTAL_GALAXIES,
    systems_per_galaxy: int = SYSTEMS_PER_GALAXY,
) -> tuple[ScanSegment, ...]:
    """返回按优先级排好的扫描分段，覆盖整个宇宙。

    显式分段排在最前，然后是 ``galaxy_order`` 里的银河系，最后是任何两者都
    没提到的银河系——这样任何银河系都不会被静默漏掉。
    """
    if priority_radius < 0:
        raise ValueError("priority_radius must not be negative")

    # 没有星球配置时，沿用老计划，避免首次运行时的行为无故改变。
    if not priority_planets:
        return _legacy_scan_segments(
            segments=segments,
            galaxy_order=galaxy_order,
            total_galaxies=total_galaxies,
            systems_per_galaxy=systems_per_galaxy,
        )

    ordered: list[ScanSegment] = []
    covered_systems: set[tuple[int, int]] = set()
    for planet in priority_planets:
        if not 1 <= planet.galaxy <= total_galaxies:
            raise ValueError(f"planet galaxy {planet.galaxy} is outside the scan universe")
        if not 1 <= planet.system <= systems_per_galaxy:
            raise ValueError(f"planet system {planet.system} is outside the scan universe")
        for system in _systems_around(planet.system, priority_radius, systems_per_galaxy):
            key = (planet.galaxy, system)
            if key in covered_systems:
                continue
            covered_systems.add(key)
            # 单系统分段是有意的：它保住“中心、左一、右一……”的实际扫描顺序。
            ordered.append(
                ScanSegment(galaxy=planet.galaxy, first_system=system, last_system=system)
            )

    # 局部优先区之后按历史银河系顺序补齐所有未扫系统；同一银河系连续部分合并，
    # 不制造上千条无意义范围记录。
    for galaxy in _global_galaxy_order(
        segments=segments, galaxy_order=galaxy_order, total_galaxies=total_galaxies
    ):
        first_uncovered: int | None = None
        for system in range(1, systems_per_galaxy + 1):
            if (galaxy, system) not in covered_systems:
                if first_uncovered is None:
                    first_uncovered = system
            elif first_uncovered is not None:
                ordered.append(
                    ScanSegment(galaxy=galaxy, first_system=first_uncovered, last_system=system - 1)
                )
                first_uncovered = None
        if first_uncovered is not None:
            ordered.append(
                ScanSegment(
                    galaxy=galaxy,
                    first_system=first_uncovered,
                    last_system=systems_per_galaxy,
                )
            )
    return tuple(ordered)


def _legacy_scan_segments(
    *,
    segments: Sequence[tuple[int, int, int]],
    galaxy_order: Sequence[int],
    total_galaxies: int,
    systems_per_galaxy: int,
) -> tuple[ScanSegment, ...]:
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


def _systems_around(center: int, radius: int, systems_per_galaxy: int) -> list[int]:
    """中心开始向两边交替展开，靠近星球的系统永远先被扫描。"""
    systems = [center]
    for gap in range(1, radius + 1):
        lower = center - gap
        upper = center + gap
        if lower >= 1:
            systems.append(lower)
        if upper <= systems_per_galaxy:
            systems.append(upper)
    return systems


def _global_galaxy_order(
    *,
    segments: Sequence[tuple[int, int, int]],
    galaxy_order: Sequence[int],
    total_galaxies: int,
) -> tuple[int, ...]:
    """把旧分段/银河系配置折成不重复的全局银河系顺序。"""
    ordered = [galaxy for galaxy, _first, _last in segments]
    ordered.extend(galaxy_order)
    ordered.extend(range(1, total_galaxies + 1))
    return tuple(galaxy for galaxy in dict.fromkeys(ordered) if 1 <= galaxy <= total_galaxies)
