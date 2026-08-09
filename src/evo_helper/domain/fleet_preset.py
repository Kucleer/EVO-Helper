"""舰队预设的身份与校验。

**身份是预设名**——游戏里就是按名字从预设列表里挑的（`预设 2/10`：海盗、探路），
所以派遣链路必须按名字选，用户已确认这一点。

组成签名不再充当身份，但**保留为选中之后的复核**：挑完预设，面板上列出的舰种与数量
必须与预期一致才允许派出。名字选错和名字读错是两类事故，组成复核能挡住前者；
安全不变量 9 要求名称、舰种、数量三者都对得上，这两半合起来才够。

⚠️ 这里曾经存的是 `轻型战斗机:1`，与实机不符——游戏里 `探路` 是 **小型运输船 × 1**。
签名一旦对不上，`workflow` 会判定 preset 不匹配并安全暂停，也就是永远派不出去。
改这个常量时**必须**对着游戏里的预设面板核一遍，不能照抄文档。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class FleetPreset:
    """``name`` 是身份，``signature`` 是选中后的组成复核。"""

    name: str
    signature: str


def composition_signature(counts: Mapping[str, int]) -> str:
    """规范化的 ``舰种:数量`` 签名，排序固定，顺序变化不影响结果。"""
    return ",".join(f"{ship}:{count}" for ship, count in sorted(counts.items()))


#: 本账号用于攻击侦查的游戏内预设：探路 = 小型运输船 × 1（实机核对，2026-08-08）。
DEFAULT_PRESET = FleetPreset(name="探路", signature=composition_signature({"小型运输船": 1}))
