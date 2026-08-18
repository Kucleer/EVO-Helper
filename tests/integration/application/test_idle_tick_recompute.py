"""一个 tick 里 `_step` 该转几圈：**只有「刚停用了谁」才值得重算。**

## 这一条修的是什么

`_launch()` 原先用同一个 `False` 表达三件语义完全不同的事——任务被删了、
`MissionIdle`（这会儿没活干）、`MissionParamError`（刚就地停用了这条链路）——而
`_act()` 的 `return not self._launch(...)` 把三件都翻成「值得再算一次」。
`tick()` 里是 `for _ in range(len(MissionKind))`，于是**每一个「没活干」的 tick 都把
`_step` 转满 4 圈，每圈跑一次完整的 `_facts()`**。

代价（`_act` 的 docstring 里记着的实测）：一次 `_facts` 本地 16 条 SQL、约 194 ms，
生产 0.32 秒；乘以 4 就是每个空转 tick 近一秒钟全花在重算全库候选池上。它同时是
2026-08-18 16:00 那一小时日志刷屏的成因（同一秒内最多重复 4 次，与这 4 圈对得上）。

## ⚠️ 为什么这不是一次「派遣次序变更」

改之前量过（同一套夹具，`_facts` 调用 4 次、`launcher.kinds == []`）：那 4 圈
**一发都派不出去**。原因是结构性的——`MissionIdle` 不写库、不停用、不起进程，
而 `decide()` 是 `(tasks, facts)` 的纯函数，第二圈读同一份库、按同一个排序键挑出
**同一个**任务，原样再抛一次 `MissionIdle`。

所以本文件的口径是：**「没活干」之后本 tick 到此为止，下一条链路等下一个 tick。**
今天让不让它上位，答案本来就是「不让」；改动只是把那 3 圈白工去掉。
「顺位立刻让给下一条」只对**停用**成立，因为只有停用真的改了候选集
（`disabled_reason` 落库 → `decide()` 的候选里少一个）——那一条由
`test_mission_scheduler.py::test_a_bad_radius_yields_its_turn_in_the_same_tick` 守着，
本文件再从「转几圈」这个角度守一遍。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.application.mission_scheduler import LaunchOutcome, MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, enable, only_gap_filler, task, task_id

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: 军力优先，截断要 2 个，**上限 100**——库里那批候选全都强过它。
#:
#: 选的是这一档而不是「池子空了」：`_facts` 把 `targets_remaining` 填成
#: `reading.usable`（第 2 步之后、有军力读数的候选数），而 `max_score` 要到第 4 步
#: 才生效。于是 `has_work()` 看得见「还有 3 个能打」、`_military_command()` 却挑不出
#: 一个来——这正是 `MissionIdle` 在生产里真实可达的形状：会自己好起来（军力榜扫到
#: 弱一点的目标就成立了），不该停用任何东西。
ALL_TOO_STRONG = '{"by_military": true, "top_n": 2, "score_max_age_hours": 24, "max_score": 100}'

#: 海盗半径填 0：要到组命令行才校验得出来，所以停用发生在决策**之后**。
BAD_RADIUS = '{"radius": 0}'


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


class FactsCounter:
    """数 `_facts` 被调了几次，也就是这个 tick 里 `_step` 转了几圈。

    量的是 `_facts` 而不是 `_step`：一圈一次是它们的关系，而**代价全在 `_facts`
    这一边**（一次 16 条 SQL、生产 0.32 秒）。
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.rounds = 0
        original = MissionScheduler._facts

        def counted(inner: MissionScheduler, *args: Any, **kwargs: Any) -> Any:
            self.rounds += 1
            return original(inner, *args, **kwargs)

        monkeypatch.setattr(MissionScheduler, "_facts", counted, raising=True)


@pytest.fixture
def rounds(monkeypatch: pytest.MonkeyPatch) -> FactsCounter:
    return FactsCounter(monkeypatch)


