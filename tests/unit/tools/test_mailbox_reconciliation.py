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

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY
from evo_helper.tools import pirate_loop
from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import (
    MAIL_MAX_OPENS,
    RECONCILE_MAX_PAGES,
    LoopOptions,
    MailboxUnreachable,
    MailRow,
    Outcome,
    PirateLoop,
    ReportIngest,
    RoundExhausted,
    exit_code_for,
    mail_row_from_text,
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


def _reread(index: int, at: datetime, subject: str) -> MailRow:
    """按 OCR **读到的原文**造一行：主题就是那串噪声，时间是实机的那个格式。

    走 `mail_row_from_text` 而不是直接构造 `MailRow`：主题与时间的认法要和实机
    同一条，否则钉住的是夹具而不是判据。
    """
    return mail_row_from_text(index, f"{subject}\n{at:%d/%m/%Y %H:%M:%S}\n")


class _Repository:
    def __init__(
        self,
        *,
        oldest_open: datetime | None = None,
        due: Sequence[Coordinate] = (),
        expected: Sequence[datetime | None] | None = None,
    ) -> None:
        self.oldest_open = oldest_open
        #: 单子：已派出、理论上该有战报、库里还没有的那些攻击发的目标。
        self.due = list(due)
        #: 单子上每一发的期望战报时刻。飞行时间没读到时是 None（见 `DueDispatch`）。
        self.expected = list(expected) if expected is not None else [None] * len(self.due)
        self.records: list[dict[str, Any]] = []

    def oldest_open_attack_at(
        self, target_kind: str, *, now_utc: datetime, max_age: timedelta
    ) -> datetime | None:
        return self.oldest_open

    def due_attack_dispatches(self, target_kind: str, **_fields: Any) -> list[Any]:
        return [
            SimpleNamespace(target=target, expected_report_at_utc=expected)
            for target, expected in zip(self.due, self.expected, strict=True)
        ]

    def record_daily_reconciliation(self, target_kind: str, **fields: Any) -> Any:
        self.records.append({"target_kind": target_kind, **fields})
        return None


class _Keeper:
    """`SessionKeeper` 的替身：只记「关窗重开被叫过几次」，并按剧本给结局。

    补的是升级那条路：翻不了信箱时**是不是真的走了既有的那条关窗重开**
    （配额 3 次 / 滚动 1 小时），而不是另起一套重试。
    """

    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.restarts: list[str] = []

    def restart_and_reenter(self, reason: str) -> Any:
        self.restarts.append(reason)
        return SimpleNamespace(ready=self.ready, detail="重开结局")


def _loop(
    pages: list[list[MailRow]],
    *,
    cls: type = PirateLoop,
    repository: _Repository | None = None,
    ingest: ReportIngest = ReportIngest.STORED,
    surfaces: Sequence[bool] = (True,),
    keeper: _Keeper | None = None,
) -> tuple[Any, _Repository, list[int]]:
    """一个只装了「开工那一趟」所需零件的循环。第三个返回值是开过的行号。

    `surfaces` 是每次 `_goto_planet_surface()` 的结果，用完之后一直沿用最后一个。
    默认 `(True,)` = 永远切得到地表，也就是改这条之前的行为。
    """
    repository = repository or _Repository()
    opened: list[int] = []
    loop = cls.__new__(cls)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)
    loop._started_at = NOON
    loop._driver = _Driver()
    loop._mail_dumps = 0
    loop._current_planet = None
    loop._navigator = SimpleNamespace(invalidate=lambda: None)
    loop._session_keeper = keeper or _Keeper()
    loop._keeper = lambda: loop._session_keeper
    loop._ensure_run = lambda: (repository, None)
    loop._reset_to_known_screen = lambda: None
    reachable = list(surfaces)
    loop._goto_planet_surface = lambda: reachable.pop(0) if len(reachable) > 1 else reachable[0]
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


