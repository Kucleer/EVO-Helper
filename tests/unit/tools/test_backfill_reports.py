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
    ExpectedReportWindows,
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
    def __init__(
        self,
        *,
        due: Sequence[Coordinate] = (),
        dispatches: Sequence[Any] | None = None,
        scan_hours: int | None = None,
    ) -> None:
        self.due = list(due)
        self.dispatches = list(dispatches) if dispatches is not None else None
        self.scan_hours = scan_hours

    def due_attack_dispatches(self, target_kind: str, **_fields: Any) -> list[Any]:
        if self.dispatches is not None:
            return self.dispatches
        return [SimpleNamespace(target=target) for target in self.due]

    def military_attack_config(self) -> Any:
        """攻击配置页那一行。`scan_hours=None` = 页面上留空。"""
        return SimpleNamespace(report_scan_hours=self.scan_hours)


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
    # 拖回顶部另有专文（`test_mailbox_scroll_to_top.py`）；不打桩会吃掉 `screens`。
    loop._scroll_mail_list_to_top = lambda: None
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
    monkeypatch.setattr(pirate_loop, "record_system_log", lambda *args, **kwargs: None)


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

    tally = loop.backfill_reports(not_before=DAY_START, now=NOON)

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

    loop.backfill_reports(not_before=DAY_START, now=NOON)

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

    loop.backfill_reports(not_before=DAY_START, max_pages=7, max_opens=5, now=NOON)

    assert budgets["max_pages"] == 7
    assert budgets["max_opens"] == 5


def test_the_backfill_never_records_a_daily_reconciliation() -> None:
    """补录**不写 `daily_reconciliations`**：它往回翻到昨天，数出来的「今天」是错的。

    那张表按 UTC 日取大，写进去就抹不掉；而它正是「今日 X/32」的来源。
    仓储替身故意没有 `record_daily_reconciliation`，写了就 AttributeError。
    """
    loop, _opened, _budgets = _loop([[_row(0)]])

    loop.backfill_reports(not_before=DAY_START, now=NOON)  # 不抛异常即通过


# -- 按预计时刻预筛 ----------------------------------------------------------


def test_expected_report_windows_keep_the_late_side_for_whole_minutes() -> None:
    """整分钟不是精确到秒的飞行时长：OCR 可能吃掉了 0–59 秒。

    这条故意用窄窗口会漏掉的 +60 秒样本钉住分档；若将来上游修好丢秒，再连同
    这段与实现一起删，而不是悄悄把它收成对称 ±10 秒。
    """
    windows = ExpectedReportWindows.from_dispatches(
        [
            SimpleNamespace(
                dispatched_at_utc=DAY_START,
                expected_report_at_utc=DAY_START + timedelta(minutes=62),
            )
        ]
    )

    assert windows.matches(DAY_START + timedelta(minutes=63))
    assert not windows.matches(DAY_START + timedelta(minutes=63, seconds=11))


def test_expected_report_windows_keep_the_normal_window_narrow() -> None:
    """读全秒数的那一族不许为了极少数异常退回顺序盲开。"""
    expected = DAY_START + timedelta(minutes=62, seconds=6)
    windows = ExpectedReportWindows.from_dispatches(
        [SimpleNamespace(dispatched_at_utc=DAY_START, expected_report_at_utc=expected)]
    )

    assert windows.matches(expected + timedelta(seconds=10))
    assert not windows.matches(expected + timedelta(seconds=11))


def test_backfill_opens_only_rows_in_the_expected_time_windows() -> None:
    """时刻只作为开封预筛，命中行仍由详情里的坐标归属判定。"""
    dispatch = SimpleNamespace(
        target=Coordinate(2, 56, 20),
        dispatched_at_utc=NOON - timedelta(minutes=62, seconds=6),
        expected_report_at_utc=NOON,
    )
    loop, opened, _budgets = _loop(
        [[_row(0, at=NOON + timedelta(seconds=30)), _row(1, at=NOON + timedelta(seconds=5))]],
        repository=_Repository(dispatches=[dispatch]),
    )

    loop.backfill_reports(not_before=DAY_START, now=NOON)

    assert opened == [1]


