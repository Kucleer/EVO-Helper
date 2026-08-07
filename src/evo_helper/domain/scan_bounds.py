"""扫描范围的边界与优先级。

两条来自实测和用户确认的事实：

- **1–4 号位恒为海盗**，扫了不会有 bot。跳过可省掉约 27% 的坐标。
- **优先扫 2 系**：玩家自己在 2 系，那里的 bot 情报最有实用价值。

注意 `coordinates.POSITION_LIMIT` 是 499——那是**每银河系的恒星系数**，不是每恒星系的
行星位数。用它当位数上限会让游标空转 479 个不存在的位。位数上限在这里独立定义。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 恒为海盗的行星位，扫描时跳过。
PIRATE_POSITIONS: tuple[int, ...] = (1, 2, 3, 4)

#: 每恒星系的最大行星位（用户确认）。配合 1–4 为海盗，实际可扫 5–20 共 16 位。
MAX_POSITION = 20

#: 玩家所在银河系，优先扫描。
PREFERRED_GALAXY = 2

#: 每银河系的恒星系数（用户提供）。
SYSTEMS_PER_GALAXY = 499

#: 银河系总数（用户提供）。
TOTAL_GALAXIES = 9


@dataclass(frozen=True)
class ScanBounds:
    """一次扫描要覆盖的行星位区间。"""

    first_position: int = len(PIRATE_POSITIONS) + 1
    position_limit: int = MAX_POSITION

    def __post_init__(self) -> None:
        if self.first_position < 1:
            raise ValueError("first_position must be a positive integer")
        if self.position_limit < self.first_position:
            raise ValueError("position_limit must not be below first_position")

    def skips(self, position: int) -> bool:
        return position < self.first_position

    @property
    def positions_per_system(self) -> int:
        return self.position_limit - self.first_position + 1


def galaxy_scan_order(
    *, total_galaxies: int = TOTAL_GALAXIES, preferred: int = PREFERRED_GALAXY
) -> list[int]:
    """返回银河系扫描顺序：优先的排最前，其余升序。

    只改顺序、不改集合——每个银河系恰好出现一次，所以「优先」不会悄悄变成「只扫」。
    """
    if not 1 <= preferred <= total_galaxies:
        raise ValueError(f"preferred galaxy {preferred} is outside 1..{total_galaxies}")
    return [preferred] + [g for g in range(1, total_galaxies + 1) if g != preferred]
