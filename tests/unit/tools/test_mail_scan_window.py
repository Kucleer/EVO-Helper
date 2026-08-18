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
  行的身份取自它自己的**时间那一格**（`MailRow.identity`；原先取主题 + 时间，
  被 OCR 噪声打穿，见文末那一节）。
- **按时间早停。** 列表按时间倒序，翻到比「最早那一发的派出时刻」还早的报告，
  往下就不可能再有本轮的报告了。这是把窗口开大之后仍然不会翻一整个信箱的闸门。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools.pirate_loop import (
    MAIL_MAX_OPENS,
    MAIL_MAX_REENTRIES,
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
    # 拖回顶部是另一件事，另有专文（`test_mailbox_scroll_to_top.py`）。不打桩的话
    # 它会把 `pages` 里的几屏当成「拖回顶部」的读数吃掉。
    loop._scroll_mail_list_to_top = lambda: events.append("拖回顶部")
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

    顺带钉住**行身份取时间那一格、不取行号**：第二屏的这一行也是「第 0 行」，
    按行号去重就会把它当成第一屏那行、直接跳过，翻屏等于白翻。

    ⚠️ 每一行给一个**各不相同**的时刻，因为信箱按时间倒序排——排在第二屏的这一封
    必然比第一屏那六封更早。原先这七行全用 `minutes_ago` 的默认值 0，也就是七封
    邮件同处一秒，而实拍上同秒最多是一对（`MailRow.identity` 里那段取舍）。
    """
    loop, _events, opened = _loop(
        [
            [_row(index, ReportKind.ATTACK, minutes_ago=index) for index in range(6)],
            [_row(0, ReportKind.SCOUT, minutes_ago=6)],
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


# -- 掉出列表：重进接着翻，不是中止整趟 --------------------------------------


class _ListFlapping:
    """让「还在列表上吗」这一问在指定的第几次返回 False。"""

    def __init__(self, on_list, fail_on: set[int]) -> None:
        self._on_list = on_list
        self._fail_on = fail_on
        self.asked = 0

    def __call__(self, predicate, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        if predicate is not self._on_list:
            return True
        self.asked += 1
        return self.asked not in self._fail_on


def test_losing_the_list_re_enters_the_mailbox_instead_of_ending_the_trip() -> None:
    """**实机 2026-08-13 20:33 的那一趟。**

    补录翻到第 3 屏时点开一封主题被 OCR 糊掉的侦察报告，详情页标题读到「侦察」
    而不是「消息」，判据正确地拒了它；但接着那一下 `MAIL_BACK` 落在一个不是详情页
    的画面上——那个坐标身兼两职（在 `_reset_to_known_screen` 里它是「关闭面板」），
    整个信箱被关掉了。

    原先的处置是当场 `break`：**30 屏的预算只走了 3 屏，却打印出一行长得像成功的
    「完成（补录）：翻了 3 屏」**，而那一趟要救的 21 份战报一份都没碰到。
    """
    pages = [
        [_row(0, ReportKind.SCOUT, minutes_ago=1)],
        [_row(1, ReportKind.SCOUT, minutes_ago=2)],
        [_row(2, ReportKind.SCOUT, minutes_ago=3)],
    ]
    loop, events, opened = _loop(pages)
    # ⚠️ 桩掉 `_open_mail_row`：它自己也要问「还在列表上吗」（退回列表要确认，
    # 见那个方法），而这条用例是**按第几次问**来制造失败的。不桩掉的话，
    # 计数会跟着开封次数漂，这条用例就变成了在测另一件事。
    # 开封那一侧由 `test_a_back_click_that_missed_is_clicked_again` 单独守。
    loop._open_mail_row = lambda row, visit: bool(visit(row, object()))
    loop._settle = _ListFlapping(loop._on_mail_list, fail_on={2})

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=6,
    )

    assert events.count("开信箱") == 2, "掉出列表之后必须重进一次"
    assert [row.index for row in opened] == [0, 1, 2], "重进之后要接着把剩下的翻完"


def test_a_mailbox_that_keeps_falling_out_eventually_gives_up() -> None:
    """重进也是要花钱的（重新进信箱 + 从顶部重扫），不能无限试。

    连着掉出列表说明画面已经不是「偶尔掉一下」那种情形，接着试只是在一个认不出的
    画面上多点几下。
    """
    loop, events, opened = _loop([[_row(0, ReportKind.SCOUT)] for _ in range(8)])
    loop._open_mail_row = lambda row, visit: bool(visit(row, object()))  # 同上一条的理由
    loop._settle = _ListFlapping(loop._on_mail_list, fail_on=set(range(1, 20)))

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=8,
    )

    assert events.count("开信箱") == 1 + MAIL_MAX_REENTRIES
    assert events[-1] == "关信箱", "放弃也要正常收尾，不能把信箱开着走人"


def test_a_back_click_that_missed_is_clicked_again() -> None:
    """**退回列表要确认，不是点一下就走。**

    实机 2026-08-13：点开一封主题被 OCR 糊掉的侦察报告，详情页标题读到「侦察」
    不是「消息」，判据正确地拒了它；但接着那一下 `MAIL_BACK` 落在一个不是详情页的
    画面上，而那个坐标身兼两职（也是「关闭面板」），于是整个信箱被关掉。

    代价不是丢一封，是丢**一整趟**：那一趟 30 屏的预算有 12 屏花在重进后的重扫上，
    两次重进用尽仍然没走到要救的战报那里。这里多花一次读屏确认便宜得多。
    """
    loop, _events, opened = _loop([[_row(0, ReportKind.SCOUT)]])
    # 只摆布「还在列表上吗」这一问：进循环时答是，第一次退回后答否，补点之后答是。
    # 详情页那一问一律答是——这条守的是退回，不是详情页判据。
    on_list = iter([True, False, True])
    loop._settle = lambda predicate, **_kwargs: (
        next(on_list, True) if predicate is loop._on_mail_list else True
    )

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=1,
    )

    assert loop._driver.clicks.count("返回") == 2, "第一次没回到列表就要再点一次"


# -- 「没有新邮件」不等于「翻到底了」 ----------------------------------------


def test_screens_of_already_seen_rows_keep_scrolling() -> None:
    """重进之后画面回到顶部，头几屏必然全是见过的。

    原先在这里 `break`，于是上面那条重进永远走不到新内容——**重进了，却等于没重进**。
    """
    first = _row(0, ReportKind.SCOUT, minutes_ago=1)
    second = _row(1, ReportKind.SCOUT, minutes_ago=2)
    pages = [
        [first, second],
        [second],  # 拖动落点飘了，这一屏全是见过的——但**和上一屏不是同一批**
        [_row(2, ReportKind.SCOUT, minutes_ago=9)],  # 再拖才露出来的新的
    ]
    loop, _events, opened = _loop(pages)

    loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=6,
    )

    assert [row.index for row in opened] == [0, 1, 2]


def test_the_trip_stops_when_a_drag_changes_nothing() -> None:
    """真的到底了：拖了一下，还是那几封。

    ⚠️ 判据是**行的内容**（时间列，`mail_times_settled`）而不是行的位置：拖动带惯性，
    同一批行在两屏之间位置会差几像素，按位置比会永远判「还能拖」，于是拖满上限才罢休。
    主题噪声那一侧另有专文
    （`test_a_drag_that_changes_nothing_stops_the_trip_despite_subject_noise`）。
    """
    same = [_row(0, ReportKind.SCOUT, minutes_ago=1)]
    loop, _events, opened = _loop([list(same) for _ in range(6)])

    scan = loop._scan_mail_rows(
        wanted=ReportKind.SCOUT,
        label="侦察报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=6,
    )

    # ⚠️ 断言的是**它停下来了**（只走了 2 屏，不是把 6 屏预算拖完）。
    # 只断言「同一封没被开第二次」是不够的——那在「没停下来」时同样成立，
    # 因为 `seen` 本来就挡着重复开封。差别全在白拖掉的那四次。
    assert scan.pages == 2
    assert loop._driver.clicks.count("打开邮件") == 1, "同一封不许开第二次"
    assert len(opened) == 1


# -- 耗时打点：只观测，不改行为 ------------------------------------------------


def test_the_step_timer_reports_every_lap_and_the_total() -> None:
    """一行里要同时有总时长和各步——只有总数的话，知道「慢」却不知道慢在哪，
    而这一层存在的全部理由就是把「45 秒花在哪」从猜变成量。
    """
    from evo_helper.tools.pirate_loop import StepTimer

    said: list[str] = []
    timer = StepTimer("2:1:1 攻击")
    timer.lap("开面板")
    timer.lap("翻预设条")

    import evo_helper.tools.pirate_loop as module

    original, module.say = module.say, said.append
    try:
        timer.say_total("派出")
    finally:
        module.say = original

    assert len(said) == 1
    assert "2:1:1 攻击" in said[0]
    assert "（派出）" in said[0]
    assert "开面板" in said[0] and "翻预设条" in said[0]


def test_the_step_timer_never_goes_backwards() -> None:
    """**用单调钟，不用墙钟。**

    这几个数是拿来相减的，而墙钟会被 NTP 校时往回拨——拨一次就能拿到负耗时，
    而负耗时在分解表上会把整晚的统计带偏。
    """
    import time as time_module

    from evo_helper.tools.pirate_loop import StepTimer

    timer = StepTimer("x")
    timer.lap("a")

    # 把墙钟往回拨一年也不该影响它。
    assert timer._laps[0][1] >= 0
    assert time_module.monotonic() >= timer._start


# -- 跨屏去重：身份只认时间那一格 --------------------------------------------
#
# ⚠️ **这一节是 2026-08-18 重复开封的正因。** 行身份原先取「主题 + 时间」，
# 而主题那一格在实机上根本读不稳（理由整段在 `mail_times_settled`：实拍
# 32 屏 192 行里，主题一字不差的是 0 行，时间读出 180 行 = 93.8%）。
# 于是同一封邮件在两屏上算成两封「没见过的」，被**重开一遍**——而开一封约八秒，
# 一趟的开封预算只有 `MAIL_MAX_OPENS` 封。


#: 生产日志（2026-08-18 20:35–20:37，`system_log`）里同一封邮件在两屏上的两次读数。
#: **主题一个字都对不上，时间那一格分毫不差**——这一对就是缺陷的全貌：
#:
#:     20:35:45 第 4 行开封（18/08/2026 09:07:54 '大 sw, 攻击报告 band'）
#:     20:36:40 第 0 行开封（18/08/2026 09:07:54 'EN ATR band , £& oe'）
#:
#: 两次读数一次认成 `ATTACK`、一次认成 `UNKNOWN`，而 `may_be` 刻意把 `UNKNOWN`
#: 也放进来——所以**两次都会被打开**，主题这一格连「筛掉重复」都指望不上。
REREAD_TWICE = (
    ("18/08/2026 09:07:54", "大 sw, 攻击报告 band", "EN ATR band , £& oe"),
    ("18/08/2026 08:55:37", "26 攻击报告 bad", "一一 bad 了六 Se"),
)


def _read(index: int, raw_time: str | None, subject: str) -> MailRow:
    """按 OCR **读到的原文**造一行，不修不补——主题就是那串噪声。

    走 `mail_row_from_text` 而不是直接构造 `MailRow`：时间的认法与时区换算
    要和实机同一条，否则钉住的是夹具而不是判据。
    """
    text = f"{subject}\n{raw_time}\n" if raw_time is not None else f"{subject}\n"
    return mail_row_from_text(index, text)


def test_the_same_mail_read_twice_with_different_subjects_is_opened_once() -> None:
    """**缺陷本体。** 同一封邮件在两屏上主题读成两样，不许因此被开第二次。

    实机 2026-08-18 那一趟：8 封的预算里有 2 封是重复的（≈46 秒），紧接着就打了
    「这一趟已经开了 8 封，到上限」——**重开挤掉的正是还没读的战报**。
    """
    older, newer = REREAD_TWICE[1][0], REREAD_TWICE[0][0]
    first = [
        _read(0, "18/08/2026 09:30:11", "攻击报告"),
        _read(1, "18/08/2026 09:20:04", "攻击报告"),
        _read(2, newer, REREAD_TWICE[0][1]),
        _read(3, older, REREAD_TWICE[1][1]),
    ]
    # 往下拖了一下：上一屏最后两行滑到了最上面，**主题换了一副样子，时间没变**。
    second = [
        _read(0, newer, REREAD_TWICE[0][2]),
        _read(1, older, REREAD_TWICE[1][2]),
        _read(2, "18/08/2026 08:41:20", "攻击报告"),
        _read(3, "18/08/2026 08:30:02", "攻击报告"),
    ]
    loop, _events, opened = _loop([first, second])

    loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击报告",
        visit=lambda row, page: opened.append(row) or False,
    )

    assert [row.raw_time_text for row in opened] == [
        "18/08/2026 09:30:11",
        "18/08/2026 09:20:04",
        newer,
        older,
        "18/08/2026 08:41:20",
        "18/08/2026 08:30:02",
    ], "同一封邮件被开了第二次——每重复一封白花八秒，还挤掉一封没读的战报"


def test_a_drag_that_changes_nothing_stops_the_trip_despite_subject_noise() -> None:
    """真的到底了：拖了一下，**时间列**还是那几个。主题噪声不许让这条判据失效。

    ⚠️ 断言的是**它停下来了**（只走 2 屏），不是「没重开」——后者在没停下来时
    同样成立，因为 `seen` 已经挡着重复开封。差别全在白拖掉的那两屏（每屏一次
    读屏加一次慢拖）。判据换成 `mail_times_settled` 之前，这里比的是行身份，
    而主题一字不差的行是 0 行，于是「还是那几封」**永远不成立**。
    """
    times = [f"18/08/2026 09:{minute:02d}:11" for minute in (30, 25, 20, 15)]
    first = [_read(index, moment, "攻击报告") for index, moment in enumerate(times)]
    # 同一批邮件、同样的时间，主题每读一遍都是一副新样子。
    second = [
        _read(index, moment, noise)
        for index, (moment, noise) in enumerate(
            zip(times, ("大 sw, band", "EN ATR £& oe", "26 bad", "一一 了六 Se"), strict=True)
        )
    ]
    loop, _events, opened = _loop([first, second, list(first), list(second)])

    scan = loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=4,
    )

    assert scan.pages == 2, "拖不动了却还在拖：每白拖一屏约 5.8 秒"
    assert loop._driver.clicks.count("打开邮件") == 4, "同一封不许开第二次"


def test_two_rows_without_a_readable_time_are_never_collapsed() -> None:
    """**时间读不出的行一律算「没见过」**，绝不拿空时间当身份互相顶掉。

    这是把身份从「主题 + 时间」换成「只认时间」时唯一真正危险的一步：写成
    `raw_time_text or ""` 的话，一屏里所有读不出时间的行会共用同一个身份，
    第一行之外全被静默判成重复——而实拍上读不出时间的行占 6.2%（192 行里 12 行），
    一趟 4 屏就摊到两三行。

    静默少开一封的代价与「多开一封」不对称：多开只是多花八秒，少开是这一轮
    少一份战报（侦察白飞、探路白派），而 `observe` 那一路少数一份**正是会超额
    的那一侧**（见 `DailyTally`）。
    """
    kept = "18/08/2026 09:20:04"
    first = [
        _read(0, None, "攻击报告"),
        _read(1, None, "攻击报告"),  # 同样读不出时间、主题也一模一样，但是**另一封**
        _read(2, kept, "攻击报告"),
    ]
    # 往下拖了一下：读出时间的那封滑到最上面（认得出，不再开），而它下面又是
    # 一封读不出时间的——**跨屏**这一侧才是 `or ""` 那种写法真正塌掉的地方。
    second = [
        _read(0, kept, "攻击报告"),
        _read(1, None, "攻击报告"),
    ]
    loop, _events, opened = _loop([first, second])

    loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=2,
    )

    assert [(row.index, row.raw_time_text) for row in opened] == [
        (0, None),
        (1, None),
        (2, kept),
        (1, None),
    ], "读不出时间的行被拿空时间当身份互相顶掉了"


def test_two_mails_that_share_a_second_on_one_screen_are_both_opened() -> None:
    """同一秒真的会有两封邮件，同屏时两封都要开。

    实拍 `rep-7-mail.png` 上就有一对：`08/08/2026 13:07:42` 同时是
    `远征舰队返回` 和 `远征报告`——舰队落地那一瞬间同时产出通知和报告。
    32 屏 192 行里这样的一对出现 1 次。

    所以「记下见过的」必须排在「挑出没见过的」**之后**：同一屏上的两行按构造
    就是两封不同的邮件（行号不同），任何时候都不该互相顶掉。跨屏那一侧顶不掉的
    办法不存在——只有时间这一个可信的观测量，同秒两封在观测上就是不可分的，
    取舍写在 `MailRow.identity` 里。
    """
    rows = [
        _read(0, "08/08/2026 13:07:42", "bad ao 远征舰队返回"),
        _read(1, "08/08/2026 13:07:42", "yw a 远征报告 ‘eo m"),
    ]
    loop, _events, opened = _loop([rows])

    loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=1,
    )

    assert [row.index for row in opened] == [0, 1], "同一屏上同一秒的两封被当成一封了"


def test_a_screen_that_read_nothing_is_not_mistaken_for_the_bottom() -> None:
    """整屏一行都没读出来**不算到底**——那是 OCR 没读出来，照拖不误。

    这一条从旧实现继承（`if identities and ...` 那个前置守卫），换判据时不许弄丢：
    读空当成到底，一次 OCR 抖动就能把这一趟剩下的屏全部砍掉。
    """
    first = [_read(index, f"18/08/2026 09:{30 - index:02d}:11", "攻击报告") for index in range(2)]
    later = [_read(0, "18/08/2026 08:15:03", "攻击报告")]
    loop, _events, opened = _loop([first, [], later])

    loop._scan_mail_rows(
        wanted=ReportKind.ATTACK,
        label="攻击报告",
        visit=lambda row, page: opened.append(row) or False,
        max_pages=3,
    )

    assert [row.raw_time_text for row in opened] == [
        "18/08/2026 09:30:11",
        "18/08/2026 09:29:11",
        "18/08/2026 08:15:03",
    ], "读空那一屏被当成到底了，后面的邮件再也翻不到"
