"""未读邮件**必开**：它越过哪几道闸、不越过哪几道、以及它自己的那道上限。

## 为什么要有这一档

用户口径（2026-09-08）：「邮箱中未读邮件（前 4 个），字体颜色是与已读邮件不一致的，
你在开未读邮件时，需要把这些内容都阅读了。」

未读 ⇒ 按定义我们还没开过它 ⇒ 库里不可能有它的战报。而现在的开封预算
（`MAIL_MAX_OPENS` = 8）加上早停（`_stop_after_known`），**留着未读邮件不开是可能的**
——那不是「这一趟慢了」：它会一直排在别人后面，直到掉出扫描下限
（`_routine_scan_floor`，默认 6 小时），那之后**日常那趟永远翻不到它**，
只剩人手动 `--exhaustive` 能救。

⚠️ **别把这条写成「下一趟它已经是已读、会被 #288 跳掉」**：`_already_in_library`
比的是「库里该秒份数 ≥ 同屏该秒行数」，而这一封根本没入库 ⇒ 它照开。

## 取舍不对称，这是整份用例的骨架

| 判错的方向 | 代价 |
|---|---|
| 未读判成已读、于是没开 | 一直排队到掉出扫描下限 ⇒ **那份战报再也读不回来** |
| 已读判成未读、于是多开 | 多花约 20 秒 |

所以未读那一档**越过**开封上限、越过早停、越过两道时刻闸；但**不越过**主题闸
（这一趟不找的那几类邮件不该由它来开），也**不越过** `not_before`（那是整趟唯一的
时间下界）。

## `unread is None` 仍旧要让行为逐字节退回改动之前

颜色阈值已于 2026-09-08 标定（`vision.mail_unread.CALIBRATION`），所以实机上
`MailRow.unread` 开始给出真答案；但 `None`（定位不到、落在空档、或者换版面之后
撤回未标定）必须让行为**逐字节退回**改动之前。本文件专门有一条钉这件事
（`test_an_unreadable_colour_changes_nothing`）。

## 上限是**参数**，不是模块常量

日常那趟用 `MAIL_UNREAD_MAX_OPENS`（12），`--exhaustive` 补录用
`BACKFILL_UNREAD_MAX_OPENS`。理由是存量的历史空洞：散落在深处、时刻早就不在
任何 `should_open` 窗口里的那些未读，撞了 12 之后退回常规预算就再次被时刻闸拦住，
**两条路都到不了**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import (
    BACKFILL_UNREAD_MAX_OPENS,
    MAIL_UNREAD_MAX_OPENS,
    LoopOptions,
    MailRow,
    MailScan,
    PirateLoop,
    mail_unread_payload,
    say_mail_unread_tally,
)
from evo_helper.vision.parsers import ReportKind

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, seconds: float) -> None:
        return None


def _row(
    index: int,
    *,
    unread: bool | None,
    kind: ReportKind = ReportKind.PIRATE,
    minutes_ago: int = 0,
) -> MailRow:
    moment = NOW - timedelta(minutes=minutes_ago)
    return MailRow(
        index=index,
        subject={
            ReportKind.PIRATE: "海盗攻击报告",
            ReportKind.SCOUT: "侦察报告",
        }[kind],
        raw_time_text=moment.strftime("%d/%m/%Y %H:%M:%S"),
        reported_at_utc=moment,
        kind=kind,
        unread=unread,
    )


def _page(unread: bool | None, *, count: int = 6, first: int = 0) -> list[MailRow]:
    """一屏六行，时刻各不相同（否则跨屏去重会把它们当成同一封）。"""
    return [_row(index, unread=unread, minutes_ago=first + index) for index in range(count)]


def _loop(pages: list[list[MailRow]], *, in_library: bool = False) -> tuple[Any, list[MailRow]]:
    """一个只装了「翻信箱」所需零件的 `PirateLoop`，和它开过的那些行。"""
    opened: list[MailRow] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._started_at = NOW
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._reset_to_known_screen = lambda: None
    loop._goto_planet_surface = lambda: True
    loop._dump_frame = lambda name, roi=None: None
    loop._open_mail = lambda: None
    loop._scroll_mail_list_to_top = lambda: None
    loop._close_mail = lambda: None
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    loop._on_mail_detail = lambda: True
    loop._report_screens = lambda: object()
    # 「库里已经有了」那道闸（#288）与它的早停判据。默认「库里什么都没有」。
    loop._already_in_library = lambda row, times: in_library
    loop._due_dispatches = lambda _now: []
    screens = list(pages)
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []

    def _open(row: MailRow, visit: Any) -> bool:
        opened.append(row)
        return bool(visit(row, object()))

    loop._open_mail_row = _open
    return loop, opened


def _scan(loop: Any, **kwargs: Any) -> MailScan:
    defaults: dict[str, Any] = {
        "wanted": ReportKind.PIRATE,
        "label": "海盗攻击报告",
        "visit": lambda row, page: False,
    }
    return loop._scan_mail_rows(**{**defaults, **kwargs})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)
    monkeypatch.setattr(pirate_loop, "warn", lambda _line: None)


# -- 全未读 ------------------------------------------------------------------


def test_unread_rows_are_opened_past_the_open_budget() -> None:
    """⚠️⚠️ **一屏全未读、常规预算不够 → 六封全开。**

    这是整个改动要修的那个洞：`MAIL_MAX_OPENS` 会把未读邮件留在信箱里，
    而它们下一趟仍旧要和别人抢同一笔预算，一直排到掉出扫描下限为止。

    顺带钉住**两笔预算是分开记的**：常规那格一封都没花。
    """
    #: 两档常规预算各跑一遍。`0` 那一档钉「越过上限」，`2` 那一档钉
    #: 「越过之后没有偷偷记到常规格上」——只留一档的话，另一半改坏了也是绿的。
    for max_opens in (0, 2):
        loop, opened = _loop([_page(True)])

        scan = _scan(loop, max_opens=max_opens, max_pages=1)

        assert len(opened) == 6, max_opens
        assert scan.unread_opened == 6, max_opens
        assert scan.opened == 0, "未读那一档不许花常规预算，否则「必开」就是假的"
        assert scan.unread_seen == 6


def test_unread_rows_are_opened_past_the_early_stop() -> None:
    """⚠️ **早停也拦不住未读。** `visit` 第一封就说「收齐了」，后面五封照开。

    早停的假定是「往下都是更旧的、都读过了」。这个假定对**未读**行不成立，
    而它恰恰是这条路上唯一还会漏数据的地方。
    """
    loop, opened = _loop([_page(True)])

    scan = _scan(loop, visit=lambda row, page: True, max_pages=1)

    assert len(opened) == 6
    assert scan.unread_opened == 6


def test_unread_rows_are_opened_past_the_two_time_gates() -> None:
    """⚠️ **两道时刻闸都拦不住未读**：`skip_known`（#288）与 `should_open`（补录）。

    两道闸都是拿时刻去**猜**「这一封读过了 / 不值得开」，而未读是**直接读到**
    「还没读过」。一个推测不该否掉一个观测。
    """
    loop, opened = _loop([_page(True)], in_library=True)

    scan = _scan(
        loop,
        max_pages=1,
        skip_known=True,
        should_open=lambda row: False,
    )

    assert len(opened) == 6
    assert scan.skipped_known == 0, "未读行不该去查库，更不该被跳"


# -- 全已读：行为必须和改动之前一样 -------------------------------------------


def test_read_rows_still_obey_the_open_budget() -> None:
    """已读那一侧一个字都没改：常规上限照旧封住它。"""
    loop, opened = _loop([_page(False)])

    scan = _scan(loop, max_opens=2, max_pages=1)

    assert len(opened) == 2
    assert scan.opened == 2
    assert scan.unread_opened == 0
    assert scan.unread_seen == 0


def test_read_rows_still_go_through_the_library_gate() -> None:
    """已读行照旧走 #288 那道时刻跳过，**而且照旧在第一封就早停**。

    跳过之后该不该收工由 `_stop_after_known()` 说了算（单子空了就停），
    所以只跳一封就收工是对的 —— 这一条同时钉住未读那一档没有把早停搞坏。
    """
    loop, opened = _loop([_page(False)], in_library=True)

    scan = _scan(loop, max_pages=1, skip_known=True)

    assert opened == []
    assert scan.skipped_known == 1


# -- `None` ⇒ 退回改动之前 ----------------------------------------------------


def test_an_unreadable_colour_changes_nothing() -> None:
    """⚠️⚠️ **`unread is None` 必须和改动之前逐字节一致 —— 这是实机上的常态。**

    颜色阈值还没标定（`CALIBRATION is None`），所以生产上每一行都是 `None`。
    把 `None` 当未读（`row.unread is not False` 那种写法）会让每屏六行全进
    「必开」，开封预算翻倍烧掉而日志上看着一切正常。
    """
    loop, opened = _loop([_page(None)])

    scan = _scan(loop, max_opens=2, max_pages=1)

    assert len(opened) == 2, "读不出颜色时的行为必须等于改动之前"
    assert scan.opened == 2
    assert scan.unread_opened == 0
    assert scan.unread_seen == 0
    assert scan.unread_unknown == 6, "读不出的行数要能在日志里数出来"


# -- 混合 --------------------------------------------------------------------


def test_only_the_unread_rows_jump_the_queue() -> None:
    """前两行未读、后四行已读，常规预算 0 → 只开那两封未读。"""
    rows = [
        _row(0, unread=True, minutes_ago=0),
        _row(1, unread=True, minutes_ago=1),
        *[_row(index, unread=False, minutes_ago=index) for index in range(2, 6)],
    ]
    loop, opened = _loop([rows])

    scan = _scan(loop, max_opens=0, max_pages=1)

    assert [row.index for row in opened] == [0, 1]
    assert scan.unread_opened == 2
    assert scan.opened == 0


def test_the_subject_gate_still_applies_to_unread_rows() -> None:
    """⚠️ **「必开」的范围是这一趟在找的那几类，不是信箱里所有未读。**

    一封未读的侦察报告不该由收攻击战报的那一趟去开：那一趟的 `visit` 读不懂它，
    而它下一趟收侦察报告时**仍然是未读**，照样开得到。这也是「必开」有界的
    第二道保障 —— 一整屏未读的活动通知不会挤进来。
    """
    rows = [
        _row(index, unread=True, kind=ReportKind.SCOUT, minutes_ago=index) for index in range(6)
    ]
    loop, opened = _loop([rows])

    scan = _scan(loop, max_pages=1)

    assert opened == []
    assert scan.unread_seen == 6, "看见了，只是主题不对；两件事要分得开"
    assert scan.unread_opened == 0


# -- 未读数超过上限 -----------------------------------------------------------


def test_the_unread_budget_is_capped() -> None:
    """⚠️⚠️ **「必开」也必须封上界。**

    一组标偏的颜色阈值（把已读判成未读）会让每屏六行全进这条路：4 屏 × 6 行 ×
    ≈20 秒 ≈ 8 分钟，而日志上看着一切正常。撞上上限的那些行退回常规预算排队
    （这里常规预算是 0，所以一封都开不了），并且**记在账上**。
    """
    pages = [_page(True, first=index * 6) for index in range(4)]
    loop, opened = _loop(pages)

    scan = _scan(loop, max_opens=0, max_pages=4)

    assert len(opened) == MAIL_UNREAD_MAX_OPENS
    assert scan.unread_opened == MAIL_UNREAD_MAX_OPENS
    assert scan.unread_over_budget == 24 - MAIL_UNREAD_MAX_OPENS
    assert scan.unread_seen == 24
    assert scan.unread_budget == MAIL_UNREAD_MAX_OPENS, "撞了哪个上限要记在账上"


def test_the_backfill_path_gets_a_bigger_unread_budget() -> None:
    """⚠️⚠️ **存量的历史空洞归补录管，所以补录那条路的未读预算要单独放大。**

    实测（生产库 2026-09-08）：4 封未读的攻击报告躺在信箱 24–72 行深处、
    8.5–16.7 小时没人开过，`battle_reports` 里 4 封全缺。日常那趟够不到它们
    （早就掉出扫描下限），而补录那条路原先也够不到——未读那一档封在 12，
    撞了之后剩下的**退回常规预算**，于是又要过 `should_open` 那道时刻窗口闸，
    而历史空洞的时刻恰恰不在任何窗口里。

    这一条把两件事一起钉住：上限是**参数**（不是模块常量），而且退回常规预算的
    那一侧**在时刻闸关着的时候真的开不出来**。
    """
    pages = [_page(True, first=index * 6) for index in range(4)]
    loop, opened = _loop(pages)

    scan = _scan(
        loop,
        max_opens=60,
        max_pages=4,
        max_unread_opens=BACKFILL_UNREAD_MAX_OPENS,
        should_open=lambda row: False,
    )

    assert BACKFILL_UNREAD_MAX_OPENS >= 24
    assert len(opened) == 24, "24 行全是未读，补录那条路应该一封都不落下"
    assert scan.unread_opened == 24
    assert scan.unread_over_budget == 0
    assert scan.opened == 0, "时刻闸关着，退回常规预算的那一侧一封都开不出来"
    assert scan.unread_budget == BACKFILL_UNREAD_MAX_OPENS


# -- 跨屏：未读是连续的一段，可能被屏幕下边缘切开 -----------------------------


def test_a_screen_full_of_unread_keeps_the_scan_going_past_the_early_stop() -> None:
    """⚠️ 第一屏全未读且已经「收齐了」→ **还要往下看一屏**。

    未读在时间倒序的列表里是连续的一段；这一段正好被屏幕下边缘切开时，
    「收齐了就收工」会把下一屏那几封未读留在信箱里。第二屏没有未读时这个
    条件立刻不成立，所以它不是「无限往下翻」。
    """
    pages = [_page(True), _page(False, first=6), _page(False, first=12)]
    loop, opened = _loop(pages)

    scan = _scan(loop, visit=lambda row, page: True, max_pages=3)

    assert scan.pages == 2, "第二屏没有未读了，就该在那里收工"
    assert len(opened) == 6, "第二屏全是已读，且已经收齐；一封都不该再开"


# -- 日志：这道闸「在不在工作」必须一眼可查 -----------------------------------


def test_the_tally_says_so_even_when_nothing_is_calibrated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 没标定时也要说一句 —— 那一句是「生产跑的是哪个版本」的凭据。

    标定落地之后这条仍旧要留着：换了游戏版面就要把 `CALIBRATION` 撤回 `None`
    重采，而那一刻用户在日志里看到的必须是「判据没通电」，不是一片安静。
    """
    said: list[str] = []
    monkeypatch.setattr(pirate_loop, "CALIBRATION", None)
    pirate_loop.say = said.append  # type: ignore[assignment]

    say_mail_unread_tally(MailScan(observed=6, unread_unknown=6))

    assert any("还没标定" in line for line in said)


