"""bot 目标在一轮里的推进状态。

纯函数：只看「这个目标本轮打过哪几发、各自的战报回来了没有、打成了什么」，
不碰数据库也不碰屏幕。调度器和 runner 用的是同一份判据，
两边对同一个目标的看法不会分叉。

## 三态，不再有探路

用户口径（2026-08-13）：

> 基于第一条，bot攻击模式变更，不再进行攻击侦查，直接用预设BBB进行攻击，
> 如果同一坐标攻击结果为平局，则继续进行攻击

原先是五态：探路 → 等探路战报 → 分档 → 攻击 → 等攻击战报。`NEEDS_PROBE` 与
`AWAITING_PROBE_REPORT` 随探路发一起删掉了，**不是留成死态**：留着的话
`phase_of` 就有一条永远走不到的分支，而下一个读它的人会以为那条路还在跑，
照着它去改判据。分档也一并删了（`domain.fleet_tier` 整个模块），
所以「守方多少艘」不再是这条链路的输入。

## 平局重打，但有界

平局（`DRAW`）意味着两边都还有船，同一个坐标再打一发是有意义的。但**无限重打
一个坐标**会把整轮卡在它身上：航线被它吃光、别的目标一发都轮不到，而战报读不到
时还会变成死循环。所以本轮每个目标最多打 `MAX_ATTACKS_PER_TARGET` 发，
理由见那个常量。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from evo_helper.domain.battle_outcome import OUTCOME_DRAW

#: bot 攻击用的游戏内预设标题。用户口径（2026-08-13）：一律 BBB，不再分档。
#:
#: 存的必须是**标题原文**：派遣链路是按标题在预设条上 OCR 找的
#: （`game.preset_picker`），标题对不上就抛 `PresetNotFound`，整发放弃。
#: 实机日志里出现过 `预设条上找不到 'CCC'；这一屏读到的是 ['AAA', '探路']`
#: ——成因是选择器只往左拖、够不到右边的预设（PR #100 已修）。**BBB 正是要往右
#: 拖才看得到的那一档**，所以这条风险对它尤其实在。
#: 改这个常量之前对着游戏的预设条核一遍标题。
#: 预设里装了什么由用户在游戏里维护，助手不读也不校验。
BOT_ATTACK_PRESET = "BBB"

#: 本轮同一个坐标最多打几发（含第一发）。也就是：**平局最多再补两发**。
#:
#: 上限本身不可省。「平局就继续攻击」这条口径没有自带终点：两边势均力敌时
#: 每一发都可能又是平局，而一轮的航线数是个位数——一个咬死的目标能把整轮吃光，
#: 别的目标一发都轮不到，日志上还只是一句「又打了一发」。
#:
#: 取 3 的依据是**和仓里已有的两条自愈配额同一档**：断线重开 3 次/滚动 1 小时
#: （`game.reconnect`）、认不出目标只自愈一次（`PirateLoop._goto_checked`）。
#: 都是「给它几次机会，但绝不让它一直试」。3 发之后仍是平局，说明这个目标不是
#: 多打一发能解决的，留给下一轮（或让用户看日志改预设）比在这一轮里耗完航线好。
#:
#: **周期是「一轮」**，不是一天也不是滚动窗口：计数直接由
#: `repository.bot_dispatch_facts(since=本轮起点)` 给出，`begin_bot_round` 挪一次
#: 轮次起点，计数就自然归零——不需要任何一列去记「打了几发」，也就没有一列会和
#: 事实对不上。用户在控制台上点「新一轮」就是重置。
MAX_ATTACKS_PER_TARGET = 3


class BotPhase(Enum):
    """一个目标本轮走到哪一步了。"""

    #: 该打了。本轮还没打过，或者上一发是平局且配额还有剩。
    NEEDS_ATTACK = "NEEDS_ATTACK"
    #: 攻击已派出，等它的战报。
    AWAITING_ATTACK_REPORT = "AWAITING_ATTACK_REPORT"
    #: 走完了。分出了胜负，或者平局但已经打满 `MAX_ATTACKS_PER_TARGET` 发。
    DONE = "DONE"


@dataclass(frozen=True)
class DispatchFact:
    """本轮针对某个目标的一次攻击派遣，以及它那份战报的结论。"""

    #: 这一发的战报入库了没有。
    has_report: bool
    #: 那份战报算出来的战果（`domain.battle_outcome` 的三个词之一）。
    #:
    #: **`None` 有两种来源，而它们在这里合流是对的**：没有战报，或者有战报但四个
    #: 数缺一个、算不出胜负（`battle_outcome.outcome_from_totals`）。两种都不构成
    #: 「这一发打成了平局」的证据，而重打的唯一依据就是**确认是平局**——
    #: 拿「算不出」去重打，等于凭一次 OCR 失手再送一支舰队出去。
    outcome: str | None = None


def phase_of(attacks: Sequence[DispatchFact]) -> BotPhase:
    """这个目标本轮该干什么。

    ⚠️ **前置条件：调用方必须先把「已判定战报永远不会来」的派遣剔除掉。**
    这个函数只认「战报回来了没有」，不判定超时——判超时要知道派出时刻、
    飞行时间、战报有效期，那些事实在仓储那一侧，不在这里
    （落实处是 `repository.bot_dispatch_facts`，它按 `MAX_REPORT_AGE` 剔）。

    没剔干净的后果不是报错，是**静默卡死**：一发攻击的战报永远不到，
    这个目标就永远停在 `AWAITING_ATTACK_REPORT`，于是整个 bot 任务永远不退出，
    而画面上看起来只是「在等」。

    ⚠️ **等战报优先于重打。** 只要还有一发没回来就一律等：不等的话，一个目标会在
    第一发还在飞的时候就被当成「还没打够」再补一发，几趟下来同一个坐标上摞着
    四五支舰队——而平局与否本来就要等战报回来才知道。
    """
    if not attacks:
        return BotPhase.NEEDS_ATTACK
    if not all(item.has_report for item in attacks):
        return BotPhase.AWAITING_ATTACK_REPORT
    if len(attacks) >= MAX_ATTACKS_PER_TARGET:
        # 打满了。**不看最后一发是不是平局**：满了就是满了，说成 `NEEDS_ATTACK`
        # 只会让调用方每趟重算一次、再被别处拦下来，日志上还看不出是被上限挡的。
        return BotPhase.DONE
    return BotPhase.NEEDS_ATTACK if _last_outcome(attacks) == OUTCOME_DRAW else BotPhase.DONE


def _last_outcome(attacks: Sequence[DispatchFact]) -> str | None:
    """最后一发打成了什么。

    按**最后一发**而不是「有没有任何一发是平局」判：口径是「同一坐标攻击结果为
    平局则继续攻击」，说的是这一发的结果。按 any 判的话，第一发平局、第二发打赢
    的目标会被永远判成还要再打——直到撞上发数上限才停。

    次序由调用方保证（仓储按 `dispatched_at_utc` 排）。
    """
    return attacks[-1].outcome


__all__ = [
    "BOT_ATTACK_PRESET",
    "MAX_ATTACKS_PER_TARGET",
    "DispatchFact",
    "BotPhase",
    "phase_of",
]
