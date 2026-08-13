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

from evo_helper.domain.models import Coordinate

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

#: 多条链路的异常退出落在这么长的一个窗口里，就当成**同一阵**故障看。
#:
#: 取 15 分钟的依据是 `RESTART_COOLDOWN`（5 分钟）：环境坏掉时每条链路都是起来
#: 就崩、崩完等一个冷却、再来，所以 15 分钟里每条启用着的链路都轮得到两三次。
#: 也就是说「环境坏了」这件事**必然**在一个 15 分钟窗口里留下两条以上链路的
#: 失败记录；反过来，两处互不相干的真故障恰好落进同一个 15 分钟窗口，在一整夜里
#: 是小概率——除非它们各自都在高频复发，而那一档由 `MAX_ENVIRONMENT_EXEMPTIONS`
#: 的上限兜底（见 `application.mission_scheduler`）。
ENVIRONMENT_FAULT_WINDOW = timedelta(minutes=15)

#: 至少要有这么多个**不同任务**一起失败，才谈得上「环境坏了」。
#:
#: 必须是 2 而不是 1：1 就等于「所有失败都不算失败」，自动停用直接失效。
#: 也不该是 3——启用着的任务可能只有两个，要求三个会让这条判据在最常见的
#: 配置下永远不成立。
#:
#: 口径从「不同链路」放宽到「不同任务」是多任务带来的：两个 bot 任务共用同一个
#: 游戏窗口、同一只鼠标，它们一起倒同样是那些共用的东西坏了的证据。代价是两个
#: 配置写错的 bot 任务会互相佐证——但配置不合格走的是 `disable_mission_task`
#: 那条路（不计失败），而豁免本身有上限（`MAX_ENVIRONMENT_EXEMPTIONS`）。
ENVIRONMENT_FAULT_TASKS = 2


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
    """一个任务的身份与配置。**同一 `kind` 可以有多行**（用户口径 2026-08-13：
    「可能会新增多个同一个类型的任务，比如 2 个 bot 攻击」）。

    因此判据一律按 `task_id` 认人，不再按 `kind` 认人：按 kind 认的话，两个 bot
    任务会共用冷却、共用「上一轮空手而归」，一个刚跑完就把另一个压住五分钟。
    """

    task_id: int
    kind: MissionKind
    #: 用户给这个任务起的名字。**只用于显示与记账**，判据一个字都不看它。
    name: str
    enabled: bool
    priority: int
    #: 出发星球。**航线上限是按星球各一份的**（用户口径 2026-08-13），所以它不只
    #: 是显示值：占用只算同一颗星球上的在飞派遣，见 `free_lines_for`。
    origin: Coordinate
    #: 这个任务允许在它那颗出发星球上占用几条航线。
    fleet_lines: int
    #: 连续失败被自动停用的原因。非空即视为不参与调度。
    disabled_reason: str | None = None


@dataclass(frozen=True)
class RunningProcess:
    task_id: int
    kind: MissionKind
    started_at_utc: datetime


@dataclass(frozen=True)
class TaskFacts:
    """**某一个任务**自己的事实。按 `task_id` 挂在 `SchedulerFacts.per_task` 上。

    分成两层（任务一层、账号一层）是因为两类事实的作用域本来就不同：航线按
    **星球**算，海盗每天 32 次按**账号**算。把它们摊平在一个扁平的结构里，迟早
    有人拿账号级的数去判一颗星球，或者反过来。
    """

    #: 这个任务此刻还能派几发。**已经把它自己的航线数、以及它那颗出发星球上的
    #: 在飞数算进去了**（`free_lines_for`）。
    #:
    #: 它是**乐观估算**，不含用户自己派出去的舰队。权威的航线闸门在 runner 的
    #: `game.capacity.LineCapacityGate` 里——它看屏。估高了不会误派，但**也不是
    #: 没有代价**：runner 空跑那一轮要几十秒导航，还一直占着鼠标，而且错估没有
    #: 回写路径，同一轮会每隔一个 `RESTART_COOLDOWN` 原样再来。兜这一层的是
    #: `waiting_for_a_line`。
    free_lines: int = 0
    #: 来自 `ReportWaitPlanner.plan(...).action is WaitAction.COLLECT`，
    #: **不是**自己另写一份 SQL 判据——规格明令只能有一份战报判据。
    #: `expected_report_at_utc` 为 NULL（飞行时间没读到）时 planner 的语义
    #: 是「立即收取」，若自建 `WHERE expected_report_at_utc <= now_utc`
    #: 会把这一档漏掉。
    reports_due: bool = False
    #: 仅 BOT：本轮范围内还有几个目标没走完流程。
    targets_remaining: int = 0
    #: 上一次**启动**的时刻（不是上一次结束）。冷却按启动算：一个刚起来就秒退的
    #: runner，正是最该被节流的那种。事实来自 `mission_runs` 里该任务的最大
    #: `started_at_utc`，这一层不去查库。
    last_started_at_utc: datetime | None = None
    #: 最近一次从**这个任务的出发星球**上真的把舰队派出去的时刻。和上一条比大小，
    #: 就知道上一轮是不是从头跑到尾一发都没派出去，见 `came_back_empty`。
    last_dispatch_at_utc: datetime | None = None
    #: **这颗出发星球上**已知最早会空出来的那条航线在什么时刻空。一条在飞记录都
    #: 没有（或全是航线钟读不到的那种）时为 None。`free_lines` 说的是「现在有
    #: 几条」，这一条说的是「下一条什么时候来」——`free_lines` 被现场推翻之后，
    #: 只有后者能给出一个值得再试的时刻。
    next_line_free_at_utc: datetime | None = None
    #: 上一次**自己退、且退出码非 0** 的时刻（正常收尾、抢占、用户点停都不算）。
    #: 只有 `cooling_down` 用它，而且只对 `SCAN` 用——理由写在那里。
    #:
    #: 口径比「算不算连续失败」宽一档：`EXIT_ENVIRONMENT_BUSY` 不计入失败，
    #: 但照样要冷却——用户正在用别的窗口，十几秒后再起一次还是抢不到前台。
    #: 由调用方按本次控制台运行期间的记忆填，这一层不去查库。
    last_failure_at_utc: datetime | None = None


