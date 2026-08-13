"""把**信箱里已经躺着的**侦察报告补录进库。手动跑的一次性入口。

侦察报告此前从来没进过库：`PirateLoop.collect_scout_reports()` 读成
`PirateScoutReading` 交给判定用一次就丢，进程一退什么都不剩。链路因此每一轮都
当作没侦察过，同样几颗星球被来回重侦——2026-08-11 当天 31 发派遣里 25 发是重复
侦察。活链路那一侧已经补上了自动落库，但**那天已经飞出去的那些报告还躺在信箱里
没被读过**，这个入口就是把它们捞回来的。

    # 补录 UTC+0 今天的（默认）
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_scout_reports

    # 补录指定的某一个 UTC 日（左闭右开：只翻这一天及之后的）
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_scout_reports --since 2026-08-11

    # 信箱里存货多时把预算放大
    .venv/Scripts/python.exe -m evo_helper.tools.backfill_scout_reports --max-opens 80

## 它做什么、不做什么

- **只翻信箱、只读、只写库。** 这条路径上没有任何派遣动作：不侦察、不攻击、
  不取消任务、不领奖励、不删邮件。
- **只切「报告」标签**，别的筛选一个都不碰（用户口径 2026-08-11）。走的是
  `PirateLoop._scan_mail_rows` 那一份共用实现，白名单由
  `tests/unit/tools/test_mailbox_clicks.py` 钉着。
- **要点鼠标。** 开信箱、开邮件、返回都是真实点击，所以 `LiveDriver` 必须
  `allow_actions=True`。这不是「打开了派遣能力」——派遣走的是另外那几个坐标，
  这条路径根本不碰。
- **读不出来就不存**，不存半份，不猜；已经在库里的（按目标 + 报告时间）不重写。

## ⚠️ 跑之前

调度器要**停着**。它随时可能起一轮海盗/bot/扫描去抢鼠标，两边同时点同一个
游戏窗口，谁也读不对。
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from evo_helper.tools.pirate_loop import (
    BACKFILL_MAX_OPENS,
    BACKFILL_SCAN_PAGES,
    LoopOptions,
    PirateLoop,
)
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    make_console_encoding_safe,
    make_ocr,
    say,
)


def parse_day(text: str) -> datetime:
    """`YYYY-MM-DD` → 那一天 UTC+0 的 00:00。

    日界写死成 UTC，与游戏里配额的日界（`domain.scheduler.quota_day_start_utc`）
    和报告上写的时间是同一套——报告时间本来就是 UTC+0 显示的。
    """
    try:
        day = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"日期要写成 YYYY-MM-DD（收到 {text!r}）") from error
    return day.replace(tzinfo=UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since",
        type=parse_day,
        default=None,
        help="只补录这个 UTC 日 00:00 之后的报告；默认是今天（UTC+0）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="不设时间下界，翻到预算用完为止。信箱很深时会很慢",
    )
    parser.add_argument("--max-pages", type=int, default=BACKFILL_SCAN_PAGES)
    parser.add_argument("--max-opens", type=int, default=BACKFILL_MAX_OPENS)
    return parser


def main(argv: list[str] | None = None) -> int:
    make_console_encoding_safe()  # 必须在 parse_args 之前，理由见那个函数
    args = build_parser().parse_args(argv)

    if args.all:
        not_before: datetime | None = None
    elif args.since is not None:
        not_before = args.since
    else:
        now = datetime.now(UTC)
        not_before = now.replace(hour=0, minute=0, second=0, microsecond=0)

    import ctypes

    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    window = "整个信箱（不设时间下界）" if not_before is None else f"UTC {not_before:%Y-%m-%d} 起"
    say(f"补录侦察报告：{window}，最多翻 {args.max_pages} 屏、开 {args.max_opens} 封")
    say("⚠️ 这一趟只读信箱、只写库，一发都不派。请确认调度器是停着的。")

    # 开信箱、开邮件、返回都要真点鼠标，所以必须允许动作。
    # 派遣走的是另外那几个坐标，这条路径根本不碰。
    driver = LiveDriver(allow_actions=True)
    driver.window()

    loop = PirateLoop(driver, make_ocr(), LoopOptions(systems=(), scout=False, attack=False))
    # 校几何、查会话。与 `PirateLoop.run()` 开头同序，理由见那边。
    loop.prepare_for_mailbox()
    read, written = loop.backfill_scout_reports(
        not_before=not_before, max_pages=args.max_pages, max_opens=args.max_opens
    )
    say(f"完成：读通 {read} 份，新入库 {written} 份（其余库里已有）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