def test_the_same_mail_is_not_counted_twice_when_its_subject_is_read_differently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一封战报在两屏上主题读成两样，**只能算一发**。

    数数走的是 `observe`，而 `observe` 只喂「没见过的行」。行身份原先取
    「主题 + 时间」，而主题那一格实拍上一字不差的是 0 行——噪声不同、类型却仍认得出
    （实拍里 `'ea 侦察报告'`、`'wet 侦察报告'`、`'《oO 侦察报告'` 是三行的三副样子），
    于是同一封被数了两遍。

    数多了不像数少了那样会超额（`DailyTally` 里那一段），但它同样是把配额记错：
    以为今天已经打够了，于是**提前收手**，白放着额度不打。
    """
    at = NOON - timedelta(minutes=5)
    kept = NOON - timedelta(minutes=20)
    first = [
        _row(0, ReportKind.PIRATE, at),
        _reread(1, kept, "大 sw, 海盗攻击报告 band"),
    ]
    # 往下拖了一下：上一屏最后那封滑到最上面，时间没变，主题换了一副样子。
    second = [
        _reread(0, kept, "26 海盗攻击报告 bad"),
        _row(1, ReportKind.PIRATE, DAY_START - timedelta(minutes=1)),
    ]
    loop, repository, _opened = _loop([first, second])

    _reconcile(loop, monkeypatch)

    assert repository.records[0]["observed_reports"] == 2, "同一封战报被数成了两发"


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


def test_a_pending_dispatch_keeps_the_opening_going_past_a_known_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **本次修复的落点。** 由库驱动：单子上还有没找到的，就不能因为撞见
    一份已入库的战报就收工。

    早停假定「库里已有 ⇒ 往下都读过了」。实机（2026-08-11）推翻了它：那四发 AAA
    的战报确实在库里，只是没接到该接的那一发派遣上（成因见
    `repository._unmatched_dispatch_candidates`：同一目标当天先侦察后攻击，
    而侦察发本不该当候选）。于是每一趟都在第一封就收工，要找的那几封就躺在
    它下面，那几个目标永远停在「待战报」。

    ⚠️ 早停本身**不删**（用户明确要的）——它只是要先问过那张单子。
    """
    loop, _repository, opened = _loop(
        [
            [_row(index, ReportKind.PIRATE, NOON) for index in range(4)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        repository=_Repository(due=[Coordinate(2, 138, 2)]),
        ingest=ReportIngest.KNOWN,
    )

    _reconcile(loop, monkeypatch)

    assert opened == [0, 1, 2, 3], "单子上还有一发没找到，不该在第一封「已有」就收工"


def test_the_worklist_line_says_when_the_latest_report_is_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """开工那一句要说清「在等的那几发**最晚该在什么时候**」。

    用户口径（2026-08-18）：「应首先验证邮件时间与待读战报」。紧接着
    `_scroll_mail_list_to_top` 会打「第 0 行是什么时候的邮件」，两行并排就能
    一眼看出邮箱最上面那封比在等的那几发新还是旧——而这正是 2026-08-18
    那一趟排障时日志里**缺的东西**。

    ⚠️ 它只进日志，**不参与「要不要拖」的判定**：`expected_report_at_utc` 与战报
    真正的时刻实测差 −4…+219 秒，拿它去跳过拖动就是拿漏战报赌 OCR 准不准
    （理由整段在 `_scroll_mail_list_to_top`）。
    """
    said: list[str] = []
    monkeypatch.setattr(pirate_loop, "say", said.append)
    early = NOON - timedelta(minutes=30)
    late = NOON - timedelta(minutes=5)
    loop, _repository, _opened = _loop(
        [[_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))]],
        repository=_Repository(
            due=[Coordinate(2, 56, 20), Coordinate(2, 57, 5)], expected=[early, late]
        ),
    )

    _reconcile(loop, monkeypatch)

    worklist = next(line for line in said if "到点还没战报" in line)
    assert late.strftime("%Y-%m-%d %H:%M:%S") in worklist
    assert early.strftime("%Y-%m-%d %H:%M:%S") not in worklist, "要的是最晚那一发"