#: 没有任何事实的任务看到的那一份。`free_lines=0` 是有意的保守值：
#: 事实没读到就当作「派不了」，而不是当作「随便派」。
NO_FACTS = TaskFacts()


@dataclass(frozen=True)
class SchedulerFacts:
    """一次调度所需的全部事实，全部来自数据库。

    只留**账号级**的那几个；按任务分的都在 `per_task` 里。
    """

    now_utc: datetime
    #: 口径是 **UTC 00:00** 起累计（对应本地早 8 点），不是本地日历天。
    #: 按本地日历数，每天 UTC 0–8 点这段会把跨天前的次数错当成当天的，
    #: 提前把配额判成用尽。
    #:
    #: ⚠️ **它是账号级的，不按星球分。** 海盗每天 32 次是游戏对账号的硬限制，
    #: 和航线（按星球各一份）不是一回事——跟着改成按星球，等于把配额凭空翻倍，
    #: 超了会收到超限邮件且舰队被强制返回。
    pirate_dispatches_today: int = 0
    pirate_quota: int = 32
    #: 收到游戏的超限邮件时写下的封锁截止时刻。比计数更硬的信号。同样是账号级。
    pirate_blocked_until_utc: datetime | None = None
    #: 按 `task_id` 挂的逐任务事实。查不到的任务看到的是 `NO_FACTS`。
    per_task: Mapping[int, TaskFacts] = field(default_factory=dict)

    def of(self, task: TaskSnapshot) -> TaskFacts:
        return self.per_task.get(task.task_id, NO_FACTS)


def free_lines_for(task: TaskSnapshot, *, inflight_from_origin: int, reserved_lines: int) -> int:
    """这个任务此刻还能派几发。

    **`inflight_from_origin` 必须只数同一颗出发星球上的在飞派遣。** 游戏的航线
    上限是按星球各一份的（用户口径 2026-08-13：「航线上限是按星球各一份的，不是
    账号共享」），跨星球一起数等于把两颗星球的额度当成一份用——主星打满 6 条之后，
    2 号星那个任务会以为自己也没位子了，一发都不派。

    `reserved_lines` 是给用户自己留的缓冲，**按星球生效**：`free_lines` 只是估算
    （数不到用户手动派出去的舰队），这几条位子就是为那段误差留的。
    """
    usable = max(task.fleet_lines - reserved_lines, 0)
    return max(usable - inflight_from_origin, 0)


def tasks_failing_together(
    task_id: int,
    at: datetime,
    recent_faults: Mapping[int, datetime],
    *,
    window: timedelta = ENVIRONMENT_FAULT_WINDOW,
) -> frozenset[int]:
    """和这次失败挤在同一个时间窗里的所有任务（含它自己）。

    `recent_faults` 是「每个任务最近一次**真的算故障**的退出时刻」。
    `EXIT_ENVIRONMENT_BUSY` 那一档不该进来——它本来就不计失败，拿它当佐证
    等于让「用户在用别的窗口」去豁免另一个任务真正的崩溃。

    比 `at` 还晚的记录一律忽略：调用方按事件顺序喂进来，出现未来的时刻只能是
    时钟被调过，那时宁可少认一次环境故障，也不要凭一个说不清的差值去豁免。
    """
    return frozenset(
        other for other, moment in recent_faults.items() if moment <= at and at - moment <= window
    ) | {task_id}


