"""子进程的起停。界面、数据库、真实 Popen 都不在这里，逻辑分支全在这。

**这里绝不真的 Popen 一个 runner**——那会在 CI 上去点真实鼠标、派真实舰队。
`launch` 一律注入假的。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_helper.application.mission_supervisor import (
    MissionSupervisor,
    StopReason,
    SupervisorBusyError,
    log_path_for,
)
from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY, MissionKind

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class FakeProcess:
    """可以按剧本「跑完」的假子进程。"""

    def __init__(self, pid: int = 1234) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminated = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        return self.exit_code if self.exit_code is not None else 0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def make(clock: Clock | None = None) -> tuple[MissionSupervisor, list[FakeProcess]]:
    spawned: list[FakeProcess] = []

    def launch(kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        process = FakeProcess(pid=1000 + len(spawned))
        spawned.append(process)
        return process

    return MissionSupervisor(launch=launch, clock=clock or Clock()), spawned


SCAN_ARGV = ["python", "-m", "evo_helper.tools.scan_coordinates"]
PIRATE_ARGV = ["python", "-m", "evo_helper.tools.pirate_loop", "--scout", "--attack"]


def test_nothing_runs_until_something_is_started() -> None:
    supervisor, spawned = make()

    assert supervisor.running is None
    assert spawned == []


def test_starting_records_what_is_running() -> None:
    supervisor, spawned = make()

    child = supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

    assert len(spawned) == 1
    assert child.kind is MissionKind.SCAN
    # **身份是 task_id**：同一 kind 可以有多个任务，冷却与失败计数都按它记。
    assert child.task_id == 3
    assert child.pid == 1000
    assert child.started_at_utc == NOW
    assert supervisor.running == child


def test_a_second_start_is_refused_while_one_is_running() -> None:
    """一个游戏窗口，一个鼠标。两个子进程同时点，就是互相抢窗口。"""
    supervisor, spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

    with pytest.raises(SupervisorBusyError):
        supervisor.start(MissionKind.PIRATE, PIRATE_ARGV, task_id=1)

    assert len(spawned) == 1


def test_stopping_kills_it_now_rather_than_waiting_out_the_round() -> None:
    """用户口径：点了停就是停，不等它跑完手上这一个。"""
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)
    clock.advance(90)

    exited = supervisor.stop(StopReason.USER)

    assert spawned[0].terminated
    assert spawned[0].wait_timeouts == [5]
    assert exited is not None
    assert exited.kind is MissionKind.SCAN
    # 起进程时给的 task_id 必须原样回到退出记录上——`_finish` 靠它把这次失败
    # 记到对的那个任务头上，认错人就是给另一个任务白记一次连续失败。
    assert exited.task_id == 3
    assert exited.stopped_by is StopReason.USER
    assert exited.started_at_utc == NOW
    assert exited.ended_at_utc == NOW + timedelta(seconds=90)
    assert supervisor.running is None


def test_stopping_when_nothing_runs_is_not_an_error() -> None:
    """关闭、抢占、用户点停三条路都会调它，谁也不该先去判一遍有没有在跑。"""
    supervisor, _ = make()

    assert supervisor.stop(StopReason.SHUTDOWN) is None


def test_a_process_that_will_not_die_does_not_hang_the_caller() -> None:
    """`wait` 超时也要把状态清干净，否则控制台永远停在「运行中」，谁也起不来。"""

    class Stubborn(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise TimeoutError("还没死")

    supervisor = MissionSupervisor(launch=lambda kind, command, log: Stubborn(), clock=Clock())
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

    exited = supervisor.stop(StopReason.PREEMPTED)

    assert exited is not None
    assert exited.exit_code is None
    assert supervisor.running is None


def test_polling_a_live_process_reports_nothing() -> None:
    supervisor, spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

    assert supervisor.poll() is None
    assert supervisor.running is not None


def test_polling_collects_a_clean_exit() -> None:
    clock = Clock()
    supervisor, spawned = make(clock)
    supervisor.start(MissionKind.PIRATE, PIRATE_ARGV, task_id=1)
    spawned[0].exit_code = 0
    clock.advance(120)

    exited = supervisor.poll()

    assert exited is not None
    assert exited.exit_code == 0
    assert exited.stopped_by is StopReason.SELF
    assert exited.ended_at_utc == NOW + timedelta(seconds=120)
    assert supervisor.running is None


def test_a_finished_attack_round_is_not_restarted() -> None:
    """**这是与扫描控制台 `ScanSupervisor` 唯一的实质差别。**

    自动续跑是扫描链路的特性：扫描不派遣，断在哪都能接着扫。攻击类任务自己
    重启会连着再派一轮舰队——一轮 32 次配额可以在没人看着的时候悄悄打光。
    起不起下一个由调度器按判据决定，不由子进程的退出来决定。
    """
    supervisor, spawned = make()
    supervisor.start(MissionKind.PIRATE, PIRATE_ARGV, task_id=1)
    spawned[0].exit_code = 0

    supervisor.poll()
    supervisor.poll()
    supervisor.poll()

    assert len(spawned) == 1


def test_a_crashed_process_is_not_restarted_either() -> None:
    """失败多半是「窗口抢不到前台」或「甩鼠标触发 FAILSAFE」，重启只会再来一遍。"""
    supervisor, spawned = make()
    supervisor.start(MissionKind.BOT, ["python", "-m", "evo_helper.tools.bot_loop"], task_id=2)
    spawned[0].exit_code = 3

    exited = supervisor.poll()

    assert exited is not None
    assert exited.exit_code == 3
    # 非 0 也算它自己退的：`stopped_by` 记的是「谁把它停了」，成败看 `exit_code`。
    assert exited.stopped_by is StopReason.SELF
    assert len(spawned) == 1


def test_a_crash_counts_as_a_failure() -> None:
    """`failed` 是连续失败自停唯一的判据，正面这一半必须钉住。

    少了它，下面那两条豁免可以退化成「什么都不算失败」而没人发现——那样真坏了
    也永远不会停用，调度循环会在一个坏掉的任务上满速空转地重启。
    """
    supervisor, spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)
    spawned[0].exit_code = 1

    exited = supervisor.poll()

    assert exited is not None and exited.failed


def test_being_stopped_by_us_is_never_a_failure() -> None:
    """抢占、用户点停、控制台关闭：我们自己动的手，任务本身没毛病。

    实机 2026-08-11 01:16–01:32 扫描被抢占了 7 次。算进去的话，最该一直有活干、
    也最容易被抢占的那条链路，三次就被自动停用了。
    """
    for reason in (StopReason.PREEMPTED, StopReason.USER, StopReason.SHUTDOWN):
        supervisor, spawned = make()
        supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

        exited = supervisor.stop(reason)

        assert exited is not None
        assert not exited.failed


def test_a_terminated_child_never_gets_an_exit_code_put_in_its_mouth() -> None:
    """⚠️ **`terminate()` 之后那个码不是 runner 的表态。**

    Windows 上它走 `TerminateProcess(handle, 1)`，那个 1 是我们自己传给内核的
    参数；POSIX 上是 `-SIGTERM`。原样记进 `mission_runs.exit_code` 的后果是
    全库每一条 PREEMPTED / USER / SHUTDOWN 记录都长着「退出码 1」这副失败的样子
    ——判据那侧本来就挡住了（`failed` 先看 `stopped_by`），但 `/logs` 页面和
    `mission_runs` 因此分不清「被抢占的」和「自己崩了的」。

    记 None 而不是 0：0 是「这一轮正常跑完」，同样是替一个没表过态的进程编造表态，
    而且 0 会被当成成功。**没收到退出码**才是事实。
    """
    for reason in (StopReason.PREEMPTED, StopReason.USER, StopReason.SHUTDOWN, StopReason.STALLED):
        supervisor, spawned = make()
        supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

        exited = supervisor.stop(reason)

        assert exited is not None
        assert spawned[0].terminated, "该杀还是要杀，只是不采信它留下的码"
        assert exited.exit_code is None


def test_a_stalled_round_still_counts_as_a_failure() -> None:
    """`STALLED` 手是我们动的，但毛病是这条链路自己的——它不吃上面那条豁免。

    退出码变成 None 之后这一条尤其要钉住：`failed` 一旦退化成看退出码，
    停顿看门狗掐掉的那一轮就会被当成「没退出码 = 不算失败」，同一个卡死会
    一轮接一轮地复现，每轮白烧 45 分钟。
    """
    supervisor, _spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)

    exited = supervisor.stop(StopReason.STALLED)

    assert exited is not None and exited.failed


def test_the_environment_busy_code_is_not_a_failure() -> None:
    """runner 明说「这会儿轮不到我」（用户正在用别的窗口）不算它坏了。

    ⚠️ 豁免只认这一个码。`1` 仍然是故障——见上面那条正面用例。
    """
    supervisor, spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)
    spawned[0].exit_code = EXIT_ENVIRONMENT_BUSY

    exited = supervisor.poll()

    assert exited is not None
    assert exited.stopped_by is StopReason.SELF
    assert not exited.failed


def test_polling_after_the_exit_was_collected_reports_nothing_again() -> None:
    """退出只该被收一次，否则每个 tick 都会再记一次失败，三次就误停用。"""
    supervisor, spawned = make()
    supervisor.start(MissionKind.SCAN, SCAN_ARGV, task_id=3)
    spawned[0].exit_code = 1

    assert supervisor.poll() is not None
    assert supervisor.poll() is None


def test_each_chain_writes_its_own_log() -> None:
    """三条链路混在一个文件里，出事时分不出是谁的输出。"""
    paths = {log_path_for(kind) for kind in MissionKind}

    assert len(paths) == len(MissionKind)
    assert log_path_for(MissionKind.PIRATE).name == "mission-pirate.log"
