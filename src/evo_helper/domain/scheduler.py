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

#: 同一条**攻击**链路两次启动之间的最小间隔。
#:
#: 堵的是「立即收取」的空转：`expected_report_at_utc` 为 NULL 时战报判据恒为
#: 「该去收」，而战报可能只是还没到（同系短程飞行按分钟计）。runner 进信箱、
#: 扑空、退出、下一 tick 判据仍为真、再起一次——不是死循环，但每轮几十秒的
#: 导航全是白费，还一直占着鼠标不让扫描进来。
#:
#: **`SCAN` 只在崩过之后才受它约束**，见 `cooling_down` 里那段。
RESTART_COOLDOWN = timedelta(minutes=5)

#: runner 用这个退出码说：**不是我坏了，是这会儿轮不到我**。
#:
#: 目前唯一的成因是「游戏窗口抢不到前台」——用户正在用别的窗口，而抢不到前台时
#: 唯一正确的动作是停下（把点击打到别人窗口上比什么都不做糟得多）。它和「这条
#: 链路坏了」在进程间协议上必须分得开：前者重试有意义、且**不该**计入连续失败，
#: 后者重试只会再来一遍。
#:
#: 取 75 是 BSD `sysexits.h` 的 `EX_TEMPFAIL`（"temporary failure; user is invited
#: to retry"），语义正好，也不会和 Python 未捕获异常的 1、`argparse` 的 2 撞上。
#:
#: ⚠️ **不能退化成「所有退出码 1 都不算失败」**：那样真坏了也永远不会停用，
#: 调度循环会在一个坏掉的任务上变成满速空转的重启循环。
EXIT_ENVIRONMENT_BUSY = 75


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


class TaskStatus(Enum):
    """页面与桌面悬浮窗上显示的那一句话。

    值直接是中文：显示层拿到就能用，不必再维护一张 `枚举 → 文案` 的映射表。
    多一张表就多一处能和判据走散的地方，而这一层本来就只为显示存在。
    （同 `domain.records.TARGET_KIND_LABELS`。）

    它放在领域层而不是 `web/`，是因为**这句话必须和调度器的实际行为出自同一份
    判据**：页面写着「等航线」而调度器其实在冷却，用户会去调航线数，调完还是
    不动。所以 `status_of` 复用 `has_work`，只在它为假时再去问「为什么」。
    """

    #: 这条链路的子进程正在跑。
    RUNNING = "运行中"
    #: 有活干，只是还没轮到它（或者调度器整个停着）。
    READY = "待命"
    #: 没有到期未收的战报，而且现在不值得为「去派」起一轮：估算的空闲航线为 0，
    #: 或者上一轮空手而归、正等着一条航线真的空出来（见 `waiting_for_a_line`）。
    WAITING_LINES = "等航线"
    #: 在重启冷却里。和「等航线」分开说，是因为用户能做的事不一样：
    #: 冷却只要等，等航线得看是不是航线数配小了。
    COOLING_DOWN = "冷却中"
    #: 当日 32 次用尽，或收到了游戏的超限硬信号。
    QUOTA_EXHAUSTED = "配额用尽"
    #: 仅 bot：本轮范围内每个目标都走完了流程。
    DONE = "已完成"
    #: 连续失败或参数不合格被自动停用，`disabled_reason` 里有原因。
    DISABLED = "已停用"
    #: 复选框没勾。**不能显示成「待命」**——它永远不会被起起来，那是句谎话。
    OFF = "未启用"


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
    在 runner 的 `game.capacity.LineCapacityGate` 里——它看屏。估高了不会误派，
    但**也不是没有代价**：runner 空跑那一轮要几十秒导航，还一直占着鼠标，而且
    错估没有回写路径，同一轮会每隔一个 `RESTART_COOLDOWN` 原样再来。兜这一层的
    是 `waiting_for_a_line`——它用 `last_dispatch_at_utc` 与 `next_line_free_at_utc`
    把「上一轮空手而归」变成「等到有航线真的空出来再试」。
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
    #: 每条链路最近一次**真的把舰队派出去**的时刻。和上一条比大小，就知道上一轮
    #: 是不是从头跑到尾一发都没派出去，见 `came_back_empty`。
    #: 来自 `repository.last_dispatch_at`，这一层不去查库。
    last_dispatch_at_utc: Mapping[MissionKind, datetime] = field(default_factory=dict)
    #: 已知最早会空出来的那条航线在什么时刻空。一条在飞记录都没有（或全是航线钟
    #: 读不到的那种）时为 None。`free_lines` 说的是「现在有几条」，这一条说的是
    #: 「下一条什么时候来」——`free_lines` 被现场推翻之后，只有后者能给出一个
    #: 值得再试的时刻。
    next_line_free_at_utc: datetime | None = None
    #: 每条链路上一次**自己退、且退出码非 0** 的时刻（正常收尾、抢占、用户点停
    #: 都不算）。只有 `cooling_down` 用它，而且只对 `SCAN` 用——理由写在那里。
    #:
    #: 口径比「算不算连续失败」宽一档：`EXIT_ENVIRONMENT_BUSY` 不计入失败，
    #: 但照样要冷却——用户正在用别的窗口，十几秒后再起一次还是抢不到前台。
    #: 由调用方按本次控制台运行期间的记忆填，这一层不去查库。
    last_failure_at_utc: Mapping[MissionKind, datetime] = field(default_factory=dict)