def test_the_tally_reports_the_budget_it_actually_used() -> None:
    """⚠️ 撞上限那句告警里的数字必须是**这一趟真的用的那个**。

    上限成了参数之后，打日志的地方再去读模块常量就会在补录那条路上报出一个
    假数字（记忆里那条「引用数字前先查出处」）。
    """
    warned: list[str] = []
    pirate_loop.warn = warned.append  # type: ignore[assignment]

    say_mail_unread_tally(
        MailScan(observed=24, unread_seen=24, unread_over_budget=4, unread_budget=60)
    )

    assert any("（60）" in line for line in warned)
    assert not any(f"（{MAIL_UNREAD_MAX_OPENS}）" in line for line in warned)


def test_the_payload_carries_a_key_only_the_new_code_writes() -> None:
    """`unread_calibrated` 是那个只有新代码写得出的键（见记忆里 #266 那条教训）。

    标定落地之后 `unread_measured_on` 接着回答下一个问题：**生产上跑的是哪一组数**。
    库里查不到代码分支，所以换过版面之后「这条日志是新标定还是旧标定写的」
    只有这个键分得出。
    """
    payload = mail_unread_payload(
        MailScan(observed=6, unread_seen=2, unread_opened=2, unread_budget=12)
    )

    assert payload["unread_calibrated"] is True
    assert "2026-09-08" in str(payload["unread_measured_on"])
    assert payload["unread_seen"] == 2
    assert payload["unread_opened"] == 2
    assert payload["unread_over_budget"] == 0
    assert payload["unread_budget"] == 12


