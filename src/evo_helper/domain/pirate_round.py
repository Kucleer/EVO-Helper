"""一个海盗目标在一轮里走到哪一步了。

海盗链路原先只看得出三件事：侦察派出了、还在等侦察报告、攻击有没有战果。
**判定结论看不出来**——报告回来了却没打，界面上和「还没轮到打」长得一模一样。
用户 2026-08-11 提的就是这条：侦察拿到报告之后要能分出「不触发攻击 /
触发攻击 / 攻击完成」。

## 为什么是四态而不是三态

用户列了三态，这里落成四个终态，多出来的那个是 `SCOUT_UNREADABLE`。
依据是实测数据，不是洁癖：

侦察判定本来就是三值的（`domain.scout_verdict`），而 `UNREADABLE`（读不出来）
与 `SKIP`（读全了，都 ≤ 1，不值得打）**是两件事**。把它们并成「不触发攻击」，
就是把「没看清」记成「这里是空的」——这条错误本模块与
`domain.records.ScoutTriggerShip` 各写了一遍，因为它真的发生过。

而且并不是一个理论上的边角：库里 98 份侦察报告，`收割者` 那一格
**一份都没读出来**（ROI 落空，另案）。于是 28 份非攻击判定**全部**是
`UNREADABLE`，`SKIP` 一份都没有。三态显示会把这 28 个海盗一律标成
「不值得打」，而真相是「有一格从来没看清过」——正好是最不该混的那两件事。

## 与 `domain.bot_round` 的关系

两条链路的形状像但判据不同，所以是两个模块，不是一个泛化的：

- bot：**不做任何前置侦查**，直接用预设 BBB 打，看战报算出来的战果决定要不要
  对同一坐标再打一发（平局才重打，`domain.battle_outcome`）。
- 海盗：侦察发打回来的是**侦察报告**，看四个触发舰种的数量（`domain.scout_verdict`）
  决定打不打，打完就完了，不看战果。

共同点只有「攻击发回没回战报」，而那点共同点不值得为它造一个抽象。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from evo_helper.domain.scout_verdict import (
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
)


class PiratePhase(Enum):
    """一个海盗目标本轮走到哪一步了。值是接口与库里用的英文常量。"""

    #: 本轮还没派过侦察。
    NEEDS_SCOUT = "NEEDS_SCOUT"
    #: 侦察已派出，等它的侦察报告。
    AWAITING_SCOUT_REPORT = "AWAITING_SCOUT_REPORT"
    #: 侦察报告已回，判定**不触发攻击**（四格都读全了，都 ≤ 1）。
    NO_ATTACK = "NO_ATTACK"
    #: 侦察报告已回，但判定输入没读全。**这不是「不触发攻击」**，是没看清。
    SCOUT_UNREADABLE = "SCOUT_UNREADABLE"
    #: 判定说该打，攻击还没派出去。停在这一态就是链路卡住了，见下面的说明。
    NEEDS_ATTACK = "NEEDS_ATTACK"
    #: 已按判定派出攻击，等攻击战报。
    AWAITING_ATTACK_REPORT = "AWAITING_ATTACK_REPORT"
    #: 攻击完成——攻击战报回来了。
    ATTACK_DONE = "ATTACK_DONE"


@dataclass(frozen=True)
class AttackFact:
    """本轮针对某个海盗真的派出去的一发攻击。

    只有一个字段是有意的：海盗这边一发攻击就是一发攻击，战果不参与判态——
    **平局重打是 bot 那条链路的规则**（`domain.bot_round`），海盗不受它影响。
    """

    has_report: bool


def phase_of(*, verdict: str | None, attacks: Sequence[AttackFact]) -> PiratePhase:
    """这个海盗本轮走到哪一步了。

    `verdict` 是**现算**出来的侦察判定（`domain.scout_verdict.verdict_of_record`），
    `None` 表示本轮的侦察报告还没回来（或者压根没派侦察，由 `scouted` 区分——
    见下面的重载入口 `phase_for`）。

    ⚠️ **派出去的攻击压过判定。** 判定是会变的规则，攻击是已经发生的事实：
    规则改了、或者报告后来被重读成别的结论，都不该让一发已经飞出去的攻击
    在界面上退回「不触发攻击」。所以有攻击发时只看战报回没回。

    ⚠️ **`NEEDS_ATTACK` 是一个会长期停住的态，不是过场。** 活链路里判定与派出
    是同一趟做完的（`tools.pirate_loop._decide_and_attack`），所以正常情况下
    看不到它；看到了就说明那一趟被拦下了——航线满、面板认不出、简报不是攻击、
    或者预设条上找不到那个预设。把它单列出来，正是为了让这类「判定要打却
    一发没出去」在界面上有名字，而不是和「不触发攻击」混成一片。
    """
    if attacks:
        return (
            PiratePhase.ATTACK_DONE
            if all(item.has_report for item in attacks)
            else PiratePhase.AWAITING_ATTACK_REPORT
        )
    if verdict is None:
        return PiratePhase.AWAITING_SCOUT_REPORT
    if verdict == VERDICT_ATTACK:
        return PiratePhase.NEEDS_ATTACK
    if verdict == VERDICT_SKIP:
        return PiratePhase.NO_ATTACK
    if verdict == VERDICT_UNREADABLE:
        return PiratePhase.SCOUT_UNREADABLE
    raise ValueError(f"未知的侦察判定：{verdict!r}")


def phase_for(*, scouted: bool, verdict: str | None, attacks: Sequence[AttackFact]) -> PiratePhase:
    """同上，但把「本轮连侦察都还没派」也算进来。

    分成两个函数而不是给 `phase_of` 加默认参数：`phase_of` 的前提是
    「至少派过一发侦察」，那是活链路唯一会问的情形；而列表页要把
    「这一位是海盗、本轮还没轮到它」也显示出来，那是另一个问题。
    """
    if not scouted and not attacks and verdict is None:
        return PiratePhase.NEEDS_SCOUT
    return phase_of(verdict=verdict, attacks=attacks)


#: 界面上的中文说法。**接口与库里一律用英文常量**，这张表只管显示。
#: 放在 domain 而不是 web，是因为「没看清」和「不触发攻击」措辞一旦被改成
#: 同一句话，四态就白分了——措辞在这里和判据摆在一起，改的人看得见理由。
PHASE_LABELS: dict[PiratePhase, str] = {
    PiratePhase.NEEDS_SCOUT: "待侦察",
    PiratePhase.AWAITING_SCOUT_REPORT: "待侦察报告",
    PiratePhase.NO_ATTACK: "不触发攻击",
    PiratePhase.SCOUT_UNREADABLE: "侦察没看清",
    PiratePhase.NEEDS_ATTACK: "待触发攻击",
    PiratePhase.AWAITING_ATTACK_REPORT: "已触发攻击 · 待战报",
    PiratePhase.ATTACK_DONE: "攻击完成",
}


__all__ = [
    "PHASE_LABELS",
    "AttackFact",
    "PiratePhase",
    "phase_for",
    "phase_of",
]
