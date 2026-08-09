"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、**不看屏**。所有事实由调用方从数据库读好传进来，
这样调度器看到的和 `/logs` 页面看到的是同一份东西。

一条硬不变量贯穿全篇：**任何时刻最多一个子进程在点鼠标**。一个游戏窗口，
一个鼠标。所以这里给出的永远是「下一步做一件事」，不是任务队列。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class MissionKind(Enum):
    PIRATE = "PIRATE"
    BOT = "BOT"
    SCAN = "SCAN"


class Action(Enum):
    #: 空闲，起一个新的。
    START = "START"
    #: 打断正在跑的扫描，换成 `kind`。
    PREEMPT = "PREEMPT"
    #: 什么都不做。
    IDLE = "IDLE"


@dataclass(frozen=True)
class TaskSnapshot:
    kind: MissionKind
    enabled: bool
    priority: int
    #: 连续失败被自动停用的原因。非空即视为不参与调度。
    disabled_reason: str | None = None


@dataclass(frozen=True)
class RunningProcess:
    kind: MissionKind
    started_at: datetime


@dataclass(frozen=True)
class SchedulerFacts:
    """一次调度所需的全部事实，全部来自数据库。

    `free_lines` 是**乐观估算**，不含用户自己派出去的舰队。权威的航线闸门
    在 runner 的 `game.capacity.LineCapacityGate` 里——它看屏。这里估高了，
    最坏结果是 runner 起来发现没位子、空跑一轮就退，不会误派。
    """

    now: datetime
    free_lines: int
    pirate_dispatches_today: int
    pirate_quota: int
    #: 收到游戏的超限邮件时写下的封锁截止时刻。比计数更硬的信号。
    pirate_blocked_until: datetime | None
    pirate_reports_due: bool
    bot_reports_due: bool
    bot_targets_remaining: int


@dataclass(frozen=True)
class Decision:
    action: Action
    kind: MissionKind | None = None

    def __iter__(self):  # type: ignore[no-untyped-def]
        """允许 `assert decide(...) == (Action.START, kind)` 这样写测试。"""
        yield self.action
        yield self.kind

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return (self.action, self.kind) == other
        if isinstance(other, Decision):
            return (self.action, self.kind) == (other.action, other.kind)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.action, self.kind))


def has_work(kind: MissionKind, facts: SchedulerFacts) -> bool:
    """这条链路现在有没有事可做。"""
    if kind is MissionKind.SCAN:
        # 扫描不派遣，因此不受航线约束，也没有完成态。它正是用来填空隙的。
        return True

    if kind is MissionKind.PIRATE:
        if facts.pirate_blocked_until is not None and facts.pirate_blocked_until > facts.now:
            return False
        if facts.pirate_dispatches_today >= facts.pirate_quota:
            return False
        return facts.free_lines > 0 or facts.pirate_reports_due

    if facts.bot_targets_remaining <= 0:
        return False
    return facts.free_lines > 0 or facts.bot_reports_due


def decide(
    tasks: Sequence[TaskSnapshot],
    facts: SchedulerFacts,
    *,
    running: RunningProcess | None,
    min_dwell: timedelta,
) -> Decision:
    """下一步该做什么。"""
    candidates = sorted(
        (task for task in tasks if task.enabled and task.disabled_reason is None),
        key=lambda task: task.priority,
    )
    wanted = next((task.kind for task in candidates if has_work(task.kind, facts)), None)

    if running is not None:
        # 抢占只有一条规则：只有扫描会被打断。攻击轮中途杀掉可能正停在派遣面板上。
        if (
            running.kind is MissionKind.SCAN
            and wanted is not None
            and wanted is not MissionKind.SCAN
            and facts.now - running.started_at >= min_dwell
        ):
            return Decision(Action.PREEMPT, wanted)
        return Decision(Action.IDLE)

    if wanted is None:
        return Decision(Action.IDLE)
    return Decision(Action.START, wanted)
