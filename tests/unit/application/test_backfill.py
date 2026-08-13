"""战报补录的状态机、命令行与摘要算术。

**这里不真的 Popen 任何东西**：`launch` 一律注入假的。真起一个会去点用户的真实
鼠标翻信箱。调度器那一侧（抢占扫描、等海盗跑完、闸门）在
`tests/integration/application/test_backfill_priority.py`。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from evo_helper.application.backfill import (
    BACKFILL_KINDS,
    REASON_STARTUP,
    BackfillBusyError,
    BackfillCoordinator,
    BackfillMeasurement,
    BackfillPhase,
    BackfillRequest,
    BackfillSummary,
    build_command,
    default_since,
    log_path_for,
)

NOW = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)
SINCE = date(2026, 8, 12)


class FakeProcess:
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


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.log_paths: list[Path] = []
        self.spawned: list[FakeProcess] = []

    def __call__(self, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        self.log_paths.append(log_path)
        process = FakeProcess(pid=6000 + len(self.spawned))
        self.spawned.append(process)
        return process


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(tmp_path: Path) -> tuple[BackfillCoordinator, FakeLauncher, Clock]:
    launcher = FakeLauncher()
    clock = Clock(NOW)
    return (
        BackfillCoordinator(launch=launcher, clock=clock, log_dir=tmp_path),
        launcher,
        clock,
    )


def measurement(reports: int = 0, claimed: int = 0, **phases: str) -> BackfillMeasurement:
    return BackfillMeasurement(
        reports=reports,
        claimed=claimed,
        bot_phases={(1, coordinate): phase for coordinate, phase in phases.items()},
    )


def never_measured() -> BackfillMeasurement:  # pragma: no cover - 调到就说明判据错了
    raise AssertionError("这一下不该去量底数：它要跑两个 COUNT(*) 外加逐个目标问库")


# -- 命令行 ---------------------------------------------------------------------


def test_the_command_addresses_the_backfill_cli_with_kind_and_since() -> None:
    """命令行契约。**多一个参数不是「多余」，是这条链路起不来**：argparse 见到
    不认识的开关会 `SystemExit(2)`，而页面上只会显示「补录失败」。
    """
    command = build_command("pirate", SINCE)

    assert command == (
        sys.executable,
        "-u",
        "-m",
        "evo_helper.tools.backfill_reports",
        "--kind",
        "pirate",
        "--since",
        "2026-08-12",
    )


def test_the_command_runs_the_same_interpreter_the_console_runs_on() -> None:
    """写死 `"python"` 会走 PATH 解析：控制台若跑在 venv 外的系统解释器下，
    拉起的补录会跟着跑到系统解释器上，找不到本仓的依赖。
    """
    assert build_command("bot", SINCE)[0] == sys.executable


def test_the_command_is_unbuffered_so_the_page_can_show_progress() -> None:
    """`-u` 不是可有可无的。

    子进程的 stdout 重定向到文件之后是全缓冲的，4KB 攒满才落盘。补录要跑十几
    分钟，页面上那份日志尾巴是唯一的进度来源——少了这个字母，用户会盯着一个空
    文件看十分钟，然后得出「点了没反应」。
    """
    assert "-u" in build_command("pirate", SINCE)


def test_the_budgets_are_left_to_the_cli_when_they_are_not_given() -> None:
    """两处各写一份默认值，改了一边就是另一边悄悄按旧值跑。"""
    command = build_command("pirate", SINCE)

    assert "--max-pages" not in command
    assert "--max-opens" not in command


def test_the_budgets_are_passed_through_when_they_are_given() -> None:
    command = build_command("pirate", SINCE, max_pages=8, max_opens=80)

    assert command[-4:] == ("--max-pages", "8", "--max-opens", "80")


def test_the_exhaustive_flag_reaches_the_cli() -> None:
    """**救过期战报只能靠这个开关。**

    那些派遣早就掉出了 `due_attack_dispatches` 的 6 小时窗口，单子从头到尾是空的，
    而默认的对账模式撞见第一封「库里已有」就收工——一份都够不着，跑完还显示
    「补录完成」。

    这条差点就漏了：命令契约是在 CLI 长出 `--exhaustive` 之前定下的，于是页面上
    那个按钮一度只会跑对账模式，**而全套测试是绿的**——没有任何一条用例守着它。
    """
    assert "--exhaustive" in build_command("bot", SINCE, exhaustive=True)


def test_the_exhaustive_flag_stays_off_unless_asked() -> None:
    """默认必须是关：点「开始」时的启动对账走同一条路，而它要的正是早停。

    默认开就等于每按一次「开始」都把 60 封的预算烧满，用户还等着任务开跑。
    """
    assert "--exhaustive" not in build_command("bot", SINCE)


def test_a_request_carries_its_mode_into_the_command() -> None:
    """`BackfillRequest` 到命令行这一段也要接上，不然上面两条守的是个没人用的函数。"""
    manual = BackfillRequest(kind="bot", since=SINCE, exhaustive=True)
    startup = BackfillRequest(kind="bot", since=SINCE)

    assert "--exhaustive" in manual.command
    assert "--exhaustive" not in startup.command


def test_each_chain_writes_its_own_log(tmp_path: Path) -> None:
    """混进 `mission-pirate.log` 的话，事后翻「那一轮海盗干了什么」会读到一段
    根本不是它写的输出。
    """
    assert log_path_for("pirate", log_dir=tmp_path) != log_path_for("bot", log_dir=tmp_path)
    assert log_path_for("pirate", log_dir=tmp_path).name == "backfill-pirate.log"


# -- 默认起始日期 ---------------------------------------------------------------


def test_the_default_since_is_yesterday_not_today() -> None:
    """**默认「今天」会把用户要补的那批整批藏起来。**

    游戏时间按 UTC+0 显示，UTC 的今天要到现实时间（UTC+8）早上 8 点才开始——
    早上打开控制台点一次补录，`--since 今天` 会把昨夜漏掉的战报全部排除在外，
    而漏掉的恰恰就是昨夜的。情报中心的「舰队总数 > 0」默认筛选踩过同一个坑。
    """
    now = datetime(2026, 8, 13, 4, 0, tzinfo=UTC)

    assert default_since(now) == date(2026, 8, 12)
    assert default_since(now) != now.date()


def test_the_default_since_is_measured_in_utc_not_in_the_local_zone() -> None:
    """现实时间 UTC+8 的 8 月 13 日早上 7 点，UTC 还是 12 日。

    按本地日算的话，这一刻的默认值会变成 12 日的前一天还是后一天全看时区，
    而战报上写的时间是 UTC+0。
    """
    seven_am_local = datetime(2026, 8, 12, 23, 0, tzinfo=UTC)  # = UTC+8 的 13 日 07:00

    assert default_since(seven_am_local) == date(2026, 8, 11)


# -- 状态机 ---------------------------------------------------------------------


def test_a_request_only_queues_it_and_does_not_touch_the_mouse(tmp_path: Path) -> None:
    """窗口可能还在海盗那一轮手上，所以「收下了」和「开跑了」必须分得开。"""
    coordinator, launcher, _ = make(tmp_path)

    state = coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    assert state.phase is BackfillPhase.PENDING
    assert launcher.commands == []
    assert coordinator.blocking is True


def test_launching_marks_it_running_and_records_the_pid(tmp_path: Path) -> None:
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    assert coordinator.launch_if_pending(measurement()) is True

    state = coordinator.state()
    assert state.phase is BackfillPhase.RUNNING
    assert state.pid == launcher.spawned[0].pid
    assert launcher.commands[0] == build_command("pirate", SINCE)


def test_nothing_launches_unless_something_is_pending(tmp_path: Path) -> None:
    coordinator, launcher, _ = make(tmp_path)

    assert coordinator.launch_if_pending(measurement()) is False
    assert launcher.commands == []


def test_a_running_backfill_is_not_measured_on_every_poll(tmp_path: Path) -> None:
    """`measure` 要跑两个 `COUNT(*)` 外加逐个 bot 目标问库，而 tick 每秒一次。"""
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement())

    state = coordinator.poll(never_measured)

    assert state.phase is BackfillPhase.RUNNING


def test_a_clean_exit_becomes_done_with_a_summary(tmp_path: Path) -> None:
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement(reports=10, claimed=4, a="NEEDS_ATTACK"))
    launcher.spawned[0].exit_code = 0

    state = coordinator.poll(lambda: measurement(reports=13, claimed=7, a="DONE"))

    assert state.phase is BackfillPhase.DONE
    assert state.summary is not None
    assert state.summary.reports_ingested == 3
    assert state.summary.dispatches_claimed == 3
    assert state.summary.bot_targets_settled == 1


def test_a_finished_backfill_keeps_blocking_until_the_user_acknowledges(tmp_path: Path) -> None:
    """**跑完不自动放行。**

    用户口径是要在放行前看一眼摘要——他要凭「认领上了几发、几个 bot 目标不用
    再打了」决定放不放行。自动放行的话，任务会在他还没看到那几个数之前就起来。
    """
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement())
    launcher.spawned[0].exit_code = 0
    coordinator.poll(measurement)

    assert coordinator.blocking is True

    coordinator.acknowledge()

    assert coordinator.blocking is False


def test_a_failed_backfill_also_holds_the_tasks(tmp_path: Path) -> None:
    """失败尤其不能自动放行：那意味着数据仍然不全，而「拿不全的数据做决策」
    正是这整件事要防的东西。
    """
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement())
    launcher.spawned[0].exit_code = 1

    state = coordinator.poll(measurement)

    assert state.phase is BackfillPhase.FAILED
    assert coordinator.blocking is True


def test_acknowledging_a_backfill_that_has_not_finished_does_not_count(tmp_path: Path) -> None:
    """**提前确认不算数。**

    `_settle` 是在现状上 `replace`，所以一次提前的「确认」会一路留在状态上，
    等这一批真的跑完时直接放行——用户永远看不到那几个数。
    `MissionScheduler.start()` 顺手确认上一批时正好会撞上这一条：手动排了一趟
    补录、紧接着点「开始」，那一趟就再也不会拦住任务了。
    """
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement())

    coordinator.acknowledge()
    launcher.spawned[0].exit_code = 0
    coordinator.poll(measurement)

    assert coordinator.blocking is True


def test_the_summary_stays_on_the_page_after_it_is_acknowledged(tmp_path: Path) -> None:
    """放行之后那几个数字仍然是「刚才那一批干了什么」的唯一答案。"""
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement(reports=1))
    launcher.spawned[0].exit_code = 0
    coordinator.poll(lambda: measurement(reports=4))

    state = coordinator.acknowledge()

    assert state.summary is not None
    assert state.summary.reports_ingested == 3


# -- 取消 -----------------------------------------------------------------------


def test_cancelling_a_queued_backfill_never_starts_it(tmp_path: Path) -> None:
    """排队时用户唯一的退路本来是「把整台调度器停掉」，而那会把三条链路一起停。"""
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    state = coordinator.cancel(never_measured)

    assert state.phase is BackfillPhase.CANCELLED
    assert launcher.commands == []
    assert coordinator.blocking is False


def test_cancelling_a_running_backfill_kills_the_child(tmp_path: Path) -> None:
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement(reports=1))

    state = coordinator.cancel(lambda: measurement(reports=2))

    assert launcher.spawned[0].terminated is True
    assert state.phase is BackfillPhase.CANCELLED
    # 已经补进去的那些照样算数。
    assert state.summary is not None
    assert state.summary.reports_ingested == 1


def test_cancelling_releases_the_tasks_immediately(tmp_path: Path) -> None:
    """取消这一下的用户意图就是「别占着窗口了」，不该再等一次确认。"""
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    coordinator.launch_if_pending(measurement())

    coordinator.cancel(measurement)

    assert coordinator.blocking is False


def test_cancelling_nothing_is_not_an_error(tmp_path: Path) -> None:
    coordinator, _, _ = make(tmp_path)

    assert coordinator.cancel(never_measured).phase is BackfillPhase.IDLE


# -- 排队 -----------------------------------------------------------------------


def test_the_same_chain_cannot_be_queued_twice(tmp_path: Path) -> None:
    """第二趟只会去读一遍刚读过的信箱，而它得先占着游戏窗口十几分钟。"""
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    with pytest.raises(BackfillBusyError):
        coordinator.request(BackfillRequest(kind="pirate", since=SINCE))


def test_the_other_chain_queues_up_behind_it(tmp_path: Path) -> None:
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    state = coordinator.request(BackfillRequest(kind="bot", since=SINCE))

    assert state.kind == "pirate"
    assert state.queued == 1


def test_an_unknown_chain_is_refused(tmp_path: Path) -> None:
    coordinator, _, _ = make(tmp_path)

    with pytest.raises(ValueError, match="pirate"):
        coordinator.request(BackfillRequest(kind="scan", since=SINCE))


def test_a_batch_runs_its_second_leg_after_the_first_one_finishes(tmp_path: Path) -> None:
    """启动对账一次排两趟：两条链路的信箱主题不同，一趟只读得了一种。"""
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request_batch(
        [BackfillRequest(kind=kind, since=SINCE, reason=REASON_STARTUP) for kind in BACKFILL_KINDS]
    )
    coordinator.launch_if_pending(measurement())
    launcher.spawned[0].exit_code = 0

    state = coordinator.poll(measurement)

    assert state.phase is BackfillPhase.PENDING
    assert state.kind == "bot"
    # 第一趟跑完不放行：整批走完才轮到用户确认。
    assert coordinator.blocking is True


def test_a_batch_reports_what_the_whole_batch_changed(tmp_path: Path) -> None:
    """摘要说的是「这一批」，不是「最后那一趟」。

    第二趟拿它自己的底数去比的话，海盗那趟补回来的战报就凭空消失了。
    """
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request_batch(
        [BackfillRequest(kind=kind, since=SINCE, reason=REASON_STARTUP) for kind in BACKFILL_KINDS]
    )
    coordinator.launch_if_pending(measurement(reports=10))
    launcher.spawned[0].exit_code = 0
    coordinator.poll(lambda: measurement(reports=14))
    # 第二趟：底数必须还是整批开跑前那一份。
    coordinator.launch_if_pending(measurement(reports=999))
    launcher.spawned[1].exit_code = 0

    state = coordinator.poll(lambda: measurement(reports=15))

    assert state.summary is not None
    assert state.summary.reports_ingested == 5


def test_a_failed_leg_stops_the_batch(tmp_path: Path) -> None:
    """在一个已经不对劲的环境里接着再占十几分钟窗口没有意义，而用户此刻多半
    想看的是那条失败。
    """
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request_batch(
        [BackfillRequest(kind=kind, since=SINCE, reason=REASON_STARTUP) for kind in BACKFILL_KINDS]
    )
    coordinator.launch_if_pending(measurement())
    launcher.spawned[0].exit_code = 1

    state = coordinator.poll(measurement)

    assert state.phase is BackfillPhase.FAILED
    assert state.kind == "pirate"


def test_cancelling_drops_the_rest_of_the_batch(tmp_path: Path) -> None:
    coordinator, launcher, _ = make(tmp_path)
    coordinator.request_batch(
        [BackfillRequest(kind=kind, since=SINCE, reason=REASON_STARTUP) for kind in BACKFILL_KINDS]
    )
    coordinator.launch_if_pending(measurement())

    coordinator.cancel(measurement)

    assert coordinator.blocking is False
    assert coordinator.launch_if_pending(measurement()) is False
    assert len(launcher.commands) == 1


def test_a_batch_skips_the_chain_that_is_already_queued(tmp_path: Path) -> None:
    """「刚手动补完海盗、紧接着点开始」是一条完全正常的路，不该把「开始」
    整个打成错误。
    """
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    state = coordinator.request_batch(
        [BackfillRequest(kind=kind, since=SINCE, reason=REASON_STARTUP) for kind in BACKFILL_KINDS]
    )

    assert state.kind == "pirate"
    assert state.queued == 1


# -- 摘要算术 -------------------------------------------------------------------


def test_the_summary_counts_targets_that_stopped_needing_an_attack() -> None:
    """**这就是省下来的重复攻击**，也是这个功能眼下最直接的价值。"""
    before = measurement(a="NEEDS_ATTACK", b="AWAITING_ATTACK_REPORT", c="DONE")
    after = measurement(a="DONE", b="AWAITING_ATTACK_REPORT", c="DONE")

    summary = BackfillSummary.between(before, after)

    assert summary.bot_targets_settled == 1
    assert summary.bot_targets_measured == 3


def test_the_summary_separates_never_measured_from_nothing_changed() -> None:
    """没有参与调度的 bot 任务时那个 0 的意思是「没量」，写成「0 个不用再打」
    是一句假话。
    """
    summary = BackfillSummary.between(measurement(), measurement())

    assert summary.bot_targets_measured == 0


def test_the_summary_reports_claims_separately_from_ingests() -> None:
    """**认领上了才会影响任务决策**——一份挂在那里没认领的战报，`phase_of`
    根本看不见。两个数合成一个，用户就分不出「补回来了但没配上」这一档。
    """
    summary = BackfillSummary.between(measurement(reports=0, claimed=0), measurement(4, 1))

    assert summary.reports_ingested == 4
    assert summary.dispatches_claimed == 1


# -- 日志 -----------------------------------------------------------------------


def test_the_log_tail_is_the_last_few_lines(tmp_path: Path) -> None:
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))
    log_path_for("pirate", log_dir=tmp_path).write_text(
        "\n".join(f"第 {index} 行" for index in range(100)), encoding="utf-8"
    )

    tail = coordinator.log_tail(lines=3)

    assert tail.splitlines() == ["第 97 行", "第 98 行", "第 99 行"]


def test_a_missing_log_is_not_an_error(tmp_path: Path) -> None:
    """一次读文件失败把整个状态接口打成 500 的话，页面连「在跑」都显示不出来。"""
    coordinator, _, _ = make(tmp_path)
    coordinator.request(BackfillRequest(kind="pirate", since=SINCE))

    assert coordinator.log_tail() == ""
