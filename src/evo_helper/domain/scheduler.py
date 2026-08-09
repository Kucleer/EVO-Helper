"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、**不看屏**。所有事实由调用方从数据库读好传进来，
这样调度器看到的和 `/logs` 页面看到的是同一份东西。

一条硬不变量贯穿全篇：**任何时刻最多一个子进程在点鼠标**。一个游戏窗口，
一个鼠标。所以这里给出的永远是「下一步做一件事」，不是任务队列。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import assert_never

#: 同一条链路两次启动之间的最小间隔。
#:
#: 堵的是「立即收取」的空转：`expected_report_at_utc` 为 NULL 时战报判据恒为
#: 「该去收」，而战报可能只是还没到（同系短程飞行按分钟计）。runner 进信箱、
#: 扑空、退出、下一 tick 判据仍为真、再起一次——不是死循环，但每轮几十秒的
#: 导航全是白费，还一直占着鼠标不让扫描进来。
#:
#: 冷却对扫描一视同仁：`MIN_DWELL` 只限制多快离开扫描，冷却限制多快回到扫描，
#: 两条合起来才挡得住「抢占—还回去—再抢占」的秒级来回，而每次来回都要
#: `ensure_game_window()` + 认屏。代价是扫描被抢占后有几分钟没人干活。
RESTART_COOLDOWN = timedelta(minutes=5)


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
    #: 每条链路上一次**启动**的时刻（不是上一次结束）。冷却按启动算：
    #: 一个刚起来就秒退的 runner，正是最该被节流的那种。
    #: 事实来自 `mission_runs` 里各 kind 的最大 `started_at_utc`，
    #: 这一层不去查库。
    last_started_at_utc: Mapping[MissionKind, datetime] = field(default_factory=dict)


def quota_day_start_utc(now: datetime) -> datetime:
    """当日配额从哪一刻起算。

    游戏的重置点是 **UTC 00:00**，本地（UTC+8）是每天早上 8 点。做成具名函数
    是因为调用方一旦自己写 `replace(hour=0)`，那个 `replace` 落在本地时刻上就
    悄悄变成了本地日历天：本地早上 0–8 点这段，起算点会被推后到本地 0 点
    （= 昨天 UTC 16:00），UTC 16:00 之后真实派出去的那些发被漏数，海盗会以为
    还有额度——而超限的代价是舰队被强制返回，白飞一趟。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("配额起算时刻必须带时区，否则无从判断它属于哪个 UTC 日")
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Decision:
    action: Action
    kind: MissionKind | None = None


def has_work(
    kind: MissionKind,
    facts: SchedulerFacts,
    *,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> bool:
    """这条链路现在有没有事可做。

    冷却期内一律算「没活干」，顺位让给下一个——它是判据的一部分而不是启动前的
    一道额外闸门，这样抢占那一路（`decide` 里靠 `wanted` 判断值不值得打断扫描）
    自动跟着生效：一条正在冷却的链路不该把扫描打断成谁都不在跑。
    """
    last_started = facts.last_started_at_utc.get(kind)
    if last_started is not None and facts.now_utc - last_started < restart_cooldown:
        return False

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
    restart_cooldown: timedelta = RESTART_COOLDOWN,
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
    wanted = next(
        (
            task.kind
            for task in candidates
            if has_work(task.kind, facts, restart_cooldown=restart_cooldown)
        ),
        None,
    )

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