def looks_like_an_environment_fault(
    task_id: int,
    at: datetime,
    recent_faults: Mapping[int, datetime],
    *,
    window: timedelta = ENVIRONMENT_FAULT_WINDOW,
    min_tasks: int = ENVIRONMENT_FAULT_TASKS,
) -> bool:
    """这次失败该不该记到这个任务头上——不该，如果别的任务同时也在倒。

    **实机 2026-08-12。** 01:55「BOT 已停用（连续 3 次异常退出，退出码 1）」，
    04:37 三条**全部**已停用。三条链路共用一个游戏窗口、一个鼠标、一份网络连接
    和一台机器，同时坏掉几乎必然是那些共用的东西坏了——掉线、服务端维护、窗口
    被别的程序抢走、机器休眠——而不是三处互不相干的代码在同一晚一起长出 bug。
    把它记成三条链路各自的故障，代价是 BOT 从 01:55 停到 04:37，近三个小时
    一发没派，而中间那段时间环境早就好了。

    **怎么和「三条恰好各自坏了」分开。** 单看一阵失败是分不开的，分得开的是
    「之后有没有一起好起来」：环境故障会结束，结束之后三条都能跑通；三处真故障
    不会。所以这一层只给出「值得怀疑是环境」这个信号，**豁免不是无限的**——
    调用方按 `MAX_ENVIRONMENT_EXEMPTIONS` 记账，任何一条链路跑出一次退出码 0
    就清零，一次都跑不通时豁免会用尽，自动停用照旧生效，只是来得晚一些。

    这和仓库里已有的两档豁免是同一个形状：`RoundExhausted`（资源耗尽不是失败）、
    `EXIT_ENVIRONMENT_BUSY`（抢不到前台不是失败）。缺的一直是「多条一起倒」。
    """
    return len(tasks_failing_together(task_id, at, recent_faults, window=window)) >= min_tasks


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
    #: 该起（或该抢占换上）的那个任务。`IDLE` 时为 None。
    #: 带整个快照而不是只带 `task_id`：调用方接下来要拿它的 `kind` 组命令行、
    #: 拿它的 `origin` 记账，再去库里按 id 捞一遍只是给两份事实走散留机会。
    task: TaskSnapshot | None = None


