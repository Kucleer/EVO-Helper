"""按 bot 舰队规模分档，决定用哪套攻击组合。

用户的实际需求不是精确数量，是**落在哪一档**：

    2K–5K   → 攻击组合甲
    5K–8K   → 攻击组合乙
    8K+     → 攻击组合丙

所以识别的目标随之改变：`5.36K` 读成 `5.35K` 无所谓，读成 `.36K` 才致命——
差一个数量级就会换错组合。防的是**量级错**，不是末位误差。

游戏里大数显示成 `5.36K` 这样的四舍五入值，所以精确总数本来就取不到；
逐行相加更凑不出精确值。分档口径正好绕开了这件事。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: 数量文本：`517`、`5.36K`、`1.09K`。K 是千。
_COUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([Kk])?$")


class FleetTier(Enum):
    """舰队规模档位。值就是界面上显示的名字。"""

    #: 小到不值得为它挑组合——用户明确说过 2K 以下的误差可以完全忽略。
    NEGLIGIBLE = "2K 以下"
    ALPHA = "2K–5K"
    BETA = "5K–8K"
    GAMMA = "8K+"

    @property
    def preset(self) -> str | None:
        """这一档该用的攻击组合；`NEGLIGIBLE` 档不派。"""
        return _TIER_PRESETS.get(self)


_TIER_PRESETS = {
    FleetTier.ALPHA: "攻击组合甲",
    FleetTier.BETA: "攻击组合乙",
    FleetTier.GAMMA: "攻击组合丙",
}

#: 档位边界（单位：艘）。
TIER_BOUNDARIES: tuple[int, ...] = (2000, 5000, 8000)

#: 离边界这么近就标出来。分档只在边界附近才怕读错——
#: `5.36K` 读成 `5.35K` 不影响任何判断，`4.98K` 读成 `5.02K` 却会换一套组合。
BOUNDARY_MARGIN = 200


def parse_fleet_count(text: str) -> int | None:
    """把 `5.36K` / `517` 解析成艘数；认不出返回 None。

    `K` 是游戏自己的四舍五入显示，`5.36K` 的真实值在 5355–5364 之间。
    这里取 5360——档位判断用不着更准。
    """
    match = _COUNT_RE.match(text.strip())
    if match is None:
        return None
    value = float(match.group(1))
    return round(value * 1000) if match.group(2) else round(value)


def tier_for(total: int) -> FleetTier:
    """总数落在哪一档。边界取左闭右开：5000 属于 5K–8K。"""
    if total < TIER_BOUNDARIES[0]:
        return FleetTier.NEGLIGIBLE
    if total < TIER_BOUNDARIES[1]:
        return FleetTier.ALPHA
    if total < TIER_BOUNDARIES[2]:
        return FleetTier.BETA
    return FleetTier.GAMMA


@dataclass(frozen=True)
class TierVerdict:
    total: int
    tier: FleetTier
    near_boundary: bool

    @property
    def preset(self) -> str | None:
        return self.tier.preset


def classify(total: int, *, margin: int = BOUNDARY_MARGIN) -> TierVerdict:
    """定档，并标出「离边界太近、读数误差可能改变结论」的情形。

    这是识别误差唯一真正要紧的地方。档位中间的读数错几十艘没有后果；
    边界附近错几十艘就会换一套攻击组合。
    """
    return TierVerdict(
        total=total,
        tier=tier_for(total),
        near_boundary=any(abs(total - edge) <= margin for edge in TIER_BOUNDARIES),
    )