def quota_day_start_utc(now: datetime) -> datetime:
    """当日配额从哪一刻起算。

    游戏的重置点是 **UTC 00:00**，本地（UTC+8）是每天早上 8 点。做成具名函数
    是因为调用方一旦自己写 `replace(hour=0)`，那个 `replace` 落在本地时刻上就
    悄悄变成了本地日历天，而两者只在一天里的某几个钟头对得上：

    - 本地 0–8 点（UTC 还停在前一天的 16:00–24:00）：本地起算点 = UTC 当日
      16:00，比真正的 UTC 00:00 晚 16 小时，当日 00:00–16:00 派出去的全被**漏数**。
      海盗以为还有额度，超限的代价是舰队被强制返回，白飞一趟。
    - 本地 8 点之后：本地起算点跑到 UTC 当日之前，把昨天尾巴上的那些发**多数**
      进来，配额提前判成用尽，白白少打几次。
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("配额起算时刻必须带时区，否则无从判断它属于哪个 UTC 日")
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class Decision:
    action: Action
    kind: MissionKind | None = None


def cooling_down(
    kind: MissionKind,
    facts: SchedulerFacts,
    *,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> bool:
    """这条链路是不是还在两次启动之间的最小间隔里。

    **`SCAN` 只在上一次是异常退出时才冷却。** 冷却堵的 churn 是收战报特有的：
    `expected_report_at_utc` 为 NULL → 恒判「该去收」→ 进信箱扑空 → 退出 → 再来。
    扫描没有这种循环，它的游标持久化、随起随停没有代价，所以正常跑完之后不必等
    ——攻击轮两分钟跑完、扫描还得再等三分钟才允许回来，而填这种空隙正是扫描存在
    的全部理由。秒级来回由 `MIN_DWELL` 挡（它限制多快**离开**扫描），与这里限制
    多快**回到**某条链路不重复。

    **崩掉的那一档不一样，它有代价。** 扫描起来 14 秒就崩（实机
    2026-08-11 08:40:30 / 08:40:45 / 08:40:59，同一个「窗口抢不到前台」），
    而 `MAX_CONSECUTIVE_FAILURES` 是 3——不冷却的话，**43 秒**就把这条链路
    自动停用了，而另外两条有冷却的链路要撞满 10 分钟才落到同一个下场。于是最该
    一直有活干的那条，反而最容易被一阵前台争抢误判成坏掉。让它和别人一样等：
    三次连崩就意味着「持续十分钟起不来」，而不是「四十三秒里连崩三次」。

    起算点两档不同，也必须不同：别人按**启动**算（刚起来就秒退的 runner 正是最
    该被节流的那种），扫描按**上一次崩**算——按启动算就等于把它那条「跑完随时
    可以再来」的特性一起砍掉了。
    """
    if kind is MissionKind.SCAN:
        last_failure = facts.last_failure_at_utc.get(kind)
        return last_failure is not None and facts.now_utc - last_failure < restart_cooldown
    last_started = facts.last_started_at_utc.get(kind)
    return last_started is not None and facts.now_utc - last_started < restart_cooldown


def pirate_quota_exhausted(facts: SchedulerFacts) -> bool:
    """海盗当日还能不能打。两个判据取先到者。

    单独成函数是为了让状态文案能复用它：「配额用尽」和「等航线」的处置完全
    不同（一个要等到 UTC 00:00，一个等舰队回来），而重写一遍比较式就等于给
    同一条判据留了第二份实现。
    """
    blocked_until = facts.pirate_blocked_until_utc
    if blocked_until is not None and blocked_until > facts.now_utc:
        return True
    return facts.pirate_dispatches_today >= facts.pirate_quota


def came_back_empty(kind: MissionKind, facts: SchedulerFacts) -> bool:
    """这条链路上一轮跑完，一发都没派出去。

    判据就是两个时刻比大小：上一次启动之后再没有过一条被接受的派遣记录。
    没跑过的链路不算（没有「上一轮」可言）。

    **它不声称自己认出了「航线满了」。** 认那件事的是 runner，它看屏，而它撞上
    「同时派遣的舰队数量已达上限。」之后走的是正常收尾、退出码 0——和「这一圈
    没有海盗」在进程间协议上一模一样，调度器这一侧分不出来，也不该去猜。
    这里只陈述一个能查证的事实：那一轮空手而归。
    """
    started = facts.last_started_at_utc.get(kind)
    if started is None:
        return False
    dispatched = facts.last_dispatch_at_utc.get(kind)
    return dispatched is None or dispatched < started


def waiting_for_a_line(kind: MissionKind, facts: SchedulerFacts) -> bool:
    """要不要压着这条链路，等到有一条航线真的空出来再让它去派。

    两个条件同时成立才压：**上一轮空手而归**，而且**还有一条在飞的舰队没回来**。

    **为什么空手而归就要疑心航线。** `free_lines` 是只按自家派遣记录算出来的
    估算，数不到用户自己派出去的舰队，也数不到航线钟被读错的那些。估错了没有
    任何回写路径——runner 在屏上看到了真相，可它撞上限之后的退出码和跑完一轮
    正常收尾一模一样，于是同一个错估每隔一个 `RESTART_COOLDOWN` 就原样再来一次。
    实机 2026-08-11 01:12–01:34（本地 09:12–09:34）：`free_lines` 一路报 3，游戏
    那边 6 条航线全满，海盗与 bot 交替起了九轮，每轮几十秒导航之后撞上限退出。

    **为什么还要有第二个条件。** 空手而归有别的成因（这一圈没有海盗、目标都在
    保护期里），单凭它就压着链路，等于把一条与航线无关的规则塞进航线判据。
    `next_line_free_at_utc` 就是把话说死的那个锚点：只有真有舰队在外面没回来，
    「等它回来再试」才成立；一条在飞记录都没有的时候，航线满不满这件事这一层
    没有任何证据，那就不猜——照旧交给 `RESTART_COOLDOWN` 节流。

    **因此它不可能变成永久不起**：压到的那个时刻是库里查出来的，到点自动解除。
    而且它只挡「去派」这半边判据，「回去收战报」那半边一个字都不动——收报告不占
    航线，压着它只会让战报烂在信箱里。

    **`SCAN` 恒为假。** 它压根不派遣，航线满不满与它无关；而 `came_back_empty`
    对它恒为真（它永远不会出现在 `last_dispatch_at_utc` 里），不挡一道的话，
    一条只是在崩溃冷却里的扫描会被 `status_of` 说成「等航线」——一句用户照着
    去调航线数、调完也不会有任何变化的假话。
    """
    if kind is MissionKind.SCAN:
        return False
    if not came_back_empty(kind, facts):
        return False
    next_free = facts.next_line_free_at_utc
    return next_free is not None and next_free > facts.now_utc


def bot_round_complete(facts: SchedulerFacts) -> bool:
    """本轮范围内是不是每个目标都走完了流程。同上，供状态文案复用。"""
    return facts.bot_targets_remaining <= 0


def has_work(
    kind: MissionKind,
    facts: SchedulerFacts,
    *,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> bool:
    """这条链路现在有没有事可做。

    冷却期内一律算「没活干」（`cooling_down`；`SCAN` 只在崩过之后才有冷却），
    顺位让给下一个——它是判据的一部分而不是启动前的一道额外闸门，这样抢占那一路
    （`decide` 里靠
    `wanted` 判断值不值得打断扫描）自动跟着生效：一条正在冷却的链路不该把扫描
    打断成谁都不在跑。

    两条攻击链路的判据都是「**有航线可派** 或 **有战报该收**」。左半边多一道
    `waiting_for_a_line`：`free_lines` 只是估算，被现场推翻过就不能再照着它起轮。
    右半边不加任何闸门——收报告不占航线。
    """
    if cooling_down(kind, facts, restart_cooldown=restart_cooldown):
        return False

    if kind is MissionKind.SCAN:
        # 扫描不派遣，因此不受航线约束，也没有完成态。它正是用来填空隙的。
        return True

    can_dispatch = facts.free_lines > 0 and not waiting_for_a_line(kind, facts)

    if kind is MissionKind.PIRATE:
        if pirate_quota_exhausted(facts):
            return False
        return can_dispatch or facts.pirate_reports_due

    if kind is MissionKind.BOT:
        if bot_round_complete(facts):
            return False
        return can_dispatch or facts.bot_reports_due

    # 穷举到这里说明 MissionKind 加了新成员却没人补上面的分支——宁可让
    # strict mypy 在这里报错，也不要让新种类静默套用 BOT 的判据跑起来。
    assert_never(kind)


def scheduling_order(task: TaskSnapshot) -> tuple[bool, int]:
    """排序键：`(是不是 SCAN, priority)`。升序即调度次序。

    `SCAN` 恒为 True，结构性地排到所有非 `SCAN` 之后，**不看数据库里 priority
    的实际数值**。理由：`SCAN` 不派遣、没有完成态，`has_work()` 永远为真，
    谁排在它后面就永远轮不到——海盗每天 32 次配额会在不知不觉间被扫描占满
    窗口耗光。页面已经禁止拖动 `SCAN` 行、接口也拒绝写它的 priority，但那些
    只挡得住用户；挡不住数据库里一条手改的坏行，所以领域层自己兜底一次。

    抽成具名函数是为了让页面的展示次序和 `decide()` 的调度次序共用同一把尺子：
    两处各写一遍，就会出现「页面上排第一、实际最后才跑」。
    """
    return (task.kind is MissionKind.SCAN, task.priority)


def status_of(
    task: TaskSnapshot,
    facts: SchedulerFacts,
    *,
    running: RunningProcess | None,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> TaskStatus:
    """这条链路现在该显示成什么。

    次序即优先级，前面的更值得说：停用原因 > 没勾 > 正在跑 > 为什么不跑。

    **不新写判据。** 「有没有活干」一律问 `has_work`；只有它说「没有」时才
    再去问「为什么没有」，而那几个原因也都是复用出来的谓词。这样页面上的一句话
    和调度器的下一步动作不可能走散——走散的后果是用户照着一句错话去调错参数。
    """
    if task.disabled_reason is not None:
        return TaskStatus.DISABLED
    if not task.enabled:
        return TaskStatus.OFF
    if running is not None and running.kind is task.kind:
        return TaskStatus.RUNNING
    if has_work(task.kind, facts, restart_cooldown=restart_cooldown):
        return TaskStatus.READY
    # 以下都是「没活干」的几种原因。先说结构性的（配额、完成），再说会自己
    # 好起来的（冷却、航线）——前两种要用户动手，后两种只要等。
    if task.kind is MissionKind.PIRATE and pirate_quota_exhausted(facts):
        return TaskStatus.QUOTA_EXHAUSTED
    if task.kind is MissionKind.BOT and bot_round_complete(facts):
        return TaskStatus.DONE
    # 「等航线」排在「冷却中」前面：两者同时成立时，等航线是那个更长、也更该让
    # 用户看到的原因。反过来显示成「冷却中」，用户会以为再等五分钟就动，
    # 然后眼看着它到点也不动。
    if waiting_for_a_line(task.kind, facts):
        return TaskStatus.WAITING_LINES
    if cooling_down(task.kind, facts, restart_cooldown=restart_cooldown):
        return TaskStatus.COOLING_DOWN
    return TaskStatus.WAITING_LINES


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
        key=scheduling_order,
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