def test_the_worklist_line_admits_when_it_cannot_bound_the_latest_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有一发没读到飞行时间时，**照实说说不出上界**，不许拿其余几发的最大值冒充。

    `expected_report_at_utc` 为 NULL 的那一档是「飞行时间没读到，当作现在就该有」
    ——它的战报可能是任何时刻。把它当不存在，日志上那句话就成了假话。
    """
    said: list[str] = []
    monkeypatch.setattr(pirate_loop, "say", said.append)
    loop, _repository, _opened = _loop(
        [[_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))]],
        repository=_Repository(
            due=[Coordinate(2, 56, 20), Coordinate(2, 57, 5)], expected=[NOON, None]
        ),
    )

    _reconcile(loop, monkeypatch)

    worklist = next(line for line in said if "到点还没战报" in line)
    assert "说不出最晚该在什么时候" in worklist
    assert NOON.strftime("%Y-%m-%d %H:%M:%S") not in worklist


def test_an_empty_worklist_restores_the_early_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """单子空了，早停照旧生效——它省的是每封八秒的开封，不能因为修盲点就丢掉。"""
    loop, _repository, opened = _loop(
        [
            [_row(index, ReportKind.PIRATE, NOON) for index in range(4)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        repository=_Repository(due=[]),
        ingest=ReportIngest.KNOWN,
    )

    _reconcile(loop, monkeypatch)

    assert opened == [0]


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


def test_an_unreachable_mailbox_does_not_kill_the_round_when_nothing_is_owed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**单子为空**时翻不了信箱不该把这一轮判死。

    它只是让配额判据退回按库计数，也就是今天没修正的那个状态——不比没有对账更糟。
    而抛出去的话，`RuntimeError` 计入连续失败，三次就把整条链路自动停用。
    也不写记录：下一轮还要再试。

    ⚠️ 这一条的前提是 `due=[]`。单子非空时结论完全相反，见下面那几条。
    """
    keeper = _Keeper()
    loop, repository, _opened = _loop(
        [], repository=_Repository(due=[]), surfaces=(False,), keeper=keeper
    )

    _reconcile(loop, monkeypatch)

    assert repository.records == []
    assert keeper.restarts == [], "单子空着的时候不该为了对账去关一次 Chrome"


# -- 单子非空却翻不了信箱：升级，然后必须以可见的方式收场 ----------------------


def test_a_pending_worklist_escalates_to_a_window_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **本次修复的落点之一。**

    2026-08-12：BOT 在 6 小时死线内只跑起过三轮，其中两轮（23:51 / 00:30）都把
    单子上那 10 发、15 发一个不落地打印出来，下一行就「这一轮先按库内计数走」。
    那 21 发的钟一直在走，两轮撞同一堵墙 = 永久判缺失。

    单子非空时唯一正确的动作是**升级**：走既有的关窗重开（`SessionKeeper`，
    配额 3 次 / 滚动 1 小时）再翻一次，而不是把名单打印完就走。
    """
    keeper = _Keeper()
    loop, repository, _opened = _loop(
        [
            [_row(0, ReportKind.PIRATE, NOON)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
        # 第一趟切不到地表，重开之后切得到。
        surfaces=(False, True),
        keeper=keeper,
    )

    _reconcile(loop, monkeypatch)

    assert len(keeper.restarts) == 1, "单子非空却翻不了信箱，必须升级重启一次"
    assert repository.records[0]["observed_reports"] == 1, "重开之后那一趟的账要真的记下来"


def test_the_retry_counts_from_scratch(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试要用一份**干净的账**，不能接着失败那一趟继续加。

    `DailyTally` 是边翻边累加的。第一趟已经数了两行才倒下去，拿同一个对象再翻
    一遍，这两行会被数两遍——而这个数就是「今日 X/32」显示的东西。
    """
    page = [_row(0, ReportKind.PIRATE, NOON), _row(1, ReportKind.PIRATE, NOON)]
    yesterday = [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))]
    loop, repository, _opened = _loop(
        # 两趟各看同样的两行 + 一行昨天的。数对了是 2，接着上一趟加就是 4。
        [list(page), list(yesterday), list(page), list(yesterday)],
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
    )
    # 第一趟一直翻到底，最后一步（关信箱）才倒下去。
    calls: list[int] = []

    def close_mail() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("读完邮件切不回恒星系视图")

    loop._close_mail = close_mail

    _reconcile(loop, monkeypatch)

    assert calls == [1, 1], "该翻两趟"
    assert repository.records[0]["observed_reports"] == 2, "重试那一趟只该数它自己翻到的两行"