def a_pool_with_nothing_dispatchable(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository, session_factory
) -> None:
    """军力优先的 BOT：候选有读数（所以「有活干」），但全都强过上限（所以挑不出）。

    **只留 SCAN 一种填空隙的**：它是这个 tick 里排在 BOT 后面的那条链路，
    「让不让它上位」正是这一组用例要钉的东西，留两种的话断言分不清是谁顶上来的。
    """
    for index in range(3):
        add_bot_target(
            session_factory,
            Coordinate(2, 400 + index, 5),
            military_score=9_000.0 - index,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=ALL_TOO_STRONG)
    enable(repository, MissionKind.SCAN)
    only_gap_filler(repository, MissionKind.SCAN)


# -- 「没活干」：只转一圈 ------------------------------------------------------


def test_an_idle_tick_reads_the_facts_only_once(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, rounds: FactsCounter
) -> None:
    """⚠️ **一个「没活干」的 tick 只跑一次 `_facts`。**

    改之前是 4 次（`len(MissionKind)`），而那 3 次多出来的读的是同一份库、挑出同一个
    任务、抛同一个 `MissionIdle`——纯粹白付近一秒钟的全库候选池重算。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    scheduler.tick()

    assert rounds.rounds == 1, f"「没活干」的 tick 转了 {rounds.rounds} 圈，只该转 1 圈"


def test_an_idle_tick_starts_nothing_and_disables_nobody(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **这一条守的是「省下的 3 圈里本来什么都没发生」，别顺手改掉次序。**

    改之前实测就是 `launcher.kinds == []`：`MissionIdle` 不起进程，而排在 BOT 后面的
    SCAN 这一秒也没顶上——`decide()` 每圈都挑中同一个 BOT。所以「只转一圈」不该让
    任何人多跑或少跑一轮。

    顺带守住 `MissionIdle` 那半条老合同：**它绝不停用任何东西**。判成参数错误的话，
    一次正常的间歇会把整条链路自动停用到用户手动恢复为止。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    scheduler.tick()

    assert launcher.kinds == [], "「没活干」的 tick 不该起任何子进程"
    assert task(repository, MissionKind.BOT).disabled_reason is None, "正常的间歇被当成配置错误了"


def test_the_next_chain_takes_its_turn_on_the_next_tick(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **口径的另一半：下一条链路是「等下一个 tick」，不是「永远不上」。**

    只写「本 tick 什么都不起」的话，一个把 `MissionIdle` 直接翻成「整个调度器歇了」
    的实现也能过——那会让 BOT 一没活干就把 SCAN 也一起卡死。这里让 BOT 这一秒确实
    没活干，下一秒把它关掉，SCAN 就该当场接上鼠标。
    """
    a_pool_with_nothing_dispatchable(repository, session_factory)
    scheduler.start()

    scheduler.tick()
    assert launcher.kinds == []

    repository.update_mission_task(task_id(repository, MissionKind.BOT), enabled=False)
    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


# -- 任务被删：作废，也只转一圈 ------------------------------------------------


def test_a_decision_pointing_at_a_deleted_task_does_not_recompute(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, monkeypatch: pytest.MonkeyPatch, rounds: FactsCounter
) -> None:
    """⚠️ **决策与起进程之间任务被删了：作废本轮，不重算，也不起任何进程。**

    这一档只能这样构造：读事实在锁外、`_launch` 在锁内，中间那一瞬没有别的入口。
    掐掉的是 `mission_task(...)` 这一次读——`_launch` 恰恰是靠它认「这一行还在吗」。

    为什么是「不重算」而不是「重算」：作废时次序一个字都没变，重算读的还是同一份
    库。删任务是用户偶尔一次的动作，慢一个 tick（1 秒）没有任何代价，而把它算进
    「值得重算」就得为一件罕见事在每个 tick 上留一条多转的路。
    """
    enable(repository, MissionKind.PIRATE)
    only_gap_filler(repository, MissionKind.SCAN)
    scheduler.start()
    monkeypatch.setattr(type(repository), "mission_task", lambda *_: None, raising=True)

    scheduler.tick()

    assert launcher.kinds == [], "指向已删任务的决策不该起子进程"
    assert rounds.rounds == 1, f"作废本轮却转了 {rounds.rounds} 圈"