def test_an_unreadable_list_timestamp_is_still_opened_with_time_windows() -> None:
    """时间 OCR 读不出时不敢筛掉，避免把唯一一封战报永久留在信箱。"""
    dispatch = SimpleNamespace(
        target=Coordinate(2, 56, 20),
        dispatched_at_utc=NOON - timedelta(minutes=62, seconds=6),
        expected_report_at_utc=NOON,
    )
    blind = MailRow(0, "海盗攻击报告", None, None, ReportKind.PIRATE)
    loop, opened, _budgets = _loop(
        [[blind, _row(1, at=NOON + timedelta(seconds=30))]],
        repository=_Repository(dispatches=[dispatch]),
    )

    loop.backfill_reports(not_before=DAY_START, now=NOON)

    assert opened == [0]


# -- 时间下限（攻击配置页可配） ----------------------------------------------
#
# 用户口径（2026-08-17）：活动期间信箱最上面堆着几百封活动战报，而库里最近一封
# 战报停在好几天前，于是「撞见库里已有的那一封」这个早停迟迟不触发，对账那一趟
# 把翻页预算整个烧满。「不要读那么多，毕竟数量是大几百封」「这个参数改为可配置，
# 这样遇到活动我可以灵活调整」。


def test_the_routine_pass_stops_at_rows_older_than_the_floor() -> None:
    """对账翻到比下限更旧的那一行就收工——**断言实际读了几行**。

    只断言「返回了结果」是测不出这条的：不设下限时它照样返回，只是多翻了几屏
    几百封。所以这里数的是 `observed`（每一行都会经过它）与开封数。
    """
    loop, opened, _budgets = _loop(
        [
            # 前两行在 6 小时窗口内，第三行是 7 小时前的——列表按时间倒序，
            # 翻到它就该收工，它后面那一行连开都不该开。
            [
                _row(0, at=NOON - timedelta(hours=1)),
                _row(1, at=NOON - timedelta(hours=2)),
                _row(2, at=NOON - timedelta(hours=7)),
                _row(3, at=NOON - timedelta(hours=8)),
            ],
            [_row(0)],  # 不该翻到这一屏
        ],
        # 单子非空，所以「撞见库里已有的」那条早停在这一趟里不会触发——
        # 停下来的只可能是时间下限。
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
        ingest=ReportIngest.KNOWN,
    )

    tally = loop.backfill_reports(not_before=DAY_START, now=NOON)

    assert opened == [0, 1], "7 小时前那两行不该开封"
    assert tally.scan.pages == 1, "第一屏就该收工，第二屏一屏都不该翻"


def test_an_unreadable_timestamp_never_triggers_the_early_stop() -> None:
    """⚠️ **时间读不出来的行一律不早停。**（既有取舍，`MailRow.is_older_than`）

    停错的代价是把还没翻到的战报永久判成「不在信箱里」；多翻一屏只花一两秒。
    OCR 糊掉一行时间在实机上一点都不罕见。
    """
    blind = MailRow(
        index=1,
        subject="海盗攻击报告",
        raw_time_text=None,
        reported_at_utc=None,
        kind=ReportKind.PIRATE,
    )
    loop, opened, _budgets = _loop(
        [[_row(0, at=NOON - timedelta(hours=1)), blind, _row(2, at=NOON - timedelta(hours=2))]],
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
        ingest=ReportIngest.KNOWN,
    )

    loop.backfill_reports(not_before=DAY_START, now=NOON)

    assert opened == [0, 1, 2], "读不出时间的那一行不该把整趟停在半路"


def test_an_empty_setting_falls_back_to_six_hours() -> None:
    """留空 = 6 小时。**钉死具体数字**，不去引那个常量。

    断言 `floor == now - DEFAULT_REPORT_SCAN_FLOOR` 的话，把默认值改成 0 小时
    （＝一封都翻不到）这条用例照样绿。
    """
    loop, _opened, budgets = _loop([[_row(0)]], repository=_Repository(scan_hours=None))

    loop.backfill_reports(not_before=None, now=NOON)

    assert budgets["not_before"] == NOON - timedelta(hours=6)


def test_a_configured_setting_wins_over_the_default() -> None:
    """配了 N 小时就按 N 小时停。活动期间用户要调的正是这个数。"""
    loop, _opened, budgets = _loop([[_row(0)]], repository=_Repository(scan_hours=2))

    loop.backfill_reports(not_before=None, now=NOON)

    assert budgets["not_before"] == NOON - timedelta(hours=2)


