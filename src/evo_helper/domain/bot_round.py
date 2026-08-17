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

## 平局不再重打（2026-08-17 移除）

原先这里还有一条规则：最后一发打成平局（`DRAW`）就对同一坐标再补一发，上限
`MAX_ATTACKS_PER_TARGET = 3`。用户口径（2026-08-17）：「bot 攻击移除平局再打
一次机制」——**平局就当这一发结束**，和打赢、打输一样收工。

所以现在战果**根本不参与判态**：一发攻击的战报回来了，这个目标本轮就走完了。
判据只剩「派过没有」和「战报回来没有」两件事实。

⚠️ **移除的是「平局要重打」这条规则，不是「平局」这个战果。** `DRAW` 仍然照常
算出来（`domain.battle_outcome`）、照常写进 `battle_reports.outcome`、照常出现在
日志页与情报中心的战果筛选里。这里不看它，不等于别处看不到它。

随规则一起删掉的还有 `MAX_ATTACKS_PER_TARGET` 与 `DispatchFact.outcome`：
两者都只为这条规则存在（前者是它的上限，后者是它的输入）。**删掉而不是留成
死常量/死字段**——留着的话下一个读它的人会以为「上限」还在约束什么、
「战果」还在被谁读，照着它去改判据。同一条理由当初也用在探路那两个态上。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

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


class BotPhase(Enum):
    """一个目标本轮走到哪一步了。"""

    #: 该打了。本轮还没打过。
    NEEDS_ATTACK = "NEEDS_ATTACK"
    #: 攻击已派出，等它的战报。
    AWAITING_ATTACK_REPORT = "AWAITING_ATTACK_REPORT"
    #: 走完了。那一发的战报回来了——**不论打成了什么**，平局也在内。
    DONE = "DONE"


@dataclass(frozen=True)
class DispatchFact:
    """本轮针对某个目标的一次攻击派遣。

    只有一个字段是有意的，形状与 `domain.pirate_round.AttackFact` 一致：
    平局重打移除之后（2026-08-17），战果不再参与判态，一发攻击就是一发攻击。
    战果本身仍然存在，只是它的读者在展示那一侧（日志页、情报中心），不在这里。
    """

    #: 这一发的战报入库了没有。
    has_report: bool


def phase_of(attacks: Sequence[DispatchFact]) -> BotPhase:
    """这个目标本轮该干什么。

    ⚠️ **前置条件：调用方必须先把「已判定战报永远不会来」的派遣剔除掉。**
    这个函数只认「战报回来了没有」，不判定超时——判超时要知道派出时刻、
    飞行时间、战报有效期，那些事实在仓储那一侧，不在这里
    （落实处是 `repository.bot_dispatch_facts`，它按 `MAX_REPORT_AGE` 剔）。

    没剔干净的后果不是报错，是**静默卡死**：一发攻击的战报永远不到，
    这个目标就永远停在 `AWAITING_ATTACK_REPORT`，于是整个 bot 任务永远不退出，
    而画面上看起来只是「在等」。

    ⚠️ **有一发没回来就一律等。** 平局重打移除之后，同一坐标在一轮里通常只有
    一发；但战报过期被剔掉之后允许重来一发（见 `repository.bot_dispatch_facts`），
    所以「多发」仍然是会发生的情形。不等的话，第一发还在飞时这个目标就会被再补
    一发，几趟下来同一个坐标上摞着四五支舰队。
    """
    if not attacks:
        return BotPhase.NEEDS_ATTACK
    if not all(item.has_report for item in attacks):
        return BotPhase.AWAITING_ATTACK_REPORT
    # 战报都回来了就收工。**不看战果**——平局、胜、负、算不出，一律走完
    # （用户口径 2026-08-17，见模块头）。
    return BotPhase.DONE


__all__ = [
    "BOT_ATTACK_PRESET",
    "DispatchFact",
    "BotPhase",
    "phase_of",
]
