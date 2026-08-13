"""补录**优先于任务**：抢占、等待、闸门、启动对账。

判据本身在 `tests/unit/application/test_backfill.py`（纯状态机），这里守的是**接线**：
补录一排上，正在跑的扫描会不会被抢占、正在跑的海盗会不会被硬杀、补录扣着窗口时
调度器起不起任务、点「开始」是不是真的先对账。

为什么这个顺序是硬要求（一句话：补录改的正是任务读来做决策的那批数据，抢在它前面
跑就会把已经打赢的目标再打一遍），见 `application.backfill` 的模块头。

**这里不真的 Popen 任何东西**：任务与补录两侧的 `launch` 都注入假的。补录那一侧
尤其容易漏——它是第二个进程管理器，默认那份用的是真的 `subprocess.Popen`。

种子工具（`dispatch` / `attach_report` / `enable`）直接从同目录那份用例里取：
它们是同一张库的同一批形状，各写一份迟早会有一份和真实表结构分家。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from evo_helper.application.backfill import (
    BackfillCoordinator,
    BackfillPhase,
    BackfillRequest,
)
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import (
    BOT_RANGE,
    add_bot_target,
    attach_report,
    dispatch,
    enable,
    task_id,
)

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
SINCE = date(2026, 8, 12)
BOT = Coordinate(2, 137, 14)


class FakeBackfillProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class FakeBackfillLauncher:
    """补录那一侧的假 `Popen`。签名少一个 `kind`——补录不是一条链路。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.spawned: list[FakeBackfillProcess] = []

    def __call__(self, command: Sequence[str], log_path: Path) -> FakeBackfillProcess:
        self.commands.append(tuple(command))
        process = FakeBackfillProcess(pid=5000 + len(self.spawned))
        self.spawned.append(process)
        return process

    @property
    def kinds(self) -> list[str]:
        """每一趟补的是哪条链路，按起跑先后排。"""
        return [command[command.index("--kind") + 1] for command in self.commands]


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def backfill_launcher() -> FakeBackfillLauncher:
    return FakeBackfillLauncher()


@pytest.fixture
def scheduler(repository, launcher, backfill_launcher, clock, tmp_path) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(
        repository,
        make_supervisor(launcher, clock),
        clock=clock,
        backfill=BackfillCoordinator(
            launch=backfill_launcher, clock=clock, log_dir=tmp_path / "logs"
        ),
    )
    scheduler.prepare()
    return scheduler


def ask(scheduler: MissionScheduler, kind: str = "pirate") -> None:
    scheduler.request_backfill(BackfillRequest(kind=kind, since=SINCE))


# -- 抢占与等待 ----------------------------------------------------------------


def test_a_backfill_preempts_a_running_scan(scheduler, repository, launcher, backfill_launcher):  # type: ignore[no-untyped-def]
    """扫描的游标持久化，随时可断——`decide()` 里那条「只有扫描会被抢占」
    用的也是同一个理由。
    """
    enable(repository, MissionKind.SCAN)
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.SCAN]

    ask(scheduler)

    assert launcher.latest.terminated is True
    assert backfill_launcher.kinds == ["pirate"]


def test_the_preempted_scan_is_recorded_as_preempted_not_as_a_crash(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
):
    """抢占是我们自己动的手，不该计进那条链路的连续失败——一个被频繁抢占的
    扫描三次就会被自动停用，而扫描的定位恰恰是「始终填空隙」。
    """
    enable(repository, MissionKind.SCAN)
    scheduler.start()
    scheduler.tick()

    ask(scheduler)

    row = repository.mission_runs(limit=1)[0]
    assert row.stopped_by == "PREEMPTED"
    scan = next(item for item in repository.mission_tasks() if item.kind == MissionKind.SCAN.value)
    assert scan.consecutive_failures == 0


def test_a_backfill_never_hard_kills_a_pirate_round(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    """**海盗和 bot 不可抢占，这是故意的。**

    它们可能正卡在「点了出发」和「把这一发记进库」之间，硬杀会留下一发飞出去了
    却没记账的舰队——而那正是战报永远配不上的成因。补录宁可等半小时。
    """
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 1}')
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.PIRATE]

    ask(scheduler)

    assert launcher.latest.terminated is False
    assert backfill_launcher.commands == []
    assert scheduler.backfill_state().phase is BackfillPhase.PENDING


