"""**连着 N 封开出来都是「库里已有」就停止开封** —— 这道闸与它的失效探测。

## 在省什么（生产实测 2026-09-09）

进一趟信箱平均 **104.8 秒才入库一封新战报**，绝大部分花在白开上：

    新窗口（33 趟）：开封 278 封，入库 113 封
      · 未读强开   120 封 → 入库 119，白开 1     （白开率 0.8%）
      · 常规闸     162 封 → 入库 0，白开 162    （白开率 100%）

**162 封常规开封，一封新的都没有**，折算约 2 小时/天。

根因不是「白开判不出来」，是**早停从来不触发**：`_stop_after_known()` 要「待认领
单子空了」才肯停，可近 12 天有 153 发派遣到点没战报，6 小时的老化窗口里平均挂着
3.59 发 —— 单子为空的时刻只占 19.8%。所以本文件里的替身**一律让单子非空**：
那才是生产上 80% 时间的样子，也是唯一能把新闸单独隔出来的办法。

## ⚠️⚠️ 为什么必须是「连续」

2026-08-11 那颗雷（整段在 `_ingest_report_row`）：报告确实在库里、**却没接到该接
的那一发派遣**，于是「库里已有 ⇒ 往下都读过了」把四发派遣永久钉在「待战报」。

「连续」版遇到一封真入库就归零，所以它自己能翻案；「有一封白开就停」不能。
`test_one_real_ingest_resets_the_run` 就是钉这一条的，把「连续」改成「有就停」
必须让它变红。

## 取舍表（回放历史日志得出，见 `MAIL_MAX_KNOWN_RUN`）

| 保险值 | 新窗口漏掉入库 | 旧窗口漏掉入库（= 未读判据失效时的样子） |
|---|---|---|
| 1 | 0 | 123（10.3%） |
| **2** | **0** | 35（2.9%） |
| 3 | 0 | 25（2.1%） |

所以这道闸的安全性**整个建立在未读判据还活着上** —— 失效探测那一节
（`unread_gate_looks_silent`）就是为这句话准备的。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import (
    MAIL_MAX_KNOWN_RUN,
    UNREAD_SILENT_ROUNDS,
    KnownRunGate,
    LoopOptions,
    MailRow,
    MailScan,
    PirateLoop,
    ReportIngest,
    mail_blank_payload,
    say_mail_blank_tally,
    unread_gate_looks_silent,
)
from evo_helper.vision.parsers import ReportKind

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

#: 一个不是 `None` 的标定，专供失效探测那几条：`CALIBRATION is None` 时探测按设计
#: 不说话（那一档由 `say_mail_unread_tally` 每趟明说「判据没通电」）。
CALIBRATED = SimpleNamespace(measured_on="2026-09-08")


class _Driver:
    def click(self, x: int, y: int, *, label: str = "") -> None:
        return None

    def wait(self, seconds: float) -> None:
        return None


def _row(index: int, *, unread: bool | None = False, minutes_ago: int | None = None) -> MailRow:
    moment = NOW - timedelta(minutes=index if minutes_ago is None else minutes_ago)
    return MailRow(
        index=index,
        subject="海盗攻击报告",
        raw_time_text=moment.strftime("%d/%m/%Y %H:%M:%S"),
        reported_at_utc=moment,
        kind=ReportKind.PIRATE,
        unread=unread,
    )


def _loop(
    rows: list[MailRow],
    *,
    outcomes: list[ReportIngest],
    outstanding: int = 3,
    unrendered: frozenset[int] = frozenset(),
) -> tuple[Any, list[int]]:
    """一个只装了「翻信箱 + 入库」所需零件的循环。第二个返回值是开过的行号。

    `outcomes` 是每一封**按开封顺序**的入库结局；用完之后一直沿用
    `ReportIngest.KNOWN`（生产上白开就是这么一串一串来的）。

    `outstanding` 默认 3 ≈ 实测的 3.59 发：**单子非空是这里的默认状态**，
    这样 `_stop_after_known()` 永远说「接着往下开」，剩下的行为就只有新闸负责。

    `unrendered` 里的行号模拟「详情页没铺开」：`_open_mail_row` 那一层直接返回，
    `visit` 一次都不调 —— 也就是 `KnownRunGate.note()` 没被调到的那条路。
    """
    opened: list[int] = []
    script = list(outcomes)
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
    loop._already_in_library = lambda row, times: False
    loop._ingest_non_report_mail = lambda row, page: False
    pending = [SimpleNamespace(target=f"4:100:{index}") for index in range(outstanding)]
    loop._due_dispatches = lambda _now: list(pending)

    def _ingest(row: MailRow, page: Any) -> ReportIngest:
        return script.pop(0) if script else ReportIngest.KNOWN

    loop._ingest_report = _ingest
    screens = [list(rows)]
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []

    def _open(row: MailRow, visit: Any) -> bool:
        opened.append(row.index)
        if row.index in unrendered:
            return False
        return bool(visit(row, object()))

    loop._open_mail_row = _open
    return loop, opened


def _scan(loop: Any, gate: KnownRunGate, **kwargs: Any) -> MailScan:
    """开工那一趟的接法：`visit` 就是 `_ingest_report_row`，带上这道闸。"""

    def visit(row: MailRow, page: Any) -> bool:
        if loop._ingest_non_report_mail(row, page):
            return False
        return bool(loop._ingest_report_row(row, page, known_run=gate))

    defaults: dict[str, Any] = {
        "wanted": ReportKind.PIRATE,
        "label": "海盗攻击报告",
        "visit": visit,
        "max_pages": 1,
        "max_opens": pirate_loop.MAIL_MAX_OPENS,
        "known_run": gate,
    }
    return loop._scan_mail_rows(**{**defaults, **kwargs})


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)
    monkeypatch.setattr(pirate_loop, "warn", lambda _line: None)


# -- 触发：连着 N 封白开就停 --------------------------------------------------


def test_a_run_of_blank_opens_stops_the_opening() -> None:
    """⚠️ **连着 `MAIL_MAX_KNOWN_RUN` 封白开 → 后面一封都不再开。**

    单子上还挂着 3 发，所以 `_stop_after_known()` 每一封都说「接着往下开」——
    这一趟能停下来，只可能是新闸停的。
    """
    rows = [_row(index) for index in range(6)]
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=[ReportIngest.KNOWN] * 6)

    scan = _scan(loop, gate)

    assert opened == list(range(MAIL_MAX_KNOWN_RUN))
    assert scan.blank_run_stopped is True
    assert scan.blank_opens == MAIL_MAX_KNOWN_RUN
    assert scan.blank_run_max == MAIL_MAX_KNOWN_RUN
    assert scan.blank_run_limit == MAIL_MAX_KNOWN_RUN


def test_the_run_must_reach_the_limit_before_it_stops() -> None:
    """差一封就不许停。**只留上一条的话，「有一封就停」也能全绿。**

    剧本是「(N-1) 封白开、一封入库」循环铺满六行，最长连续段永远差一封到保险值。
    """
    rows = [_row(index) for index in range(6)]
    block = [*[ReportIngest.KNOWN] * (MAIL_MAX_KNOWN_RUN - 1), ReportIngest.STORED]
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=(block * 6)[:6])

    scan = _scan(loop, gate)

    assert len(opened) == 6, "一段没到保险值的白开不许截断这一趟"
    assert scan.blank_run_stopped is False
    assert scan.blank_run_max == MAIL_MAX_KNOWN_RUN - 1


# -- ⚠️⚠️ 归零：这道闸和「有一封就停」的全部差别 -----------------------------


def test_one_real_ingest_resets_the_run() -> None:
    """⚠️⚠️ **本文件最重要的一条：中间一封真入库 → 计数归零，接着往下开。**

    2026-08-11 那四发就躺在一封「库里已有」的下面（报告在库里却没认领上派遣）。
    「有一封白开就停」在那一刻收工，把它们永久钉死；而「连续」版开到那一封真入库
    的行时计数就清了，于是它**自己能翻案**。

    剧本（六行）：已有、已有…（差一封到保险值）、**入库**、已有、已有…
    要求：那一封入库之后仍旧接着开，直到又攒够一段连续的白开。
    """
    rows = [_row(index) for index in range(6)]
    # 前 N-1 封白开（差一封到保险值），第 N 封真入库归零，之后再攒满一段
    outcomes: list[ReportIngest] = [ReportIngest.KNOWN] * (MAIL_MAX_KNOWN_RUN - 1)
    outcomes.append(ReportIngest.STORED)
    outcomes.extend([ReportIngest.KNOWN] * MAIL_MAX_KNOWN_RUN)
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=outcomes)

    scan = _scan(loop, gate)

    assert len(opened) == len(outcomes), "真入库那一封必须把计数清掉，否则就是「有就停」"
    assert scan.blank_run_stopped is True, "归零之后重新攒满一段，仍旧该停"
    assert scan.blank_opens == 2 * MAIL_MAX_KNOWN_RUN - 1


def test_an_unreadable_report_also_resets_the_run() -> None:
    """读不出来的那一封**不算白开**，也把计数清零。

    `ReportIngest.UNREADABLE` 下面还可能躺着没入库的战报（`ReportIngest` 的注释：
    一次 OCR 抖动不许变成「今天剩下的都不读了」）。把它算进白开，等于让 OCR 抖动
    替早停做决定。
    """
    rows = [_row(index) for index in range(6)]
    gate = KnownRunGate()
    loop, opened = _loop(
        rows,
        outcomes=[
            ReportIngest.KNOWN,
            ReportIngest.UNREADABLE,
            ReportIngest.KNOWN,
            ReportIngest.UNREADABLE,
            ReportIngest.KNOWN,
            ReportIngest.STORED,
        ],
    )

    scan = _scan(loop, gate)

    assert len(opened) == 6
    assert scan.blank_run_stopped is False
    assert scan.blank_run_max == 1


def test_a_detail_page_that_never_rendered_is_not_a_blank_open() -> None:
    """⚠️ 详情页没铺开时 `visit` 一次都不调 → 那一封落在「不是白开」那一侧。

    `KnownRunGate.blank` 默认为假，所以没人交账就等于「这一封不是白开」。
    这一侧是安全的（只会多开几封）；反过来（默认算白开）会让一屏渲染故障
    直接把开封停掉，而那正是最该多看两眼的时候。
    """
    rows = [_row(index) for index in range(6)]
    gate = KnownRunGate()
    loop, opened = _loop(
        rows,
        outcomes=[ReportIngest.KNOWN, ReportIngest.KNOWN, ReportIngest.KNOWN],
        unrendered=frozenset({1}),
    )

    scan = _scan(loop, gate)

    assert len(opened) > MAIL_MAX_KNOWN_RUN, "没铺开的那一封不许顶替一封白开"
    assert scan.blank_run_max == MAIL_MAX_KNOWN_RUN


# -- 未读强开那一档不受这道闸约束 ---------------------------------------------


def test_unread_forced_opens_are_never_cut_by_this_gate() -> None:
    """⚠️⚠️ **未读强开越过这道闸**，而且它们的白开也不算进连续段。

    未读那一档实测白开率 0.8%（120 封只白开 1 封），本来就不该被截断；而它「必开」
    的理由（`MAIL_UNREAD_MAX_OPENS`：漏一封就再也读不回来）也不允许被一个按已读
    推出来的计数否掉。

    这里六行**全是未读**、六封全是白开：常规闸那一档一封都没花，这道闸不许说话。
    """
    rows = [_row(index, unread=True) for index in range(6)]
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=[ReportIngest.KNOWN] * 6)

    scan = _scan(loop, gate, max_opens=0)

    assert len(opened) == 6
    assert scan.unread_opened == 6
    assert scan.blank_opens == 0, "未读的白开混进来会把这个体检指标稀释掉"
    assert scan.blank_run_max == 0
    assert scan.blank_run_stopped is False


def test_a_run_of_read_rows_still_stops_while_unread_rows_keep_going() -> None:
    """混着来：前两行未读（照开），后面几行已读且白开 → 只截常规那一档。"""
    rows = [_row(0, unread=True), _row(1, unread=True), *[_row(index) for index in range(2, 6)]]
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=[ReportIngest.KNOWN] * 6)

    scan = _scan(loop, gate)

    assert opened[:2] == [0, 1], "两封未读照开"
    assert len(opened) == 2 + MAIL_MAX_KNOWN_RUN, "常规那一档攒满一段就该停"
    assert scan.blank_run_stopped is True


# -- 不接这道闸的调用方：行为逐字节不变 ---------------------------------------


def test_a_caller_without_the_gate_behaves_exactly_as_before() -> None:
    """⚠️ 不传 `known_run` 的那几条路（补录、侦察）一个字都不变。

    六行全白开、单子非空 → 照旧把 `max_opens` 开满。**账单上那一格是 `None`**，
    库里凭它就分得出「这条路没接这道闸」和「接了但没触发」。
    """
    rows = [_row(index) for index in range(6)]
    loop, opened = _loop(rows, outcomes=[ReportIngest.KNOWN] * 6)

    def visit(row: MailRow, page: Any) -> bool:
        return bool(loop._ingest_report_row(row, page))

    scan = loop._scan_mail_rows(
        wanted=ReportKind.PIRATE,
        label="海盗攻击报告",
        visit=visit,
        max_pages=1,
    )

    assert len(opened) == 6
    assert scan.blank_run_limit is None
    assert scan.blank_opens == 0
    assert scan.blank_run_stopped is False


def test_only_the_daily_path_wires_the_gate_in() -> None:
    """⚠️ 接线必须只落在 `_scan_for_reconcile` 上。

    补录那两个入口不接：那个入口存在的意义正是要够到被各种闸筛掉的邮件
    （`exhaustive` 那一档连早停都不走）。这一条读源码 —— 接错了跑流程也看不出来
    （几个入口的 `visit` 各不相同，而这道闸挂在 `visit` 交的那笔账上）。
    """
    from pathlib import Path

    source = Path(pirate_loop.__file__).read_text(encoding="utf-8")
    assert source.count("KnownRunGate()") == 1, "接这道闸的入口不该多于一个"

    head, _sep, tail = source.partition("def _scan_for_reconcile")
    assert "KnownRunGate()" not in head, "这道闸被接到了 `_scan_for_reconcile` 之前的入口上"
    assert "KnownRunGate()" in tail


# -- 日志：每趟至少报一次，没触发也报 -----------------------------------------


def test_the_tally_speaks_even_when_the_gate_never_trips() -> None:
    """⚠️⚠️ **没触发也要报数。**

    这道闸最可能的失效形态是「一直不触发」，而那和「今天没有白开」在日志上一模
    一样。所以没停的那一趟也要把白开数与**最长连续段**说出来 —— 有了连续段，
    「一封白开都没有」和「差一封就触发」才分得开。
    """
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]

    say_mail_blank_tally(MailScan(blank_run_limit=2, blank_opens=3, blank_run_max=1))

    assert len(said) == 1
    assert "白开 3 封" in said[0]
    assert "最长连着 1 封" in said[0]
    assert "没停" in said[0]


def test_the_tally_says_which_safety_number_it_used() -> None:
    """触发那一趟也报，而且报的是**这一趟真的用的那个保险值**。

    它是 `_scan_mail_rows` 的参数（等配置项上了就是页面上那个框），打日志的地方
    再去读模块常量就会报出一个假数字（记忆里「引用数字前先查出处」）。
    """
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]

    say_mail_blank_tally(
        MailScan(blank_run_limit=5, blank_opens=5, blank_run_max=5, blank_run_stopped=True)
    )

    assert any("（保险值 5）" in line for line in said)
    assert not any(f"（保险值 {MAIL_MAX_KNOWN_RUN}）" in line for line in said)


def test_the_tally_says_so_when_this_path_has_no_gate() -> None:
    """没接这道闸的那几趟也说一句：安静和「接了没触发」不许长得一样。"""
    said: list[str] = []
    pirate_loop.say = said.append  # type: ignore[assignment]

    say_mail_blank_tally(MailScan())

    assert any("没接这道闸" in line for line in said)


def test_the_payload_carries_a_key_only_the_new_code_writes() -> None:
    """`blank_run_limit` 就是那个只有新代码写得出的键（记忆里 #266 那条教训）。"""
    payload = mail_blank_payload(
        MailScan(blank_run_limit=2, blank_opens=4, blank_run_max=2, blank_run_stopped=True)
    )

    assert payload["blank_run_limit"] == 2
    assert payload["blank_opens"] == 4
    assert payload["blank_run_max"] == 2
    assert payload["blank_run_stopped"] is True


