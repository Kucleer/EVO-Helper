"""把库里**已经存着、却没认领上派遣**的战报按现在的判据重认一遍。

    # 干跑：只打印会认上哪几份，一个字都不写库
    .venv/Scripts/python.exe -m evo_helper.tools.rematch_reports

    # 看过干跑结果、确认无误之后才写
    .venv/Scripts/python.exe -m evo_helper.tools.rematch_reports --apply

## 为什么需要它

`append_report` 只在**写入的那一刻**认领一次，而 `has_report_at` 那道去重保证了
一份读过的战报永远不会被重新读一遍。于是「认领判据修好了」并不能让已经在库里的
那些行自己接上——它们会永远停在 `dispatch_id = NULL`，攻击日志上的战果列永远空着。

实机（生产库 2026-08-18）：第二颗出发星 `9:250:8` 的 7 份战报因为出发点被 OCR
读成 `3:250:8` 而全部认不上（判据的修法见 `storage.repository._link_dispatch`）。

## 它和「控制台一开工就自动重认」是什么关系

`application.mission_scheduler` 在**每次点开始**时都会调一次
`rematch_unlinked_reports()`，所以判据改好之后，用户重启一次控制台，这批行本来
就会自己接上。这个命令存在的意义只有一个：**在那之前先看一眼会发生什么**。
`--apply` 只是把同一件事提前做掉，两边跑的是同一段判据、同一批行。

## 它绝不做什么

- ⚠️ **只碰 `battle_reports` 的 `dispatch_id` / `match_status` / `match_confidence`
  三列。** 战果、战损、资源、截图一个字节都不动。
- ⚠️ **只碰 `dispatch_id` 为空的行。** 已经认上的不重算——那会把一次判据变动变成
  一次静默的改档，而 `dispatch_id` 上有唯一约束，算错一次连原本对的那一发也丢了。
- ⚠️ **一行 `attack_dispatches` 都不新建。** 认不上就认不上。
- 不碰游戏、不动鼠标、不开窗口。全程离线，随时能跑。

## ⚠️ 干跑的输出是给人看的，不许存进仓库

每一行都带着出发星与目标坐标，本仓是公开仓库。看完就算，别重定向进任何一个
仓库里的文件。
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import record_system_log, shutdown_system_log_sink
from evo_helper.infrastructure.system_log_db import install_database_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import ReportRematchPlan, SqlAlchemyRepository

#: `system_log.source`。事后按它一条查询就能翻出这条路径改过什么。
LOG_SOURCE = "report-rematch"


def describe(plan: ReportRematchPlan) -> str:
    """一行说清这一份会怎么处置。**出发点读数与派遣不一致时要明说**。"""
    when = plan.reported_at_utc.strftime("%m-%d %H:%M:%S")
    head = f"{plan.target}  {when}  {plan.previous_status or '?'} →"
    if not plan.claims:
        return f"{head} {plan.status}（认不上，保持空着）"
    tail = f"{plan.status} 置信度 {plan.match_confidence}  派遣 {str(plan.dispatch_id)[:8]}"
    if plan.dispatch_origin is not None and plan.dispatch_origin != plan.report_origin:
        return (
            f"{head} {tail}  ⚠️ 出发点：战报读作 {plan.report_origin}，"
            f"派遣记的是 {plan.dispatch_origin}"
        )
    return f"{head} {tail}"


def print_plans(plans: Sequence[ReportRematchPlan]) -> None:
    if not plans:
        print("库里没有未认领的战报，没什么可做的。")
        return
    for plan in plans:
        print(f"  {describe(plan)}")
    claimed = [plan for plan in plans if plan.claims]
    mismatched = [
        plan
        for plan in claimed
        if plan.dispatch_origin is not None and plan.dispatch_origin != plan.report_origin
    ]
    print(
        f"\n未认领 {len(plans)} 份；这一趟能认上 {len(claimed)} 份"
        f"（其中 {len(mismatched)} 份的出发点读数与派遣对不上），"
        f"仍然认不上 {len(plans) - len(claimed)} 份。"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真的写库；不给就只打印会认上哪几份（默认）",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="连哪个库。默认取配置里的那个；调试请指到测试库，别指生产",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="一趟最多看几份未认领战报（默认 500，新的在前）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit 要的是正整数")

    url = args.database_url or Settings().database_url
    engine = create_database_engine(url)
    session_factory = create_session_factory(engine)
    repository = SqlAlchemyRepository(session_factory)

    plans = repository.plan_unlinked_rematch(limit=args.limit)
    print_plans(plans)

    if not args.apply:
        print("\n干跑：一个字都没写库。看过没问题再加 --apply。")
        return 0

    # ⚠️ 日志出口**只在真要写库时才装**。干跑必须一个字都不写，而 `system_log`
    # 和这些数据住在同一个库里——装上它，干跑就不再是干跑了。
    install_database_system_log(session_factory)
    try:
        matched = repository.rematch_unlinked_reports(limit=args.limit)
        record_system_log(
            "INFO",
            LOG_SOURCE,
            f"离线重认收工：看了 {len(plans)} 份未认领战报，补上 {matched} 份",
            payload={
                "planned": len(plans),
                "planned_claims": sum(1 for plan in plans if plan.claims),
                "matched": matched,
            },
        )
    finally:
        # 队列是异步刷盘的，不 flush 的话最后那批日志正好还在内存里。
        shutdown_system_log_sink()
    print(f"\n已写库：{matched} 份战报接上了派遣。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