def test_a_restart_that_does_not_come_back_fails_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重开之后回不到游戏内 → `MailboxUnreachable`，整轮判失败。

    这一档不能吞：`restart_and_reenter` 拒绝多半是**配额用完了**（1 小时内已经
    重开过 3 次），也就是环境正在持续坏着，而单子上那几发还在倒计时。

    ⚠️ 剧本刻意写成「重开被拒，但下一趟本来是切得到地表的」：不这么写，
    把「重开被拒就抛」这一句删掉之后，第二趟照样失败、照样抛，这条就永远是绿的
    ——那就成了一条拿被守代码当尺子的假测试。
    """
    loop, _repository, _opened = _loop(
        [
            [_row(0, ReportKind.PIRATE, NOON)],
            [_row(0, ReportKind.PIRATE, DAY_START - timedelta(minutes=1))],
        ],
        repository=_Repository(due=[Coordinate(2, 56, 20)]),
        surfaces=(False, True),
        keeper=_Keeper(ready=False),
    )

    with pytest.raises(MailboxUnreachable):
        _reconcile(loop, monkeypatch)


def test_still_unreachable_after_the_restart_fails_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **本次修复的另一半。** 重开之后还是翻不了 → 这一轮必须以失败收场。

    光有重试不够。用户的原话是「不许打印完受害名单就走人」：升级之后仍然翻不了
    而单子非空时，这一轮要计入失败（退出码 1、连撞三次自动停用并报警），
    而不是把名单打印完照常跑目标循环。

    也不能报 `EXIT_ENVIRONMENT_BUSY`——那一档**不计入连续失败**，准入条件是
    「会自己好」，而这里已经关窗重开过一次仍然不行。
    """
    loop, repository, _opened = _loop(
        [],
        repository=_Repository(due=[Coordinate(2, 56, 20), Coordinate(2, 57, 5)]),
        surfaces=(False,),
    )

    with pytest.raises(MailboxUnreachable) as caught:
        _reconcile(loop, monkeypatch)

    assert "2:56:20" in str(caught.value), "受害名单要写进异常，否则日志上只剩一句「翻不了」"
    assert repository.records == [], "没翻成就不许写当日对账记录"


def test_the_round_ends_as_a_failure_instead_of_sweeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run()` 收到 `MailboxUnreachable` → 记 `Outcome.failed`、**不跑目标循环**、退出码 1。

    这条守的是「不能打印完名单照常跑目标循环」那一句：2026-08-12 那两轮在放弃
    对账之后照样把 386 个目标走了一遍，而库里的态全靠战报推进——战报一份都没读
    进来，那一趟目标循环只会把上一轮的判断重复一遍。
    """
    loop, _repository, _opened = _loop([])
    loop._options.reconcile_on_start = True
    swept: list[int] = []
    loop._sweep = lambda: swept.append(1)
    loop.reconcile_today = lambda: (_ for _ in ()).throw(MailboxUnreachable("翻不了信箱"))
    loop.ensure_origin_planet = lambda: True
    loop._require_system_view = lambda what: None
    loop._ensure_session = lambda force=False: False
    loop._outcome = Outcome()
    monkeypatch.setattr("evo_helper.game.game_window.ensure_game_window", lambda: None)

    outcome = loop.run()

    assert swept == [], "对账都没做成，不该接着跑目标循环"
    assert outcome.failed
    assert exit_code_for(outcome) == 1


def test_a_busy_exit_code_is_not_reused_for_a_failed_mailbox() -> None:
    """`EXIT_ENVIRONMENT_BUSY` 是**不计入连续失败**的那一档，不许挪来盖这个场景。

    挪过去的后果正是这次要修的那种静默：任务整夜显示「在跑」，每轮打印一遍受害
    名单就退，不计故障、不报警，而战报一份都没读回来。
    """
    assert exit_code_for(Outcome(failed="翻不了信箱")) == 1
    assert exit_code_for(Outcome(failed="翻不了信箱")) != EXIT_ENVIRONMENT_BUSY
    # 没失败时原来那两档一个字没变。
    assert exit_code_for(Outcome()) == 0
    assert exit_code_for(Outcome(busy="切不到出发星球")) == EXIT_ENVIRONMENT_BUSY


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
