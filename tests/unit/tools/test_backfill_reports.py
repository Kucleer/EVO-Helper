"""战报补录入口：命令行契约、两种模式的早停判据、只读。

## 为什么要有这个入口

`tools.backfill_scout_reports` 只管**侦察报告**（写死 `wanted=ReportKind.SCOUT`），
战报没有对应的入口。2026-08-12 那夜丢掉的 21 份 bot 战报因此没有任何工具能取
回来：活链路的 `reconcile_today` 只翻到当日 UTC 日界，而那 21 发早就掉出了
`due_attack_dispatches` 的 6 小时窗口。

## 本文件守两件事

1. **命令行契约一个字都不能变。** 控制台那侧（`application.backfill.build_command`）
   照它拼命令，改了就对不上——而对不上的表现是子进程 `argparse` 退 2，
   页面上只看得到「补录失败」。
2. **两种模式的早停判据必须分开。** 混成一个的后果是二选一：要么每次点「开始」
   都把 60 封的预算烧满（十几分钟），要么手动补录永远救不回过期的那些。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools import backfill_reports
from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import (
    BACKFILL_MAX_OPENS,
    BACKFILL_SCAN_PAGES,
    BackfillTally,
    LoopOptions,
    MailRow,
    MailScan,
    PirateLoop,
    ReportIngest,
)
from evo_helper.vision.parsers import ReportKind

DAY_START = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
NOON = DAY_START + timedelta(hours=12)


class _Driver:
    def click(self, x: int, y: int, *, label: str = "") -> None:
        return None

    def wait(self, seconds: float) -> None:
        return None


class _Repository:
    def __init__(self, *, due: Sequence[Coordinate] = ()) -> None:
        self.due = list(due)

    def due_attack_dispatches(self, target_kind: str, **_fields: Any) -> list[Any]:
        return [SimpleNamespace(target=target) for target in self.due]


def _row(index: int, kind: ReportKind = ReportKind.PIRATE, *, at: datetime = NOON) -> MailRow:
    return MailRow(
        index=index,
        subject={ReportKind.PIRATE: "海盗攻击报告", ReportKind.ATTACK: "攻击报告"}[kind],
        raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
        reported_at_utc=at,
        kind=kind,
    )


def _loop(
    pages: list[list[MailRow]],
    *,
    cls: type = PirateLoop,
    repository: _Repository | None = None,
    ingest: ReportIngest = ReportIngest.STORED,
) -> tuple[Any, list[int], dict[str, Any]]:
    """只装了「翻一趟信箱读战报」所需零件的循环。第三项记下传给 `_scan_mail_rows` 的预算。"""
    repository = repository or _Repository()
    opened: list[int] = []
    budgets: dict[str, Any] = {}
    loop = cls.__new__(cls)
    loop._options = LoopOptions(systems=(), scout=False, attack=False)
    loop._started_at = NOON
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._ensure_run = lambda: (repository, None)
    loop._reset_to_known_screen = lambda: None
    loop._goto_planet_surface = lambda: True
    loop._dump_frame = lambda name, roi=None: None
    loop._say_mail_badge_reads = lambda: None
    loop._open_mail = lambda: None
    loop._close_mail = lambda: None
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    loop._on_mail_detail = lambda: True
    loop._report_screens = lambda: object()
    loop._ingest_report = lambda row, page: (opened.append(row.index), ingest)[1]
    screens = list(pages)
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []

    inner = loop._scan_mail_rows

    def spy(**kwargs: Any) -> MailScan:
        budgets.update(kwargs)
        return inner(**kwargs)

    loop._scan_mail_rows = spy
    return loop, opened, budgets


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)
    monkeypatch.setattr(backfill_reports, "say", lambda _line: None)


# -- 命令行契约 --------------------------------------------------------------


def test_the_command_line_contract_is_exactly_what_the_console_builds() -> None:
    """控制台按这几个参数拼命令（`application.backfill.build_command`），一个字不能改。

    改了的表现是子进程 `argparse` 退 2，而页面上只看得到「补录失败」——
    最贵的那种不匹配：两边都没错，只是对不上。
    """
    args = backfill_reports.build_parser().parse_args(
        ["--kind", "bot", "--since", "2026-08-12", "--max-pages", "3", "--max-opens", "9"]
    )

    assert args.kind == "bot"
    assert args.since == datetime(2026, 8, 12, tzinfo=UTC)
    assert (args.max_pages, args.max_opens) == (3, 9)
    assert args.exhaustive is False, "默认必须是对账模式：控制台点「开始」时不传这个开关"


def test_the_budgets_default_to_the_backfill_sized_ones() -> None:
    """不给预算时用**补录那两个大的**，不是活链路那两个小的（4 屏 / 8 封）。

    活链路那两个是按「一轮在等 6–8 份报告」定的；补录要把一整天翻出来。
    """
    args = backfill_reports.build_parser().parse_args(["--kind", "pirate", "--since", "2026-08-12"])

    assert (args.max_pages, args.max_opens) == (BACKFILL_SCAN_PAGES, BACKFILL_MAX_OPENS)


@pytest.mark.parametrize("argv", [["--since", "2026-08-12"], ["--kind", "bot"]])
def test_both_kind_and_since_are_required(argv: list[str]) -> None:
    """两个都是必填。少一个就该当场退，而不是拿一个默认值去翻真实信箱。"""
    with pytest.raises(SystemExit):
        backfill_reports.build_parser().parse_args(argv)


def test_an_unknown_kind_is_rejected() -> None:
    """只有 pirate / bot 两档。多打一个字母不该变成「翻了一趟什么都没找到」。"""
    with pytest.raises(SystemExit):
        backfill_reports.build_parser().parse_args(["--kind", "scan", "--since", "2026-08-12"])


def test_the_kind_picks_the_chain_and_with_it_the_report_subject() -> None:
    """`--kind` 决定用哪条链路，而链路决定信箱里认哪一类主题。

    认错主题的后果是**静默的**：海盗战报的主题是「海盗攻击报告」，打 bot 的是
    「攻击报告」，认错了这一趟一封都不会开，日志上只有「不是攻击战报；不打开」。
    """
    driver, ocr = _Driver(), object()

    pirate = backfill_reports.build_loop("pirate", driver, ocr)
    bot = backfill_reports.build_loop("bot", driver, ocr)

    assert type(pirate) is PirateLoop
    assert type(bot) is BotLoop
    assert pirate.RECONCILE_KIND is ReportKind.PIRATE
    assert bot.RECONCILE_KIND is ReportKind.ATTACK
    assert pirate.TARGET_KIND != bot.TARGET_KIND, "单子也要各查各的"


def test_neither_chain_is_built_with_dispatch_powers() -> None:
    """补录是只读的：两条链路都不许带上「真派」那个开关。

    这条与 `LiveDriver(allow_actions=...)` 是两件事——那个管的是「能不能点鼠标」
    （开信箱要点），这个管的是「循环自己会不会去派」。
    """
    driver, ocr = _Driver(), object()

    for kind in (backfill_reports.KIND_PIRATE, backfill_reports.KIND_BOT):
        loop = backfill_reports.build_loop(kind, driver, ocr)
        assert loop._options.attack is False
        assert loop._options.scout is False


# -- 两种模式的早停 ----------------------------------------------------------


def test_the_default_mode_stops_once_the_worklist_is_empty() -> None:
    """⚠️ **这条是它能挂在「开始」按钮上的前提。**

    控制台每次点「开始」都会先跑这个入口。撞见一封库里已有、而单子上没有欠账时
    就收工——于是 `--max-opens 60` 是**封顶而不是指标**：没有欠账时几十秒走完。
    没有这条，用户每按一次开始都要等十几分钟。
    """
    loop, opened, _budgets = _loop(
        [[_row(index) for index in range(4)]],
        repository=_Repository(due=[]),
        ingest=ReportIngest.KNOWN,
    )

    tally = loop.backfill_reports(not_before=DAY_START)

    assert opened == [0], "第一封就是库里已有的，单子又是空的，不该再往下开"
    assert tally.scan.opened == 1


def test_a_pending_worklist_keeps_the_default_mode_going() -> None:
    """单子上还有到点没战报的，就不能因为撞见一份已入库的就收工。

    判据与开工那一趟是**同一份**（`_stop_after_known`）：早停假定「库里已有 ⇒
    往下都读过了」，而这个假定在「报告已入库、却没接到该接的那一发上」时是假的。
    """
    loop, opened, _budgets = _loop(
        [[_row(index) for index in range(4)]],
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
        ingest=ReportIngest.KNOWN,
    )

    loop.backfill_reports(not_before=DAY_START)

    assert opened == [0, 1, 2, 3]


def test_exhaustive_mode_ignores_the_worklist() -> None:
    """⚠️ **补录模式的落点。** 救过期战报时，单子空不空都得接着翻。

    那 21 发早就掉出了单子（`due_attack_dispatches` 只装派出不超过 6 小时的），
    所以对账模式在第一封「库里已有」就会收工，一封都开不了。
    """
    loop, opened, _budgets = _loop(
        [[_row(index) for index in range(4)]],
        repository=_Repository(due=[]),
        ingest=ReportIngest.KNOWN,
    )

    loop.backfill_reports(not_before=DAY_START, exhaustive=True)

    assert opened == [0, 1, 2, 3]


def test_exhaustive_mode_still_stops_at_the_since_date() -> None:
    """补录模式不是「翻到天荒地老」：`--since` 仍然是硬下界。

    列表按时间倒序，翻到第一行比 `--since` 还早的就收工。没有这条，
    `--exhaustive` 会一路把 12 屏 / 60 封的预算烧满，而下面全是无关的旧报告。
    """
    loop, opened, _budgets = _loop(
        [
            [_row(0), _row(1, at=DAY_START - timedelta(minutes=1))],
            [_row(0)],  # 不该翻到这一屏
        ],
        ingest=ReportIngest.STORED,
    )

    loop.backfill_reports(not_before=DAY_START, exhaustive=True)

    assert opened == [0]


def test_the_budgets_reach_the_mailbox_scan() -> None:
    """传进来的预算要真的落到那一趟信箱上，而不是被默认值盖掉。"""
    loop, _opened, budgets = _loop([[_row(0)]])

    loop.backfill_reports(not_before=DAY_START, max_pages=7, max_opens=5)

    assert budgets["max_pages"] == 7
    assert budgets["max_opens"] == 5


def test_the_backfill_never_records_a_daily_reconciliation() -> None:
    """补录**不写 `daily_reconciliations`**：它往回翻到昨天，数出来的「今天」是错的。

    那张表按 UTC 日取大，写进去就抹不掉；而它正是「今日 X/32」的来源。
    仓储替身故意没有 `record_daily_reconciliation`，写了就 AttributeError。
    """
    loop, _opened, _budgets = _loop([[_row(0)]])

    loop.backfill_reports(not_before=DAY_START)  # 不抛异常即通过


# -- 摘要 --------------------------------------------------------------------


def summary(tally: BackfillTally) -> list[str]:
    return backfill_reports.summary_lines("bot", tally, exhaustive=True)


def test_the_summary_reports_every_number_the_caller_asked_for() -> None:
    """退出前要说清：翻了几屏、开了几封、读通几份、新入库几份、认领上几份。"""
    tally = BackfillTally(
        scan=MailScan(pages=3, opened=5), read=4, stored=2, due_before=6, due_after=2
    )

    text = "\n".join(backfill_reports.summary_lines("bot", tally, exhaustive=False))

    assert "翻了 3 屏" in text
    assert "开了 5 封" in text
    assert "读通 4 份" in text
    assert "新入库 2 份" in text
    assert "认领上 4 发" in text


def test_the_summary_says_which_mode_it_ran() -> None:
    """两种模式翻到哪为止完全不同，摘要必须说清跑的是哪一种。

    不说的话，「读通 0 份」既可能是「没有欠账，正常早停」，也可能是
    「该补的没补到」——而这两件事的下一步操作相反。
    """
    tally = BackfillTally()

    assert "对账" in backfill_reports.summary_lines("bot", tally, exhaustive=False)[0]
    assert "补录" in backfill_reports.summary_lines("bot", tally, exhaustive=True)[0]


def test_an_empty_worklist_gets_a_caveat_instead_of_a_silent_zero() -> None:
    """⚠️ 「认领上 0 发」不等于没认领——这句提示不能省。

    单子只装派出不超过 6 小时的那些，人手动补昨晚的战报时它多半从 0 开始也以 0
    结束。而认领窗口是 `dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，
    相对**战报自己的时间戳**算的，所以那一趟其实认领得好好的。
    没有这句，用户看到 0 会以为补录白跑了。
    """
    stored = BackfillTally(due_before=0, due_after=0, stored=3)
    nothing = BackfillTally(due_before=0, due_after=0, stored=0)

    assert any("不代表没认领" in line for line in summary(stored))
    assert not any("不代表没认领" in line for line in summary(nothing)), (
        "一份都没入库时不该冒出这句安慰话"
    )


def test_the_claimed_count_never_goes_negative() -> None:
    """单子在这一趟里变长（别的进程又派了几发）时，认领数记 0 而不是负数。"""
    assert BackfillTally(due_before=1, due_after=4).claimed == 0