# -- 失效探测：这道闸的安全性建立在未读判据还活着上 ---------------------------


def _silent_scan() -> MailScan:
    """一趟「一封未读都没判出来」的账单。"""
    return MailScan(observed=6, unread_seen=0, blank_run_limit=MAIL_MAX_KNOWN_RUN)


def test_a_silent_round_with_an_empty_worklist_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单子空着时一封未读都没有，**再正常不过** —— 没人在等战报。

    少了这一条，探测会在每个闲下来的夜里刷告警，而刷出来的告警等于没有告警。
    """
    monkeypatch.setattr(pirate_loop, "CALIBRATION", CALIBRATED)

    assert unread_gate_looks_silent(_silent_scan(), outstanding=0) is False


def test_a_round_that_saw_unread_rows_is_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判出来过未读 → 判据活着。新窗口 34 趟里 `unread_seen` 一次都没有是 0。"""
    monkeypatch.setattr(pirate_loop, "CALIBRATION", CALIBRATED)
    scan = MailScan(observed=6, unread_seen=1, blank_run_limit=MAIL_MAX_KNOWN_RUN)

    assert unread_gate_looks_silent(scan, outstanding=3) is False


def test_an_uncalibrated_gate_is_not_reported_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 没标定时 `unread_seen` 按定义恒为 0，那不是「失效」而是「没通电」。

    那一档由 `say_mail_unread_tally` 每趟明说，这里再报一条 WARNING 只会把真正的
    失效淹掉。
    """
    monkeypatch.setattr(pirate_loop, "CALIBRATION", None)

    assert unread_gate_looks_silent(_silent_scan(), outstanding=3) is False


def _detector(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[str]]:
    monkeypatch.setattr(pirate_loop, "CALIBRATION", CALIBRATED)
    warned: list[str] = []
    monkeypatch.setattr(pirate_loop, "warn", warned.append)
    return PirateLoop.__new__(PirateLoop), warned


def test_one_silent_round_does_not_warn_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **单趟不许告警。**

    刚把一批未读开完、信箱确实清了的时候，单趟 `unread_seen == 0` 完全是真的。
    单趟就报的话，这条 WARNING 会天天出现，然后被当成噪音关掉 —— 而它是这道闸
    唯一的体检项。
    """
    loop, warned = _detector(monkeypatch)

    assert loop._note_unread_silence(_silent_scan(), outstanding=3) == 1
    assert warned == []


