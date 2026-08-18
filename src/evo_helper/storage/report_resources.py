"""`battle_report_resources` 的读回与**逐格改写**。

写入那条正路在 `SqlAlchemyRepository.append_report`：战报第一次入库时连着收获
一起写。这个模块管的是另一件事——**改历史数据**：拿 `battle_report_screenshots`
里存着的面板重跑一遍识别，把当年读不全（整块作废）的补上、把读错的改对
（入口在 `tools.reread_report_resources`）。

## 为什么另起一个模块

`SqlAlchemyRepository` 是攻击链路的账本，每加一个方法四条链路都跟着继承；
而这里的方法只有一条离线路径会调，还带着「改历史数据」这种任何实机链路都不该
有的能力。分开之后这条边界是**类型层面**的——实机拿到的那个仓储对象上根本没有
`apply_slot_changes` 这个方法，不是靠注释提醒。理由与
`storage.report_screenshots` 当初分表是同一条。

## ⚠️ 这个模块只碰 `battle_report_resources` 一张表

`battle_reports` 的 `outcome` / `attacker_units` / `defender_units` /
`attacker_losses` / `defender_losses` / `match_status` / `dispatch_id` 一个字段
都不许在这条路径上改。那些是当年那一屏读出来的观测与认领结果，重跑资源识别
既没有重读它们、也没有资格改它们——改了就等于用今天的一次离线重跑，覆盖掉一份
无法复原的历史观测。
"""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT
from evo_helper.domain.records import BattleResourceEntry

from . import models as orm


class ReportResourceRepository:
    """按战报读回收获明细，以及逐格改写它。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load(self, report_id: UUID) -> tuple[BattleResourceEntry, ...]:
        """这份战报库里已有的收获明细，按槽位升序。

        ⚠️ **空元组是「库里一行都没有」，它有两种意思**：12 格全 0，或者这份
        战报的收获压根没读到过（判据写在 `orm.BattleReportResourceRow` 上，
        库里分不开）。这一层只如实交出行，不替调用方猜是哪一种。
        """
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.BattleReportResourceRow)
                .where(orm.BattleReportResourceRow.report_id == report_id)
                .order_by(orm.BattleReportResourceRow.slot)
            ).all()
        return tuple(
            BattleResourceEntry(
                slot=row.slot,
                amount=row.amount,
                approximate=row.approximate,
                uncertainty=row.uncertainty,
            )
            for row in rows
        )

    def apply_slot_changes(
        self, report_id: UUID, changes: dict[int, BattleResourceEntry | None]
    ) -> None:
        """逐格改写：`None` 表示删掉这一格，条目表示写成这个值。

        **按格改而不是「整份删了重写」**，有两个理由：

        1. 打印出来的是哪几格、真正落库的就是哪几格。整份重写时，「一格没变」
           和「一格被删了又原样写回」在库里长得一模一样，干跑的输出就不再是
           写入的凭据。
        2. 幂等由这里兜底：已经是目标值的格子传进来是「不变」，一行都不动，
           所以同一份战报跑第二遍不会产生任何新行——`report_id + slot` 上那条
           唯一约束因此永远撞不上。

        ⚠️ **不写数量为 0 的行。** 「没有这一行 = 这一格是 0」是这张表的语义
        （见 `orm.BattleReportResourceRow`），补一行 0 进去会让后来的人分不清
        「读到了 0」和「多写了一行」。调用方要表达 0，传 `None`。
        """
        for slot in changes:
            if not 0 <= slot < GAINED_SLOT_COUNT:
                raise ValueError(f"槽位 {slot} 不在 0..{GAINED_SLOT_COUNT - 1} 之内")
        for slot, entry in changes.items():
            if entry is not None and entry.slot != slot:
                raise ValueError(f"第 {slot} 格挂着 slot={entry.slot} 的条目")
            if entry is not None and entry.amount == 0:
                raise ValueError(f"第 {slot} 格要写 0；这张表里 0 是「没有这一行」，请传 None")
        if not changes:
            return
        with self._session_factory() as session:
            rows = {
                row.slot: row
                for row in session.scalars(
                    select(orm.BattleReportResourceRow).where(
                        orm.BattleReportResourceRow.report_id == report_id,
                        orm.BattleReportResourceRow.slot.in_(list(changes)),
                    )
                ).all()
            }
            for slot, entry in changes.items():
                row = rows.get(slot)
                if entry is None:
                    if row is not None:
                        session.delete(row)
                    continue
                if row is None:
                    session.add(
                        orm.BattleReportResourceRow(
                            id=uuid4(),
                            report_id=report_id,
                            slot=slot,
                            amount=entry.amount,
                            approximate=entry.approximate,
                            uncertainty=entry.uncertainty,
                        )
                    )
                    continue
                row.amount = entry.amount
                row.approximate = entry.approximate
                row.uncertainty = entry.uncertainty
            session.commit()


__all__ = ["ReportResourceRepository"]
