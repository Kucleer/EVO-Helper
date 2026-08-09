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
from typing import assert_never


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
    started_at_utc: datetime


@dataclass(frozen=True)
class SchedulerFacts:
    """一次调度所需的全部事实，全部来自数据库。

    `free_lines` 是**乐观估算**，不含用户自己派出去的舰队。权威的航线闸门
    在 runner 的 `game.capacity.LineCapacityGate` 里——它看屏。这里估高了，
    最坏结果是 runner 起来发现没位子、空跑一轮就退，不会误派。
    """

    now_utc: datetime
    free_lines: int
    #: 口径是 **UTC 00:00** 起累计（对应本地早 8 点），不是本地日历天。
    #: 按本地日历数，每天 UTC 0–8 点这段会把跨天前的次数错当成当天的，
    #: 提前把配额判成用尽。
    pirate_dispatches_today: int
    pirate_quota: int
    #: 收到游戏的超限邮件时写下的封锁截止时刻。比计数更硬的信号。
    pirate_blocked_until_utc: datetime | None
    #: 来自 `ReportWaitPlanner.plan(...).action is WaitAction.COLLECT`，
    #: **不是**自己另写一份 SQL 判据——规格明令只能有一份战报判据。
    #: `expected_report_at_utc` 为 NULL（飞行时间没读到）时 planner 的语义
    #: 是「立即收取」，若自建 `WHERE expected_report_at_utc <= now_utc`
    #: 会把这一档漏掉。
    pirate_reports_due: bool
    #: 同上，针对 BOT 链路，同样必须来自对应的 `ReportWaitPlanner.plan(...)`。
    bot_reports_due: bool
    bot_targets_remaining: int


@dataclass(frozen=True)
class Decision:
    action: Action
    kind: MissionKind | None = None


def has_work(kind: MissionKind, facts: SchedulerFacts) -> bool:
    """这条链路现在有没有事可做。"""
    if kind is MissionKind.SCAN:
        # 扫描不派遣，因此不受航线约束，也没有完成态。它正是用来填空隙的。
        return True

    if kind is MissionKind.PIRATE:
        if (
            facts.pirate_blocked_until_utc is not None
            and facts.pirate_blocked_until_utc > facts.now_utc
        ):
            return False
        if facts.pirate_dispatches_today >= facts.pirate_quota:
            return False
        return facts.free_lines > 0 or facts.pirate_reports_due

    if kind is MissionKind.BOT:
        if facts.bot_targets_remaining <= 0:
            return False
        return facts.free_lines > 0 or facts.bot_reports_due

    # 穷举到这里说明 MissionKind 加了新成员却没人补上面的分支——宁可让
    # strict mypy 在这里报错，也不要让新种类静默套用 BOT 的判据跑起来。
    assert_never(kind)


def decide(
    tasks: Sequence[TaskSnapshot],
    facts: SchedulerFacts,
    *,
    running: RunningProcess | None,
    min_dwell: timedelta,
) -> Decision:
    """下一步该做什么。

    并列 priority 之间按 `tasks` 的传入顺序决出胜负（`sorted` 是稳定排序）；
    数据库的 `priority` 列没有唯一约束，调用方要自己保证给出确定的次序，
    这里不再猜。
    """
    candidates = sorted(
        (task for task in tasks if task.enabled and task.disabled_reason is None),
        # 排序键是 (是不是 SCAN, priority)：SCAN 恒为 True，结构性地排到
        # 所有非 SCAN 之后，不看数据库里 priority 的实际数值。
        #
        # 为什么不能让 SCAN 按 priority 数值参与排序、被拖到攻击任务前面：
        # SCAN 不派遣、没有完成态，永远 has_work() == True。谁排在它后面
        # 就永远轮不到——海盗每天 32 次配额会在不知不觉间被扫描占满窗口
        # 耗光。页面已经禁止拖动 SCAN 行，但那只挡得住用户的鼠标，挡不住
        # 数据库里一条手改的坏行；领域层必须自己站得住，所以在这里结构性
        # 兜底一次。
        key=lambda task: (task.kind is MissionKind.SCAN, task.priority),
    )
    wanted = next((task.kind for task in candidates if has_work(task.kind, facts)), None)

    if running is not None:
        # 抢占只有一条规则：只有扫描会被打断。攻击轮中途杀掉可能正停在派遣面板上。
        if (
            running.kind is MissionKind.SCAN
            and wanted is not None
            # 上面的排序键已经让 SCAN 结构性地排最后，`wanted` 理论上不可能
            # 再是 SCAN——这一条在逻辑上恒真，留着是零成本的双保险，防的是
            # 排序键将来被改动却没人第一时间注意到。
            and wanted is not MissionKind.SCAN
            and facts.now_utc - running.started_at_utc >= min_dwell
        ):
            return Decision(Action.PREEMPT, wanted)
        return Decision(Action.IDLE)

    if wanted is None:
        return Decision(Action.IDLE)
    return Decision(Action.START, wanted)
