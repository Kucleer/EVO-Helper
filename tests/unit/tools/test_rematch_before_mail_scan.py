"""库内补认领的刷新周期是「**每一趟真进信箱**」，不是「用户重启控制台」。

`repository.rematch_unlinked_reports()` 只查库、不读页面：它遍历
`dispatch_id IS NULL` 的战报，拿行上自带的目标与时刻走 `_link_dispatch` 的唯一
候选规则补认领。它原先只在点「开始」时跑一次
（`application.mission_scheduler.start`）。

## 为什么那个周期不够

紧接着要做的一件事是：开封之前按「这个时刻库里有没有战报」把已入库的邮件跳过
去。跳过之后，一份「在库里但没认领」的报告就**再也不会被重新开封**，也就再也
没有机会在开封路径上补认领（`pirate_loop.rematch_note` 那条路）。那正是
2026-08-11 踩过的坑，全过程记在 `repository.rematch_report_at`：报告确实在库
里、`dispatch_id` 全空、四发派遣永远停在「待战报」。

所以这个文件钉的是**接线**，四件事：

1. 真要进信箱的那一趟调了它，而且**排在问单子之前**——补认领会把几发派遣从
   「到点还没战报」里销掉，而那个 N 是排障的人拿去对信箱的数。
2. 对账被冷却跳过、这一趟根本不进信箱的那些趟**不调**它（2026-09-07 有 66 趟
   是这种）。给它们也跑一趟库内扫，等于在最频繁的那条路径上白加一段查询。
3. 份数与**耗时**都进那一趟的收尾。用户口径（2026-09-07）：「它内部会逐份查询
   候选，并非单次 SQL，将实际耗时计入汇总即可」——它慢下来的样子是「每趟信箱
   前面多等几十秒」，不报出来只会被当成信箱本身变慢。
4. ⚠️ **它抛异常不许把整趟对账带崩。** 补认领是安全网，不是主线：它坏了只该
   少补几份，不该让战报读不回来（那就是 `domain.reconcile_cooldown` 模块头记
   的那次断流故障，只是换了个成因）。但坏了要说得出口——「没跑成」与「补上 0
   份」在日志里必须分得开，否则安全网静默失效那天没人看得出来。

还钉一件**没做**的事：侦察那两条入口不接（`test_the_scout_backfill_*`）。它们
读的是侦察报告、写的是 `_store_scout_reading`，与 `battle_reports.dispatch_id`
上的认领没有一行交集。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from evo_helper.domain.reconcile_cooldown import RECONCILE_COOLDOWN
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import (
    BackfillTally,
    DailyTally,
    LoopOptions,
    PirateLoop,
    UnlinkedRematch,
)
from evo_helper.vision.parsers import ReportKind


class _Repository:
    """只回答这条接线要问的那几件事，并**按顺序**记下被问了什么。

    调用顺序是本文件的判据之一，所以记的是一串 `calls` 而不是几个计数器：
    「补认领排在问单子之前」只有在顺序上看得见。
    """

    def __init__(self, *, matched: int = 0, error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.records: list[dict[str, Any]] = []
        self._matched = matched
        self._error = error

    def rematch_unlinked_reports(self, **_fields: Any) -> int:
        self.calls.append("rematch")
        if self._error is not None:
            raise self._error
        return self._matched

    def due_attack_dispatches(self, _target_kind: str, **_fields: Any) -> list[Any]:
        self.calls.append("due")
        return []

    def record_daily_reconciliation(self, target_kind: str, **fields: Any) -> Any:
        self.calls.append("record")
        self.records.append({"target_kind": target_kind, **fields})
        return None

    def military_attack_config(self) -> Any:
        # 配置页上那个冷却框留空 = 走 `RECONCILE_COOLDOWN` 的默认值。
        return SimpleNamespace(reconcile_cooldown_minutes=None, report_scan_hours=None)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[tuple[str, str, str, Any]]]:
    """接住这一趟说的每一行，以及落进 `system_log` 的每一条结构化记录。

    `say` 与 `warn` 都要接：级别不同（控制台日志页按级别筛），而「补认领没跑成」
    正是那条必须能被筛出来的。
    """
    said: list[str] = []
    monkeypatch.setattr(module, "say", said.append)
    monkeypatch.setattr(module, "warn", said.append)
    logged: list[tuple[str, str, str, Any]] = []
    monkeypatch.setattr(
        module,
        "record_system_log",
        lambda level, source, message, **kwargs: logged.append(
            (level, source, message, kwargs.get("payload"))
        ),
    )
    return said, logged


@pytest.fixture
def stopwatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """把秒表钉成「这一趟花了 1.5 秒」。

    只换 `time.monotonic` 而不是整个 `time`：耗时必须是**量出来的**，而一个
    真跑起来只要几毫秒的假仓储量出来的永远是 0.0s——那样这条用例连「有没有在
    计时」都分不出来。用 monotonic 而不是墙钟的理由见 `pirate_loop.StepTimer`。
    """
    ticks = iter([10.0, 11.5, 20.0, 21.5])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))


def _loop(repository: _Repository, *, last_reconciled_at: datetime | None = None) -> Any:
    """一个只装了「开工那一趟」接线所需零件的循环。

    信箱那一趟本身（翻页、开封、数数）另有专文
    （`test_mailbox_reconciliation.py`），这里把 `_scan_for_reconcile` 整个换成
    一个已知的账：这条用例要钉的是**谁在什么时候被调到**，把真信箱拖进来只会
    让它跟着那边的夹具一起坏。
    """
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=False, attack=False)
    loop._ensure_run = lambda: (repository, None)
    loop._reconcile_decision = None
    loop._last_reconciled_at = lambda: last_reconciled_at
    loop._scan_for_reconcile = lambda day_start, *, now: DailyTally(
        kind=ReportKind.PIRATE, day_start=day_start, observed=3, complete=True
    )
    return loop


# -- 真进信箱的那一趟 --------------------------------------------------------


def test_a_trip_that_really_opens_the_mailbox_rematches_first(
    logs: tuple[list[str], list[Any]], stopwatch: None
) -> None:
    """⚠️ **顺序就是判据**：补认领排在问单子之前。

    补认领会把几发派遣从「到点还没战报」里销掉。先问单子再补认领，那句
    「库里有 N 发到点还没战报」打出来的 N 就永远比真相大，而排障的人正是拿它
    去对信箱最上面那封的时刻。
    """
    repository = _Repository(matched=2)
    loop = _loop(repository)

    loop.reconcile_today()

    assert repository.calls[0] == "rematch", "补认领必须是这一趟的第一件事"
    assert "due" in repository.calls, "单子照旧要问，补认领不替代它"
    assert repository.calls.index("rematch") < repository.calls.index("due")


def test_a_round_skipped_by_the_cooldown_never_touches_the_database(
    logs: tuple[list[str], list[Any]],
) -> None:
    """不进信箱的那一趟一个字都不查。

    2026-09-07 那天有 66 趟是这种：对账被冷却挡下来，链路压根没翻信箱。用户口径
    是「每趟**实际**邮件扫描前复用一次」——给这 66 趟也跑一遍库内扫，等于在整条
    链路最频繁的路径上白加一段逐份查询。
    """
    repository = _Repository(matched=1)
    loop = _loop(
        repository,
        last_reconciled_at=datetime.now(UTC) - RECONCILE_COOLDOWN + timedelta(minutes=2),
    )

    decision = loop._reconcile_if_due()

    assert decision.sweep is False, "冷却之内本来就不该翻信箱"
    assert repository.calls == [], f"这一趟不进信箱，却查了库：{repository.calls}"


def test_a_round_past_the_cooldown_rematches_on_the_way_in(
    logs: tuple[list[str], list[Any]], stopwatch: None
) -> None:
    """过了冷却的那一趟真翻信箱，补认领跟着它走——同一个闸门，两种结局。"""
    repository = _Repository(matched=1)
    loop = _loop(
        repository,
        last_reconciled_at=datetime.now(UTC) - RECONCILE_COOLDOWN - timedelta(minutes=1),
    )

    decision = loop._reconcile_if_due()

    assert decision.sweep is True
    assert repository.calls[0] == "rematch"


# -- 耗时与份数进汇总 --------------------------------------------------------


def test_the_elapsed_time_and_the_count_both_reach_the_summary(
    logs: tuple[list[str], list[Any]], stopwatch: None
) -> None:
    """份数与耗时都要进那一趟的收尾，而且耗时是**量出来的**。

    用户口径（2026-09-07）：「它内部会逐份查询候选，并非单次 SQL，将实际耗时
    计入汇总即可」。它变慢的样子是「每趟信箱前面多等几十秒」——没有这个数，
    那只会被当成信箱本身变慢。
    """
    said, logged = logs
    repository = _Repository(matched=2)
    loop = _loop(repository)

    loop.reconcile_today()

    assert any("[耗时] 进信箱前补认领 共 1.5s（补上 2 份）" in line for line in said), said
    assert any("进信箱前补认领 2 份，用了 1.5s" in line for line in said), (
        "收尾那份账里也要有——开头那句 [耗时] 到收尾时已经被几十行翻页日志顶掉了"
    )
    payloads = [payload for level, source, _m, payload in logged if source == "tools.report_ingest"]
    assert {"stage": "开工对账", "matched": 2, "elapsed_ms": 1500} in payloads, payloads


def test_a_sub_second_rematch_is_not_rounded_down_to_zero(
    monkeypatch: pytest.MonkeyPatch, logs: tuple[list[str], list[Any]]
) -> None:
    """⚠️ 这一趟正常时不到一秒，所以**不能**照 `StepTimer` 那行用 `.0f`。

    取整会把它一律显示成 0s，而「它什么时候开始变贵」正是记它的唯一理由——
    一个恒为 0 的数看不出任何趋势。
    """
    said, logged = logs
    ticks = iter([10.0, 10.4])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(ticks))
    loop = _loop(_Repository(matched=0))

    loop.reconcile_today()

    assert any("共 0.4s" in line for line in said), said
    payloads = [payload for _l, source, _m, payload in logged if source == "tools.report_ingest"]
    assert [payload["elapsed_ms"] for payload in payloads] == [400]


# -- 反面：安全网坏了不许拖累主线 --------------------------------------------


def test_a_broken_rematch_does_not_take_the_whole_reconciliation_down(
    logs: tuple[list[str], list[Any]], stopwatch: None
) -> None:
    """⚠️ **本文件最要紧的一条。**

    补认领是安全网，不是主线。让它的异常漏出去，就是把
    `domain.reconcile_cooldown` 模块头记的那次故障（战报断流两天、86 发）换个
    成因再造一遍：这一次不是闸门关着，而是一句查库把整趟对账掀了。

    现有的另一个调用点（`application.mission_scheduler.start`，约 915 行）
    **不吞**这个异常，这里是有意与它不同：那一句在用户点「开始」的路上，抛出来
    当场有人看见、当场能重试，而且那一刻整轮任务还什么都没做。
    """
    said, logged = logs
    repository = _Repository(error=RuntimeError("数据库连不上"))
    loop = _loop(repository)

    loop.reconcile_today()

    assert repository.records, "这一趟必须照常走完：当日对账记录该写下来"
    assert repository.records[0]["observed_reports"] == 3


def test_a_broken_rematch_is_told_apart_from_having_nothing_to_rematch(
    logs: tuple[list[str], list[Any]], stopwatch: None
) -> None:
    """「没跑成」与「补上 0 份」必须分得开，而且要能按级别筛出来。

    混成一个 0，安全网静默失效那天在日志里就是无声的：库里那些没认领的行还躺着，
    而下一步的跳过开封会让它们再也没有别的机会。
    """
    said, logged = logs
    loop = _loop(_Repository(error=RuntimeError("数据库连不上")))

    loop.reconcile_today()

    assert any("没跑成" in line and "数据库连不上" in line for line in said), said
    assert not any("补认领 0 份" in line for line in said), (
        "跑不成不等于没有可补的，这两句话不许长一样"
    )
    warnings = [(source, payload) for level, source, _m, payload in logged if level == "WARNING"]
    assert warnings, "跑不成要落一条 WARNING，控制台日志页才筛得出来"
    assert warnings[0][1]["error"] == "数据库连不上"


# -- 补录那一档 --------------------------------------------------------------


def test_the_backfill_summary_reports_the_rematch_separately(stopwatch: None) -> None:
    """补录的摘要单独报这一行，**不许并进「认领上 N 发」**。

    那个数是拿单子的落差算的（`BackfillTally.claimed`），说的是「这一趟信箱办成
    了什么」。库内补认领一封邮件都没开，混进去就是让信箱白拿一份功劳，而用户看
    这份摘要问的正是「刚才那趟补录到底有没有用」。
    """
    from evo_helper.tools import backfill_reports

    tally = BackfillTally(
        due_before=6, due_after=2, rematch=UnlinkedRematch(matched=3, seconds=1.5)
    )

    text = "\n".join(backfill_reports.summary_lines("bot", tally, exhaustive=False))

    assert "认领上 4 发" in text, "信箱那一侧的落差照旧只算它自己的"
    assert "进信箱前库内补认领：补上 3 份，用了 1.5s" in text


def test_the_backfill_summary_says_when_the_rematch_did_not_run() -> None:
    """跑不成时摘要也要说出来——补录是人盯着跑的，这份摘要就是他看的那份账。"""
    from evo_helper.tools import backfill_reports

    tally = BackfillTally(rematch=UnlinkedRematch(failed="数据库连不上"))

    text = "\n".join(backfill_reports.summary_lines("bot", tally, exhaustive=True))

    assert "没跑成" in text and "数据库连不上" in text
    assert "补上 0 份" not in text


# -- 没做的事：侦察那两条不接 ------------------------------------------------


def test_the_scout_backfill_does_not_rematch_battle_reports(
    logs: tuple[list[str], list[Any]],
) -> None:
    """侦察补录**不**接这一趟，这是有意的。

    它读的是侦察报告、写的是 `_store_scout_reading`，和
    `battle_reports.dispatch_id` 上的认领没有一行交集。接上去不会出错，只会让
    每趟侦察补录白跑一段逐份查库——而「顺手也调一下」正是这种开销悄悄长出来的
    方式。
    """
    repository = _Repository(matched=1)
    loop = PirateLoop.__new__(PirateLoop)
    loop._ensure_run = lambda: (repository, None)
    loop._scan_mail_rows = lambda **_kwargs: None

    assert loop.backfill_scout_reports() == (0, 0)
    assert repository.calls == []