# -- 接线：颜色是从**同一帧**上量的，量不出来就整屏 `None` --------------------


class _Screens:
    """只会量颜色、不会读字的取图替身。"""

    def __init__(self, colors: Any, *, broken: bool = False) -> None:
        self._colors = colors
        self._broken = broken
        self.calls = 0

    def mail_row_colors(self) -> Any:
        self.calls += 1
        if self._broken:
            raise RuntimeError("Pillow 炸了")
        return self._colors


def test_a_missing_colour_capability_yields_a_screen_of_none() -> None:
    """老的取图替身（以及 `tools.ingest_report` 那个只读文字的实现）不该被要求改。

    所以这里用 `getattr` 探能力，而**不是**往 `ReportScreens` 协议上加方法。
    """
    loop, _opened = _loop([])

    assert loop._mail_row_unread(object(), 6) == [None] * 6


def test_a_broken_measurement_yields_a_screen_of_none_and_says_so() -> None:
    """⚠️ 吞掉异常是有代价的，所以它必须留痕：静默失效正是这道闸最可能的死法。"""
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]
    loop, _opened = _loop([])
    loop._mail_unread_broken = False

    assert loop._mail_row_unread(_Screens(None, broken=True), 6) == [None] * 6
    assert any("量不出来" in line for line in said)
    # 一个进程只说一次，否则每屏一条把日志淹掉
    said.clear()
    assert loop._mail_row_unread(_Screens(None, broken=True), 6) == [None] * 6
    assert said == []


