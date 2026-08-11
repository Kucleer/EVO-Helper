"""开工对账：数今天（UTC+0）信箱里已经有多少份本链路的攻击战报。

用户口径：「控制台启动先读今天（UTC+0）的邮箱，已发多少海盗、已生成多少攻击战报，
并同步更新任务数据，保证可以继续任务，并且不超额。」

对账**做在链路开工处**而不是控制台进程启动处：对账要看屏，而控制台自己不驱动
游戏（它只跑网页与调度）。链路开工时游戏窗口、会话、信箱导航全是现成的；
一天只做一次靠库里那条按 **UTC 日**去重的记录保证——按 UTC 日而不是按进程去重，
是因为配额的日界本来就是 UTC 00:00，而控制台一天可能重启好几次。

取大规则与「绝不伪造派遣」在 `tests/integration/storage/test_daily_reconciliation.py`。
这里守的是**信箱那一侧**：数什么、数到哪为止、翻不动怎么办。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import (
    RECONCILE_MAX_PAGES,
    LoopOptions,
    MailRow,
    PirateLoop,
    RoundExhausted,
)
from evo_helper.vision.parsers import ReportKind

DAY_START = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
NOON = DAY_START + timedelta(hours=12)


class _Driver:
    def click(self, x: int, y: int, *, label: str = "") -> None:
        return None

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
    def __init__(self, *, already: bool = False) -> None:
        self.already = already
        self.records: list[dict[str, Any]] = []

    def reconciled_on(self, target_kind: str, *, day_utc: datetime) -> bool:
        return self.already

    def record_daily_reconciliation(self, target_kind: str, **fields: Any) -> None:
        self.records.append({"target_kind": target_kind, **fields})


def _loop(pages: list[list[MailRow]], *, cls: type = PirateLoop) -> tuple[Any, _Repository]:
    repository = _Repository()
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
    screens = list(pages)
    loop._mail_list_rows = lambda: screens.pop(0) if screens else []
    return loop, repository


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
    loop, repository = _loop(
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
    loop, repository = _loop(
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
    loop, repository = _loop(
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

    翻到底才敢说那个数是完整的；`complete` 为真，计数那一侧才拿它去和库内计数取大。
    """
    loop, repository = _loop(
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
    loop, repository = _loop(pages)

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["complete"] is False
    assert repository.records[0]["observed_reports"] == RECONCILE_MAX_PAGES


def test_a_screen_with_nothing_new_is_not_a_complete_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """拖不动了（面板夹住 / 已经到底）同样不算「翻完了今天」。

    只有真的看见一行昨天的报告才算数——否则一次拖不动就会把半个信箱说成全部。
    """
    same = [_row(0, ReportKind.PIRATE, NOON)]
    loop, repository = _loop([list(same), list(same)])

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 1
    assert repository.records[0]["complete"] is False


def test_nothing_is_opened_while_reconciling(monkeypatch: pytest.MonkeyPatch) -> None:
    """对账**一封都不打开**：一屏的主题 ≈ 一两秒，开一封 ≈ 八秒。

    这就是「开销要有界」的落实处——把它做成逐封打开，开机对账就变成了
    把整个信箱翻一遍的长流程，挤掉真正要干的活。
    """
    clicks: list[str] = []
    loop, _repository = _loop(
        [
            [_row(0, ReportKind.PIRATE, NOON)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ]
    )
    loop._driver.click = lambda x, y, *, label="": clicks.append(label)

    _reconcile(loop, monkeypatch)

    assert "打开邮件" not in clicks


# -- 一天一次，以及翻不动时的降级 --------------------------------------------


def test_a_day_already_reconciled_is_not_scanned_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """一天一次。每轮都翻一趟就是每轮白花二三十秒，还一直占着鼠标。"""
    loop, repository = _loop([[_row(0, ReportKind.PIRATE, NOON)]])
    repository.already = True
    loop._open_mail = lambda: pytest.fail("今天已经对过账了，不该再进信箱")

    _reconcile(loop, monkeypatch)

    assert repository.records == []


def test_an_unreachable_mailbox_does_not_kill_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """对账翻不了信箱**不该把这一轮判死**。

    它只是让配额判据退回按库计数，也就是今天没修正的那个状态——不比没有对账更糟。
    而抛出去的话，`RuntimeError` 计入连续失败，三次就把整条链路自动停用。
    也不写记录：下一轮还要再试。
    """
    loop, repository = _loop([])
    loop._goto_planet_surface = lambda: False

    _reconcile(loop, monkeypatch)

    assert repository.records == []


def test_running_out_of_resources_still_ends_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RoundExhausted` 是 `RuntimeError` 的子类，但它**不是失败**，要原样传上去。

    吞掉它就等于把「这一轮没料了、正常收尾」变成「对账失败、继续开工」。
    """
    loop, _repository = _loop([])
    loop._open_mail = lambda: (_ for _ in ()).throw(RoundExhausted("同时派遣的舰队数量已达上限"))

    with pytest.raises(RoundExhausted):
        _reconcile(loop, monkeypatch)
