"""进信箱收战报这一趟：怎么进、翻多少、开哪几封、怎么认、怎么入库。

**探路发与攻击发共用这一趟**，这里的每一条对两种发都成立：两种发打的是同一个坐标、
走的是同一条攻击链路、报告主题同为「攻击报告」，认归属靠的都是 VS 块里的目标坐标。
这条路径上从头到尾没有一处读得到「这一份是哪个预设打的」——也不需要读。
「哪些目标交进来收」在 `test_bot_loop.py`（分态路由）那一侧。

翻信箱的动作两条链路共用（`PirateLoop._scan_mail_rows`），这里守的是 bot 这一侧：

1. **进信箱前必须先关浮层**，切不过去要留下现场。
2. **认报告靠 VS 块里的目标坐标，不靠行号。** 行序随新邮件变。
3. **入库前后各有一道闸门**：复核 VS 坐标、按报告时间去重。

窗口那一侧（先读主题再决定开不开、翻屏、按时间早停）钉在
`test_mail_scan_window.py`——那是两条链路共用的部分。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import BotLoop, BotOptions
from evo_helper.tools.pirate_loop import LoopOptions, MailRow, TargetCheck
from evo_helper.vision.parsers import ReportKind

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


def _attack_rows(count: int) -> list[MailRow]:
    """列表页上 `count` 行「攻击报告」，主题都读得干干净净。"""
    return [
        MailRow(
            index=index,
            subject="攻击报告",
            raw_time_text=f"06/08/2026 11:45:0{index}",
            reported_at_utc=REPORTED_AT,
            kind=ReportKind.ATTACK,
        )
        for index in range(count)
    ]


def _loop(pages: list[_Page], *, reachable: bool = True) -> tuple[Any, list[str]]:
    """一个只装了「翻信箱」所需零件的 `BotLoop`。"""
    events: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(), probe=True, attack=True)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._started_at = datetime(2026, 8, 6, tzinfo=UTC)
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._reset_to_known_screen = lambda: events.append("关浮层")
    loop._goto_planet_surface = lambda: (events.append("切地表"), reachable)[1]
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")
    loop._open_mail = lambda: events.append("开信箱")
    loop._close_mail = lambda: events.append("关信箱")
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    loop._on_mail_detail = lambda: True
    loop._mail_list_rows = lambda: _attack_rows(len(pages) or 1)
    remaining = list(pages)
    loop._report_screens = lambda: remaining.pop(0) if remaining else _Page(None)
    return loop, events


@pytest.fixture(autouse=True)
def _no_dragging(monkeypatch: pytest.MonkeyPatch) -> None:
    """慢拖要真的按住鼠标分步移动，这批测试一律桩掉。"""
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)


# -- 进信箱的姿势 ------------------------------------------------------------


def test_overlays_are_closed_before_the_surface_check() -> None:
    """**关浮层必须排在切地表之前。**

    `_on_planet_surface()` 的正面凭据是右上角那个未读数，而浮层会盖住它；
    `_goto_planet_surface()` 自己不关浮层，只会反复点视图菜单——而那个坐标
    此刻正压在浮层底下。顺序反了等于没修。
    """
    loop, events = _loop([_Page(None)])
    loop._open_mail = lambda: (_ for _ in ()).throw(RuntimeError("到此为止"))

    with pytest.raises(RuntimeError, match="到此为止"):
        loop._scan_mail((A,), lambda target, page: None)

    assert events == ["关浮层", "切地表"]


def test_an_unreachable_surface_leaves_a_frame_behind() -> None:
    """切不过去就存一帧：不知道当时画面长什么样是最贵的失败。"""
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


def test_a_detail_page_that_never_renders_is_not_read_at_all() -> None:
    """**点开之后要等详情页真的铺开，铺不开就一个字都不读。**

    面板是滑进来的（`_settle` 的注释记着「等 2.4 秒判一次判不到，而失败时存下的
    那一帧读得清清楚楚」）。没铺开的那一屏读出来是一堆读不通的字，和「这封是别人
    的报告」在下游长得一模一样——于是一份本来在信箱里的战报被静默丢掉，
    而日志上只有一句「还没翻到」。

    这里让 `_on_mail_detail` 恒为假：即使那一屏其实是 A 的战报，也一封都不读，
    并且留下现场图。
    """
    loop, events = _loop([_Page(A)])
    loop._settle = lambda predicate, **_kwargs: predicate is not loop._on_mail_detail
    seen: list[Coordinate] = []

    missing = loop._scan_mail((A,), lambda target, page: seen.append(target))

    assert seen == []
    assert missing == {A}
    assert "存图:mail-detail-unrendered" in events


def test_the_mailbox_is_closed_even_when_nothing_matched() -> None:
    """不关信箱，下一个目标的 `goto` 会在浮层上朝导航栏坐标盲点。"""
    loop, events = _loop([_Page(B)])

    loop._scan_mail((A,), lambda target, page: None)

    assert events[-1] == "关信箱"


def test_nothing_wanted_means_no_mailbox_trip_at_all() -> None:
    """没有要收的目标就别进信箱——一趟十几秒，白跑还占着鼠标。"""
    loop, events = _loop([])

    assert loop._scan_mail((), lambda target, page: None) == set()
    assert events == []


# -- 入库前后的两道闸门 ------------------------------------------------------


class _Repository:
    def __init__(self, *, already_stored: bool = False) -> None:
        self.already_stored = already_stored
        self.appended: list[Any] = []

    def has_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        return self.already_stored

    def append_report(self, report: Any) -> None:
        self.appended.append(report)


def _ingesting_loop(repository: _Repository, bottom: Any = None) -> Any:
    """一个只装了「入库」所需零件的 `BotLoop`。

    `_bottom_screens` 在生产上要真的按住鼠标把面板拖到底再拍一屏，这里桩掉；
    默认交出一屏读不出「单位」的画面，也就是**拖到底也没读到**那种情况。
    """
    loop = BotLoop.__new__(BotLoop)
    loop._ensure_run = lambda: (repository, None)
    loop._dump_frame = lambda name, roi=None: None
    loop._bottom_screens = lambda: (
        bottom if bottom is not None else _DetailScreens(A, units=("", ""))
    )
    return loop


def test_a_readable_report_is_stored() -> None:
    repository = _Repository()

    assert _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A)) is True
    assert len(repository.appended) == 1


def test_a_report_pointing_elsewhere_is_refused() -> None:
    """VS 块读了两遍（翻行时一遍、入库前一遍），两遍必须指向同一个目标。

    不复核的话，一次 OCR 抖动就足以把这份战报挂到别人头上——而挂错之后
    `append_report` 会拿错的目标坐标去认领派遣，闭合的是另一发。
    """
    repository = _Repository()

    assert _ingesting_loop(repository)._ingest_battle_report(B, _DetailScreens(A)) is False
    assert repository.appended == []


def test_an_already_stored_report_is_not_appended_again() -> None:
    """信箱里那几行每趟都在。认领不上号的战报尤其危险：`has_report` 永远为假，
    于是下一趟又读同一封——没有这道去重，它会每趟复制一行。"""
    repository = _Repository(already_stored=True)

    assert _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A)) is False
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

    assert loop._ingest_battle_report(A, _DetailScreens(A, header="装饰文字")) is False
    assert repository.appended == []
    assert dumped == ["battle-report-unreadable"]


class _DetailScreens:
    """够 `LiveReportReader.read_detail_only` 读一遍的详情页取字面。"""

    def __init__(
        self,
        target: Coordinate,
        *,
        header: str | None = None,
        units: tuple[str, str] = ("100", "5.36K"),
        losses: tuple[str, str] = ("", ""),
        banner: str = "FAIL",
    ) -> None:
        self._target = target
        self._units = units
        self._losses = losses
        self._banner = banner
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
        return self._units

    def loss_totals(self) -> tuple[str, str]:
        """默认空着——「损失单位」那一行**只有拖到底那一屏**才读得到。"""
        return self._losses

    def outcome_banner(self) -> str:
        return self._banner


# -- 胜负是算出来的，「单位」与「损失单位」是它的输入 ------------------------


def test_the_computed_outcome_reaches_the_stored_report() -> None:
    """攻击日志的战果列就取这一个字段（`web.service.AttackLogView.outcome`）。

    这里第二屏给出「我方 1 艘全损、对方 319 一艘没掉」——我方剩余 0 → `FAIL`，
    正是那五张探路战报的实际形状。
    """
    repository = _Repository()
    bottom = _DetailScreens(A, units=("1", "319"), losses=("1", "0"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].outcome == "FAIL"
    assert (repository.appended[0].attacker_losses, repository.appended[0].defender_losses) == (
        1,
        0,
    )


def test_a_wiped_out_defender_is_a_victory() -> None:
    repository = _Repository()
    bottom = _DetailScreens(A, units=("100", "783"), losses=("0", "783"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].outcome == "VICTORY"


def test_both_sides_surviving_is_a_draw() -> None:
    """**平局这一档不需要样本**——没人见过平局的横幅，但它算得出来。"""
    repository = _Repository()
    bottom = _DetailScreens(A, units=("100", "783"), losses=("30", "200"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].outcome == "DRAW"


def test_an_uncomputable_outcome_stores_nothing_rather_than_a_defeat() -> None:
    """⚠️ 本文件最要紧的一条：**「没算出胜负」不能长成「打输了」。**

    没拖到底就没有战损，没有战损就算不出胜负——这是常态。而画面上那行 `FAIL`
    大字**不许**在这时顶上来（用户口径 2026-08-11：不看游戏内的提示）：
    真顶上去，攻击日志就会出现一场根本没核过的败仗，和真败仗在页面上一模一样。
    """
    repository = _Repository()

    _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A, banner="FAIL"))

    assert repository.appended[0].outcome is None


def test_the_units_come_from_the_scrolled_screen_when_the_first_one_has_none() -> None:
    """bot 战报多一行「生成卫星概率」，「单位」那一行整个落在可视区之外。

    2026-08-11 的五张实拍里四张如此——不是锚点找错，是那一行没画出来。
    出路只有一条：把详情页拖到底再拍一屏。
    """
    repository = _Repository()
    bottom = _DetailScreens(A, units=("1", "319"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].defender_units == 319


def test_the_unscrolled_screen_wins_when_it_already_has_the_units() -> None:
    """看得见就别再问拖到底那一屏。

    两屏的「单位」是同一行字，但拖过之后的那一屏是**另一次截图**；
    没理由为一个已经读到的数再赌一次 OCR。第五张实拍就属于这一种
    （没有「生成卫星概率」那一行，「单位」直接就在屏上）。
    """
    repository = _Repository()
    bottom = _DetailScreens(A, units=("9", "9"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A))

    assert repository.appended[0].defender_units == 5360


def test_units_that_neither_screen_shows_stay_empty() -> None:
    """拖到底也没读到就留空。**不能拿 0 顶替**：0 会落进「不值得打」那一档，
    于是一个真有舰队的 bot 被静默跳过（`_tier_and_attack` 那两条测试守着同一件事）。
    """
    repository = _Repository()

    _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].defender_units is None


# -- 收不到时那句话要说准 ----------------------------------------------------


def test_a_report_that_is_not_due_yet_says_so_instead_of_blaming_the_window() -> None:
    """「还没到点」和「到点了却没翻到」的处置完全相反，日志必须分开说。

    实机上六个目标一视同仁地报「还没出现在信箱最上面几行」，连续四趟同一句——
    而其中三发确实还没到点、另三发是**窗口不够大**。那句话把后者说成了前者，
    于是「窗口太小」这个正因被盖了整整一天。
    """
    now = datetime.now(UTC)
    loop, _events = _loop([])
    said: list[str] = []
    loop._ensure_run = lambda: (_DueRepository({A: (now, now.replace(year=now.year + 1))}), None)
    loop._round_start = lambda: datetime(2026, 8, 6, tzinfo=UTC)
    loop._scan_mail = lambda wanted, visit, not_before=None: set(wanted)

    from evo_helper.tools import bot_loop as module

    original = module.say
    module.say = said.append
    try:
        loop.collect_battle_reports((A,))
    finally:
        module.say = original

    assert any("才产生；接着等" in line for line in said)
    assert not any("到点了却没翻到" in line for line in said)


def test_a_report_that_is_due_but_missing_blames_the_trip_not_the_clock() -> None:
    """到点了还翻不到，那就是这一趟没翻到——说准了才修得动。"""
    now = datetime.now(UTC)
    loop, _events = _loop([])
    said: list[str] = []
    loop._ensure_run = lambda: (
        _DueRepository({A: (now.replace(year=now.year - 1), now.replace(year=now.year - 1))}),
        None,
    )
    loop._round_start = lambda: datetime(2026, 8, 6, tzinfo=UTC)
    loop._scan_mail = lambda wanted, visit, not_before=None: set(wanted)

    from evo_helper.tools import bot_loop as module

    original = module.say
    module.say = said.append
    try:
        loop.collect_battle_reports((A,))
    finally:
        module.say = original

    assert any("到点了却没翻到" in line for line in said)


def test_the_mail_trip_floor_is_the_dispatch_time_not_the_expected_report_time() -> None:
    """翻信箱的时间下界取**派出时刻**，不取预计战报时刻。

    预计时刻来自简报上的一次 OCR，实机上同一天同距离的六发读出 8 秒到 25 分钟
    不等；拿它当下界，一次读大就能把真报告挡在窗口外，而且完全静默。
    派出时刻是本地在游戏接受「出发！」那一刻记的，是硬事实。
    """
    dispatched = datetime(2026, 8, 11, 1, 7, tzinfo=UTC)
    expected = datetime(2026, 8, 11, 1, 33, tzinfo=UTC)
    floors: list[datetime | None] = []
    loop, _events = _loop([])
    loop._ensure_run = lambda: (_DueRepository({A: (dispatched, expected)}), None)
    loop._round_start = lambda: datetime(2026, 8, 11, tzinfo=UTC)
    loop._scan_mail = lambda wanted, visit, not_before=None: (
        floors.append(not_before),
        set(wanted),
    )[1]

    from evo_helper.tools import bot_loop as module

    original = module.say
    module.say = lambda _line: None
    try:
        loop.collect_battle_reports((A,))
    finally:
        module.say = original

    assert floors == [dispatched]


class _DueRepository:
    def __init__(self, due: dict[Coordinate, tuple[datetime, datetime | None]]) -> None:
        self._due = due

    def bot_report_due_at(
        self, coordinates: Any, *, since: datetime | None
    ) -> dict[Coordinate, tuple[datetime, datetime | None]]:
        return dict(self._due)


# -- 兜底路径也要拖 ----------------------------------------------------------


def test_the_fallback_read_also_scrolls_when_the_first_screen_has_no_units() -> None:
    """`read_defender_units` 是**兜底路径**，同样得拖到底。

    只修入库那一条不够：兜底这条读不出来就返回 None，而 None 会让
    `_tier_and_attack` 整个跳过这个目标——一个真有舰队的 bot 被静默放走，
    和「库里没数」长得一模一样。
    """
    loop, _events = _loop([_Page(A, units="")])
    loop._bottom_screens = lambda: _Page(A, units="319")

    assert loop.read_defender_units(A) == 319


def test_the_fallback_read_does_not_scroll_when_it_already_has_the_number() -> None:
    """看得见就别拖——拖一次要真的按住鼠标分步走两趟，而那个数已经在手上了。"""
    loop, _events = _loop([_Page(A, units="5.36K")])
    loop._bottom_screens = lambda: pytest.fail("第一屏读到了就不该再拖")

    assert loop.read_defender_units(A) == 5360


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
    # 合流后 `_tier_and_attack` 走父类的 `_goto_checked`（两条链路共用的自愈）。
    # 这两条测试钉的是「分档的数从哪来」，导航不在范围内，桩掉即可。
    loop._goto_checked = lambda coordinate: TargetCheck.CONFIRMED
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
    # 合流后 `_tier_and_attack` 走父类的 `_goto_checked`（两条链路共用的自愈）。
    # 这两条测试钉的是「分档的数从哪来」，导航不在范围内，桩掉即可。
    loop._goto_checked = lambda coordinate: TargetCheck.CONFIRMED
    loop.attack = lambda coordinate, *, preset: attacked.append((coordinate, preset))

    loop._tier_and_attack(A)

    assert attacked == [(A, "CCC")]


class _StoredUnits:
    def __init__(self, units: int | None) -> None:
        self._units = units

    def latest_defender_units(self, target: Coordinate, *, since: datetime) -> int | None:
        return self._units
