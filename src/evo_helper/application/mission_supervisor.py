"""任务子进程的起停。

照 `tools/scan_console.py` 的 `ScanSupervisor` 长，但**去掉了自动续跑**。
那是扫描链路的特性：扫描不派遣、断在哪都能接着扫，所以它自己重启没有代价。
攻击类任务自己重启会连着再派一轮舰队——一天 32 次配额可以在没人看着的时候
悄悄打光。起不起下一个由调度器按判据决定，不由子进程的退出来决定。

这一层只管进程，不碰数据库也不碰判据：调度循环把「起谁」算好，这里负责起、
停、收退出码，并把发生了什么原样报回去。真正 `Popen` 的那个函数单独放在
`launch_mission`，测试里注入假的——**绝不能在 CI 上真的拉起一个 runner**，
那会去点真实的鼠标。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY, MissionKind

#: 子进程日志的落脚处。与 `tools/scan_console.py` 同一个目录。
LOG_DIR = Path("var/logs")

#: `terminate()` 之后等它收尾的秒数。等不到就放手——一个不肯死的子进程不该让
#: 整个控制台卡住，页面上还有「强制结束」这条退路。
TERMINATE_TIMEOUT_S = 5


class StopReason(Enum):
    """这一轮是怎么结束的。写进 `mission_runs.stopped_by`。"""

    #: 用户点了「结束」。
    USER = "USER"
    #: 它自己退的。成败看 `exit_code`，不看这里。
    SELF = "SELF"
    #: 扫描被攻击任务抢占。
    PREEMPTED = "PREEMPTED"
    #: 一轮跑着但什么都没做成，被看门狗掐掉（`application.mission_progress`）。
    #: 和上面三条不同：手是我们动的，**毛病却是这条链路自己的**，所以它算故障。
    STALLED = "STALLED"
    #: 控制台关闭时清场。
    SHUTDOWN = "SHUTDOWN"
    #: 控制台重启后发现的孤儿——上次没走正常的关闭路径，死活未知。
    UNKNOWN = "UNKNOWN"


class Process(Protocol):
    """`subprocess.Popen` 里这个模块用到的那一小部分。"""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = ...) -> int: ...


class SupervisorBusyError(RuntimeError):
    """已经有一个子进程在跑。

    一个游戏窗口，一个鼠标。两个子进程同时点就是互相抢窗口，而且两边都会以为
    自己看到的那一屏是自己点出来的。
    """


@dataclass(frozen=True)
class RunningChild:
    #: 是**哪一个任务**起的。同一 `kind` 可以有多个任务，光看 kind 分不出来，
    #: 而页面上「哪一行显示运行中」、冷却按谁算、失败记到谁头上，全按它。
    task_id: int
    kind: MissionKind
    #: 任务名，只为日志与页面显示。判据一个字都不看它。
    name: str
    command: tuple[str, ...]
    #: 供 `mission_runs` 记账、页面上认孤儿用。**不拿它去杀进程**：
    #: pid 会被系统回收复用。
    pid: int | None
    started_at_utc: datetime
    log_path: Path


@dataclass(frozen=True)
class MissionExit:
    """一次子进程从起到停的全过程，调用方据此写 `mission_runs`。"""

    task_id: int
    kind: MissionKind
    command: tuple[str, ...]
    #: 退出码是唯一的进程间协议：0 = 这一轮正常跑完，
    #: `EXIT_ENVIRONMENT_BUSY` = 这会儿轮不到我（不算故障），其余非 0 = 异常。
    #: 收不到（杀不掉、等超时）则为 None。
    exit_code: int | None
    stopped_by: StopReason
    started_at_utc: datetime
    ended_at_utc: datetime

    @property
    def failed(self) -> bool:
        """算不算一次「异常退出」，供连续失败自停计数。

        两档豁免，成因不同但形状一样——**都不是这条链路的毛病**：

        - **不是它自己退的**（抢占、用户点停、控制台关闭）：那是我们自己动的手。
          算进去的话，一个被频繁抢占的扫描三次就会被自动停用，而扫描的定位恰恰是
          「始终填空隙」，也就是最容易被抢占的那一条。实机 2026-08-11 01:16–01:32
          扫描被抢占了 7 次。
        - **`EXIT_ENVIRONMENT_BUSY`**：runner 明说「这会儿轮不到我」。它和真正的
          故障必须分得开，而进程间唯一的协议就是退出码。目前两种：
          「游戏窗口抢不到前台」（用户正在用别的窗口）、「切到出发星球之后回读
          没认出来」。

          ⚠️ **这一档的准入条件是「会自己好」，不是「不是代码的错」。**
          配错了出发星球同样不是代码的错，但它永远不会自己好——豁免它，任务就会
          整夜显示「在跑」而一发不派：不计故障、不报警，停顿看门狗也接不住
          （那东西抓的是「跑着却没进展」，而这种情形每轮几十秒就干净退出）。
          所以 runner 那侧按「会不会自己好」分了两个退出码，
          见 `tools.pirate_loop.exit_code_for`。

        ⚠️ 豁免只认这**一个**退出码，不是「非 0 都不算」也不是「1 都不算」：
        真坏了必须还能数到三，否则调度循环会在一个坏掉的任务上满速空转。

        **`STALLED` 反过来：手是我们动的，但它算故障。** 那一轮跑着却一件事都
        没做成（判据见 `application.mission_progress`），退出码是 `terminate()`
        留下的、说明不了任何事，而「一件事都没做成」本身就是这条链路此刻起不来的
        证据。不算的话，同一个卡死会一轮接一轮地复现，每轮白烧 45 分钟。
        """
        if self.stopped_by is StopReason.STALLED:
            return True
        if self.stopped_by is not StopReason.SELF:
            return False
        if self.exit_code == EXIT_ENVIRONMENT_BUSY:
            return False
        return self.exit_code != 0


def log_path_for(kind: MissionKind, *, log_dir: Path = LOG_DIR) -> Path:
    """每条链路一个日志文件。混在一起，出事时分不出是谁的输出。

    **按链路分而不是按任务分**，哪怕同一链路现在可以有多个任务：任何时刻只有一个
    子进程在跑，同一条链路的两个任务在时间上天然不重叠，交错写进同一个文件读起来
    仍然是一条连贯的时间线。而按任务分会让文件名跟着 `task_id` 走——删掉一个任务
    再建一个，日志就落到新文件里，用户手上那条「看 `mission-bot.log`」的路径
    也就断了。「那一轮是哪个任务跑的」由 `mission_runs.task_id` 回答。
    """
    return log_dir / f"mission-{kind.value.lower()}.log"


def launch_mission(kind: MissionKind, command: Sequence[str], log_path: Path) -> Process:
    """真的拉起一个 runner。**测试里绝不调它。**

    `stderr` 并进 `stdout`：两条流分开写同一个文件会互相截断，而这份日志是
    出事之后唯一能看的东西。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    return subprocess.Popen(  # noqa: S603 - 命令行全由 `domain.missions` 构造
        list(command),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class MissionSupervisor:
    """同时只管一个子进程。

    `launch` 与 `clock` 都可注入：起停逻辑是这里唯一有分支的地方，把它和真实的
    `Popen`、真实的时钟隔开才测得了。
    """

    launch: Callable[[MissionKind, Sequence[str], Path], Process] = launch_mission
    clock: Callable[[], datetime] = _utc_now
    log_dir: Path = LOG_DIR

    _process: Process | None = field(default=None, init=False)
    _running: RunningChild | None = field(default=None, init=False)

    @property
    def running(self) -> RunningChild | None:
        return self._running

    def start(
        self, kind: MissionKind, command: Sequence[str], *, task_id: int, name: str = ""
    ) -> RunningChild:
        """起一个子进程。已经有一个在跑就拒绝，不排队也不替换。"""
        if self._running is not None:
            raise SupervisorBusyError(f"{self._running.kind.value} 还在跑，不能同时起 {kind.value}")
        log_path = log_path_for(kind, log_dir=self.log_dir)
        process = self.launch(kind, command, log_path)
        self._process = process
        self._running = RunningChild(
            task_id=task_id,
            kind=kind,
            name=name,
            command=tuple(command),
            pid=getattr(process, "pid", None),
            started_at_utc=self.clock(),
            log_path=log_path,
        )
        return self._running

    def stop(self, reason: StopReason) -> MissionExit | None:
        """立刻杀掉在跑的那个。没有在跑的就什么都不做。

        用户口径是「点了停就是停」，不等它跑完手上这一个。关闭、抢占、用户点停
        三条路都走这里，所以「本来就没在跑」必须是正常情况而不是错误。
        """
        running, self._running = self._running, None
        process, self._process = self._process, None
        if running is None or process is None:
            return None
        process.terminate()
        try:
            exit_code: int | None = process.wait(timeout=TERMINATE_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - 收不到退出码也不该让控制台卡住
            exit_code = None
        return self._exit(running, exit_code=exit_code, stopped_by=reason)

    def poll(self) -> MissionExit | None:
        """收退出码。**不自动重启。**

        没退出（或本来就没在跑）返回 None。退出只会被收一次——重复上报会让每个
        tick 都记一次失败，三下就把一个其实健康的任务误停用。
        """
        if self._running is None or self._process is None:
            return None
        exit_code = self._process.poll()
        if exit_code is None:
            return None
        running, self._running = self._running, None
        self._process = None
        return self._exit(running, exit_code=exit_code, stopped_by=StopReason.SELF)

    def _exit(
        self, running: RunningChild, *, exit_code: int | None, stopped_by: StopReason
    ) -> MissionExit:
        return MissionExit(
            task_id=running.task_id,
            kind=running.kind,
            command=running.command,
            exit_code=exit_code,
            stopped_by=stopped_by,
            started_at_utc=running.started_at_utc,
            ended_at_utc=self.clock(),
        )
