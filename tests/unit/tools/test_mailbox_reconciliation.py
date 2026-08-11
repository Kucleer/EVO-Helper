"""开工那一趟信箱：**先把战报读进库，再数今天已经打了几发**。

用户口径（2026-08-11）：「任务启动先去读战报……读完后，需要更新海盗攻击/bot 攻击
的数量，因为我可能暂停任务重启启动。」

做在链路开工处而不是控制台进程启动处：这件事要看屏，而控制台自己不驱动游戏
（它只跑网页与调度）。链路开工时游戏窗口、会话、信箱导航全是现成的。

读与数**共用同一趟**，但**两笔预算互不牵连**——这是本文件最要紧的一条：

- 开封（读战报）受 `MAIL_MAX_OPENS` 和「读到库里已有的那一份」两道早停限制，
  因为开一封约八秒；
- 数数只受 `RECONCILE_MAX_PAGES` 与「翻到昨天」限制。换过库、当天战报一份都没
  入库的那天，开封八封就到顶了，而今天可能已经打了 20 发——计数跟着停就是把
  20 记成 8，**而计数偏小正是会超额的那一侧**。

取大规则与「绝不伪造派遣」在 `tests/integration/storage/test_daily_reconciliation.py`。
这里守的是**信箱那一侧**：数什么、数到哪为止、翻不动怎么办。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import (
    MAIL_MAX_OPENS,
    RECONCILE_MAX_PAGES,
    LoopOptions,
    MailRow,
    PirateLoop,
    ReportIngest,
    RoundExhausted,
)
from evo_helper.vision.parsers import ReportKind

DAY_START = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
NOON = DAY_START + timedelta(hours=12)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, seconds: float) -> None:
        return None


def _row(index: int, kind: ReportKind, at: datetime) -> MailRow:
    return MailRow(
        index=index,
        subject={
            ReportKind.PIRATE: "海盗攻击报告",
            ReportKind.ATTACK: "攻击报告",
            ReportKind.SCOUT: "侦察报告",
        }[kind],
        raw_time_text=at.strftime("%d/%m/%Y %H:%M:%S"),
        reported_at_utc=at,
        kind=kind,
    )


class _Repository:
    def __init__(self, *, oldest_open: datetime | None = None) -> None:
        self.oldest_open = oldest_open
        self.records: list[dict[str, Any]] = []

    def oldest_open_attack_at(
        self, target_kind: str, *, now_utc: datetime, max_age: timedelta
    ) -> datetime | None:
        return self.oldest_open

    def record_daily_reconciliation(self, target_kind: str, **fields: Any) -> None:
        self.records.append({"target_kind": target_kind, **fields})


def _loop(
    pages: list[list[MailRow]],
    *,
    cls: type = PirateLoop,
    repository: _Repository | None = None,
    ingest: ReportIngest = ReportIngest.STORED,
) -> tuple[Any, _Repository, list[int]]:
    """一个只装了「开工那一趟」所需零件的循环。第三个返回值是开过的行号。"""
    repository = repository or _Repository()
    opened: list[int] = []
    loop = cls.__new__(cls)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._started_at = NOON
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._ensure_run = lambda: (repository, None)
    loop._reset_to_known_screen = lambda: None
    loop._goto_planet_surface = lambda: True
    loop._dump_frame = lambda name, roi=None: None
    loop._open_mail = lambda: None
    loop._close_mail = lambda: None
    loop._settle = lambda predicate, **_kwargs: True
    loop._on_mail_list = lambda: True
    loop._on_mail_detail = lambda: True
    loop._report_screens = lambda: object()
    loop._ingest_report = lambda row, page: (opened.append(row.index), ingest)[1]
    screens = list(pages)
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []
    return loop, repository, opened


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)


def _reconcile(loop: Any, monkeypatch: pytest.MonkeyPatch, *, now: datetime = NOON) -> None:
    """把 `datetime.now(UTC)` 钉死在一个已知的 UTC 日上。"""
    from evo_helper.tools import pirate_loop

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return now

    monkeypatch.setattr(pirate_loop, "datetime", _Clock)
    loop.reconcile_today()


# -- 数什么 ------------------------------------------------------------------


def test_only_this_chains_reports_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """海盗链路数「海盗攻击报告」，侦察报告和打 bot 的攻击报告都不是它的一发。

    数混了就是把配额记错：多数会提前收手（少打），少数会超额（白飞舰队）。
    """
    loop, repository, _opened = _loop(
        [
            [
                _row(0, ReportKind.PIRATE, NOON),
                _row(1, ReportKind.SCOUT, NOON),
                _row(2, ReportKind.ATTACK, NOON),
                _row(3, ReportKind.PIRATE, NOON),
            ],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ]
    )

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 2


def test_the_bot_chain_counts_the_plain_attack_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """打 bot 的战报主题是「攻击报告」，海盗战是「海盗攻击报告」——两条链路各数各的。"""
    loop, repository, _opened = _loop(
        [
            [_row(0, ReportKind.ATTACK, NOON), _row(1, ReportKind.PIRATE, NOON)],
            [_row(0, ReportKind.ATTACK, DAY_START - timedelta(minutes=1))],
        ],
        cls=BotLoop,
    )

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 1


def test_yesterdays_reports_are_not_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """日界是 UTC 00:00（游戏时间），不是本地日历天。

    用本地 UTC+8 的日期去切，本地 0–8 点那几个钟头会把当天 00:00–16:00 UTC
    派出去的全漏掉——海盗以为还有额度，超限的代价是舰队被强制返回。
    """
    loop, repository, _opened = _loop(
        [
            [
                _row(0, ReportKind.PIRATE, DAY_START + timedelta(minutes=5)),
                _row(1, ReportKind.PIRATE, DAY_START - timedelta(minutes=5)),
                _row(2, ReportKind.PIRATE, DAY_START - timedelta(hours=3)),
            ]
        ]
    )

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 1


# -- 数到哪为止 --------------------------------------------------------------


def test_reaching_yesterday_ends_the_scan_and_marks_it_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表按时间倒序，翻到昨天的那一行就到底了——**这才是正常的停止条件**。

    翻到底才敢说那个数是完整的；`complete` 为真，日志与库里才说得清它是全天的。
    """
    loop, repository, _opened = _loop(
        [
            [_row(0, ReportKind.PIRATE, NOON), _row(1, ReportKind.PIRATE, NOON)],
            [
                _row(0, ReportKind.PIRATE, DAY_START + timedelta(minutes=1)),
                _row(1, ReportKind.PIRATE, DAY_START - timedelta(minutes=1)),
            ],
            [_row(0, ReportKind.PIRATE, NOON)],  # 不该翻到这一屏
        ]
    )

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 3
    assert repository.records[0]["complete"] is True