def cooling_down(
    task: TaskSnapshot,
    facts: SchedulerFacts,
    *,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> bool:
    """这个任务是不是还在两次启动之间的最小间隔里。

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

    **按任务算，不按链路算。** 两个 bot 任务各自有各自的冷却：按链路算的话，
    主星那个任务刚跑完，2 号星那个就得干等五分钟，而它俩占的根本不是同一份航线。
    """
    task_facts = facts.of(task)
    if task.kind is MissionKind.SCAN:
        last_failure = task_facts.last_failure_at_utc
        return last_failure is not None and facts.now_utc - last_failure < restart_cooldown
    last_started = task_facts.last_started_at_utc
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


def came_back_empty(task: TaskSnapshot, facts: SchedulerFacts) -> bool:
    """这个任务上一轮跑完，一发都没派出去。

    判据就是两个时刻比大小：上一次启动之后再没有过一条被接受的派遣记录。
    没跑过的链路不算（没有「上一轮」可言）。

    **它不声称自己认出了「航线满了」。** 认那件事的是 runner，它看屏，而它撞上
    「同时派遣的舰队数量已达上限。」之后走的是正常收尾、退出码 0——和「这一圈
    没有海盗」在进程间协议上一模一样，调度器这一侧分不出来，也不该去猜。
    这里只陈述一个能查证的事实：那一轮空手而归。

    ⚠️ **「派出去了」按出发星球数**（`TaskFacts.last_dispatch_at_utc` 由调用方按
    `origin` 过滤）。跨星球一起数的话，主星那个任务派出去的一发会让 2 号星那个
    任务看起来「上一轮有派出去」，于是它撞满航线之后照样每五分钟白跑一轮。
    """
    started = facts.of(task).last_started_at_utc
    if started is None:
        return False
    dispatched = facts.of(task).last_dispatch_at_utc
    return dispatched is None or dispatched < started


def waiting_for_a_line(task: TaskSnapshot, facts: SchedulerFacts) -> bool:
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
    if task.kind is MissionKind.SCAN:
        return False
    if not came_back_empty(task, facts):
        return False
    # 同样是**这颗出发星球上**下一条航线什么时候空：拿别的星球的返航时刻当闹钟，
    # 压住的那段时间与这个任务能不能派毫无关系。
    next_free = facts.of(task).next_line_free_at_utc
    return next_free is not None and next_free > facts.now_utc


def bot_round_complete(task: TaskSnapshot, facts: SchedulerFacts) -> bool:
    """本轮范围内是不是每个目标都走完了流程。同上，供状态文案复用。

    每个 bot 任务各有各的范围与各自的 `round_started_at_utc`，所以它是**按任务**
    问的：合起来数的话，两个任务里只要有一个还剩目标，另一个就永远显示不出
    「已完成」，「重开一轮」那个按钮也就永远不出现。
    """
    return facts.of(task).targets_remaining <= 0


def has_work(
    task: TaskSnapshot,
    facts: SchedulerFacts,
    *,
    restart_cooldown: timedelta = RESTART_COOLDOWN,
) -> bool:
    """这个任务现在有没有事可做。

    冷却期内一律算「没活干」（`cooling_down`；`SCAN` 只在崩过之后才有冷却），
    顺位让给下一个——它是判据的一部分而不是启动前的一道额外闸门，这样抢占那一路
    （`decide` 里靠
    `wanted` 判断值不值得打断扫描）自动跟着生效：一条正在冷却的链路不该把扫描
    打断成谁都不在跑。

    两条攻击链路的判据都是「**有航线可派** 或 **有战报该收**」。左半边多一道
    `waiting_for_a_line`：`free_lines` 只是估算，被现场推翻过就不能再照着它起轮。
    右半边不加任何闸门——收报告不占航线。

    `free_lines` 是**这个任务在它那颗出发星球上**还剩几条（见 `free_lines_for`），
    所以「同一颗星球在飞数达到该任务的航线数就不再派」与「不同星球互不影响」
    这两条在这里是同一个判据的两面，不需要各写一份。
    """
    if cooling_down(task, facts, restart_cooldown=restart_cooldown):
        return False

    if task.kind is MissionKind.SCAN:
        # 扫描不派遣，因此不受航线约束，也没有完成态。它正是用来填空隙的。
        return True

    can_dispatch = facts.of(task).free_lines > 0 and not waiting_for_a_line(task, facts)

    if task.kind is MissionKind.PIRATE:
        # 配额是账号级的（见 `SchedulerFacts.pirate_dispatches_today`）。
        if pirate_quota_exhausted(facts):
            return False
        return can_dispatch or facts.of(task).reports_due

    if task.kind is MissionKind.BOT:
        if bot_round_complete(task, facts):
            return False
        return can_dispatch or facts.of(task).reports_due

    # 穷举到这里说明 MissionKind 加了新成员却没人补上面的分支——宁可让
    # strict mypy 在这里报错，也不要让新种类静默套用 BOT 的判据跑起来。
    assert_never(task.kind)


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
    # 认的是 `task_id` 而不是 `kind`：两个 bot 任务同时显示「运行中」是句谎话，
    # 而任何时刻只有一个子进程在点鼠标。
    if running is not None and running.task_id == task.task_id:
        return TaskStatus.RUNNING
    if has_work(task, facts, restart_cooldown=restart_cooldown):
        return TaskStatus.READY
    # 以下都是「没活干」的几种原因。先说结构性的（配额、完成），再说会自己
    # 好起来的（冷却、航线）——前两种要用户动手，后两种只要等。
    if task.kind is MissionKind.PIRATE and pirate_quota_exhausted(facts):
        return TaskStatus.QUOTA_EXHAUSTED
    if task.kind is MissionKind.BOT and bot_round_complete(task, facts):
        return TaskStatus.DONE
    # 「等航线」排在「冷却中」前面：两者同时成立时，等航线是那个更长、也更该让
    # 用户看到的原因。反过来显示成「冷却中」，用户会以为再等五分钟就动，
    # 然后眼看着它到点也不动。
    if waiting_for_a_line(task, facts):
        return TaskStatus.WAITING_LINES
    if cooling_down(task, facts, restart_cooldown=restart_cooldown):
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
        (task for task in candidates if has_work(task, facts, restart_cooldown=restart_cooldown)),
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
            and wanted.kind is not MissionKind.SCAN
            and facts.now_utc - running.started_at_utc >= min_dwell
        ):
            return Decision(Action.PREEMPT, wanted)
        return Decision(Action.IDLE)

    if wanted is None:
        return Decision(Action.IDLE)
    return Decision(Action.START, wanted)
