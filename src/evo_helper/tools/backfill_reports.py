"""把**信箱里已经躺着的战报**补录进库（海盗战报 / 打 bot 的攻击报告）。

    # 对账：翻到 2026-08-13 UTC 00:00 为止，单子空了就收工
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_reports --kind pirate --since 2026-08-13
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_reports --kind bot --since 2026-08-13

    # 补录：救过期的那些，一直翻到 --since 为止（不受单子空不空影响）
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_reports \
        --kind bot --since 2026-08-12 --exhaustive

## 为什么要有这个入口

`tools.backfill_scout_reports` 只管**侦察报告**（写死 `wanted=ReportKind.SCOUT`），
战报一直没有对应的入口。于是 2026-08-12 那夜丢掉的 21 份 bot 战报**没有任何
工具能取回来**：活链路的 `reconcile_today` 只翻到当日 UTC 日界（外加还在等的
那一发），而那 21 发早就掉出了 `due_attack_dispatches` 的 6 小时窗口。

## 两种模式，命令行上只差一个 `--exhaustive`

| | 谁在用 | 翻到哪为止 |
|---|---|---|
| 默认（对账） | 控制台点「开始」时自动跑 | 撞见一封库里已有、且单子上没有欠账，就收工 |
| `--exhaustive`（补录） | 人手动救过期的战报 | 一直翻到 `--since`，不看单子 |

**两条判据不能混。** 混了的后果是二选一：要么每次点「开始」都把 60 封的预算
烧满（十几分钟，而用户还等着任务开跑），要么手动补录永远救不回过期的那些
——因为它们已经不在单子上了，默认模式在第一封「库里已有」就会收工。

⚠️ **过了六小时照样认领得上**，所以 `--exhaustive` 不是白跑：认领窗口是
`dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，相对**战报自己的时间戳**
算的，不是相对现在（见 `storage.repository._unmatched_dispatch_candidates`）。
单子那个 6 小时是相对现在算的，管的是「还追不追」，不是「认不认得上」。

## 它做什么、不做什么

- **只翻信箱、只读、只写库。** 这条路径上没有任何派遣动作：不侦察、不攻击、
  不取消任务、不领奖励、不删邮件。开信箱、开邮件、返回是真实点击，所以
  `LiveDriver` 必须 `allow_actions=True`——那不是「打开了派遣能力」，
  派遣走的是另外那几个坐标，这条路径根本不碰。
- **只切「报告」标签**，别的筛选一个都不碰（用户口径 2026-08-11）。走的是
  `PirateLoop._scan_mail_rows` 那一份共用实现，白名单由
  `tests/unit/tools/test_mailbox_clicks.py` 钉着。
- **不写 `daily_reconciliations`。** 补录会往回翻到昨天，那一趟数出来的
  「今天有几份」是错的，而那张表按 UTC 日取大、写进去抹不掉。
- **读不出来就不存**，不存半份，不猜；已经在库里的（按目标 + 报告时间）不重写，
  但会顺手把没认领上派遣的旧行重认一次（见 `pirate_loop.rematch_note`）。

## ⚠️ 跑之前

调度器要**停着**。它随时可能起一轮海盗/bot/扫描去抢鼠标，两边同时点同一个
游戏窗口，谁也读不对。控制台点「开始」时是它自己按顺序调这个入口，不冲突。
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from evo_helper.tools.backfill_scout_reports import parse_day
from evo_helper.tools.bot_loop import BotLoop, BotOptions
from evo_helper.tools.pirate_loop import (
    BACKFILL_MAX_OPENS,
    BACKFILL_SCAN_PAGES,
    BackfillTally,
    LoopOptions,
    PirateLoop,
)
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    make_console_encoding_safe,
    make_ocr,
    say,
)

#: `--kind` 的两档。名字与 `domain.scheduler.MissionKind` 的小写一致，
#: 控制台那侧按这两个字符串拼命令。
KIND_PIRATE = "pirate"
KIND_BOT = "bot"


def build_loop(kind: str, driver: LiveDriver, ocr: Any) -> PirateLoop:
    """按 `--kind` 建循环。**区别全在类属性上**，这里一行判据都不写。

    `RECONCILE_KIND`（信箱里哪一类主题算这条链路的战报）、`REPORT_LABEL`、
    `TARGET_KIND`（单子按哪一类查）与「一封战报怎么读」（`_ingest_report`）
    四样都由子类定义。在这里再判一次 kind 就是把同一件事写两遍，
    而两遍迟早会分家——`BotLoop` 当初覆盖 `run()` 而不是 `_sweep()` 就是先例。

    两条链路都不派遣：`attack=False` / `scout=False`，而这个入口根本不调 `run()`。
    """
    if kind == KIND_BOT:
        return BotLoop(driver, ocr, BotOptions(targets=(), attack=False))
    return PirateLoop(driver, ocr, LoopOptions(systems=(), scout=False, attack=False))


def summary_lines(kind: str, tally: BackfillTally, *, exhaustive: bool) -> list[str]:
    """退出前那几行摘要。**纯函数**，好让单元测试直接钉住措辞。

    「认领上几份」只报单子的落差，而且要说清它是个下界：单子里只装派出不超过
    6 小时的那些，人手动补昨晚的战报时它多半从 0 开始也以 0 结束，
    而那一趟其实认领得好好的（理由见 `BackfillTally.claimed`）。
    """
    mode = "补录（翻到 --since 为止）" if exhaustive else "对账（单子空了就收工）"
    lines = [
        f"完成（{kind} · {mode}）：翻了 {tally.scan.pages} 屏，开了 {tally.scan.opened} 封，"
        f"读通 {tally.read} 份，新入库 {tally.stored} 份",
        f"  单子上到点没战报的派遣：开工 {tally.due_before} 发 → 收工 {tally.due_after} 发"
        f"（认领上 {tally.claimed} 发）",
    ]
    if tally.due_before == 0 and tally.stored:
        lines.append(
            "  ⚠️ 单子开工时就是空的，所以「认领上 0 发」不代表没认领：过期派遣不在单子里，"
            "但战报照样认领得上（窗口相对战报时间算，不是相对现在）"
        )
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        choices=(KIND_PIRATE, KIND_BOT),
        required=True,
        help="补哪条链路的战报：pirate = 海盗攻击报告，bot = 打 bot 的攻击报告",
    )
    parser.add_argument(
        "--since",
        type=parse_day,
        required=True,
        help="只补录这个 UTC 日 00:00 之后的报告（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=BACKFILL_SCAN_PAGES,
        help=f"最多往下翻几屏（默认 {BACKFILL_SCAN_PAGES}，约 {BACKFILL_SCAN_PAGES * 6} 行）",
    )
    parser.add_argument(
        "--max-opens",
        type=int,
        default=BACKFILL_MAX_OPENS,
        help=f"最多打开几封（默认 {BACKFILL_MAX_OPENS}）。这是封顶不是指标",
    )
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="补录模式：一直翻到 --since 为止，不因为单子空了就收工。救过期战报时要它",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 必须在 `parse_args` 之前：argparse 的 `--help` 与「参数写错了」都直接往
    # stderr 写字，绕开 `say()`，而本文件的帮助文本里有 `⚠️`。见那个函数的注释。
    make_console_encoding_safe()
    args = build_parser().parse_args(argv)

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    say(
        f"补录{args.kind}战报：UTC {args.since:%Y-%m-%d} 起，"
        f"最多翻 {args.max_pages} 屏、开 {args.max_opens} 封"
        + ("；补录模式（不早停）" if args.exhaustive else "；对账模式（单子空了就收工）")
    )
    say("⚠️ 这一趟只读信箱、只写库，一发都不派。")

    driver = LiveDriver(allow_actions=True)
    driver.window()

    loop = build_loop(args.kind, driver, make_ocr())
    # 校几何、查会话。与 `PirateLoop.run()` 开头同序，理由见那边。
    loop.prepare_for_mailbox()
    tally = loop.backfill_reports(
        not_before=args.since,
        max_pages=args.max_pages,
        max_opens=args.max_opens,
        exhaustive=args.exhaustive,
    )
    for line in summary_lines(args.kind, tally, exhaustive=args.exhaustive):
        say(line)
    return 0


__all__ = ["KIND_BOT", "KIND_PIRATE", "build_loop", "build_parser", "main", "summary_lines"]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