def test_running_out_of_pages_marks_the_count_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    """没翻到昨天就到了上限：那个数只是「至少这么多」，必须自报家门。

    它照样参与配额取大（下界也是证据，而且往往比库内计数更紧），但要标出来——
    否则日后没人分得清「今天只打了 3 发」和「只数到 3 发」，而这两件事对
    「还能不能接着打」的含义完全相反。
    """
    pages = [
        [_row(0, ReportKind.PIRATE, NOON - timedelta(minutes=page))]
        for page in range(RECONCILE_MAX_PAGES + 2)
    ]
    loop, repository, _opened = _loop(pages)

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["complete"] is False
    assert repository.records[0]["observed_reports"] == RECONCILE_MAX_PAGES


def test_a_screen_with_nothing_new_is_not_a_complete_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """拖不动了（面板夹住 / 已经到底）同样不算「翻完了今天」。

    只有真的看见一行昨天的报告才算数——否则一次拖不动就会把半个信箱说成全部。
    """
    same = [_row(0, ReportKind.PIRATE, NOON)]
    loop, repository, _opened = _loop([list(same), list(same)])

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 1
    assert repository.records[0]["complete"] is False


# -- 读与数：同一趟，两笔预算 ------------------------------------------------


def test_the_reports_are_actually_read_on_the_way(monkeypatch: pytest.MonkeyPatch) -> None:
    """开工这一趟要**真的把战报读进库**（用户口径：任务启动先去读战报）。

    只数不读，攻击日志的战果列永远空着，「这一发打完了没有」也永远落不了库。
    """
    loop, _repository, opened = _loop(
        [
            [_row(0, ReportKind.PIRATE, NOON), _row(1, ReportKind.PIRATE, NOON)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ]
    )

    _reconcile(loop, monkeypatch)

    assert opened == [0, 1]


def test_a_report_already_in_the_database_stops_the_opening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**读到库里已有的那一份就不再开封**（用户口径 2026-08-11）。

    信箱从新往旧排，入库也是从新往旧写的，所以第一份「已有」往下的每一份都必然
    已经在库里——再开下去只是一封封确认「已有」，每封约八秒。同一天多次启动时
    这条尤其要紧：每次重启都要重新翻一遍信箱。
    """
    loop, _repository, opened = _loop(
        [
            [_row(index, ReportKind.PIRATE, NOON) for index in range(4)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        ingest=ReportIngest.KNOWN,
    )

    _reconcile(loop, monkeypatch)

    assert opened == [0], "第一封就是库里已有的，后面几封不该再开"


def test_the_count_keeps_going_after_the_opening_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """**本文件的重点。** 开封停了，数数还要接着数。

    「库里已有多少份」和「信箱里今天有多少份」是两件事，而配额要的是后者。
    早停是为了省下每封八秒的开封，不是为了少数几行——把两者绑在一起，
    换过库的那天就会把「今天打了 20 发」记成「打了 1 发」，
    于是助手以为还剩 31 发可打。
    """
    loop, repository, opened = _loop(
        [
            [_row(index, ReportKind.PIRATE, NOON - timedelta(minutes=index)) for index in range(4)],
            [
                _row(index - 4, ReportKind.PIRATE, NOON - timedelta(minutes=index))
                for index in range(4, 8)
            ],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        ingest=ReportIngest.KNOWN,
    )

    _reconcile(loop, monkeypatch)

    assert opened == [0]
    assert repository.records[0]["observed_reports"] == 8
    assert repository.records[0]["complete"] is True


def test_the_open_budget_never_truncates_the_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """开封预算用完（换过库的那天，每一封都是新的）同样不许把计数截断。

    这是上一条的另一半：一个是「收齐了」，一个是「开够了」，两条早停都只管开封。
    """
    rows_per_page = MAIL_MAX_OPENS
    loop, repository, opened = _loop(
        [
            [
                _row(index, ReportKind.PIRATE, NOON - timedelta(minutes=page))
                for index in range(rows_per_page)
            ]
            for page in range(3)
        ]
        + [[_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))]]
    )

    _reconcile(loop, monkeypatch)

    assert len(opened) == MAIL_MAX_OPENS
    assert repository.records[0]["observed_reports"] == rows_per_page * 3
    assert repository.records[0]["complete"] is True


# -- 每次开工都数，以及翻不动时的降级 ----------------------------------------


def test_every_start_counts_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """**不再是一天一次。** 用户会暂停任务再重启，重启之后「今日 X/32」必须接得上。

    一天一次意味着早上那次对账之后，库外发生的事（用户手动打的、进程崩在写库
    之前的那些）当天再也不会被数进来——而那正是对账存在的全部理由。
    这一趟本来就要跑（战报得有人读），多数一遍只是几行窄 ROI 的 OCR。

    重复数不会把数越描越小：仓储层按 UTC 日取大（见
    `tests/integration/storage/test_daily_reconciliation.py`）。
    """
    repository = _Repository()
    rows = [
        [_row(0, ReportKind.PIRATE, NOON)],
        [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
    ]
    for _ in range(2):
        loop, _repository, _opened = _loop([list(page) for page in rows], repository=repository)
        _reconcile(loop, monkeypatch)

    assert [record["observed_reports"] for record in repository.records] == [1, 1]


def test_an_unreachable_mailbox_does_not_kill_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """翻不了信箱**不该把这一轮判死**。

    它只是让配额判据退回按库计数，也就是今天没修正的那个状态——不比没有对账更糟。
    而抛出去的话，`RuntimeError` 计入连续失败，三次就把整条链路自动停用。
    也不写记录：下一轮还要再试。
    """
    loop, repository, _opened = _loop([])
    loop._goto_planet_surface = lambda: False

    _reconcile(loop, monkeypatch)

    assert repository.records == []


def test_running_out_of_resources_still_ends_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RoundExhausted` 是 `RuntimeError` 的子类，但它**不是失败**，要原样传上去。

    吞掉它就等于把「这一轮没料了、正常收尾」变成「对账失败、继续开工」。
    """
    loop, _repository, _opened = _loop([])
    loop._open_mail = lambda: (_ for _ in ()).throw(RoundExhausted("同时派遣的舰队数量已达上限"))

    with pytest.raises(RoundExhausted):
        _reconcile(loop, monkeypatch)


# -- 翻到哪一行为止 ----------------------------------------------------------


def test_the_floor_is_the_utc_day_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有在等战报的派遣时，翻到今天的 UTC 日界就够了——再往下都与配额无关。"""
    loop, _repository, _opened = _loop([])
    assert loop._report_floor(DAY_START, now=NOON) == DAY_START


def test_the_floor_reaches_back_to_a_dispatch_still_awaiting_its_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨过 UTC 午夜还在等的那一发，战报写着昨天的时间。

    翻到日界就停的话它永远读不到，那一发要一直挂到 `MAX_REPORT_AGE`（6 小时）
    才被判缺失——bot 那边还要连带把目标退回去重打一遍。所以下界取「日界」与
    「最早那发还在等战报的攻击派于何时」的更早者。
    """
    yesterday = DAY_START - timedelta(minutes=40)
    loop, _repository, _opened = _loop([], repository=_Repository(oldest_open=yesterday))

    assert loop._report_floor(DAY_START, now=DAY_START + timedelta(minutes=20)) == yesterday


def test_a_dispatch_from_today_does_not_widen_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """今天派出去的那些不用把窗口撑宽：它们的战报本来就在日界之内。"""
    loop, _repository, _opened = _loop([], repository=_Repository(oldest_open=NOON))

    assert loop._report_floor(DAY_START, now=NOON) == DAY_START
