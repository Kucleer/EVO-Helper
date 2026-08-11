"""一趟信箱的窗口：翻多少、开哪几封、什么时候停。

## 这是 bot 探路战报一整天收不回来的**正因**

实机 2026-08-11：六发探路在 09:07–09:11 派出去，此后四趟收取（09:14 / 09:19 /
09:24 / 09:30）全部报「翻不到」，`battle_reports` 一行没涨。而收取那一趟
确实在跑，信箱也确实进去了——问题在窗口：

    09:30:11 等探路战报的目标 6 个；进一趟信箱去收
    09:31:34   2:320:11 的探路战报还没出现在信箱最上面几行；这一趟不动它
    …（六个目标同一句）

两条链路的报告混在同一个收件箱里按时间倒序排，而海盗链路整夜都在产出攻击报告。
原先的写法是**盲开最上面 6 行**：不看主题、一封一封点开（每封 ≈8 秒：点开等
2.4s + 详情 OCR + 返回等 2.0s），于是那 6 次开封的预算全花在别人的报告上，
真正要找的那几份还在第 7 行往下。海盗链路的日志里同一件事说得很直白：

    第 0 行读不出侦察报告：这不是侦察报告：主题读作 '攻击报告'
    第 3 行读不出侦察报告：这不是侦察报告：主题读作 '攻击报告'
    第 5 行读不出侦察报告：这不是侦察报告：主题读作 '攻击报告'

## 取舍：先按主题筛，再把窗口开大，最后按时间早停

- **先筛后开。** 读一屏 6 行主题 = 一次截图加六次窄 ROI OCR；开一封 ≈ 8 秒。
  差一个量级，所以只要能排掉一行就已经回本。
- **筛错要往「开」的一侧倒。** 主题读不出、认不出（`UNKNOWN`）一律照开：
  漏开一封 = 这一轮少一份报告，多开一封 = 多花八秒。
- **窗口不再钉死 6 行。** 筛掉的行不花开封预算，于是同样的时间能多翻几屏。
  翻屏的落点没法标定，所以停止条件是「这一屏还有没有没见过的行」，
  行的身份取自它自己的主题+时间。
- **按时间早停。** 列表按时间倒序，翻到比「最早那一发的派出时刻」还早的报告，
  往下就不可能再有本轮的报告了。这是把窗口开大之后仍然不会翻一整个信箱的闸门。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools.pirate_loop import (
    MAIL_MAX_OPENS,
    MAIL_SCAN_PAGES,
    LoopOptions,
    MailRow,
    PirateLoop,
    mail_row_from_text,
)
from evo_helper.vision.parsers import ReportKind

NOW = datetime(2026, 8, 11, 1, 30, tzinfo=UTC)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, seconds: float) -> None:
        return None


def _row(index: int, kind: ReportKind, *, minutes_ago: int = 0) -> MailRow:
    moment = NOW - timedelta(minutes=minutes_ago)
    return MailRow(
        index=index,
        subject={
            ReportKind.SCOUT: "侦察报告",
            ReportKind.ATTACK: "攻击报告",
            ReportKind.PIRATE: "海盗攻击报告",
            ReportKind.UNKNOWN: "???",
        }[kind],
        raw_time_text=moment.strftime("%d/%m/%Y %H:%M:%S"),
        reported_at_utc=moment,
        kind=kind,
    )


def _loop(pages: list[list[MailRow]]) -> tuple[Any, list[str], list[MailRow]]:
    """一个只装了「翻信箱」所需零件的 `PirateLoop`。`pages` 是每一屏的列表行。"""
    events: list[str] = []
    opened: list[MailRow] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._started_at = NOW
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._reset_to_known_screen = lambda: None
    loop._goto_planet_surface = lambda: True
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")
    loop._open_mail = lambda: events.append("开信箱")
    loop._close_mail = lambda: events.append("关信箱")
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    loop._on_mail_detail = lambda: True
    loop._report_screens = lambda: object()
    screens = list(pages)
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []
    return loop, events, opened


@pytest.fixture(autouse=True)
def _no_dragging(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)


# -- 主题：先筛后开 ----------------------------------------------------------


def test_rows_of_the_wrong_kind_are_never_opened() -> None:
    """整屏的攻击报告，一封都不该点开。

    这就是白花掉的那 6 次开封（≈50 秒），也正是把真报告挤出窗口之后仍然
    「翻了个遍」的假象来源。
    """
    loop, _events, opened = _loop([[_row(index, ReportKind.ATTACK) for index in range(6)]])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert opened == []
    assert loop._driver.clicks.count("打开邮件") == 0


def test_only_the_matching_rows_spend_the_open_budget() -> None:
    """预算全花在候选行上：三份攻击报告混在中间，开的还是那三份侦察报告。"""
    rows = [
        _row(0, ReportKind.ATTACK),
        _row(1, ReportKind.SCOUT),
        _row(2, ReportKind.ATTACK),
        _row(3, ReportKind.SCOUT),
        _row(4, ReportKind.ATTACK),
        _row(5, ReportKind.SCOUT),
    ]
    loop, _events, opened = _loop([rows])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert [row.index for row in opened] == [1, 3, 5]


def test_an_unreadable_subject_is_opened_anyway() -> None:
    """**筛错要往「开」的一侧倒。**

    漏开一封 = 这一轮少一份报告（侦察白飞、探路白派）；多开一封 = 多花八秒。
    主题读不出来（`UNKNOWN`）时照开，真正的归属判定在打开之后。
    """
    loop, _events, opened = _loop([[_row(0, ReportKind.UNKNOWN)]])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert [row.index for row in opened] == [0]


def test_a_pirate_battle_report_is_not_a_bot_attack_report() -> None:
    """`海盗攻击报告` 含有 `攻击报告` 这个子串，但它是海盗战。

    bot 那条链路找的是打 bot 的攻击报告；把海盗战也开一遍只是白花预算。
    """
    loop, _events, opened = _loop([[_row(0, ReportKind.PIRATE), _row(1, ReportKind.ATTACK)]])

    loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击战报",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert [row.index for row in opened] == [1]


# -- 窗口：翻得比一屏更远 ----------------------------------------------------


def test_the_window_reaches_past_the_first_screen() -> None:
    """**这一条就是那个正因。** 第一屏全是别人的报告，要找的在第二屏。

    原先窗口钉死在最上面 6 行，于是这份报告永远翻不到——而日志只会说
    「还没出现在信箱最上面几行」，听起来像是报告还没到。

    顺带钉住**行身份取主题+时间、不取行号**：第二屏的这一行也是「第 0 行」，
    按行号去重就会把它当成第一屏那行、直接跳过，翻屏等于白翻。
    """
    loop, _events, opened = _loop(
        [
            [_row(index, ReportKind.ATTACK) for index in range(6)],
            [_row(0, ReportKind.SCOUT)],
        ]
    )

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert len(opened) == 1


def test_a_screen_with_nothing_new_stops_the_paging() -> None:
    """翻屏靠慢拖，落点不可标定，所以停止条件是「还有没有没见过的行」。

    面板夹住了、或者已经到底，拖了也不动——那时同一批行会再读一遍。
    认不出「这些我见过」就会一直重开同一封，每重开一封白花八秒。
    """
    same = [_row(0, ReportKind.SCOUT)]
    loop, _events, opened = _loop([list(same), list(same), list(same)])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert len(opened) == 1, "同一封邮件被重开了——每重开一封白花八秒"


def test_the_trip_never_exceeds_the_page_budget() -> None:
    """一趟的时长要有界：翻屏数封顶，剩下的留给下一趟。"""
    pages = [[_row(0, ReportKind.PIRATE, minutes_ago=page)] for page in range(MAIL_SCAN_PAGES + 3)]
    loop, _events, _opened = _loop(pages)
    read: list[int] = []
    original = loop._mail_list_rows
    loop._mail_list_rows = lambda: (read.append(1), original())[1]

    loop._scan_mail_rows(wanted=ReportKind.SCOUT, label="侦察报告", visit=lambda row, page: False)

    assert len(read) <= MAIL_SCAN_PAGES


def test_the_trip_never_exceeds_the_open_budget() -> None:
    """开封数同样封顶：主题若整体读偏，最多多花几十秒，而不是把整轮拖垮。"""
    pages = [
        [_row(index, ReportKind.SCOUT, minutes_ago=page * 6 + index) for index in range(6)]
        for page in range(MAIL_SCAN_PAGES)
    ]
    loop, _events, opened = _loop(pages)

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert len(opened) <= MAIL_MAX_OPENS


# -- 早停：列表按时间倒序 ----------------------------------------------------


def test_a_row_older_than_the_floor_ends_the_trip() -> None:
    """翻到比要找的那几发还早的报告就收工——往下不可能再有本轮的。

    没有这道闸门，把窗口开大就等于每趟都往回翻一整个信箱。
    """
    loop, _events, opened = _loop(
        [
            [
                _row(0, ReportKind.SCOUT, minutes_ago=1),
                _row(1, ReportKind.SCOUT, minutes_ago=90),
                _row(2, ReportKind.SCOUT, minutes_ago=95),
            ]
        ]
    )

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        not_before=NOW - timedelta(minutes=30),
    )

    assert [row.index for row in opened] == [0]


def test_a_row_without_a_readable_time_never_triggers_the_early_stop() -> None:
    """时间读不出来就不敢停：停错的代价是把没翻到的报告永久判成「不在信箱里」。"""
    unreadable = MailRow(
        index=0, subject="侦察报告", raw_time_text=None, reported_at_utc=None, kind=ReportKind.SCOUT
    )
    loop, _events, opened = _loop([[unreadable, _row(1, ReportKind.SCOUT, minutes_ago=1)]])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        not_before=NOW - timedelta(minutes=30),
    )

    assert [row.index for row in opened] == [0, 1]


def test_visiting_reports_done_stops_the_trip() -> None:
    """要的都收齐了就别再往下翻——剩下的每一封都是白花八秒。"""
    loop, _events, opened = _loop([[_row(index, ReportKind.SCOUT) for index in range(6)]])

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or True,
    )

    assert len(opened) == 1


# -- 详情页要真的铺开 --------------------------------------------------------


def test_a_detail_page_that_never_renders_is_not_read() -> None:
    """**点开之后要等详情页真的铺开，铺不开就一个字都不读。**

    面板是滑进来的：`_settle` 的注释记着「等 2.4 秒判一次判不到，而失败时存下的
    那一帧读得清清楚楚」。原先这里是点一下、死等 2.4 秒、读一次就走——没铺开的
    那一屏读出来是一堆读不通的字，和「这封是别人的报告」在下游长得一模一样，
    于是一份真在信箱里的报告被静默丢掉。判据 `_on_mail_detail` 早就写好了，
    只是从来没有人调用过。
    """
    loop, events, opened = _loop([[_row(0, ReportKind.SCOUT)]])
    loop._settle = lambda predicate, **_kwargs: predicate is not loop._on_mail_detail

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert opened == []
    assert "存图:mail-detail-unrendered" in events


def test_the_row_is_closed_again_even_when_the_detail_never_rendered() -> None:
    """铺不开也要退回列表——不退回去，下一下就照列表的行坐标点在详情页上。"""
    loop, _events, _opened = _loop([[_row(0, ReportKind.SCOUT)]])
    loop._settle = lambda predicate, **_kwargs: predicate is not loop._on_mail_detail

    loop._scan_mail_rows(wanted=ReportKind.SCOUT, label="侦察报告", visit=lambda row, page: False)

    assert loop._driver.clicks == ["打开邮件", "返回"]


def test_the_mailbox_is_always_closed() -> None:
    """不关信箱，下一个目标的 `goto` 会在浮层上朝导航栏坐标盲点。"""
    loop, events, _opened = _loop([[_row(0, ReportKind.ATTACK)]])

    loop._scan_mail_rows(wanted=ReportKind.SCOUT, label="侦察报告", visit=lambda row, page: False)

    assert events[-1] == "关信箱"


# -- 列表行怎么读成 MailRow --------------------------------------------------


def test_the_subject_is_not_taken_from_the_first_line() -> None:
    """`--psm 6` 在这块 ROI 上不保证行序，而时间那一行的形状是唯一确定的。

    所以先认出时间行剔掉，剩下的全拼起来当主题——`classify_report_subject`
    是子串判定，多拼几个字不改变结论，而漏掉主题那一行会。
    """
    row = mail_row_from_text(2, "11/08/2026 09:12:03\nSystem\n侦察报告")

    assert row.kind is ReportKind.SCOUT
    assert row.raw_time_text == "11/08/2026 09:12:03"
    assert row.reported_at_utc == datetime(2026, 8, 11, 9, 12, 3, tzinfo=UTC)


def test_a_row_without_a_time_still_reads_its_subject() -> None:
    """时间读不出来不影响筛主题；它只是不参与早停，也只按主题去重。"""
    row = mail_row_from_text(0, "攻击报告\nSystem")

    assert row.kind is ReportKind.ATTACK
    assert row.raw_time_text is None
    assert row.reported_at_utc is None