def test_a_round_that_starts_while_we_measure_is_not_hard_killed(  # type: ignore[no-untyped-def]
    repository, launcher, clock, tmp_path
):
    """**量底数是在锁外跑的，所以进锁之后必须重新问一次「现在跑的是谁」。**

    那一下没有上界（两个 `COUNT(*)` 外加逐个 bot 目标问库，生产库里那个范围有
    四千多个目标），压进锁里就是给用户的「结束」排队——`_facts` / `snapshot` /
    看门狗全是这个形状。代价是这中间调度器完全可能起一轮新的海盗，而照着那份
    已经过期的快照去抢占，杀掉的就是刚起来的那一发。

    这条用例把那个窗口撑开：量底数的那一刻正好有一轮海盗起来了。
    """
    supervisor = make_supervisor(launcher, clock)
    backfill_launcher = FakeBackfillLauncher()

    class RacingCounts:
        """量底数这一下，调度器正好起了一轮海盗。"""

        def read(self) -> tuple[int, int]:
            if supervisor.running is None:
                supervisor.start(MissionKind.PIRATE, ["python"], task_id=1, name="海盗")
            return (0, 0)

    scheduler = MissionScheduler(
        repository,
        supervisor,
        clock=clock,
        backfill=BackfillCoordinator(
            launch=backfill_launcher, clock=clock, log_dir=tmp_path / "logs"
        ),
        backfill_counts=RacingCounts(),
    )
    scheduler.prepare()

    scheduler.request_backfill(BackfillRequest(kind="pirate", since=SINCE))

    assert launcher.latest.terminated is False
    assert backfill_launcher.commands == []
    assert scheduler.backfill_state().phase is BackfillPhase.PENDING


def test_the_backfill_starts_once_the_pirate_round_ends_on_its_own(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 1}')
    scheduler.start()
    scheduler.tick()
    ask(scheduler)

    launcher.latest.exit_code = 0
    scheduler.tick()

    assert backfill_launcher.kinds == ["pirate"]


# -- 闸门：补录扣着窗口时一个任务都不起 ----------------------------------------


def test_no_mission_starts_while_a_backfill_holds_the_window(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
):
    """**「完成补录才会继续任务」那句用户口径的落点。**

    抢在补录前面跑，等于拿一份已知不完整的数据做决策：一个其实已经打赢的目标会
    被判成还要再打（战报没入库 → 派遣按 `MAX_REPORT_AGE` 过期剔除 → 退回
    `NEEDS_ATTACK`），配额也会数少——而数少正是会超额的那一侧。
    """
    enable(repository, MissionKind.SCAN)
    ask(scheduler)
    scheduler.start()

    scheduler.tick()

    assert launcher.spawned == []


def test_no_mission_starts_while_a_backfill_is_still_queued(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
):
    """排队那一档也要挡住。

    不挡的话，正在跑的那一轮一结束，调度器立刻起下一个任务，补录就永远排在后面
    等不到——而它等的正是「这一轮结束」。
    """
    enable(repository, MissionKind.PIRATE, params_json='{"radius": 1}')
    scheduler.start()
    scheduler.tick()
    ask(scheduler, kind="bot")
    launcher.latest.exit_code = 0

    scheduler.tick()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.PIRATE]


