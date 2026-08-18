"""拿库里**已经存着的**战报面板重跑「获得资源」12 格，回填 `battle_report_resources`。

    # 干跑：只打印要改什么，一个字都不写库
    .venv/Scripts/python.exe -m evo_helper.tools.reread_report_resources

    # 看过干跑结果、确认无误之后才写
    .venv/Scripts/python.exe -m evo_helper.tools.reread_report_resources --apply

## 为什么单独一个入口，不给 `tools.backfill_reports` 加个开关

那个入口做的是**回游戏信箱重翻**：起 Chrome、点邮件、真点鼠标，跑之前调度器
必须停着，工作时间还不许起游戏。这一条是**离线**的——像素早就在
`battle_report_screenshots` 里了，全程不碰游戏、不动鼠标、不开窗口，随时能跑，
跑一百遍也不会惊动任何东西。两者唯一的共同点是「事后补数据」这四个字，而把
它们并在一个命令下，代价是那条硬约束（调度器要停着）会跟着套到本来不需要它的
路径上，或者反过来被人误以为不需要。

## 它做什么

1. 列出 `battle_report_screenshots` 里所有的图（520×695 的面板，WEBP）；
2. 逐张按 `ReportLayout.resource_grid` 重读 12 格（字模匹配，见
   `vision.resource_digits`）；
3. 和 `battle_report_resources` 里现有的明细逐格比，算出 `旧值 → 新值`；
4. 默认**只打印**；给了 `--apply` 才落库，并把每一处改动写进 `system_log`。

## 它绝不做什么

- ⚠️ **只碰 `battle_report_resources` 一张表。** `battle_reports` 的 `outcome`、
  `attacker_units`、`defender_units`、`attacker_losses`、`defender_losses`、
  `match_status`、`dispatch_id` 一个字段都不动——那些是当年那一屏读出来的观测与
  认领结果，这一趟既没重读它们，也没资格拿今天的一次离线重跑去覆盖它们。
- ⚠️ **12 格没读全的整份跳过，一格都不写，更不补 0。** 库里只存非零行，
  「没有这一格 = 这一格是 0」这条语义只在 12 格全读到时才成立（判据在
  `domain.battle_resources.parse_resource_grid`）。要提高的是读得出，不是降低要求。
- **不删图、不改图、不碰游戏。**

## ⚠️ 干跑的输出是给人看的，不许存进仓库

每一行都带着坐标与资源数量，本仓是公开仓库。看完就算，别重定向进任何一个
仓库里的文件（`.gitignore` 的图片那几条挡的是同一类事）。
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.resource_reread import (
    PlanKind,
    ReportPlan,
    RereadSummary,
    plan_report,
    skipped_plan,
)
from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import record_system_log, shutdown_system_log_sink
from evo_helper.infrastructure.system_log_db import install_database_system_log
from evo_helper.storage import models as orm
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.report_resources import ReportResourceRepository
from evo_helper.storage.report_screenshots import ReportScreenshotRepository

#: `system_log.source`。事后按它一条查询就能翻出这条路径改过什么。
LOG_SOURCE = "resource-reread"


@dataclass(frozen=True, slots=True)
class ReportLabel:
    """打印用的一句话身份：`4:20:6  08/17/2026 03:24`。

    只为看得懂而取，**不参与任何判据**——匹配、去重、写入全都按 `report_id`。
    """

    target: str
    when: str

    def __str__(self) -> str:
        return f"{self.target}  {self.when}"


def report_labels(
    session_factory: sessionmaker[Session], report_ids: Sequence[UUID]
) -> dict[UUID, ReportLabel]:
    """一次查出这些战报的目标坐标与时间。**只读 `battle_reports`，不改它。**"""
    if not report_ids:
        return {}
    with session_factory() as session:
        rows = session.execute(
            select(
                orm.BattleReportRow.id,
                orm.BattleReportRow.defender_target_galaxy,
                orm.BattleReportRow.defender_target_system,
                orm.BattleReportRow.defender_target_position,
                orm.BattleReportRow.raw_time_text,
                orm.BattleReportRow.reported_at_utc,
            ).where(orm.BattleReportRow.id.in_(list(report_ids)))
        ).all()
    return {
        UUID(str(row.id)): ReportLabel(
            target=f"{row[1]}:{row[2]}:{row[3]}",
            when=row.raw_time_text or row.reported_at_utc.isoformat(sep=" ", timespec="minutes"),
        )
        for row in rows
    }


def plan_all(
    screenshots: ReportScreenshotRepository,
    resources: ReportResourceRepository,
    *,
    only: UUID | None = None,
) -> list[ReportPlan]:
    """把库里每一张面板都重跑一遍，交出逐份的计划。**全程只读。**"""
    from evo_helper.vision.optional.panel_resources import read_panel_resource_cells

    plans: list[ReportPlan] = []
    for ref in screenshots.list_refs():
        if only is not None and ref.report_id != only:
            continue
        shot = screenshots.load(ref.report_id)
        if shot is None:
            # 列清单与取字节之间那张图被保留期清理删掉了。算跳过，不算失败。
            plans.append(skipped_plan(ref.report_id, "取字节时这张图已经不在库里了"))
            continue
        try:
            cells = read_panel_resource_cells(shot.image_bytes)
        except ValueError as error:
            plans.append(skipped_plan(ref.report_id, str(error)))
            continue
        plans.append(plan_report(ref.report_id, cells, resources.load(ref.report_id)))
    return plans


def print_plans(plans: Iterable[ReportPlan], labels: dict[UUID, ReportLabel]) -> None:
    """把计划打给人看。跳过的也逐条打——它们才是下一次要修的东西。"""
    for plan in plans:
        who = labels.get(plan.report_id)
        head = f"{plan.report_id}  {who}" if who else str(plan.report_id)
        if plan.kind is PlanKind.SKIPPED:
            print(f"[跳过] {head}\n        {plan.skip_reason}")
            continue
        if plan.kind is PlanKind.UNCHANGED:
            print(f"[不变] {head}")
            continue
        title = "新增明细" if plan.kind is PlanKind.ADDED else "修改已有"
        print(f"[{title}] {head}")
        for change in plan.changes:
            print(f"        {change.describe()}")


def apply_plans(
    resources: ReportResourceRepository,
    plans: Sequence[ReportPlan],
    labels: dict[UUID, ReportLabel],
) -> int:
    """把计划落库，返回改了几份。**每一份都先写日志再写库。**

    先日志后库是有意的：写库那一步失败时，库里没有变化而日志里留着「打算改什么」，
    排查的人看得见这一趟走到哪了。反过来（先库后日志）失败时，数据已经变了却
    一条痕迹都没有——而这是**改历史数据**的路径，事后必须查得出改过什么。
    """
    changed = 0
    for plan in plans:
        if plan.kind is PlanKind.SKIPPED:
            # 判据把这一份挡掉的那一刻要留痕：说清为什么、以及当时读到了什么。
            record_system_log(
                "WARNING",
                LOG_SOURCE,
                f"存档面板重跑：{plan.skip_reason}",
                payload={
                    "report_id": str(plan.report_id),
                    "target": str(labels.get(plan.report_id) or ""),
                    "cells": list(plan.cells),
                },
            )
            continue
        if not plan.changes:
            continue
        record_system_log(
            "INFO",
            LOG_SOURCE,
            f"存档面板重跑：改写 {len(plan.changes)} 格收获明细",
            payload={
                "report_id": str(plan.report_id),
                "target": str(labels.get(plan.report_id) or ""),
                "kind": plan.kind.value,
                "cells": list(plan.cells),
                "changes": [
                    {
                        "slot": change.slot,
                        "label": change.label,
                        "before": None if change.before is None else change.before.amount,
                        "after": None if change.after is None else change.after.amount,
                    }
                    for change in plan.changes
                ],
            },
        )
        resources.apply_slot_changes(plan.report_id, plan.writes)
        changed += 1
    return changed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库；不给就只打印要改什么（默认）",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="连哪个库。默认取配置里的那个；调试请指到测试库，别指生产",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="只跑这一份战报（战报 id）。不给就跑库里所有有图的战报",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    only: UUID | None = None
    if args.report:
        try:
            only = UUID(args.report)
        except ValueError:
            parser.error(f"--report 要的是战报 id（UUID），给了 {args.report!r}")

    url = args.database_url or Settings().database_url
    engine = create_database_engine(url)
    session_factory = create_session_factory(engine)
    screenshots = ReportScreenshotRepository(session_factory)
    resources = ReportResourceRepository(session_factory)

    plans = plan_all(screenshots, resources, only=only)
    labels = report_labels(session_factory, [plan.report_id for plan in plans])
    print_plans(plans, labels)
    summary = RereadSummary.of(plans)
    print(f"\n{summary.describe()}")

    if not args.apply:
        print("\n干跑：一个字都没写库。看过没问题再加 --apply。")
        return 0

    # ⚠️ 日志出口**只在真要写库时才装**。干跑必须一个字都不写，而 `system_log`
    # 和这些数据住在同一个库里——装上它，干跑就不再是干跑了。
    install_database_system_log(session_factory)
    try:
        changed = apply_plans(resources, plans, labels)
        record_system_log(
            "INFO",
            LOG_SOURCE,
            f"存档面板重跑收工：{summary.describe()}",
            payload={
                "total": summary.total,
                "skipped": summary.skipped,
                "added": summary.added,
                "updated": summary.updated,
                "unchanged": summary.unchanged,
                "changed_slots": summary.changed_slots,
            },
        )
    finally:
        # 队列是异步刷盘的，不 flush 的话最后那批日志正好还在内存里。
        shutdown_system_log_sink()
    print(f"\n已写库：{changed} 份战报的收获明细。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