def test_the_floor_only_ever_tightens_the_since_date() -> None:
    """`--since` 是硬下界，这道下限只让它更紧，绝不把它顶开。

    反过来（取更早的那个）会让一个配大了的时长翻到用户根本没要的日期去。
    """
    loop, _opened, budgets = _loop([[_row(0)]], repository=_Repository(scan_hours=240))

    loop.backfill_reports(not_before=NOON - timedelta(hours=3), now=NOON)

    assert budgets["not_before"] == NOON - timedelta(hours=3)


def test_a_missing_configuration_row_falls_back_instead_of_failing() -> None:
    """老库、或者 `ensure_mission_rows()` 还没跑：当成留空，别把整趟对账停掉。"""
    repository = _Repository()
    repository.military_attack_config = _raise_not_initialised  # type: ignore[method-assign]
    loop, _opened, budgets = _loop([[_row(0)]], repository=repository)

    loop.backfill_reports(not_before=None, now=NOON)

    assert budgets["not_before"] == NOON - timedelta(hours=6)


@pytest.mark.parametrize("stored", [0, -3, True, "两小时", None])
def test_a_nonsense_stored_value_falls_back_to_the_default(stored: object) -> None:
    """库里那个值也要复核：0 / 负数 / 不是整数一律当留空。

    页面那把尺子管不到直接改库的人，而下界落在「此刻」之后的后果是**一封都翻不到，
    还一声不响**。
    """
    loop, _opened, budgets = _loop(
        [[_row(0)]],
        repository=_Repository(scan_hours=stored),  # type: ignore[arg-type]
    )

    loop.backfill_reports(not_before=None, now=NOON)

    assert budgets["not_before"] == NOON - timedelta(hours=6)


def test_the_exhaustive_backfill_is_never_touched_by_the_floor() -> None:
    """⚠️⚠️ **本次改动最要紧的一条。**

    `--exhaustive` 手动补录存在的唯一理由，就是够到那些早就掉出追踪窗口的历史
    战报。下限要是也作用在它身上，这个入口就被悄悄废掉了——**而且不报错**：
    用户会看到一趟「完成」的补录和一句「读通 0 份」，然后以为信箱里真的没有。
    """
    loop, opened, budgets = _loop(
        # 配一个极紧的下限（1 小时），而这几封都是几小时前的：对账那一档到这里
        # 一封都开不了，补录必须照开不误（它们仍在 `--since` 之内）。
        [[_row(index, at=NOON - timedelta(hours=2 + index)) for index in range(4)]],
        repository=_Repository(due=[], scan_hours=1),
        ingest=ReportIngest.KNOWN,
    )

    loop.backfill_reports(not_before=DAY_START, exhaustive=True, now=NOON)

    assert budgets["not_before"] == DAY_START, "补录的下界只能是 --since 那一个"
    assert opened == [0, 1, 2, 3], "配置页上那个框不许够到补录"


def _raise_not_initialised() -> Any:
    raise ValueError("military_attack_config 还没初始化；先调 ensure_mission_rows()")


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


def test_a_trip_that_was_cut_short_does_not_say_it_finished() -> None:
    """**实机 2026-08-13 20:35。**

    一趟给了 30 屏预算的补录在第 3 屏丢了邮件列表、当场中止，打出来的却是

        完成（bot · 补录（翻到 --since 为止））：翻了 3 屏，开了 3 封，读通 1 份…

    ——一行**长得完全像成功**的话，而那一趟要救的 21 份战报一份都没碰到。
    「翻了 3 屏」本身没撒谎，但只有知道预算是 30 屏的人才看得出不对劲，
    而看摘要的人恰恰是不看命令行的那个人。
    """
    scan = MailScan(pages=3, opened=3, cut_short="丢了邮件列表")
    tally = BackfillTally(scan=scan, read=1, stored=1)

    lines = summary(tally)

    assert lines[0].startswith("中断"), "头一个字就要说清这一趟没走完"
    assert any("没走完" in line and "重跑" in line for line in lines), "还要说下一步该干什么"


def test_a_trip_that_ran_its_course_still_says_it_finished() -> None:
    """没有这条对照，「一律说中断」也能让上面那条变绿。"""
    tally = BackfillTally(scan=MailScan(pages=3, opened=3), read=1, stored=1)

    lines = summary(tally)

    assert lines[0].startswith("完成")
    assert not any("没走完" in line for line in lines)