def test_a_finished_backfill_still_holds_the_tasks_until_it_is_acknowledged(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    """跑完不自动放行：用户要先看一眼摘要。"""
    enable(repository, MissionKind.SCAN)
    ask(scheduler)
    scheduler.start()
    scheduler.tick()
    backfill_launcher.spawned[0].exit_code = 0

    scheduler.tick()

    assert scheduler.backfill_state().phase is BackfillPhase.DONE
    assert launcher.spawned == []


def test_missions_start_again_once_the_backfill_is_acknowledged(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    enable(repository, MissionKind.SCAN)
    ask(scheduler)
    scheduler.start()
    scheduler.tick()
    backfill_launcher.spawned[0].exit_code = 0
    scheduler.tick()

    scheduler.acknowledge_backfill()
    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


def test_cancelling_a_backfill_releases_the_tasks(scheduler, repository, launcher):  # type: ignore[no-untyped-def]
    """取消这一下的用户意图就是「别占着窗口了」。"""
    enable(repository, MissionKind.SCAN)
    ask(scheduler)
    scheduler.start()
    scheduler.cancel_backfill()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


# -- 启动对账 ------------------------------------------------------------------


def test_pressing_start_reconciles_both_chains_before_any_mission_runs(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    """用户口径：「启动调度台之后，先检查有多少应读未读战报 → 读完所有应读未读
    战报 → …… → 继续执行任务」。

    两趟：两条链路的信箱主题不同，一趟只读得了一种。海盗在前——每天 32 次配额是
    账号级的，先把它数准。
    """
    enable(repository, MissionKind.SCAN)

    scheduler.start(reconcile=True)

    assert backfill_launcher.kinds == ["pirate"]
    assert launcher.spawned == []

    backfill_launcher.spawned[0].exit_code = 0
    scheduler.tick()

    assert backfill_launcher.kinds == ["pirate", "bot"]
    assert launcher.spawned == []


def test_the_startup_reconciliation_hands_the_window_back_when_it_is_done(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    enable(repository, MissionKind.SCAN)
    scheduler.start(reconcile=True)
    for _ in range(2):
        backfill_launcher.spawned[-1].exit_code = 0
        scheduler.tick()
    scheduler.acknowledge_backfill()

    scheduler.tick()

    assert launcher.kinds == [MissionKind.SCAN]


def test_the_startup_reconciliation_can_be_skipped(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, backfill_launcher
):
    """「刚停了两分钟又点开始、明知没有欠账」那条口子。**默认不走这一条。**"""
    enable(repository, MissionKind.SCAN)

    scheduler.start(reconcile=False)
    scheduler.tick()

    assert backfill_launcher.commands == []
    assert launcher.kinds == [MissionKind.SCAN]


def test_the_startup_reconciliation_uses_the_cli_budget(  # type: ignore[no-untyped-def]
    scheduler, repository, backfill_launcher
):
    """`--max-opens` 是封顶不是指标：信箱单子一空就早停，所以给大是安全的，
    而在这里另调一个小值只会让欠账多的那天补不完。
    """
    scheduler.start(reconcile=True)

    assert "--max-opens" not in backfill_launcher.commands[0]


def test_pressing_start_twice_does_not_queue_a_second_reconciliation(  # type: ignore[no-untyped-def]
    scheduler, backfill_launcher
):
    """第二下什么都没变。再排一批等于让窗口又被占十几分钟。"""
    scheduler.start(reconcile=True)
    scheduler.start(reconcile=True)

    assert backfill_launcher.kinds == ["pirate"]
    assert scheduler.backfill_state().queued == 1


# -- 摘要 ----------------------------------------------------------------------


def test_the_summary_counts_a_target_that_no_longer_needs_an_attack(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, run_id, backfill_launcher, clock
):
    """**这就是省下来的重复攻击。**

    战报没入库时这个目标是「本轮还要打」（那一发已经超过 `MAX_REPORT_AGE`，
    `bot_dispatch_facts` 把它整条剔掉，于是看起来一发都没打过）。补录把战报补进来
    之后它就是「已完成」——而这正是用户点补录时想知道的那个数。
    """
    add_bot_target(session_factory, BOT)
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    dispatch_id = dispatch(
        repository,
        run_id,
        TARGET_KIND_BOT,
        target=BOT,
        dispatched_at=NOW - timedelta(hours=12),
        flight=timedelta(minutes=10),
    )
    ask(scheduler, kind="bot")
    assert scheduler.backfill_state().phase is BackfillPhase.RUNNING

    # 「补录」在这里就是往库里挂一份战报——真的那趟做的也是这件事。
    attach_report(session_factory, dispatch_id, BOT, NOW - timedelta(hours=11), outcome="VICTORY")
    backfill_launcher.spawned[0].exit_code = 0
    scheduler.tick()

    summary = scheduler.backfill_state().summary
    assert summary is not None
    assert summary.reports_ingested == 1
    assert summary.dispatches_claimed == 1
    assert summary.bot_targets_settled == 1
    assert summary.bot_targets_measured == 1


def test_a_backfill_that_changed_nothing_says_so(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, backfill_launcher
):
    """一趟白跑也要说清楚：静默的「补录完成」会被读成「都补回来了」。"""
    add_bot_target(session_factory, BOT)
    enable(repository, MissionKind.BOT, params_json=BOT_RANGE)
    ask(scheduler, kind="bot")

    backfill_launcher.spawned[0].exit_code = 0
    scheduler.tick()

    summary = scheduler.backfill_state().summary
    assert summary is not None
    assert summary.reports_ingested == 0
    assert summary.bot_targets_settled == 0
    # 「量了 1 个、没变」和「一个都没量」不是一回事。
    assert summary.bot_targets_measured == 1


def test_a_chain_that_is_not_participating_is_not_measured(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, backfill_launcher
):
    """没勾的 bot 任务不会因为补录而动起来，为它逐个目标问一遍库只是白付钱
    （生产库里那个范围有四千多个目标）。

    范围要填好、只是不勾：不填的话这一行会因为「参数换算不出来」被跳过，
    于是这条用例守的就变成了另一条判据。
    """
    add_bot_target(session_factory, BOT)
    repository.update_mission_task(task_id(repository, MissionKind.BOT), params_json=BOT_RANGE)
    ask(scheduler, kind="bot")

    backfill_launcher.spawned[0].exit_code = 0
    scheduler.tick()

    summary = scheduler.backfill_state().summary
    assert summary is not None
    assert summary.bot_targets_measured == 0


# -- 收场 ----------------------------------------------------------------------


def test_shutdown_kills_the_backfill_child(scheduler, backfill_launcher):  # type: ignore[no-untyped-def]
    """控制台关了，一个还在翻信箱点鼠标的补录留在后台——和子进程那条挡的是
    同一件事，只是它归另一个进程管理器管。
    """
    ask(scheduler)
    assert backfill_launcher.spawned

    scheduler.shutdown()

    assert backfill_launcher.spawned[0].terminated is True


def test_force_kill_also_takes_the_backfill_down(scheduler, backfill_launcher):  # type: ignore[no-untyped-def]
    """「强制结束」的用户口径是全停，补录同样是一个在点鼠标的子进程。"""
    ask(scheduler)

    scheduler.force_kill()

    assert backfill_launcher.spawned[0].terminated is True


def test_stopping_the_scheduler_leaves_the_backfill_alone(scheduler, backfill_launcher):  # type: ignore[no-untyped-def]
    """「结束」说的是任务：正在补录时点它，意思是「补完之后别再起任务了」。

    连补录一起掐的话，用户为了停掉调度器就得放弃一趟已经跑了十分钟的补录。
    """
    ask(scheduler)
    scheduler.start()

    scheduler.stop()

    assert backfill_launcher.spawned[0].terminated is False


def test_a_backfill_runs_even_while_the_scheduler_is_stopped(  # type: ignore[no-untyped-def]
    scheduler, backfill_launcher
):
    """补录不归调度器那个开关管：它是修复工具，用户完全可以在调度器停着的时候
    先把欠账补回来，再点「开始」。
    """
    assert scheduler.enabled is False

    ask(scheduler)

    assert scheduler.backfill_state().phase is BackfillPhase.RUNNING


def test_a_repository_backed_scheduler_still_reports_no_backfill(  # type: ignore[no-untyped-def]
    repository: SqlAlchemyRepository, launcher, clock
):
    """默认那份协调器一直停在「未在补录」，除非有人真的请求过一次。

    钉的是**接线**：`MissionScheduler` 自己建的那份用的是真的 `subprocess.Popen`，
    要是它一开机就有活干，跑一次测试就会真的去起一个补录进程（实测在工作区里落下
    过一份 `var/logs/backfill-pirate.log`）。
    """
    plain = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    plain.prepare()
    plain.start()
    plain.tick()

    assert plain.backfill_state().phase is BackfillPhase.IDLE
    assert plain.backfill_state().blocking is False
    assert task_id(repository, MissionKind.SCAN) is not None