# -- 「刚停用了谁」：仍然重算 --------------------------------------------------


def test_a_disabled_chain_still_costs_a_second_round(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, rounds: FactsCounter
) -> None:
    """⚠️ **停用是唯一值得重算的那一档：候选集真的少了一个。**

    海盗的 `radius` 要到组命令行才校验得出来，所以停用发生在决策之后。停完这一秒就
    该让 SCAN 顶上，否则谁都不跑——每秒一次 tick 看起来只是慢，但配额窗口按天算，
    白丢的次数补不回来。

    连断言圈数是为了把「让位」和「重算」钉成同一件事：只断言 `launcher.kinds` 的话，
    一个碰巧在别处补起了 SCAN 的实现也能过。
    """
    enable(repository, MissionKind.PIRATE, params_json=BAD_RADIUS)
    only_gap_filler(repository, MissionKind.SCAN)
    scheduler.start()

    scheduler.tick()

    assert task(repository, MissionKind.PIRATE).disabled_reason is not None
    assert launcher.kinds == [MissionKind.SCAN], "停用之后这一秒谁都没跑"
    assert rounds.rounds == 2, f"停用之后该重算一次（共 2 圈），实际转了 {rounds.rounds} 圈"


def test_two_chains_disabled_in_a_row_both_yield_within_the_tick(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, rounds: FactsCounter
) -> None:
    """⚠️ **圈数的上界仍然由「停用」撑着：连着停用两条，两条都当场让位。**

    这一条守的是修法没把重算那条路整个焊死：把 `_act` 写成恒返回「不必再算」也能让
    上面那几条绿，但那样第二条坏链路要等下一个 tick，而 `tick()` 里
    `for _ in range(len(MissionKind))` 那个上界就白留了。

    **两条都用海盗**，是因为要的正是「两次都在 `_launch` 里停用」：`radius` 要到组
    命令行才校验得出来。换成一条海盗加一条范围反填的 bot 量不到这件事——bot 的范围
    在 `_facts` 数「还剩几个目标」时就已经把它停用了（`_targets_remaining` 那处），
    那一次停用不经过 `_launch`，也就不花一圈。
    """
    enable(repository, MissionKind.PIRATE, params_json=BAD_RADIUS)
    second = repository.create_mission_task(
        MissionKind.PIRATE,
        name="海盗二号",
        priority=5,
        params_json=BAD_RADIUS,
        origin=None,
        fleet_lines=None,
        now_utc=NOW,
    )
    repository.update_mission_task(second, enabled=True)
    only_gap_filler(repository, MissionKind.SCAN)
    scheduler.start()

    scheduler.tick()

    disabled = [row for row in repository.mission_tasks() if row.disabled_reason is not None]
    assert {row.id for row in disabled} == {task_id(repository, MissionKind.PIRATE), second}
    assert launcher.kinds == [MissionKind.SCAN], "两条都停用之后 SCAN 该在同一个 tick 里顶上"
    assert rounds.rounds == 3, f"停两条该转 3 圈（停、停、起 SCAN），实际 {rounds.rounds} 圈"


# -- 四态本身 ------------------------------------------------------------------


def test_only_a_disabled_chain_is_worth_another_round() -> None:
    """⚠️ **判据是白名单（`is DISABLED`），不是黑名单（`is not IDLE`）。**

    将来加第五个成员时，忘了改那一行的后果必须是「少重算一次」（浪费一个 tick），
    而不是「每秒空转四圈」——后者正是这次要修的东西，它不该有第二次机会。

    所以这里逐个成员问一遍，而不是只问 `IDLE`。
    """
    worth = {outcome for outcome in LaunchOutcome if outcome.worth_another_round}

    assert worth == {LaunchOutcome.DISABLED}
