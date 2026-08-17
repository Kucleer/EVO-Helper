"""收 bot 战报这一侧：怎么认、怎么入库、算出来的战果怎么落进那一行。

认归属靠的是 VS 块里的目标坐标——这条路径上从头到尾没有一处读得到「这一份是
哪个预设打的」，也不需要读。「哪些目标交进来收」在 `test_bot_loop.py`（分态路由）
那一侧。

守三件事：

1. **入库前后各有一道闸门**：复核 VS 坐标、按报告时间去重。
2. **战果是算出来的**（剩余 = 单位 − 损失），算不出就留空。战果已经不参与判态了
   （平局重打于 2026-08-17 移除，见 `domain.bot_round`），但它仍是攻击日志与情报
   中心那一列的来源，留空就是一个补不回来的空洞。
3. **收不到时那句话要说准**：「还没到点」和「到点了却没翻到」处置相反。

进信箱的姿势（关浮层 → 切地表 → 开面板）与窗口那一侧（先读主题再决定开不开、
翻屏、按时间早停）是两条链路共用的部分，钉在 `test_pirate_loop_mailbox_entry.py`
与 `test_mail_scan_window.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.reconcile_cooldown import decide_reconcile
from evo_helper.tools.bot_loop import BotLoop, BotOptions
from evo_helper.tools.pirate_loop import LoopOptions, MailRow, ReportIngest
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
    """一个装好了「会碰屏的那些零件」的 `BotLoop`。

    `events` 记的是它有没有去动屏幕——`_say_still_waiting` 那几条正是靠这个
    断言「这句话是纯日志，不再进一趟信箱」。
    """
    events: list[str] = []
    loop = BotLoop.__new__(BotLoop)
    loop._bot = BotOptions(targets=(), attack=True)
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._started_at = datetime(2026, 8, 6, tzinfo=UTC)
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._reset_to_known_screen = lambda: events.append("关浮层")
    loop._goto_planet_surface = lambda: (events.append("切地表"), reachable)[1]
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")
    loop._open_mail = lambda: events.append("开信箱")
    # 拖回顶部另有专文（`test_mailbox_scroll_to_top.py`）。
    loop._scroll_mail_list_to_top = lambda: None
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


# -- 入库前后的两道闸门 ------------------------------------------------------


class _Repository:
    def __init__(self, *, already_stored: bool = False) -> None:
        self.already_stored = already_stored
        self.appended: list[Any] = []
        self.rematched: list[tuple[Coordinate, datetime]] = []

    def has_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        return self.already_stored

    def append_report(self, report: Any) -> None:
        self.appended.append(report)

    def rematch_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        """已在库里的那一行未必认领上了派遣，收报告那一趟顺手重认一次。"""
        self.rematched.append((target, reported_at_utc))
        return False


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

    ingest = _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A))
    assert ingest is ReportIngest.STORED
    assert len(repository.appended) == 1


def test_a_report_pointing_elsewhere_is_refused() -> None:
    """VS 块读了两遍（翻行时一遍、入库前一遍），两遍必须指向同一个目标。

    不复核的话，一次 OCR 抖动就足以把这份战报挂到别人头上——而挂错之后
    `append_report` 会拿错的目标坐标去认领派遣，闭合的是另一发。
    """
    repository = _Repository()

    ingest = _ingesting_loop(repository)._ingest_battle_report(B, _DetailScreens(A))
    assert ingest is ReportIngest.UNREADABLE
    assert repository.appended == []


def test_an_already_stored_report_is_not_appended_again() -> None:
    """信箱里那几行每趟都在。认领不上号的战报尤其危险：`has_report` 永远为假，
    于是下一趟又读同一封——没有这道去重，它会每趟复制一行。

    结论是 `KNOWN` 而不是「没入库」：开工那一趟拿它当早停凭据（信箱从新往旧排，
    第一份库里已有的往下都已经在库里了），而这一档必须与「读不出来」分开——
    见 `ReportIngest`。
    """
    repository = _Repository(already_stored=True)

    ingest = _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A))
    assert ingest is ReportIngest.KNOWN
    assert repository.appended == []


def test_an_unreadable_report_is_skipped_and_dumped() -> None:
    """读不出来就放过，**不存半份**，并留下现场。

    这一份就这么放着，等 `MAX_REPORT_AGE` 把那发派遣判掉、允许重打一发——
    这就是「报告就是读不到」时的出路，而不是让目标静默卡死。
    """
    repository = _Repository()
    loop = _ingesting_loop(repository)
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)

    ingest = loop._ingest_battle_report(A, _DetailScreens(A, header="装饰文字"))
    assert ingest is ReportIngest.UNREADABLE
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


# -- 战果以横幅为准，剩余舰艇数是兜底 ----------------------------------------


def test_the_outcome_reaches_the_stored_report() -> None:
    """攻击日志的战果列就取这一个字段（`web.service.AttackLogView.outcome`）。

    第一屏的横幅读作 `FAIL`，第二屏的四个数也给出 `FAIL`（我方 1 艘全损）——
    两条路一致，正是那五张探路战报的实际形状。战损照旧一起落库。
    """
    repository = _Repository()
    bottom = _DetailScreens(A, units=("1", "319"), losses=("1", "0"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].outcome == "FAIL"
    assert (repository.appended[0].attacker_losses, repository.appended[0].defender_losses) == (
        1,
        0,
    )


def test_a_victory_banner_reaches_the_stored_report() -> None:
    """横幅写 `VICTORY` 就存 `VICTORY`——哪怕四个数是齐的。"""
    repository = _Repository()
    bottom = _DetailScreens(A, units=("100", "783"), losses=("0", "783"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(
        A, _DetailScreens(A, units=("", ""), banner="VICTORY")
    )

    assert repository.appended[0].outcome == "VICTORY"


def test_both_sides_surviving_is_a_draw_when_the_banner_cannot_be_read() -> None:
    """**平局这一档没有横幅样本**，只会从兜底算式里出来。

    第一屏的横幅给一段实拍上真读到过的噪声，于是回落到四个数：两边都还有船。
    """
    repository = _Repository()
    bottom = _DetailScreens(A, units=("100", "783"), losses=("30", "200"))

    _ingesting_loop(repository, bottom)._ingest_battle_report(
        A, _DetailScreens(A, units=("", ""), banner="- a")
    )

    assert repository.appended[0].outcome == "DRAW"


def test_an_undecidable_outcome_stores_nothing_rather_than_a_defeat() -> None:
    """⚠️ 本文件最要紧的一条：**「没定出胜负」不能长成「打输了」。**

    横幅是噪声、战损又没拖到，两条路都不成——就存 None。真顶一档上去，
    攻击日志会出现一场根本没核过的败仗，和真败仗在页面上一模一样。
    """
    repository = _Repository()

    _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A, banner="Z ?"))

    assert repository.appended[0].outcome is None


def test_a_readable_banner_alone_is_enough_without_the_second_screen() -> None:
    """⚠️ 换判据最实在的一处收益：不拖第二屏也有战果。

    没拖到底就没有战损，算式一律给 None——2026-08-11 那版于是让这一格永远空着。
    """
    repository = _Repository()

    _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A, banner="FAIL"))

    assert repository.appended[0].outcome == "FAIL"
    assert repository.appended[0].attacker_losses is None


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
    """拖到底也没读到就留空。**不能拿 0 顶替**。

    0 会让 `剩余 = 单位 − 损失` 算成负数或零，于是一份读不出的战报会被记成
    一场全歼或一场惨败——而战果决定这个坐标要不要再挨一发。留空则整份不判，
    这是安全的那一侧（`domain.battle_outcome.survivors`）。
    """
    repository = _Repository()

    _ingesting_loop(repository)._ingest_battle_report(A, _DetailScreens(A, units=("", "")))

    assert repository.appended[0].defender_units is None


# -- 收不到时那句话要说准 ----------------------------------------------------


def _waiting_lines(loop: Any, target: Coordinate) -> list[str]:
    from evo_helper.tools import bot_loop as module

    said: list[str] = []
    original = module.say
    module.say = said.append
    try:
        loop._say_still_waiting(target)
    finally:
        module.say = original
    return said


def test_a_report_that_is_not_due_yet_says_so_instead_of_blaming_the_window() -> None:
    """「还没到点」和「到点了却没翻到」的处置完全相反，日志必须分开说。

    实机上六个目标一视同仁地报「还没出现在信箱最上面几行」，连续四趟同一句——
    而其中三发确实还没到点、另三发是**窗口不够大**。那句话把后者说成了前者，
    于是「窗口太小」这个正因被盖了整整一天。
    """
    now = datetime.now(UTC)
    loop, _events = _loop([])
    loop._ensure_run = lambda: (_DueRepository({A: (now, now.replace(year=now.year + 1))}), None)
    loop._round_start = lambda: datetime(2026, 8, 6, tzinfo=UTC)

    said = _waiting_lines(loop, A)

    assert any("才产生；接着等" in line for line in said)
    assert not any("到点了却没翻到" in line for line in said)


def _overdue_loop() -> Any:
    """一个「战报早该到了却还没有」的循环。到点判据由 `_DueRepository` 给。"""
    now = datetime.now(UTC)
    loop, _events = _loop([])
    loop._ensure_run = lambda: (
        _DueRepository({A: (now.replace(year=now.year - 1), now.replace(year=now.year - 1))}),
        None,
    )
    loop._round_start = lambda: datetime(2026, 8, 6, tzinfo=UTC)
    return loop


def test_a_report_that_is_due_but_missing_blames_the_trip_not_the_clock() -> None:
    """本轮**翻过**信箱却没找到，才可以说「没找到」。

    措辞里要有「翻过信箱」四个字：这句话与下面那条「本轮没翻信箱」是一对，
    读日志的人靠它们区分「我找过了，没有」和「我根本没去找」。
    """
    loop = _overdue_loop()
    loop._reconcile_decision = decide_reconcile(last_reconciled_at_utc=None, now=datetime.now(UTC))
    assert loop._reconcile_decision.sweep is True

    said = _waiting_lines(loop, A)

    assert any("本轮翻过信箱，没找到" in line for line in said)
    assert not any("本轮没翻信箱" in line for line in said)


def test_a_round_that_skipped_the_mailbox_never_claims_the_report_was_missing() -> None:
    """⚠️ **这条钉的就是那次两天的故障。**

    冷却中的那一轮**一封信都没开**，这时说「战报到点了却没翻到」是一句假话——
    2026-08-15 起整整两天，每一轮、每一个目标都在说它，而真相是这条链路根本
    没进过信箱。日志把「我找过了，没有」和「我根本没去找」说成同一句，
    故障就被伪装成了常态。

    所以措辞必须换掉，而且要带上**上次真正翻信箱的时刻**——那才是用户判断
    「这一发到底有没有人去看过」的依据。
    """
    now = datetime.now(UTC)
    last = now - timedelta(minutes=3)
    loop = _overdue_loop()
    loop._reconcile_decision = decide_reconcile(last_reconciled_at_utc=last, now=now)
    assert loop._reconcile_decision.sweep is False

    said = _waiting_lines(loop, A)

    assert any("本轮没翻信箱" in line for line in said)
    assert not any("本轮翻过信箱" in line for line in said)
    # 上次真正翻信箱是什么时候，必须写出来。
    assert any(f"{last:%Y-%m-%d %H:%M:%S} UTC" in line for line in said)


def test_saying_it_is_still_waiting_never_opens_the_mailbox() -> None:
    """这句话是**纯日志**：战报已经在开工那一趟收过了。

    再进一趟信箱要把「关浮层 → 切地表 → 开信箱 → 慢拖回顶 → 翻页 → 关面板」
    整套再付一遍（实机约 20 秒），而那几行报告刚刚才被同一个流程翻过。
    """
    loop, events = _loop([])
    loop._ensure_run = lambda: (_DueRepository({}), None)
    loop._round_start = lambda: datetime(2026, 8, 6, tzinfo=UTC)

    _waiting_lines(loop, A)

    assert events == []
    assert loop._driver.clicks == []


class _DueRepository:
    def __init__(self, due: dict[Coordinate, tuple[datetime, datetime | None]]) -> None:
        self._due = due

    def bot_report_due_at(
        self, coordinates: Any, *, since: datetime | None
    ) -> dict[Coordinate, tuple[datetime, datetime | None]]:
        return dict(self._due)