def _warm_color() -> Any:
    """一行「明显是黄字」的读数：暖占比 0.9，远在 `unread_min_share` 之上。"""
    from evo_helper.vision.mail_unread import WARMTH_BUCKETS, MailRowColor

    return MailRowColor(
        pixels=1000,
        warmth_buckets=tuple(0 if index != 11 else 900 for index in range(WARMTH_BUCKETS)),
        mean_luminance=90,
    )


def test_a_measured_warm_row_now_reads_as_unread() -> None:
    """标定落地之后，量出来的黄字这一屏真的判成未读 —— 判据通电了。"""
    loop, _opened = _loop([])
    screens = _Screens([_warm_color()] * 6)

    assert loop._mail_row_unread(screens, 6) == [True] * 6
    assert screens.calls == 1, "一屏只量一次"


def test_an_uncalibrated_measurement_still_yields_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """量出来了、但**没标定** ⇒ 一律 `None`。

    ⚠️ 打的是 `vision.mail_unread` 那个常量，因为 `classify_unread` 读的是它
    ——换版面时撤回标定走的就是这条路。
    """
    from evo_helper.vision import mail_unread

    monkeypatch.setattr(mail_unread, "CALIBRATION", None)
    loop, _opened = _loop([])
    screens = _Screens([_warm_color()] * 6)

    assert loop._mail_row_unread(screens, 6) == [None] * 6