def test_two_silent_rounds_in_a_row_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """连着 `UNREAD_SILENT_ROUNDS` 趟就说话，而且把两个数都说出来。"""
    loop, warned = _detector(monkeypatch)

    for _ in range(UNREAD_SILENT_ROUNDS):
        rounds = loop._note_unread_silence(_silent_scan(), outstanding=4)

    assert rounds == UNREAD_SILENT_ROUNDS
    assert len(warned) == 1
    assert f"连着 {UNREAD_SILENT_ROUNDS} 趟" in warned[0]
    assert "还有 4 发" in warned[0]
    assert f"保险值 {MAIL_MAX_KNOWN_RUN}" in warned[0], "告警要说清受影响的是哪道闸"


def test_a_healthy_round_resets_the_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """中间只要有一趟判出未读，连续段就归零 —— 「连续」在这里同样是判据。

    少了归零，探测会把散落在几天里的几趟安静攒成一条告警，而那时人去查判据
    只会发现它好着。
    """
    loop, warned = _detector(monkeypatch)
    healthy = MailScan(observed=6, unread_seen=2, blank_run_limit=MAIL_MAX_KNOWN_RUN)

    assert loop._note_unread_silence(_silent_scan(), outstanding=3) == 1
    assert loop._note_unread_silence(healthy, outstanding=3) == 0
    assert loop._note_unread_silence(_silent_scan(), outstanding=3) == 1
    assert warned == [], "归零之后要重新攒够两趟才许说话"


