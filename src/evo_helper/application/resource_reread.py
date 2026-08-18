"""拿存档面板重跑「获得资源」识别，算出**要对库做哪几处改动**。

这个模块只做算账，不碰数据库、不碰像素：进来的是「12 格原文」与「库里现有的
明细」，出去的是逐格的 `旧值 → 新值`。落库在 `storage.report_resources`，
取像素在 `vision.optional.panel_resources`，串起来的入口是
`tools.reread_report_resources`。

## 为什么要有这条离线路径

`battle_report_screenshots` 里躺着 34 份战报的面板图，而 `battle_report_resources`
只有 5 份有明细——另外 29 份当年「12 格没读全」，按全有或全无整块作废了。
PR #191 把那 12 格改成字模匹配之后，同一批图 34 份全部读得全。图还在，所以
**不用回游戏、不用碰鼠标**，离线重跑一遍就能把那 29 份补回来。

## ⚠️ 全有或全无一个字都没放松

`parse_resource_grid` 返回 None（12 格里但凡有一格读不出）就整份跳过，一格都
不写。**要提高的是读得出，不是降低要求**——放松了的话，读到的 8 格进了库，
剩下 4 格会被后来的人当成 0，一次读不全就此变成四个凭空捏造的零，而且不留痕迹。
理由的完整版写在 `domain.battle_resources.parse_resource_grid` 上。

## 「改历史数据」这件事怎么才算安全

1. **默认只算不写。** 写库要调用方显式说（工具上是 `--apply`）。
2. **算出来的是逐格的差**，不是「整份删了重写」。打印出来的是哪几格、落库的
   就是哪几格，两者是同一份清单。
3. **幂等。** 差算完是空的就一行都不动，所以同一份战报跑第二遍什么都不会发生。
4. **每一处改动都要能事后翻出来**（工具那一层往 `system_log` 写）。

## ⚠️ 「库里没有这一格」与「这一格是 0」是同一件事

这张表只存非零行。所以「重跑读到 0、库里却有一行」意味着**那一行当年读错了**，
处置是删掉它，不是留着——留着的话页面上会显示一笔从来没捞到过的收获。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT, parse_resource_grid, slot_label
from evo_helper.domain.records import BattleResourceEntry


class PlanKind(StrEnum):
    """一份战报重跑之后的去向。"""

    #: 12 格没读全（或者这张图根本读不了），整份跳过，一格都不写。
    SKIPPED = "skipped"
    #: 读全了，但和库里现有的一模一样。第二次跑同一份战报必然落在这里。
    UNCHANGED = "unchanged"
    #: 库里原先一行都没有，这次补上明细。29 份作废的战报走的是这条。
    ADDED = "added"
    #: 库里原先有明细，这次改了其中几格。
    UPDATED = "updated"


@dataclass(frozen=True, slots=True)
class SlotChange:
    """一格的改动。`before` / `after` 为 `None` 表示「库里没有这一行」，也就是 0。"""

    slot: int
    before: BattleResourceEntry | None
    after: BattleResourceEntry | None

    @property
    def label(self) -> str:
        """这一格在页面上叫什么。翻译只发生在展示时，库里存的仍是槽位。"""
        return slot_label(self.slot)

    def describe(self) -> str:
        """`第 N 格 资源名: 旧值 -> 新值`，供干跑输出与日志共用一套措辞。"""
        before = "（无）" if self.before is None else str(self.before.amount)
        after = "（无）" if self.after is None else str(self.after.amount)
        return f"slot {self.slot} {self.label}: {before} -> {after}"


@dataclass(frozen=True, slots=True)
class ReportPlan:
    """一份战报的重跑结果。"""

    report_id: UUID
    #: 12 格原文，行优先。跳过时也留着——出事时它是唯一能说明「当时看到了什么」的东西。
    cells: tuple[str, ...]
    kind: PlanKind
    changes: tuple[SlotChange, ...] = ()
    #: 跳过的原因，只在 `SKIPPED` 时有值。
    skip_reason: str | None = None

    @property
    def writes(self) -> dict[int, BattleResourceEntry | None]:
        """交给 `storage.report_resources.ReportResourceRepository.apply_slot_changes` 的差。"""
        return {change.slot: change.after for change in self.changes}


def slot_changes(
    existing: Sequence[BattleResourceEntry], desired: Sequence[BattleResourceEntry]
) -> tuple[SlotChange, ...]:
    """两份明细的逐格差，按槽位升序。相同的格子不出现在结果里。

    比的是**整条条目**而不只是数量：`approximate` 与 `uncertainty` 记的是
    「这个数准到什么程度」（`928K` 是 ±500、`233` 是精确读到的），页面照着它们
    写误差范围。数量对了而精度标记错了，页面上就会把一个近似值显示得像精确值。
    """
    before = {entry.slot: entry for entry in existing}
    after = {entry.slot: entry for entry in desired}
    changes = [
        SlotChange(slot=slot, before=before.get(slot), after=after.get(slot))
        for slot in sorted(before.keys() | after.keys())
        if before.get(slot) != after.get(slot)
    ]
    return tuple(changes)


def plan_report(
    report_id: UUID, cells: Sequence[str], existing: Sequence[BattleResourceEntry]
) -> ReportPlan:
    """算出这一份战报要改哪几格。

    `cells` 是 12 格原文（读不出的格子是空串）。**空串一律不当 0**：整块作废的
    判据在 `parse_resource_grid`，这里只负责把它的 `None` 翻译成「跳过」，
    并把没读出来的是哪几格记进跳过原因——出事时那句话就是全部线索。
    """
    if len(cells) != GAINED_SLOT_COUNT:
        raise ValueError(f"获得资源必须是 {GAINED_SLOT_COUNT} 格，给了 {len(cells)} 格")
    texts = tuple(cells)
    desired = parse_resource_grid(texts)
    if desired is None:
        blank = [slot for slot, text in enumerate(texts) if not text]
        reason = (
            f"12 格没读全（第 {blank} 格是空的），整份跳过"
            if blank
            else "有格子读出来不是个合法数量，整份跳过"
        )
        return ReportPlan(
            report_id=report_id, cells=texts, kind=PlanKind.SKIPPED, skip_reason=reason
        )
    changes = slot_changes(existing, desired)
    if not changes:
        return ReportPlan(report_id=report_id, cells=texts, kind=PlanKind.UNCHANGED)
    kind = PlanKind.ADDED if not existing else PlanKind.UPDATED
    return ReportPlan(report_id=report_id, cells=texts, kind=kind, changes=changes)


def skipped_plan(report_id: UUID, reason: str) -> ReportPlan:
    """连 12 格都没能读出来（图解不开、尺寸不符）时的计划。

    单独一个构造函数而不是让调用方自己拼 `ReportPlan`：这类失败与「读了但没读全」
    在统计上算同一档（都是跳过、都不写库），但原因得说清是哪一种——图的问题
    要去修采集，识别的问题才轮得到改识别。
    """
    return ReportPlan(
        report_id=report_id,
        cells=("",) * GAINED_SLOT_COUNT,
        kind=PlanKind.SKIPPED,
        skip_reason=reason,
    )


@dataclass(frozen=True, slots=True)
class RereadSummary:
    """一整批的账。干跑与写库共用——两者唯一的差别是有没有真的落库。"""

    total: int
    skipped: int
    unchanged: int
    added: int
    updated: int
    changed_slots: int

    @classmethod
    def of(cls, plans: Sequence[ReportPlan]) -> RereadSummary:
        counted = {kind: 0 for kind in PlanKind}
        for plan in plans:
            counted[plan.kind] += 1
        return cls(
            total=len(plans),
            skipped=counted[PlanKind.SKIPPED],
            unchanged=counted[PlanKind.UNCHANGED],
            added=counted[PlanKind.ADDED],
            updated=counted[PlanKind.UPDATED],
            changed_slots=sum(len(plan.changes) for plan in plans),
        )

    def describe(self) -> str:
        return (
            f"共 {self.total} 份：读全 {self.total - self.skipped} 份、跳过 {self.skipped} 份；"
            f"新增明细 {self.added} 份、修改已有 {self.updated} 份、无变化 {self.unchanged} 份；"
            f"合计 {self.changed_slots} 格"
        )


__all__ = [
    "PlanKind",
    "ReportPlan",
    "RereadSummary",
    "SlotChange",
    "plan_report",
    "skipped_plan",
    "slot_changes",
]