def test_a_row_that_could_not_be_located_is_none_and_says_so() -> None:
    """⚠️ **取样定位不到时刻带的那几行交回 `None`，别的行照判 —— 而且要留痕。**

    定位失败是取样这一侧**唯一**的失效形态，而它在别的日志上和「今天没有未读」
    长得一模一样（`unread_unknown` 两种情形都会涨）。实拍四屏 24 行定位成功率
    是 24/24，所以这条 WARNING 一出现就意味着版面变了。

    ⚠️ 一个进程只说一次，否则每屏一条把真东西淹掉（同 `_say_mail_unread_broken`）。
    """
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]
    loop, _opened = _loop([])
    warm = _warm_color()

    assert loop._mail_row_unread(_Screens([warm, None, warm, None, None, None]), 6) == [
        True,
        None,
        True,
        None,
        None,
        None,
    ]
    assert any("定位不到时刻带" in line for line in said)
    said.clear()
    loop._mail_row_unread(_Screens([None] * 6), 6)
    assert said == []


@pytest.fixture(autouse=True)
def _restore_say() -> Any:
    """`say` 是模块级函数，改了要还原 —— 否则后面的文件跟着受影响。"""
    original = pirate_loop.say
    yield
    pirate_loop.say = original  # type: ignore[assignment]
