"""进信箱收探路战报这一趟：怎么进、怎么认、怎么入库。

真正驱动鼠标的部分和海盗那条链路共用（`pirate_loop`），这里守的是三件事：

1. **进信箱前必须先关浮层**，切不过去要留下现场。这是刚在
   `collect_scout_reports` 里修过的同一个缺陷，`read_defender_units` 那边漏了。
2. **认报告靠 VS 块里的目标坐标，不靠行号。** 行序随新邮件变。
3. **入库前后各有一道闸门**：复核 VS 坐标、按报告时间去重。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import BotLoop, BotOptions

A = Coordinate(2, 149, 17)
B = Coordinate(2, 149, 18)

REPORTED_AT = datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, seconds: float) -> None:
        return None


class _Page:
    """一屏详情页。`versus` 为 None 表示 VS 块读不出来（还没渲染完）。"""

    def __init__(self, target: Coordinate | None, units: str = "5.36K") -> None:
        self.target = target
        self.units = units

    def versus_block(self) -> str:
        if self.target is None:
            return ""
        return (
            "Kucleer                    bot\n"
            "奥格瑞玛                   bot's Planet\n"
            f"[2:137:18]                 [{self.target.galaxy}:"
            f"{self.target.system}:{self.target.position}]"
        )

    def unit_totals(self) -> tuple[str, str]:
        return ("100", self.units)


def _loop(pages: list[_Page], *, reachable: bool = True) -> tuple[Any, list[str]]:
    """一个只装了「翻信箱」所需零件的 `BotLoop`。"""
    events: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(), probe=True, attack=True)
    loop._driver = _Driver()
    loop._reset_to_known_screen = lambda: events.append("关浮层")
    loop._goto_planet_surface = lambda: (events.append("切地表"), reachable)[1]
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")
    loop._open_mail = lambda: events.append("开信箱")
    loop._close_mail = lambda: events.append("关信箱")
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    remaining = list(pages)
    loop._report_screens = lambda: remaining.pop(0) if remaining else _Page(None)
    return loop, events


@pytest.fixture(autouse=True)
def _no_dragging(monkeypatch: pytest.MonkeyPatch) -> None:
    """慢拖要真的按住鼠标分步移动，这批测试一律桩掉。"""
    from evo_helper.tools import bot_loop

    monkeypatch.setattr(bot_loop, "slow_drag", lambda *args, **kwargs: None)


# -- 进信箱的姿势 ------------------------------------------------------------


def test_overlays_are_closed_before_the_surface_check() -> None:
    """**关浮层必须排在切地表之前。**

    `_on_planet_surface()` 的正面凭据是右上角那个未读数，而浮层会盖住它；
    `_goto_planet_surface()` 自己不关浮层，只会反复点视图菜单——而那个坐标
    此刻正压在浮层底下。顺序反了等于没修。

    这一步紧跟在派遣与等待之后，正是舰队返航之类的通知最容易冒出来的时刻：
    海盗那条链路实机三次都倒在这里，每次都已经先派出 4 发侦察。
    """
    loop, events = _loop([_Page(None)])
    loop._open_mail = lambda: (_ for _ in ()).throw(RuntimeError("到此为止"))

    with pytest.raises(RuntimeError, match="到此为止"):
        loop._scan_mail((A,), lambda target, page: None)

    assert events == ["关浮层", "切地表"]


def test_an_unreachable_surface_leaves_a_frame_behind() -> None:
    """切不过去就存一帧：不知道当时画面长什么样是最贵的失败。

    原先 `read_defender_units` 这一处只抛异常、一张图都不留——而它偏偏是
    整条链路唯一会去信箱的地方。
    """
    loop, events = _loop([], reachable=False)

    with pytest.raises(RuntimeError, match="切不到自己星球地表"):
        loop._scan_mail((A,), lambda target, page: None)

    assert events == ["关浮层", "切地表", "存图:planet-surface-unreachable"]


# -- 认报告 ------------------------------------------------------------------


def test_a_report_is_matched_by_its_own_coordinate_not_its_row() -> None:
    """**行序随新邮件变，而报告自己写着打的是谁。**

    这里目标按 (A, B) 给，信箱里却是 B 在前——照行号对位就会把 B 那份当成 A 的，
    于是一份战报挂到另一个 bot 头上，接着按它的舰队量去挑攻击组合。
    """
    loop, _events = _loop([_Page(B), _Page(A)])
    seen: list[Coordinate] = []

    missing = loop._scan_mail((A, B), lambda target, page: seen.append(target))

    assert seen == [B, A]
    assert missing == set()


def test_several_targets_are_collected_in_one_trip() -> None:
    """一趟读完。一个目标进一次信箱要切视图、开面板、慢拖三下，一趟十几秒。"""
    loop, events = _loop([_Page(A), _Page(B)])
    seen: list[Coordinate] = []

    loop._scan_mail((A, B), lambda target, page: seen.append(target))

    assert seen == [A, B]
    assert events.count("开信箱") == 1


def test_a_target_whose_report_has_not_arrived_is_reported_as_missing() -> None:
    """**收不到不是错误。** 探路刚派出去，战报本来就还没到；
    这一趟不动它，下一趟再来（真的一直收不到，由放弃阈值兜底）。"""
    loop, _events = _loop([_Page(B)])

    missing = loop._scan_mail((A,), lambda target, page: None)

    assert missing == {A}


def test_an_unrendered_panel_is_skipped_rather_than_guessed() -> None:
    """VS 块读不出来时不猜是谁的报告——猜错就把战报挂到别的目标上。"""
    loop, _events = _loop([_Page(None)])
    seen: list[Coordinate] = []

    missing = loop._scan_mail((A,), lambda target, page: seen.append(target))

    assert seen == []
    assert missing == {A}


def test_the_mailbox_is_closed_even_when_nothing_matched() -> None:
    """不关信箱，下一个目标的 `goto` 会在浮层上朝导航栏坐标盲点。"""
    loop, events = _loop([_Page(B)])

    loop._scan_mail((A,), lambda target, page: None)

    assert events[-1] == "关信箱"


# -- 入库前后的两道闸门 ------------------------------------------------------


class _Repository:
    def __init__(self, *, already_stored: bool = False) -> None:
        self.already_stored = already_stored
        self.appended: list[Any] = []

    def has_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        return self.already_stored

    def append_report(self, report: Any) -> None:
        self.appended.append(report)


def _ingesting_loop(repository: _Repository) -> Any:
    loop = BotLoop.__new__(BotLoop)
    loop._ensure_run = lambda: (repository, None)
    loop._dump_frame = lambda name, roi=None: None
    return loop


def test_a_readable_report_is_stored() -> None:
    repository = _Repository()

    assert _ingesting_loop(repository)._ingest_probe_report(A, _DetailScreens(A)) is True
    assert len(repository.appended) == 1


def test_a_report_pointing_elsewhere_is_refused() -> None:
    """VS 块读了两遍（翻行时一遍、入库前一遍），两遍必须指向同一个目标。

    不复核的话，一次 OCR 抖动就足以把这份战报挂到别人头上——而挂错之后
    `append_report` 会拿错的目标坐标去认领派遣，闭合的是另一发。
    """
    repository = _Repository()

    assert _ingesting_loop(repository)._ingest_probe_report(B, _DetailScreens(A)) is False
    assert repository.appended == []


def test_an_already_stored_report_is_not_appended_again() -> None:
    """信箱里那几行每趟都在。认领不上号的战报尤其危险：`has_report` 永远为假，
    于是下一趟又读同一封——没有这道去重，它会每趟复制一行。"""
    repository = _Repository(already_stored=True)

    assert _ingesting_loop(repository)._ingest_probe_report(A, _DetailScreens(A)) is False
    assert repository.appended == []


def test_an_unreadable_report_is_skipped_and_dumped() -> None:
    """读不出来就放过，**不存半份**，并留下现场。

    这一份就这么放着，等 `MAX_REPORT_AGE` 把那发派遣判掉、允许重新探路——
    这就是「报告就是读不到」时的出路，而不是让目标静默卡死。
    """
    repository = _Repository()
    loop = _ingesting_loop(repository)
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)

    assert loop._ingest_probe_report(A, _DetailScreens(A, header="装饰文字")) is False
    assert repository.appended == []
    assert dumped == ["probe-report-unreadable"]


class _DetailScreens:
    """够 `LiveReportReader.read_detail_only` 读一遍的详情页取字面。"""

    def __init__(self, target: Coordinate, *, header: str | None = None) -> None:
        self._target = target
        self._header = (
            header
            if header is not None
            else "发件人: System                    06/08/2026 11:45:03\n主题: 攻击报告"
        )

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        return self._header

    def versus_block(self) -> str:
        return (
            "Kucleer                    bot\n"
            "奥格瑞玛                   bot's Planet\n"
            f"[2:137:18]                 [{self._target.galaxy}:"
            f"{self._target.system}:{self._target.position}]"
        )

    def participating_columns(self) -> tuple[str, str]:
        return ("", "")

    def round_columns(self) -> list[tuple[int, str, str]]:
        return []

    def unit_totals(self) -> tuple[str, str]:
        return ("100", "5.36K")


# -- 分档取数 ----------------------------------------------------------------


def test_the_tier_uses_the_stored_total_without_a_second_mailbox_trip() -> None:
    """走到这一态的前提就是「本轮的探路战报已经入库」，那个数已经读过了。

    再进一趟信箱不只是多花十几秒：信箱那条路**没有时间闸门**，翻到的可能是
    上一轮甚至上一天的报告，照它分档挑出来的档次是错的，而且完全静默。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(A,), probe=True, attack=True)
    loop._ensure_run = lambda: (_StoredUnits(5360), None)
    loop.read_defender_units = lambda coordinate: pytest.fail("库里有数就不该再进信箱")
    attacked: list[tuple[Coordinate, str]] = []
    loop._goto_confirmed = lambda coordinate: True
    loop.attack = lambda coordinate, *, preset: attacked.append((coordinate, preset))

    loop._tier_and_attack(A)

    assert attacked == [(A, "BBB")]


def test_the_tier_falls_back_to_the_mailbox_when_the_row_has_no_total() -> None:
    """库里那一列可空（「单位」那一行读不出来时留空）。**不能把 None 当 0**——
    0 会落进「不值得打」那一档，于是一个真有舰队的 bot 被静默跳过。"""
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(A,), probe=True, attack=True)
    loop._ensure_run = lambda: (_StoredUnits(None), None)
    loop.read_defender_units = lambda coordinate: 9000
    attacked: list[tuple[Coordinate, str]] = []
    loop._goto_confirmed = lambda coordinate: True
    loop.attack = lambda coordinate, *, preset: attacked.append((coordinate, preset))

    loop._tier_and_attack(A)

    assert attacked == [(A, "CCC")]


class _StoredUnits:
    def __init__(self, units: int | None) -> None:
        self._units = units

    def latest_defender_units(self, target: Coordinate, *, since: datetime) -> int | None:
        return self._units
