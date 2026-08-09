"""bot 目标在一轮里的推进状态。

纯函数：只看「这个目标本轮派过哪些发、各自的战报回来了没有」，
不碰数据库也不碰屏幕。调度器和 runner 用的是同一份判据，
两边对同一个目标的看法不会分叉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

#: 攻击侦查用的预设标题。与 `tools.bot_loop.PROBE_PRESET` 同源，
#: 但这里不 import 那个模块——domain 层不依赖 tools 层。
PROBE_PRESET_NAME = "探路"


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
    #: 分档判定为「不值得打」，本轮不会再有攻击发。
    skipped: bool = False


def phase_of(dispatches: Sequence[DispatchFact]) -> BotPhase:
    """这个目标本轮该干什么。

    判据只看预设标题：探路发用「探路」，攻击发用分档预设。
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

    if any(item.skipped for item in probes):
        # 分档说不值得打。它不会再产生攻击发，算走完。
        return BotPhase.DONE

    if not probes:
        return BotPhase.NEEDS_PROBE

    return (
        BotPhase.NEEDS_ATTACK
        if all(item.has_report for item in probes)
        else BotPhase.AWAITING_PROBE_REPORT
    )