def test_the_warning_keeps_repeating_while_it_stays_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一直哑就一直报（一趟一条）：修好之前它必须留在库里看得见的地方。"""
    loop, warned = _detector(monkeypatch)

    for _ in range(UNREAD_SILENT_ROUNDS + 2):
        loop._note_unread_silence(_silent_scan(), outstanding=3)

    assert len(warned) == 3


def test_the_detector_does_not_change_the_gate_behaviour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **报警之后不退回旧行为。** 探测只负责让失效被看见，处置留给人。

    三条理由写在 `_note_unread_silence` 上，最硬的一条是：判据只有「一封未读都没
    判出来」加「单子非空」，它自己会误报（一发丢了的派遣能在单子上挂满 6 小时）；
    让一个会误报的推断去改行为，误报的代价就从「多一行 WARNING」变成「每趟多开
    满一轮」—— 正好把这道闸省下来的时间还回去。

    这一条钉住那件事：连着报了好几趟之后，这一趟的开封仍旧按新闸停。
    """
    monkeypatch.setattr(pirate_loop, "CALIBRATION", CALIBRATED)
    rows = [_row(index) for index in range(6)]
    gate = KnownRunGate()
    loop, opened = _loop(rows, outcomes=[ReportIngest.KNOWN] * 6)
    for _ in range(UNREAD_SILENT_ROUNDS + 1):
        loop._note_unread_silence(_silent_scan(), outstanding=3)

    scan = _scan(loop, gate)

    assert len(opened) == MAIL_MAX_KNOWN_RUN, "告警不许把这道闸关掉，也不许把它变严"
    assert scan.blank_run_stopped is True


@pytest.fixture(autouse=True)
def _restore_say() -> Any:
    """`say` 是模块级函数，改了要还原 —— 否则后面的文件跟着受影响。"""
    original = pirate_loop.say
    yield
    pirate_loop.say = original  # type: ignore[assignment]
