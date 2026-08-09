"""bot 目标在一轮里的推进状态。

纯函数：只看「这个目标本轮派过哪些发、各自的战报回来了没有」，
不碰数据库也不碰屏幕。调度器和 runner 用的是同一份判据，
两边对同一个目标的看法不会分叉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from evo_helper.domain.fleet_preset import DEFAULT_PRESET

#: 攻击侦查用的预设标题。取自 `domain.fleet_preset` 这个**同层**的权威来源，
#: 而不是抄一份字面量：`tools.bot_loop.PROBE_PRESET` 也是从这里取的，
#: 两边同源才不会有一天各自改成不同的字。
PROBE_PRESET_NAME = DEFAULT_PRESET.name


class BotPhase(Enum):
    """一个目标本轮走到哪一步了。"""

    #: 本轮还没碰过它。
    NEEDS_PROBE = "NEEDS_PROBE"
    #: 探路已派出，等它的战报。
    AWAITING_PROBE_REPORT = "AWAITING_PROBE_REPORT"
    #: 探路战报回来了，该分档并真打。
    NEEDS_ATTACK = "NEEDS_ATTACK"
    #: 攻击已派出，等它的战报。
    AWAITING_ATTACK_REPORT = "AWAITING_ATTACK_REPORT"
    #: 走完了。含「分档判定不值得打」而没派攻击的目标。
    DONE = "DONE"


@dataclass(frozen=True)
class DispatchFact:
    """本轮针对某个目标的一次派遣。"""

    preset_name: str
    has_report: bool
    #: **只对探路发有意义**：分档判定为「不值得打」，本轮不会再有攻击发。
    #: 攻击发的战报收不到该怎么办，见 `phase_of` 的前置条件。
    skipped: bool = False


def phase_of(dispatches: Sequence[DispatchFact]) -> BotPhase:
    """这个目标本轮该干什么。

    判据只看预设标题：探路发用「探路」，攻击发用分档预设。

    ⚠️ **前置条件：调用方必须先把「已判定战报永远不会来」的派遣剔除掉。**
    这个函数只认「战报回来了没有」，不判定超时——判超时要知道派出时刻、
    飞行时间、战报有效期，那些事实在仓储那一侧，不在这里。

    没剔干净的后果不是报错，是**静默卡死**：一发攻击的战报永远不到，
    这个目标就永远停在 `AWAITING_ATTACK_REPORT`，于是整个 bot 任务永远不退出，
    而画面上看起来只是「在等」。`DispatchFact.skipped` 挡不住这一条——
    它只表达探路之后「分档说不值得打」，不表达「这一发的战报丢了」。
    """
    if not dispatches:
        return BotPhase.NEEDS_PROBE

    probes = [item for item in dispatches if item.preset_name == PROBE_PRESET_NAME]
    attacks = [item for item in dispatches if item.preset_name != PROBE_PRESET_NAME]

    if attacks:
        return (
            BotPhase.DONE
            if all(item.has_report for item in attacks)
            else BotPhase.AWAITING_ATTACK_REPORT
        )

    # 走到这里 `attacks` 必空。它是 `probes` 的补集而 `dispatches` 非空，
    # 所以 `probes` 必非空——不必再判一次「没有探路发」。
    if any(item.skipped for item in probes):
        # 分档说不值得打。它不会再产生攻击发，算走完。
        return BotPhase.DONE

    return (
        BotPhase.NEEDS_ATTACK
        if all(item.has_report for item in probes)
        else BotPhase.AWAITING_PROBE_REPORT
    )
