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


#: 本账号里那套一次性的探路组合：探路 = 小型运输船 × 1（实机核对，2026-08-08）。
DEFAULT_PRESET = FleetPreset(name="探路", signature=composition_signature({"小型运输船": 1}))

#: 探路预设的标题。
PROBE_PRESET_NAME = DEFAULT_PRESET.name


def is_probe_preset(preset_name: str) -> bool:
    """这一发用的是不是探路组合。

    ⚠️ **这条判据与 bot 那条链路无关了。** bot 从 2026-08-13 起不再派探路发
    （直接用 BBB 打，见 `domain.bot_round`），所以它原先住在 `domain.bot_round`
    里已经名不副实——留在那里会让人以为「没有 bot 探路发 = 这个函数没用了」。

    它现在只剩一个消费者，而那个用途和轮次判态毫无关系：
    `report_wait.line_free_at` 用它决定这条航线按 1× 还是 2× 飞行时长释放。
    **探路舰队会在攻击中损失，没有返程**，按 2× 记就会凭空多占一条航线到天亮。
    只要 `config.default_fleet_preset` 还是探路（`application.workflow` 那条老
    路径照用），就仍然会有用这个预设派出去的舰队，这条分岔就不是死枝。
    """
    return preset_name == PROBE_PRESET_NAME
