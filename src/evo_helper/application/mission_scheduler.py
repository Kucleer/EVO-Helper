"""常驻调度循环：把纯判据、子进程管理、数据库粘起来。

判据在 `domain/scheduler.py`（纯函数，不碰 IO），进程在
`application/mission_supervisor.py`（不碰判据），事实在
`storage/repository.py`。这一层只做三件事：**把事实读对**、**把参数换算成
命令行**、**把每次起停记进账**。

「读对」不是修辞。`pending_reports_for_kind` 的 `grace` / `max_age` 没有默认值，
传错了不会报错，只会让调度器静默地空转或者永久卡死——那正是这整条修复要防的
东西。日配额的起算点同理：本地日历天和 UTC 日只在一天里的某几个钟头对得上。
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from evo_helper.application.backfill import (
    BACKFILL_KINDS,
    REASON_STARTUP,
    BackfillCoordinator,
    BackfillCounts,
    BackfillMeasurement,
    BackfillRequest,
    BackfillState,
    SqlAlchemyBackfillCounts,
    default_since,
)
from evo_helper.application.mission_freeze import (
    FrozenTask,
    MissionConfigFreeze,
    MissionFreezeLog,
    freeze_now,
)
from evo_helper.application.mission_progress import (
    MissionProgress,
    SqlAlchemyMissionProgress,
    StallWatchdog,
)
from evo_helper.application.mission_supervisor import (
    MissionExit,
    MissionSupervisor,
    RunningChild,
    StopReason,
)
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET, BotPhase, phase_of
from evo_helper.domain.distance import nearest_first
from evo_helper.domain.military_attack import (
    AssignedTarget,
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_distance,
    military_pool,
)
from evo_helper.domain.missions import (
    ORIGIN,
    MissionParamError,
    bot_command,
    bot_targets_in_range,
    pirate_command,
    pirate_systems,
    ranking_command,
    scan_command,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import is_bot_coordinate
from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
from evo_helper.domain.report_wait import MAX_REPORT_AGE, ReportWaitPlanner, WaitAction
from evo_helper.domain.scheduler import (
    Action,
    Decision,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    decide,
    fills_gaps,
    free_lines_for,
    has_work,
    looks_like_an_environment_fault,
    quota_day_start_utc,
    tasks_failing_together,
    within_schedule_window,
)
from evo_helper.domain.target_order import (
    TOP_BY_MILITARY,
    ScoredTarget,
    strongest_then_nearest,
)
from evo_helper.infrastructure.system_log import child_environment, record_system_log
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

_LOGGER = logging.getLogger(__name__)

#: 同一任务连续这么多次异常退出就自动停用。
#:
#: 没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环：起、崩、
#: 下一 tick 判据仍为真、再起。失败多半是「窗口抢不到前台」或「甩鼠标触发
#: FAILSAFE」，重试只会再来一遍，所以三次就够——再多只是多刷几行日志。
MAX_CONSECUTIVE_FAILURES = 3

#: 「多个任务一起倒 → 不记到任何一个头上」这条豁免，同一个任务最多连着吃几次。
#:
#: **豁免必须有尽头，否则两处真故障就永远停不掉。** 两个任务各自都在高频复发
#: 时，它们的失败会一直互相佐证，判据永远说「像是环境坏了」——那就退回到
#: 「一个坏掉的任务上满速空转」，正是 `MAX_CONSECUTIVE_FAILURES` 当初要防的。
#:
#: 取 6：每次豁免之间至少隔一个 `RESTART_COOLDOWN`（5 分钟），六次≈半小时。
#: 真的环境故障（掉线、服务端维护、被抢前台）里，半小时足够撑过绝大多数；
#: 撑不过的那种（整晚维护）本来也该停下来等人。豁免用尽之后计数照常，
#: 再撞三次才停用，加起来给了一个任务约 45 分钟的余地——而原先只有约 10 分钟。
#:
#: **任何一个任务跑出一次退出码 0 就全部清零**：那一刻环境被证明是好的，
#: 之前那几次豁免不该再算在谁头上（见 `_finish`）。
MAX_ENVIRONMENT_EXEMPTIONS = 6

#: 调度器的任务种类 → `attack_intents.target_kind` 的取值。
#: 两套词汇本来就不同（一个是链路，一个是打谁），映射写明白比两边硬凑一致好。
_TARGET_KIND = {
    MissionKind.PIRATE: TARGET_KIND_PIRATE,
    MissionKind.BOT: TARGET_KIND_BOT,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class SchedulerSnapshot:
    """一眼看全的调度器现状，供 API 搬给页面和桌面悬浮窗。

    事实与判据分开放：`facts` 原样来自数据库，状态那句话由
    `domain.scheduler.status_of` 现算。这一层不解释任何事情——解释一旦在这里
    再写一遍，页面显示的和调度器下一步要做的就会是两份判据。
    """

    enabled: bool
    #: 点「开始」的时刻。页面上那块秒表的起点；`enabled` 为假时是 None。
    started_at_utc: datetime | None
    running: RunningChild | None
    #: 上次没走正常关闭路径留下的进程号，只用来显示，**不拿它开枪**。
    orphan_pid: int | None
    tasks: tuple[orm.MissionTaskRow, ...]
    #: 与 `tasks` 一一对应的领域快照（出发星球与航线数已经把默认值解析完）。
    #: 一起带出来而不是让每个读者自己再算一遍：解析规则（NULL = 用全局）只该有
    #: 一份，两份迟早会在「页面显示的出发星球」和「舰队真正从哪出发」上分家。
    snapshots: tuple[TaskSnapshot, ...]
    config: orm.SchedulerConfigRow
    facts: SchedulerFacts
    #: 任务配置现在改不改得动。见 `MissionScheduler.config_locked`。
    config_locked: bool = False
    #: **本轮**开始那一刻固化下来的配置。停着时为 None——停着的时候「本轮」
    #: 不存在，把上一轮那份继续挂在页面上会被读成「现在跑的就是这套」。
    #: 历史那几份走 `MissionScheduler.config_freezes()`。
    frozen_config: MissionConfigFreeze | None = None


class MissionScheduler:
    """点一次「开始」就常驻运行，直到点「结束」。

    开关**不持久化**：控制台重启后一律停在「已停止」。重启多半意味着出了事，
    自动接着派舰队不是好默认。
    """

    def __init__(
        self,
        repository: SqlAlchemyRepository,
        supervisor: MissionSupervisor,
        *,
        clock: Callable[[], datetime] = _utc_now,
        planner: ReportWaitPlanner | None = None,
        origin: Coordinate = ORIGIN,
        freeze_log: MissionFreezeLog | None = None,
        progress: MissionProgress | None = None,
        watchdog: StallWatchdog | None = None,
        backfill: BackfillCoordinator | None = None,
        backfill_counts: BackfillCounts | None = None,
    ) -> None:
        self._repository = repository
        self._supervisor = supervisor
        self._clock = clock
        #: 手动战报补录。**它优先于所有任务**，理由写在 `application.backfill`
        #: 的模块头上（一句话：补录改的正是任务读来做决策的那批数据）。
        #: 默认那一份一直停在 `IDLE`，除非有人真的请求过一次，所以给它一个真的
        #: 协调器不会让任何测试意外拉起子进程。
        self._backfill = backfill or BackfillCoordinator(clock=clock)
        self._backfill_counts = backfill_counts
        #: 每按一次「开始」记一条当时的配置。默认是只留在内存里的那种——
        #: 往仓库里写文件必须是组装点（`web.app.create_persistent_app`）明确
        #: 决定的事，不能由一个默认值替测试和假服务做主。
        self._freezes = freeze_log or MissionFreezeLog()
        #: 主星。默认值来自 `domain.missions`，真正的取值由建这个对象的那一层
        #: （`web.app`）从 Settings 解析后注入——`domain` 不许 import `config`，
        #: 否则纯领域层就绑死在配置上。
        #:
        #: 页面回显的范围也读这里（`web.persistent_service`），不另读一次默认值：
        #: 两边各读一次的话，配了 `EVO_HELPER_ORIGIN` 之后页面显示旧主星、
        #: 舰队却从新主星出发，而用户看着「没问题」。
        self._origin = origin
        #: 「该等还是该收」只能有一份实现，所以复用 runner 那一套 planner，
        #: 不在这里另写一遍 SQL 判据。
        self._planner = planner or ReportWaitPlanner()
        self._enabled = False
        self._started_at_utc: datetime | None = None
        #: 开机时认出的孤儿进程号。只显示，不据此杀进程。用户点了「强制结束」
        #: 就清掉——那一下的含义是「我知道了，别再提醒我」。
        self._orphan_pid: int | None = None
        self._run_id: UUID | None = None
        #: 每个**任务**上一次异常退出的时刻，喂给 `domain.scheduler.cooling_down`。
        #: **只记在内存里**：它的用途是压住本次运行里的重启 churn，控制台重启就
        #: 该忘掉；真正跨进程的那份记忆是 `mission_tasks.consecutive_failures`。
        #:
        #: 键从 `MissionKind` 换成 `task_id`：按链路记的话，两个 bot 任务共用一份
        #: 冷却，一个崩了会把另一个也压住五分钟。
        self._last_failure_at: dict[int, datetime] = {}
        #: 每个任务上一次**真的算故障**的退出时刻，喂给
        #: `domain.scheduler.tasks_failing_together`。
        #:
        #: ⚠️ 和上面那份**必须分开**：上面那份连 `EXIT_ENVIRONMENT_BUSY` 也记
        #: （它要吃冷却），而拿「用户正在用别的窗口」去佐证另一个任务真正的崩溃，
        #: 等于把最常见的一档正常情况变成万能豁免。
        self._last_fault_at: dict[int, datetime] = {}
        #: 每个任务连着吃了几次「环境故障」豁免，上限 `MAX_ENVIRONMENT_EXEMPTIONS`。
        #: 任何一个任务跑出退出码 0 就整个清空。
        self._exemptions: dict[int, int] = {}
        #: 每个**配了定时窗口的**任务上一 tick 的窗口判定（True = 在窗口里）。
        #: 只为「到点开 / 到点关各写一条 `system_log`」而存在。
        #:
        #: **只记在内存里**，理由和上面那两份一样：真正的判据是每 tick 现算的
        #: （`domain.scheduler.within_schedule_window`），这里记的只是「上一次
        #: 我说的是什么」，好让日志只在**变化**时写一条而不是每秒刷一条。
        #: 控制台重启后它是空的，于是重复写一条——那是可以接受的代价
        #: （用户口径 2026-08-17），换来的是判定本身不依赖任何内存状态。
        self._schedule_window_open: dict[int, bool] = {}
        #: 「跑着不动」的看门狗。**惰性建**：组装点
        #: （`web.app.create_persistent_app`）只往这里传 repository，所以默认那
        #: 一个要自己从 repository 摸出 session 工厂，而摸这一下必须等到真的要用
        #: ——有测试拿 `None` 当 repository，只为验参数换算。
        self._progress = progress
        self._watchdog_instance = watchdog
        #: tick 跑在后台线程里，而页面的「开始 / 结束」来自请求线程。没有这把锁，
        #: 一次「结束」可能正好落在 tick 的「起进程」中间——supervisor 停掉的是
        #: 上一个，紧接着 tick 又起了一个新的，于是控制台以为已经停了，实际还有
        #: 一个 runner 在点鼠标。这直接违反「任何时刻最多一个子进程」。
        #:
        #: ⚠️ **它只护「起停」这几行，绝不能护到查库上去。** 查库要多久没有上界：
        #: 一次 `_facts()` 会按 bot 目标逐个问库，生产库里那个范围有 4237 个目标，
        #: 实测一次 0.32 秒；而 tick 每秒一次、页面每 2 秒问一次状态、桌面悬浮窗
        #: 还有一次。这些活儿一旦压在同一把锁上，用户点「结束」就得排在它们后面
        #: ——`RLock` 没有公平性，排在一群反复重取的线程后面可以饿任意久；而且
        #: FastAPI 的同步接口跑在容量 40 的线程池里，轮询全卡在锁上之后，那个
        #: POST 连线程都分不到，于是页面上「点了结束毫无反应、秒表照走」。
        #: 实机 2026-08-11 就是这样：调度器显示已运行 2:29:08，点「结束」没反应。
        #:
        #: 可重入锁：`stop()` 与 `tick()` 内部都会再调 `_finish()`。
        self._lock = threading.RLock()
        # Web 层短缓存用它辨认后台 tick 已经更新状态。它是纯内存值，控制台重启
        # 时会随调度器对象一同重置。
        self._view_generation = 0
        #: 军力榜正在为哪个 bot 攻击任务采一批目标。榜单一旦开始采这一批，
        #: 不能在写入前几行后就被新出现的 bot 候选抢占；采够 ``top_n`` 后反过来
        #: 也必须先启动该任务，再交还普通优先级排序。
        self._military_ranking_batch_task_id: int | None = None
        # 点「开始」后军力任务只使用这一份档位；运行中修改全局配置不会让
        # 固化记录与实际派遣分家。停掉后才允许下一轮取新配置。
        self._active_military_tiers_json: str | None = None

    # -- 对外 ------------------------------------------------------------------

    @property
    def origin(self) -> Coordinate:
        """本次运行认定的主星。页面回显必须读这个，而不是再读一次默认值。"""
        return self._origin

    def now_utc(self) -> datetime:
        """调度器认的「现在」。

        写库的时刻要和判据用的「现在」同源：调用方各取一次 `datetime.now()` 的话，
        测试里注入的假时钟就只管住一半，而两个时钟差一点就足以让刚建好的一轮把
        边界上那条战报算成上一轮的。
        """
        return self._clock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> RunningChild | None:
        return self._supervisor.running

    @property
    def config_locked(self) -> bool:
        """任务配置现在改不改得动。开着 = 锁着。

        **为什么锁**：`_step()` 每秒重新去库里读一遍配置，所以运行中改参数会
        立刻生效到下一轮，而上一轮正拿着旧参数在飞。一轮之内两套口径，事后
        从台账里分不出当时用的是哪一套。用户口径就是「开始后无法修改，只有
        结束状态才可以修改」。

        **为什么第二个条件**：`stop()` 是同步的（`terminate()` 之后
        `wait(TERMINATE_TIMEOUT_S)`），返回时 `supervisor.running` 已经是
        None，所以正常路径上这一条恒为假、不会多锁哪怕一毫秒。留着它是因为
        「结束之后子进程还在收尾」这个问题的答案不该藏在别的模块的实现细节
        里：哪天 `stop()` 改成异步收尾，锁会自己跟着延到子进程真的走完，而
        不是静默地在收尾途中放行一次改参数。

        `disabled_reason` 那一路不受这里约束——它不是配置，见
        `web.persistent_service.MissionConsoleService.patch_mission`。
        """
        return self._enabled or self._supervisor.running is not None

    @property
    def view_generation(self) -> int:
        """后台调度状态的内存版本，供只读快照缓存快速判失效。"""
        with self._lock:
            return self._view_generation

    def config_freezes(self) -> tuple[MissionConfigFreeze, ...]:
        """历次「开始」固化下来的配置，旧的在前。页面上那张历史表读它。"""
        return self._freezes.records()

    @property
    def freeze_log_path(self) -> Path | None:
        """固化记录落在磁盘上的什么地方。只留在内存里时为 None。"""
        return self._freezes.path

    def prepare(self) -> int:
        """开机：补齐三行任务与单行配置，标出孤儿，返回孤儿条数。

        孤儿是上次没走正常关闭路径留下的行。**只标不杀**——pid 会被系统回收
        复用，照着一个可能已经换了主人的号码开枪比留个警告更糟。

        pid 要在标记**之前**读：标完那些行就闭合了，事后再也认不出是哪一条。
        """
        with self._lock:
            now = self._clock()
            self._repository.ensure_mission_rows(now_utc=now)
            self._orphan_pid = next(
                (row.pid for row in self._repository.open_mission_runs() if row.pid is not None),
                None,
            )
            return self._repository.mark_orphan_mission_runs(ended_at_utc=now)

    def start(self, *, reconcile: bool = False) -> None:
        """用户点「开始」。顺手把这一刻的三条链路配置固化成一条记录。

        **先对账，再放行任务。** 用户口径（2026-08-13）：「启动调度台之后，
        先检查有多少应读未读战报 → 读完所有应读未读战报 → …… → 继续执行任务，
        但是已攻击的海盗/BOT 不再重复侦查/攻击」。所以点「开始」会先排一批补录
        （海盗一趟、bot 一趟——两条链路的信箱主题不同，一趟只读得了一种），
        走的是和手动补录**同一套闸门**（`_act` 里那一句），不是第二套机制。
        为什么这个顺序是硬要求，见 `application.backfill` 的模块头。

        那趟不怕慢：信箱单子一空就早停，没有欠账时几十秒走完。

        ⚠️ **这里的默认值是「不对账」，而用户那一侧的默认是「对账」**
        （`web.persistent_service.MissionConsoleService.start_scheduler` 与
        `web.schemas.SchedulerStartIn.reconcile`，页面上那个复选框默认勾着）。
        两个默认值反着来是**故意的**，同 `freeze_log` 那一条：`reconcile=True`
        会真的 `Popen` 一个去点鼠标翻信箱的子进程，而「起一个真的子进程」必须是
        组装点明确决定的事，不能由一个默认值替所有调用方做主。默认成 True 的
        代价是具体的：一大批只关心调度循环的测试会在 CI 上真的拉起补录进程。
        用户意图（「点开始要不要先对账」）本来也属于有用户的那一层。

        固化只发生在**停 → 开**这一次跃迁上。连点两下「开始」不该记两条——
        第二下什么都没变，记下来只会让历史表里多一条「与上一次相同」，把真正
        改过的那几条淹掉；秒表同理，不按回零。对账也只排一批：`request_batch`
        对已经排着的链路直接跳过。

        **查库在锁外**（同 `snapshot()`）：`mission_tasks()` 只有三行、比
        `_facts()` 轻得多，但把任何一次查库压进这把锁都是在给「结束」排队，
        而那正是上一轮修复刚拆开的东西。锁里只剩几个字段的赋值。
        """
        # 抄配置和按下秒表用的是同一个时刻：两次取「现在」的话，记录上的固化
        # 时刻会和页面上那块秒表的起点差一点，而事后翻账正是拿这两个对时间线。
        tasks = self._repository.mission_tasks()
        config = self._repository.scheduler_config()
        military_tiers_json = self._repository.military_attack_config().tiers_json
        rematched = self._repository.rematch_unlinked_reports()
        if rematched:
            _LOGGER.info("启动调度前补认 %s 份既有战报，攻击日志已同步战果", rematched)
        now = self._clock()
        with self._lock:
            if self._enabled:
                return
            self._started_at_utc = now
            self._enabled = True
            self._active_military_tiers_json = military_tiers_json
            freeze = freeze_now(
                [
                    FrozenTask(
                        kind=MissionKind(row.kind),
                        enabled=row.enabled,
                        priority=row.priority,
                        params_json=row.params_json,
                        task_id=row.id,
                        name=row.name,
                        # 存**解析后**的出发星球，不是 `origin_*` 那三列原样。
                        # 记录要回答的是「那一轮舰队从哪出发」，而 NULL 的答案是
                        # 「当时的全局主星」——原样存 NULL，改了
                        # `EVO_HELPER_ORIGIN` 之后旧记录会跟着一起改口。
                        origin=str(self._origin_of(row)),
                        fleet_lines=self._fleet_lines_of(row, config),
                    )
                    for row in tasks
                    if _known(row.kind)
                ],
                frozen_at_utc=now,
                military_tiers_json=military_tiers_json,
            )
        # 落账在锁外：写文件的耗时没有上界（磁盘、杀毒软件），而它对
        # 「任何时刻最多一个子进程」这条不变量毫无影响。
        self._freezes.append(freeze)
        # 「开始」这一下本身就是「放任务出来」的意思，所以它顺带确认掉上一批
        # 补录的摘要。不确认的话，手动补完、看完、直接点「开始」的用户会撞上一
        # 台开着却一个任务都不起的调度器，而页面上唯一的解释是另一个按钮。
        self._backfill.acknowledge()
        if reconcile:
            self._backfill.request_batch(
                [
                    BackfillRequest(kind=kind, since=default_since(now), reason=REASON_STARTUP)
                    for kind in BACKFILL_KINDS
                ]
            )
            self._advance_backfill()

    def stop(self) -> None:
        """用户点「结束」。立刻杀，不等它跑完手上这一个。

        **不动补录。** 它不是一条链路，也不由这个开关管：正在补录时点「结束」
        的含义是「补完之后别再起任务了」，而不是「把补录也掐了」。要掐补录有
        它自己的「取消」按钮，以及红条上的「强制结束」（那一下的口径是全停）。
        """
        with self._lock:
            self._enabled = False
            self._started_at_utc = None
            self._active_military_tiers_json = None
            self._finish(self._supervisor.stop(StopReason.USER))

    def shutdown(self) -> None:
        """控制台关闭时清场，覆盖「正常重启」这条最常见的路径。

        **补录也要一起收掉。** 不收的话，控制台关了，一个还在翻信箱点鼠标的
        补录进程留在后台——和 `supervisor.stop()` 挡的是同一件事，只是它归另一个
        进程管理器管。
        """
        with self._lock:
            self._enabled = False
            self._started_at_utc = None
            self._active_military_tiers_json = None
            self._finish(self._supervisor.stop(StopReason.SHUTDOWN))
            self._backfill.cancel(self._measure_backfill)

    def force_kill(self) -> None:
        """页面顶部那条红条上的「强制结束」。

        只做两件事：**停掉我们自己手上的那个子进程**，**把台账里还没闭合的行
        闭合掉**。绝不按 pid 去杀一个不认识的进程——pid 会被系统回收复用，
        那一枪可能打在别人身上。

        它顺带把调度器停掉（走 `stop()`）：只杀不停的话，下一个 tick 立刻又起
        一个新的，按钮看上去毫无作用。「强制结束」的用户口径是全停——**补录也
        算在「全」里面**，它同样是一个在点鼠标的子进程。
        """
        with self._lock:
            self.stop()
            self._backfill.cancel(self._measure_backfill)
            self._repository.mark_orphan_mission_runs(ended_at_utc=self._clock())
            self._orphan_pid = None

    def snapshot(self) -> SchedulerSnapshot:
        """当前的完整现状。页面每几秒问一次，桌面悬浮窗也问同一个。

        走的是和 `tick()` 同一套 `_facts`，所以页面上看到的判据依据与调度器
        下一步据以行动的是同一份事实。

        **查库在锁外。** 每 2 秒一次的状态轮询没有任何理由把用户的「结束」堵在
        后面；bot 阶段由仓储批量读取，锁里只剩几个字段的读取。
        """
        tasks = self._repository.mission_tasks()
        config = self._repository.scheduler_config()
        snapshots = self._snapshots(tasks, config)
        facts = self._facts(snapshots, config, self._clock())
        with self._lock:
            return SchedulerSnapshot(
                enabled=self._enabled,
                started_at_utc=self._started_at_utc,
                running=self._supervisor.running,
                orphan_pid=self._orphan_pid,
                tasks=tuple(tasks),
                snapshots=snapshots,
                config=config,
                facts=facts,
                config_locked=self.config_locked,
                frozen_config=self._freezes.latest() if self._enabled else None,
            )

    def begin_bot_round(self, task_id: int) -> None:
        """页面上的「重开一轮」：把这个任务的 `round_started_at_utc` 推到当前。

        走调度器的时钟而不是调用方自己取一个 `now()`：本轮的起点和判定完成度
        时用的「现在」必须同源，否则两个时钟差一点，刚开的一轮就可能把边界上
        那条战报算成本轮的。
        """
        with self._lock:
            self._repository.begin_bot_round(task_id, now_utc=self._clock())

    def command_for(self, kind: MissionKind, params_json: str, *, origin: Coordinate) -> list[str]:
        """把一份参数换算成命令行，换不出来就抛 `MissionParamError`。

        对外开放是为了让 API 能在**写库之前**用调度器自己的那把尺子量一遍：
        范围内一个 bot 都没有、半径 ≤ 0、系号区间首尾颠倒、出发星球还切不过去，
        这些配置存下来只会让调度器起一个必然空转的 runner，或者干脆在启动时把
        任务自动停用——两种都要等用户下次看页面才发现。校验必须和启动走同一段
        代码，否则「页面收下了、调度器起不来」这种分歧迟早出现。
        """
        return self._command_for(kind, params_json, origin)

    def validate_military_params(self, params_json: str) -> None:
        """只校验军力方案本身，不伪造一颗 origin 去组命令行。

        多出发点由任务表配置，页面保存军力参数时它们可能正好还没一并落库；此时
        调 ``command_for`` 会错误地走旧的区域攻击参数校验。这里与真正派遣共用
        同一套解析器，专门给保存前校验使用。
        """
        params = _params(params_json)
        if not _bot_by_military(params_json):
            return
        if _bot_top_n(params_json) < 1:
            raise MissionParamError("top_n 必须至少为 1")
        maximum = _bot_max_score(params_json)
        if maximum is not None and maximum < 0:
            raise MissionParamError("max_score 不能小于 0")
        _bot_rescan_after_hours(params)

    def validate_military_tiers(self, tiers: list[dict[str, Any]]) -> tuple[MilitaryTier, ...]:
        """校验全局攻击档位；任务参数不再携带档位。"""
        return _bot_tiers({"tiers": tiers})

    def tick(self) -> None:
        """每秒一次。收退出码、看判据、该起就起。

        收退出码不能只在页面轮询时做——没人开着页面时，那条记录会一直挂在
        「运行中」，而连续失败也就永远数不到三。

        **读事实那一段在锁外**（见 `_lock` 上的注释）：它没有上界，而「结束」
        必须能立刻插进来。

        补录那两句在 `if not self._enabled` **上面**：补录不归调度器的开关管，
        用户完全可以在调度器停着的时候点一次补录，而那时也得有人去起它、去收
        它的退出码。
        """
        try:
            with self._lock:
                self._finish(self._supervisor.poll())
            # 锁外：收到退出码那一次要量两个 `COUNT(*)` 外加批量 bot 阶段查询。
            self._backfill.poll(self._measure_backfill)
            self._advance_backfill()
            if not self._enabled:
                return
            self._cut_off_a_stalled_round()
            # 一个任务因参数不合格被就地停用后要能立刻让位给下一个，否则这一秒
            # 谁都不跑。上限取任务条数：每转一圈至少停用一个，不可能无限转。
            for _ in range(len(MissionKind)):
                if not self._step():
                    return
        finally:
            # `return` 分支也要前进版本：runner 或补录刚结束时，TTL 内的下一次
            # 读取不能复用它开始前那份快照。
            with self._lock:
                self._view_generation += 1

    # -- 手动战报补录 ----------------------------------------------------------
    #
    # 补录**优先于所有任务**，理由在 `application.backfill` 的模块头上。这一节
    # 只做「动手」那一半：判据（能不能起、扣不扣着窗口）全在协调器那边。

    def backfill_state(self) -> BackfillState:
        return self._backfill.state()

    def backfill_log_tail(self, lines: int) -> str:
        return self._backfill.log_tail(lines)

    def request_backfill(self, request: BackfillRequest) -> BackfillState:
        """用户点了「开始补录」。

        请求落下之后**立刻推一格**，不等下一个 tick：窗口空着时用户按下按钮
        就该看见「补录中」，正在跑扫描时那一下就该把扫描抢占掉。差的那一秒
        本身无所谓，但「点了之后页面上什么都没变」会让人再点一次。
        """
        self._backfill.request(request)
        self._advance_backfill()
        return self._backfill.state()

    def cancel_backfill(self) -> BackfillState:
        """排队中就撤销，跑着就杀掉。取消之后立刻放行。"""
        return self._backfill.cancel(self._measure_backfill)

    def acknowledge_backfill(self) -> BackfillState:
        """用户看过摘要，点了「继续任务」。**这一下才放行。**"""
        return self._backfill.acknowledge()

    def _advance_backfill(self) -> None:
        """把补录往前推一格：抢占扫描 / 等海盗跑完 / 窗口空了就起。

        三条分支对应用户口径里的三段：

        - 正在跑**扫描** → 立刻抢占。扫描的游标持久化，随时可断，`decide()` 里
          那条「只有扫描会被抢占」用的也是同一个理由。
        - 正在跑**海盗 / bot** → 什么都不做，等它自己跑完。**绝不硬杀**：
          它们可能正卡在「点了出发」和「把这一发记进库」之间，硬杀会留下一发
          飞出去了却没记账的舰队，而那正是战报永远配不上的成因。
        - 窗口空着 → 量一次底数，起补录。

        **量底数在锁外**（同 `_facts`、`snapshot`、`_cut_off_a_stalled_round`）：
        它要跑两个 `COUNT(*)` 外加逐个 bot 目标问库，压进 `_lock` 就是给用户的
        「结束」排队。进锁之后重新确认一遍——不是就作废，照着一份过期的快照去
        抢占，杀掉的可能是下一轮刚起来的那个。

        ⚠️ 进锁前那两行**只是省钱，不是判据**：判据是锁里那一份（照着锁外读到的
        状态动手，等于凭一份可能已经过期的快照去杀子进程）。省的是量底数那一下
        ——海盗那一轮能跑半小时，而 tick 每秒一次，不省就是每秒白付一次逐个 bot
        目标问库。改坏这两行只会变慢，不会变错；真正的护栏在下面。
        """
        if not self._backfill.pending:
            return
        running = self._supervisor.running
        if running is not None and running.kind is not MissionKind.SCAN:
            return
        before = self._measure_backfill()
        with self._lock:
            if not self._backfill.pending:
                return
            running = self._supervisor.running
            if running is not None:
                if running.kind is not MissionKind.SCAN:
                    return
                self._finish(self._supervisor.stop(StopReason.PREEMPTED))
            self._backfill.launch_if_pending(before)

    def _measure_backfill(self) -> BackfillMeasurement:
        """补录前后各量一次的那份底数。**只读。**

        「新入库几份战报」「认领上几发派遣」两个数来自 `battle_reports`；
        「哪几个 bot 目标的态变了」只能逐个目标问库，那正是任务自己判「还要不要
        再打一遍」用的同一段判据（`_bot_remaining` 也这么问），所以摘要里那个数
        和调度器下一步的行为出自同一份事实。
        """
        reports, claimed = self._backfill_reader.read()
        return BackfillMeasurement(reports=reports, claimed=claimed, bot_phases=self._bot_phases())

    @property
    def _backfill_reader(self) -> BackfillCounts:
        """战报计数器，第一次真要用时才建（同 `_watchdog` 那一份，理由一样）。"""
        if self._backfill_counts is None:
            self._backfill_counts = SqlAlchemyBackfillCounts(self._repository._session_factory)  # noqa: SLF001
        return self._backfill_counts

    def _bot_phases(self) -> dict[tuple[int, str], str]:
        """每个参与调度的 bot 任务、本轮范围内每个目标此刻的态。

        **只量参与调度的那些**：没勾或已停用的任务不会因为补录而动起来，为它们
        逐个目标问一遍库只是白付钱（这段在 tick 线程之外，但 bot 范围里有四千
        多个目标）。参数填错的任务同样跳过——它此刻连命令行都换算不出来。
        """
        phases: dict[tuple[int, str], str] = {}
        targets: list[Coordinate] | None = None
        now = self._clock()
        for row in self._repository.mission_tasks():
            if row.kind != MissionKind.BOT.value or not row.enabled:
                continue
            if row.disabled_reason is not None:
                continue
            if targets is None:
                targets = self._bot_targets()
            try:
                in_range = self._bot_selection(row.params_json, self._origin_of(row))
            except MissionParamError:
                continue
            facts_by_target = self._repository.bot_dispatch_facts_many(
                in_range, since=row.round_started_at_utc, now_utc=now
            )
            for target in in_range:
                phases[(row.id, str(target))] = phase_of(facts_by_target[target]).name
        return phases

    # -- 跑着不动 --------------------------------------------------------------

    @property
    def _watchdog(self) -> StallWatchdog:
        """看门狗，第一次真要用时才建。

        默认那一份借 repository 的 session 工厂：那四个 `COUNT(*)` 是只读的，
        而 `storage/repository.py` 这一轮由别人在改，加不了公开的只读入口。
        下一轮该在 `SqlAlchemyRepository` 上开一个 `progress_counts()`，
        把这一行收掉。
        """
        if self._watchdog_instance is None:
            self._watchdog_instance = StallWatchdog(
                self._progress or SqlAlchemyMissionProgress(self._repository._session_factory)  # noqa: SLF001
            )
        return self._watchdog_instance

    def _cut_off_a_stalled_round(self) -> None:
        """一轮跑着却一件事都没做成，到阈值就掐掉。

        **调度器原本只知道子进程还活着，不知道它已经不干活了。** 实机
        2026-08-12 05:14–06:46：六次心跳、七个计数一个没变，状态一直是「运行中」，
        白丢一个半小时。判据（什么算「进展」、阈值为什么是这个数）全在
        `application.mission_progress`，这里只负责按它的结论动手。

        **查库在锁外**（同 `_facts` 与 `snapshot`）：看门狗每 30 秒去数四张表，
        把它压进 `_lock` 就是给用户的「结束」排队。进锁之后必须重新确认
        「在跑的还是刚才那个」——不是就作废，照着一份过期的快照去杀子进程，
        杀掉的可能是下一轮刚起来的那个。
        """
        running = self._supervisor.running
        now = self._clock()
        idle = self._watchdog.check(running, now)
        if idle is None or running is None:
            return
        with self._lock:
            current = self._supervisor.running
            if current is None or current.started_at_utc != running.started_at_utc:
                return
            _LOGGER.warning(
                "%s 这一轮已经 %.0f 分钟没有任何进展（没有新的派遣、战报、"
                "侦察报告或坐标扫描）；判死并收掉",
                running.kind.value,
                idle.total_seconds() / 60,
            )
            self._finish(self._supervisor.stop(StopReason.STALLED))

    # -- 一次决策 --------------------------------------------------------------

    def _step(self) -> bool:
        """走一遍「读事实 → 判 → 起」。返回 True 表示刚停用了谁，值得再算一次。"""
        now = self._clock()
        tasks = self._repository.mission_tasks()
        config = self._repository.scheduler_config()
        snapshots = self._snapshots(tasks, config)
        self._log_schedule_window_changes(snapshots, now)
        facts = self._facts(snapshots, config, now)
        running = self._supervisor.running
        batch_decision = self._military_batch_decision(snapshots, facts, running)
        if batch_decision is not None:
            if batch_decision.action is Action.IDLE:
                return False
            return self._act(batch_decision, facts)
        decision = decide(
            snapshots,
            facts,
            running=(
                None
                if running is None
                else RunningProcess(
                    task_id=running.task_id,
                    kind=running.kind,
                    started_at_utc=running.started_at_utc,
                )
            ),
            min_dwell=timedelta(seconds=config.min_dwell_seconds),
            restart_cooldown=timedelta(seconds=config.restart_cooldown_seconds),
        )
        if decision.action is Action.IDLE or decision.task is None:
            return False
        return self._act(decision, facts)

    def _log_schedule_window_changes(
        self, snapshots: Sequence[TaskSnapshot], now: datetime
    ) -> None:
        """定时窗口开合的那一刻各写一条 `system_log`。

        **只在判定发生变化时写一条**，不是每 tick 刷一条：tick 每秒一次，刷起来
        一晚上就是几万行，真正要看的那两条会被淹掉。

        ⚠️ 这里**只写日志，不写库**。到点开、到点关都不去碰 `mission_tasks.enabled`
        ——那一列是用户的意志，定时器改它会造成「我手动开的被悄悄关掉」，而且事后
        分不清是谁关的（见 `domain.scheduler.within_schedule_window`）。所以这个
        方法整个是只读的，删掉它不会改变调度器的任何一个决定。

        没配窗口的任务一条都不记：它们永远在窗口里，记了只是给每次重启多刷几行。
        任务被删掉、或者窗口被清空时把记忆一起丢掉，否则重新配上窗口的那一下
        会被当成「没变过」而漏掉一条。

        **本次运行第一次看到某个任务时也记一条**，措辞与「到点开 / 到点关」分开
        （`_window_message`）。这一条不是变化，是现状——控制台重启之后翻日志的人
        需要知道「这一轮开始的时候它是开还是关」，否则窗口早在上次运行里就关掉的
        任务在新一轮日志里一个字都没有，看起来又成了「不动而不说原因」。
        """
        windowed = {
            task.task_id: task
            for task in snapshots
            if task.enabled_from_utc is not None or task.enabled_until_utc is not None
        }
        for task_id in [known for known in self._schedule_window_open if known not in windowed]:
            del self._schedule_window_open[task_id]
        for task_id, task in windowed.items():
            open_now = within_schedule_window(task, now)
            previous = self._schedule_window_open.get(task_id)
            if previous == open_now:
                continue
            self._schedule_window_open[task_id] = open_now
            record_system_log(
                "INFO",
                "application.mission_scheduler",
                _window_message(task, open_now=open_now, first_look=previous is None),
                payload={
                    "task_id": task_id,
                    "mission_kind": task.kind.value,
                    "window_open": open_now,
                    "first_look": previous is None,
                    "enabled_from_utc": (
                        None if task.enabled_from_utc is None else task.enabled_from_utc.isoformat()
                    ),
                    "enabled_until_utc": (
                        None
                        if task.enabled_until_utc is None
                        else task.enabled_until_utc.isoformat()
                    ),
                },
                logged_at_utc=now,
            )

    def _act(self, decision: Decision, facts: SchedulerFacts) -> bool:
        """把决策落地，返回「值得再算一次吗」。**只有这里动子进程，所以只有这里要锁。**

        上面那段读事实是在锁外跑的，因此进锁之后必须重新问两个问题——它们正是
        「任何时刻最多一个子进程」这条不变量的守卫：

        - **用户在这期间点了「结束」吗？** 点了就作废这一轮。少了这一句，
          `stop()` 杀掉的是上一个，紧接着这里又起一个新的，控制台以为已经停了，
          实际还有一个 runner 在点鼠标。
        - **在跑的那个还是决策时看到的那个吗？** 不是就作废，等下一 tick 拿新
          事实重算——照着过期的决策抢占或启动，等于凭一份旧快照动真鼠标。

        作废一律返回「不必再算」：再算一遍读的还是同一份库，只是白付一次
        `_facts()` 的钱。只有「刚把某条链路就地停用」才值得重算，那时次序真的
        变了，顺位该立刻让给下一条。
        """
        if decision.task is None:
            return False
        with self._lock:
            if not self._enabled:
                return False
            # **补录扣着窗口时一个任务都不起。** 这是「完成补录才会继续任务」
            # 那句用户口径的唯一落点，理由见 `application.backfill` 的模块头：
            # 补录改的正是任务读来做决策的那批数据，抢在它前面跑等于拿一份已知
            # 不完整的数据决定要不要再打一遍——那会白送一支舰队出去。
            #
            # 闸门必须在抢占**之前**：放在 `_launch` 里的话，`Action.PREEMPT`
            # 会先把正在跑的扫描杀掉，然后才发现这一轮根本起不来，等于白掐一轮。
            #
            # 返回 False（不必再算）而不是 True：这一下没有停用任何任务，次序
            # 一个字都没变，重算只是白付一次 `_facts()`——而补录要跑十几分钟，
            # 那就是十几分钟每秒三次的空转。
            if self._backfill.blocking:
                return False
            running = self._supervisor.running
            if decision.action is Action.PREEMPT:
                if running is None or running.kind is not MissionKind.SCAN:
                    return False
                # 只有扫描会被抢占（判据保证），它的游标持久化，随时可断。
                self._finish(self._supervisor.stop(StopReason.PREEMPTED))
            elif running is not None:
                return False
            return not self._launch(decision.task, facts.of(decision.task))

    def _launch(self, task: TaskSnapshot, facts: TaskFacts) -> bool:
        """组命令行、起进程、记账。参数不合格则停用该任务并返回 False。

        调用方必须已经持有 `_lock`：这里会真的拉起一个去点鼠标的子进程。

        `MissionParamError` 必须在这里被接住：让它冒出去就是整个调度循环停摆，
        而它表达的只是「这个任务的配置填错了」——别的任务没有理由跟着停。
        """
        row = self._repository.mission_task(task.task_id)
        if row is None:
            # 决策与这一刻之间用户把这个任务删了。作废本轮，等下一 tick 拿新事实
            # 重算——照着一份指向已删任务的决策去起子进程，起出来的是一轮没有账
            # 可记的派遣。
            return False
        try:
            batch_task = self._military_batch_task() if task.kind is MissionKind.RANKING else None
            if task.kind is MissionKind.RANKING:
                # 两个上限，取**小**的那个：任务上配的「扫描数量」是用户给这条
                # 链路划的天花板（留空 = 不划），军力批次要的 `top_n` 是「这一批
                # 攻击需要多少个目标」。取大的会越过用户划的线，取任务那个又会
                # 让批次采不满——`min` 是唯一同时守得住两条的。
                command = ranking_command(
                    bot_limit=_smallest_limit(
                        _ranking_bot_limit(row.params_json),
                        None if batch_task is None else _bot_top_n(batch_task.params_json),
                    )
                )
            elif task.kind is MissionKind.BOT and _bot_by_military(row.params_json):
                command = self._military_command(row, max_dispatches=facts.free_lines)
            elif task.kind is MissionKind.BOT:
                command = self._bot_command(
                    row.params_json,
                    task.origin,
                    max_dispatches=facts.free_lines,
                )
            else:
                command = self._command_for(task.kind, row.params_json, task.origin)
        except MissionParamError as exc:
            self._repository.disable_mission_task(task.task_id, str(exc))
            return False
        # 本轮的 id 要在**起子进程之前**定下来：runner 靠环境变量认领它，
        # 好把自己写进 `system_log` 的每一行都挂到这一轮上。起完再生成就晚了，
        # 那台机器上的日志会全部落成「不属于任何一轮」。
        run_id = uuid4()
        with child_environment(run_id=run_id, task_id=task.task_id, mission_kind=task.kind.value):
            child = self._supervisor.start(task.kind, command, task_id=task.task_id, name=task.name)
        self._run_id = self._repository.begin_mission_run(
            task.kind,
            task_id=task.task_id,
            command=command,
            pid=child.pid,
            started_at_utc=child.started_at_utc,
            log_path=str(child.log_path),
            run_id=run_id,
        )
        if task.kind is MissionKind.RANKING:
            self._military_ranking_batch_task_id = None if batch_task is None else batch_task.id
        elif task.kind is MissionKind.BOT and task.task_id == self._military_ranking_batch_task_id:
            # 这一批已经真正交给带 --attack 的 runner，后续排程恢复常规优先级。
            self._military_ranking_batch_task_id = None
        return True

    def _finish(self, exited: MissionExit | None) -> None:
        """一个子进程结束了：回填 `mission_runs`，并更新连续失败计数。"""
        if exited is None:
            return
        run_id, self._run_id = self._run_id, None
        if run_id is not None:
            self._repository.finish_mission_run(
                run_id,
                ended_at_utc=exited.ended_at_utc,
                exit_code=exited.exit_code,
                stopped_by=exited.stopped_by.value,
            )
        if (
            exited.kind is MissionKind.RANKING
            and self._military_ranking_batch_task_id is not None
            and (exited.stopped_by is not StopReason.SELF or exited.exit_code != 0)
        ):
            # 没采满就失败/被用户停止的榜单不能假装是一批可攻击目标。
            self._military_ranking_batch_task_id = None
        if exited.stopped_by is StopReason.SELF and exited.exit_code == 0:
            # 跑完一轮。「连续」是连续，成功过一次就重新数。
            self._last_failure_at.pop(exited.task_id, None)
            self._last_fault_at.pop(exited.task_id, None)
            # 这一刻环境被证明是好的：窗口在、会话在、鼠标是我们的。之前那几次
            # 「多条一起倒」的豁免因此各自成立，不该再占着谁的额度。
            self._exemptions.clear()
            self._repository.clear_mission_failures(exited.task_id)
            return
        if exited.stopped_by not in (StopReason.SELF, StopReason.STALLED):
            # 抢占、用户点停、控制台关闭：我们自己动的手，两个计数都不动。
            # `STALLED` 手也是我们动的，但毛病是这条链路自己的，所以它不在这里。
            return
        # 自己退且退出码非 0，或者跑着不动被掐掉。
        # **冷却与「算不算故障」是两件事，分开记。**
        # 冷却按「起来就没好好跑完」算，`EXIT_ENVIRONMENT_BUSY` 那一档也要吃：
        # 用户正在用别的窗口，14 秒后再起一次同样抢不到前台，纯 churn。
        self._last_failure_at[exited.task_id] = exited.ended_at_utc
        if not exited.failed:
            return
        self._last_fault_at[exited.task_id] = exited.ended_at_utc
        if self._excused_as_an_environment_fault(exited):
            return
        self._repository.record_mission_failure(
            exited.task_id, exit_code=exited.exit_code, limit=MAX_CONSECUTIVE_FAILURES
        )

    def _excused_as_an_environment_fault(self, exited: MissionExit) -> bool:
        """这次失败要不要免记——免，如果同一时间窗里别的链路也在倒。

        三条链路共用一个游戏窗口、一个鼠标、一份连接和一台机器。它们同时坏掉
        几乎必然是那些共用的东西坏了，而不是三处互不相干的代码一起长出 bug。
        判据本身在 `domain.scheduler.looks_like_an_environment_fault`，
        为什么这么判、怎么和「三条恰好各自坏了」分开，都写在那里。

        免记时**把同一阵里所有链路的计数一起清零**：那些数字同样是记错了账。
        清的是 `consecutive_failures`，不动 `disabled_reason`——已经被自动停用的
        那条要不要放出来，得先能分清「连续失败停用」和「参数不合格停用」，
        而那个区分住在 `storage/repository.py` 里，本轮不动那个文件。

        豁免有上限（`MAX_ENVIRONMENT_EXEMPTIONS`），用尽就退回正常计数：
        没有上限的话，两条各自高频复发的真故障会一直互相佐证，永远停不掉。
        """
        if not looks_like_an_environment_fault(
            exited.task_id, exited.ended_at_utc, self._last_fault_at
        ):
            return False
        # 判据只问一次，这里只是再问一遍「同一阵里都有谁」，好知道该清谁的计数。
        together = tasks_failing_together(exited.task_id, exited.ended_at_utc, self._last_fault_at)
        used = self._exemptions.get(exited.task_id, 0)
        names = "、".join(str(task_id) for task_id in sorted(together))
        if used >= MAX_ENVIRONMENT_EXEMPTIONS:
            _LOGGER.warning(
                "任务 %d（%s）与任务 %s 又一起失败，但它已经连着免记 %d 次、期间没有"
                "任何一轮跑通；不再当成环境故障，照常计入连续失败",
                exited.task_id,
                exited.kind.value,
                names,
                used,
            )
            return False
        self._exemptions[exited.task_id] = used + 1
        _LOGGER.warning(
            "任务 %s 在同一时间窗里一起失败，判为环境故障（掉线 / 维护 / 窗口被抢 / "
            "机器休眠），不计到任何一个任务头上（第 %d/%d 次）",
            names,
            used + 1,
            MAX_ENVIRONMENT_EXEMPTIONS,
        )
        for task_id in together:
            self._repository.clear_mission_failures(task_id)
        return True

    # -- 事实 ------------------------------------------------------------------

    def _snapshots(
        self, tasks: Sequence[orm.MissionTaskRow], config: orm.SchedulerConfigRow
    ) -> tuple[TaskSnapshot, ...]:
        """把 `mission_tasks` 的行翻成领域层认识的快照，顺手把两个默认值解析掉。

        解析（`origin_*` 全 NULL → 全局主星；`fleet_lines` NULL → 全局上限）**只在
        这一处发生**。散在各处的话，页面显示的出发星球和舰队真正的出发地会分家，
        而那种错静默、且只有在战报永远配不上之后才看得见。
        """
        return tuple(
            task_snapshot(
                row,
                origin=self._origin_of(row),
                fleet_lines=self._fleet_lines_of(row, config),
            )
            for row in tasks
            if _known(row.kind)
        )

    def _origin_of(self, row: orm.MissionTaskRow) -> Coordinate:
        """这个任务的出发星球。三列有一列缺就回落到全局主星。

        「有一列缺就整个回落」而不是逐列补：半份坐标（比如只填了星系）不是一个
        能派舰队的地方，凑出来的那颗星球既不是用户填的、也不是主星。
        """
        galaxy, system, position = row.origin_galaxy, row.origin_system, row.origin_position
        if galaxy is None or system is None or position is None:
            return self._origin
        return Coordinate(galaxy, system, position)

    @staticmethod
    def _fleet_lines_of(row: orm.MissionTaskRow, config: orm.SchedulerConfigRow) -> int:
        """这个任务在它那颗星球上能占几条航线。没填就用全局那个默认值。

        全局 `scheduler_config.fleet_line_limit` **保留**，含义从「账号一共几条」
        降级成「任务没填时用几条」：海盗与扫描没有必要各配一份，新建的任务也该
        有个不至于一发都派不出去的起点。真正的上限判据一律走任务这一层。
        """
        return config.fleet_line_limit if row.fleet_lines is None else row.fleet_lines

    def _facts(
        self,
        tasks: Sequence[TaskSnapshot],
        config: orm.SchedulerConfigRow,
        now: datetime,
    ) -> SchedulerFacts:
        """一次调度所需的全部事实：一部分来自内存，其余全部来自数据库。

        没在参与调度的任务一律不去查库：bot 的完成判据要按目标逐个问库，
        而 tick 每秒一次。查了也只是丢掉。它们仍然拿得到启动/失败时刻——那两个
        本来就已经在手上（一次查询 + 一份内存），而页面要靠它们说「冷却中」。

        **按出发星球查的那几样按星球缓存**：两个任务配在同一颗星球上时，
        `count_inflight` / `next_line_free_at` 各只查一次。tick 每秒一次，
        任务数是用户加出来的，不缓存就是一路乘上去。

        **这段没有上界，所以它必须在 `_lock` 外面跑**——生产库里 bot 范围有
        4237 个目标，实测一次 0.32 秒，把它压在锁上，用户点「结束」就得排队。
        """
        grace = timedelta(minutes=config.report_grace_minutes)
        starts = self._repository.last_mission_starts()
        pirate_active = any(
            task.kind is MissionKind.PIRATE and _participating(task) for task in tasks
        )
        inflight: dict[Coordinate, int] = {}
        next_free: dict[Coordinate, datetime | None] = {}
        per_task: dict[int, TaskFacts] = {}

        for task in tasks:
            base = TaskFacts(
                last_started_at_utc=starts.get(task.task_id),
                last_failure_at_utc=self._last_failure_at.get(task.task_id),
            )
            if not _participating(task) or fills_gaps(task.kind):
                # 填空隙的那几种（扫描 / 军力榜）不派遣、也没有完成态，
                # 剩下那几样对它们恒为「没有」。
                per_task[task.task_id] = base
                continue
            row = self._repository.mission_task(task.task_id)
            if (
                task.kind is MissionKind.BOT
                and row is not None
                and _bot_by_military(row.params_json)
            ):
                origins = self._military_origins(row)
                for item in origins:
                    if item.coordinate not in inflight:
                        inflight[item.coordinate] = self._repository.count_inflight(
                            now_utc=now, origin=item.coordinate
                        )
                        next_free[item.coordinate] = self._repository.next_line_free_at(
                            now_utc=now, origin=item.coordinate
                        )
                # 多 origin 的预算是每颗星球各自的预算，绝不再拿全局保留数把它们
                # 合计校验一次；游戏的真实硬上限仍由 runner 的看屏闸门兜底。
                free = sum(max(0, item.fleet_lines - inflight[item.coordinate]) for item in origins)
                last_dispatches = [
                    self._repository.last_dispatch_at(
                        _TARGET_KIND[task.kind], origin=item.coordinate
                    )
                    for item in origins
                ]
                free_moments: list[datetime] = []
                for item in origins:
                    moment = next_free[item.coordinate]
                    if moment is not None:
                        free_moments.append(moment)
                per_task[task.task_id] = replace(
                    base,
                    free_lines=free,
                    reports_due=self._reports_due(task, now, grace),
                    targets_remaining=self._bot_remaining(task),
                    last_dispatch_at_utc=max(
                        (item for item in last_dispatches if item is not None), default=None
                    ),
                    next_line_free_at_utc=min(free_moments, default=None),
                )
                continue
            if task.origin not in inflight:
                inflight[task.origin] = self._repository.count_inflight(
                    now_utc=now, origin=task.origin
                )
                next_free[task.origin] = self._repository.next_line_free_at(
                    now_utc=now, origin=task.origin
                )
            target_kind = _TARGET_KIND[task.kind]
            per_task[task.task_id] = replace(
                base,
                free_lines=free_lines_for(
                    task,
                    inflight_from_origin=inflight[task.origin],
                    reserved_lines=config.reserved_lines,
                ),
                reports_due=self._reports_due(task, now, grace),
                targets_remaining=(
                    self._bot_remaining(task) if task.kind is MissionKind.BOT else 0
                ),
                last_dispatch_at_utc=self._repository.last_dispatch_at(
                    target_kind, origin=task.origin
                ),
                next_line_free_at_utc=next_free[task.origin],
            )

        return SchedulerFacts(
            now_utc=now,
            pirate_dispatches_today=(
                self._repository.count_dispatches_since(
                    TARGET_KIND_PIRATE, since=quota_day_start_utc(now)
                )
                if pirate_active
                else 0
            ),
            pirate_quota=config.pirate_daily_quota,
            pirate_blocked_until_utc=self._pirate_block_until(tasks),
            per_task=per_task,
        )

    def _pirate_block_until(self, tasks: Sequence[TaskSnapshot]) -> datetime | None:
        """收到游戏超限邮件时写下的封锁截止时刻，取最晚的那一个。

        它是**账号级**的（配额也是），所以哪一行任务上写着都算数，取最晚的那个
        才是安全的一侧：取最早的话，一旦以后有第二个海盗任务，它那条还没过期的
        封锁会被另一行早已过期的记录盖掉，于是助手在被封的时段里照样派。
        """
        moments = [
            row.quota_exhausted_until_utc
            for task in tasks
            if task.kind is MissionKind.PIRATE
            and (row := self._repository.mission_task(task.task_id)) is not None
            and row.quota_exhausted_until_utc is not None
        ]
        return max(moments) if moments else None

    def _reports_due(self, task: TaskSnapshot, now: datetime, grace: timedelta) -> bool:
        """这个任务有没有到期未收的战报。**只问它自己那颗出发星球派出去的那些。**

        填空隙的那几种（扫描 / 军力榜）从不派遣，`_TARGET_KIND` 里也就没有它们
        ——直接返回 False，而不是让 `_TARGET_KIND[task.kind]` 抛 KeyError。

        **`grace` 与 `max_age` 是两档完全不同的规则，不能互换也不能同值。**
        `grace` 管「飞行时间读到了」的那些：过了预计时间再等这么久还没战报就
        判缺失。`max_age` 管「读不到」的那些：`ReportWaitPlanner` 见到任何一条
        NULL 就无条件返回 `COLLECT`，没有按派出时刻算的放弃阈值，这一档就既
        永远「可收」又永远不被判缺失——调度器每个 tick 都去收一封永远不会到的
        战报，扫描永远抢不到空隙。
        """
        if fills_gaps(task.kind):
            return False
        pending = self._repository.pending_reports_for_kind(
            _TARGET_KIND[task.kind],
            now_utc=now,
            grace=grace,
            max_age=MAX_REPORT_AGE,
            origin=task.origin,
        )
        return self._planner.plan(pending, now_utc=now).action is WaitAction.COLLECT

    def _bot_remaining(self, task: TaskSnapshot) -> int:
        """本轮范围内还有几个 bot 没走完。

        完成 = 收到那一发攻击的战报，**不论战果**。平局曾经要对同一坐标再打一发，
        该规则已于 2026-08-17 按用户口径移除，所以平局的目标和打赢打输的一样算
        走完。判据在 `domain.bot_round.phase_of` 里，这里只负责把事实喂给它。

        本轮的起点是**这个任务自己的** `round_started_at_utc`：两个 bot 任务各打
        各的范围、各开各的轮，共用一个起点会让先开一轮的那个把另一个的战报一起
        判成上一轮的。
        """
        row = self._repository.mission_task(task.task_id)
        if row is None:
            return 0
        try:
            if _bot_by_military(row.params_json):
                return len(self._military_candidates(row))
            targets = self._bot_selection(row.params_json, self._origin_of(row))
        except MissionParamError as exc:
            self._repository.disable_mission_task(task.task_id, str(exc))
            return 0
        facts_by_target = self._repository.bot_dispatch_facts_many(
            targets, since=row.round_started_at_utc, now_utc=self._clock()
        )
        return sum(
            1 for target in targets if phase_of(facts_by_target[target]) is not BotPhase.DONE
        )

    def _military_batch_task(self) -> orm.MissionTaskRow | None:
        """本次军事榜采集要服务的军力 bot 任务；同优先级时按任务 id 稳定排序。"""
        candidates = [
            row
            for row in self._repository.mission_tasks()
            if row.kind == MissionKind.BOT.value
            and row.enabled
            and row.disabled_reason is None
            and _bot_by_military(row.params_json)
        ]
        return min(candidates, key=lambda row: (row.priority, row.id), default=None)

    def _military_batch_decision(
        self,
        snapshots: Sequence[TaskSnapshot],
        facts: SchedulerFacts,
        running: RunningChild | None,
    ) -> Decision | None:
        """军力榜采集与对应攻击之间的不可插队边界。

        ``RANKING`` 写到第一屏时，普通 `decide()` 会立刻发现 bot 有候选，按
        「攻击可抢占填空隙」的通用规则把榜单打断。这一批就永远采不满配置的
        100 个。批次状态在这里把两阶段连起来，但仍由同一个调度器启动两个
        独立进程，不让 BOT 自己起榜单进程。
        """
        task_id = self._military_ranking_batch_task_id
        if task_id is None:
            return None
        if running is not None:
            return Decision(Action.IDLE)
        task = next((item for item in snapshots if item.task_id == task_id), None)
        row = self._repository.mission_task(task_id)
        if (
            task is None
            or row is None
            or not task.enabled
            or task.disabled_reason is not None
            or not _bot_by_military(row.params_json)
        ):
            self._military_ranking_batch_task_id = None
            return None
        if has_work(task, facts):
            return Decision(Action.START, task)
        # 空榜、全在 24 小时排除期或当前没有航线时，不能永远扣住别的任务。
        self._military_ranking_batch_task_id = None
        return None

    def _bot_targets(self) -> list[Coordinate]:
        return [target.coordinate for target in self._scored_bot_targets()]

    def _bot_selection(self, params_json: str, origin: Coordinate) -> tuple[Coordinate, ...]:
        """这个 bot 任务这一轮要打哪些坐标，**按什么顺序**。

        ⚠️ **选靶口径只能有这一份。** 它被三处用到：算命令行、算「还剩几个没打」、
        算页面上每个目标的态。三处各写一遍的话，最先分家的是「军力优先」那一支
        ——实机 2026-08-15 就撞到了：命令行那处改了，而「还剩几个」那处仍然
        走恒星系区间，于是军力参数里没有区间、抛 `MissionParamError`、
        任务被当成没目标，**一发都不派而且不报错**。
        """
        if _bot_by_military(params_json):
            return strongest_then_nearest(
                self._scored_bot_targets(),
                origin,
                take=_bot_top_n(params_json),
                max_score=_bot_max_score(params_json),
            )
        in_range = bot_targets_in_range(self._bot_targets(), **_bot_range(params_json))
        return nearest_first(in_range, origin)

    def _military_command(
        self, row: orm.MissionTaskRow, *, max_dispatches: int | None = None
    ) -> list[str]:
        """只起一颗出发星球的一组目标，避免 runner 中途切星球留下半组状态。"""
        assignments = self._military_assignments(row)
        if not assignments:
            raise MissionParamError("本轮没有可派遣的军力攻击目标")
        first_origin = assignments[0].origin
        group = [item for item in assignments if item.origin == first_origin]
        first_origin_lines = next(
            item.fleet_lines
            for item in self._military_origins(row)
            if item.coordinate == first_origin
        )
        first_origin_free = max(
            0,
            first_origin_lines
            - self._repository.count_inflight(now_utc=self._clock(), origin=first_origin),
        )
        return bot_command(
            [item.coordinate for item in group],
            origin=first_origin,
            presets={item.coordinate: item.preset for item in group},
            # `facts.free_lines` 是所有出发点之和；runner 一次只会使用第一颗，
            # 所以这里重新按真实在飞数收窄，绝不把另一颗星球的余量借过来。
            max_dispatches=min(
                max_dispatches if max_dispatches is not None else len(group),
                first_origin_free,
            ),
        )

    def _military_assignments(self, row: orm.MissionTaskRow) -> tuple[AssignedTarget, ...]:
        """军力池先排除本轮已处理目标，否则前 N 打完会静默卡住。"""
        params = _params(row.params_json)
        candidates = self._military_candidates(row)
        pool = military_pool(
            candidates,
            take=_bot_top_n(row.params_json),
            maximum_score=_bot_max_score(row.params_json),
        )
        origins = self._military_origins(row)
        if not origins:
            raise MissionParamError("军力攻击没有启用的出发星球")
        # 只看这次要打的候选池，整张榜里一条旧记录不该把新池子误报为陈旧。
        scanned_at = [item.military_score_at_utc for item in pool if item.military_score_at_utc]
        stale_at = min(scanned_at, default=None)
        stale_after = timedelta(hours=_bot_rescan_after_hours(params))
        if stale_at is not None and self._clock() - stale_at >= stale_after:
            # 只记录，不阻塞派遣、更不能从这里启动 RANKING：两条链路会争同一只鼠标。
            _LOGGER.info(
                "军力候选池数据已过期（最旧读数 %s）；继续派遣，等待调度器空隙扫描",
                stale_at,
            )
        try:
            tiers_json = self._active_military_tiers_json
            if tiers_json is None:
                tiers_json = self._repository.military_attack_config().tiers_json
            global_tiers = json.loads(tiers_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - 写侧已校验
            raise MissionParamError("全局军力档位配置损坏") from exc
        return assign_by_capacity_and_distance(
            pool,
            origins,
            fallback_preset=BOT_ATTACK_PRESET,
            tiers=self.validate_military_tiers(global_tiers),
        )

    def _military_candidates(self, row: orm.MissionTaskRow) -> list[ScoredTarget]:
        """取前 N 名前，先排除本轮与近 24 小时已攻击的 bot。

        若先拿前 N 再排除已攻击目标，首批刚好都打过时军力任务会把候选池缩成
        空集，较低排名、从未攻击的目标永远轮不到。排除必须在 ``military_pool``
        的前面，随后再由距离给各出发星球分配。
        """
        targets = self._scored_bot_targets()
        now = self._clock()
        facts_by_target = self._repository.bot_dispatch_facts_many(
            [target.coordinate for target in targets],
            since=row.round_started_at_utc,
            now_utc=now,
        )
        attacked_last_day = self._repository.attacked_bot_targets_since(now - timedelta(hours=24))
        return [
            target
            for target in targets
            if target.coordinate not in attacked_last_day
            and phase_of(facts_by_target[target.coordinate]) is BotPhase.NEEDS_ATTACK
        ]

    def _military_origins(self, row: orm.MissionTaskRow) -> tuple[AttackOrigin, ...]:
        """新表为空才回落旧单 origin，区域攻击永远不读新表。"""
        configured = self._repository.mission_task_origins(row.id)
        if configured:
            origins: list[AttackOrigin] = []
            for item in configured:
                if not item.enabled:
                    continue
                planet = None
                if item.planet_id is not None:
                    planet = self._repository.attack_planet(item.planet_id)
                coordinate = (
                    Coordinate(item.galaxy, item.system, item.position)
                    if planet is None
                    else Coordinate(planet.galaxy, planet.system, planet.position)
                )
                origins.append(AttackOrigin(coordinate, item.fleet_lines))
            return tuple(origins)
        config = self._repository.scheduler_config()
        return (AttackOrigin(self._origin_of(row), self._fleet_lines_of(row, config)),)

    def _scored_bot_targets(self) -> list[ScoredTarget]:
        """已记录的 bot，**连军力值一起带出来**。

        军力值可能是 None（那颗还没在榜单上见过），这是常态不是异常——
        库里六千多行，昨晚一夜也只扫到一千多个有值的。`domain.target_order`
        把 None 排在所有已知的后面，不当成 0 分。
        """
        return [
            ScoredTarget(
                Coordinate(row.galaxy, row.system, row.position),
                military_score=row.military_score,
                military_score_at_utc=row.military_score_at_utc,
            )
            for row in self._repository.list_bot_targets()
            if is_bot_coordinate(Coordinate(row.galaxy, row.system, row.position))
        ]

    # -- 参数换算 --------------------------------------------------------------

    def _command_for(self, kind: MissionKind, params_json: str, origin: Coordinate) -> list[str]:
        """三条链路各有各的换算，`domain.missions` 里是纯函数。

        刻意不做成一个 `mission_command(kind, params)`：三条链路的参数类型本来
        就不通，合成一个入口就得让 `params` 退化成 `dict[str, Any]`，在 strict
        mypy 下等于放弃检查。

        ⚠️ 这里原先还有一道临时闸门（`check_origin_dispatchable`）：出发星球不是
        主星就当场拒掉。它随「切换星球」实装一起删了——runner 开工时会真的把当前
        星球切过去（`tools.pirate_loop.ensure_origin_planet`），切不成就一发都不派
        并报 `EXIT_ENVIRONMENT_BUSY`。**不要把它加回来**：加回来等于除主星以外的
        任务一律派不出去。
        """
        if kind is MissionKind.SCAN:
            return scan_command()
        if kind is MissionKind.RANKING:
            return ranking_command(bot_limit=_ranking_bot_limit(params_json))
        if kind is MissionKind.PIRATE:
            return pirate_command(
                pirate_systems(origin, _pirate_radius(params_json)), origin=origin
            )
        # ⚠️ **筛范围与排顺序是两件事，分两步写。**
        #
        # 排序按「离这个任务自己的 `origin` 由近到远」（`domain.distance`）。
        # 一夜的航线有限，而近目标的往返比远目标短一个量级（同银河近距离约
        # 20–30 分钟，跨银河约 2.6 小时，都是实机读到的）：同样 6 条航线，
        # 先打近的能派十几发，先打远的只能派两三发。
        #
        # 原先没有这一步，目标顺序就是库里的返回顺序（大致按坐标升序）。实机
        # 2026-08-13 通宵：范围配的是 2:60–2:499、里面有 376 个已知 bot，
        # 而一夜只走到第 121 系——后面那些永远轮不到。
        return self._bot_command(params_json, origin)

    def _bot_command(
        self, params_json: str, origin: Coordinate, *, max_dispatches: int | None = None
    ) -> list[str]:
        """组 bot runner 命令，并把当前可用航线变成真实的派遣预算。

        候选清单可以大于航线数：runner 依旧按距离顺序读取，达到预算后立即退出；
        等任一攻击的 ``飞行时间 × 2`` 返航后，下一轮才会继续后面的目标。
        """
        return bot_command(
            self._bot_selection(params_json, origin),
            origin=origin,
            max_dispatches=max_dispatches,
        )


def task_snapshot(row: orm.MissionTaskRow, *, origin: Coordinate, fleet_lines: int) -> TaskSnapshot:
    """一行 `mission_tasks` → 领域层认识的那个不可变快照。

    公开是给 API 用的：页面要按 `domain.scheduler` 的判据算状态和展示次序，
    而它拿到的只有 ORM 行。转换只能有一份，否则两边对「什么算已停用」的
    理解迟早分家。

    `origin` 与 `fleet_lines` 是**解析完默认值之后**的取值，由调用方传进来
    （`MissionScheduler._snapshots`）：那两条回落规则要用到 Settings 与
    `scheduler_config`，而这个函数不该去碰它们中的任何一个。
    """
    return TaskSnapshot(
        task_id=row.id,
        kind=MissionKind(row.kind),
        name=row.name,
        enabled=row.enabled,
        priority=row.priority,
        origin=origin,
        fleet_lines=fleet_lines,
        disabled_reason=row.disabled_reason,
        enabled_from_utc=row.enabled_from_utc,
        enabled_until_utc=row.enabled_until_utc,
    )


def _window_message(task: TaskSnapshot, *, open_now: bool, first_look: bool) -> str:
    """定时窗口那条 `system_log` 的正文。

    「本次运行第一次看到」和「到点变了」措辞必须分开。合成一句的话，一个窗口从头
    到尾都开着的任务，在控制台每次重启时都会留下一条「到达定时开启时刻」——
    而那一刻什么都没发生。事后按这句话去对时间，对出来的是一个假的开启时刻。
    """
    name = task.name or task.kind.value
    if first_look:
        state = "在定时窗口内，照常参与调度" if open_now else "不在定时窗口内，暂不开新的一轮"
        return f"任务「{name}」{state}"
    if open_now:
        return f"任务「{name}」到达定时开启时刻，恢复参与调度"
    # 「不打断」必须写进这句话：日志里只说「已关闭」而实机上还有个 runner 在点
    # 鼠标，读日志的人会以为进程漏杀了。
    return f"任务「{name}」已过定时关闭时刻，不再开新的一轮（正在跑的那一轮不打断）"


def _participating(task: TaskSnapshot) -> bool:
    """这个任务此刻参不参与调度。停用（不论哪种）与没勾都算不参与。"""
    return task.enabled and task.disabled_reason is None


def _known(kind: str) -> bool:
    """库里出现不认识的 kind（手改或旧版本留下的）就跳过，不让调度器崩掉。"""
    return kind in {item.value for item in MissionKind}


def _params(raw: str) -> dict[str, Any]:
    try:
        data: Any = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise MissionParamError(f"参数不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise MissionParamError("参数必须是一个 JSON 对象")
    return data


def _int_param(data: dict[str, Any], name: str) -> int:
    value = data.get(name)
    # `bool` 是 `int` 的子类，得单独排掉：`{"radius": true}` 会被当成半径 1，
    # 悄悄打出一圈根本不是用户想要的范围。
    if not isinstance(value, int) or isinstance(value, bool):
        raise MissionParamError(f"缺少整数参数 {name}")
    return value


def _pirate_radius(raw: str) -> int:
    return _int_param(_params(raw), "radius")


def _ranking_bot_limit(raw: str) -> int | None:
    """军力榜这一趟最多采几个 bot。**留空 = 全扫**，也就是保持原来的行为。

    用户口径（2026-08-17）：「军力扫描增加扫描数量范围，为空则全扫」。

    ⚠️ **「没配」和「配了 0」必须是两回事。** 空框在页面上什么都不送
    （`missions.html` 的 `.mission-param` 处理器不把空框往上送），于是这里
    读到的是 `None`——那是「不划线」。而 `0` 是一个用户真的敲进去的数字，
    它的意思只可能是「一个都别扫」，而那等于把这条链路关掉：要关掉有复选框，
    不该用一个看起来像范围的数字表达。所以 `0` 与负数一律当场拒掉，
    让页面 400 报出来，而不是悄悄跑一趟什么都不采的采集。

    没有为它加数据库列：它和 `galaxy` / `first_system` 一样，是任务参数，
    住在 `mission_tasks.params_json` 里（见 `storage.models` 那一行的注释）。
    """
    value = _params(raw).get("bot_limit")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # `bool` 是 `int` 的子类，得单独排掉（同 `_int_param` 那条）。
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError("扫描数量必须是整数；要全扫就把它留空")
    try:
        limit = int(value)
    except ValueError as exc:
        raise MissionParamError(f"扫描数量不是整数：{value!r}") from exc
    if limit < 1:
        raise MissionParamError("扫描数量至少是 1；要全扫就把它留空，别填 0")
    return limit


def _smallest_limit(*limits: int | None) -> int | None:
    """几个上限里最紧的那个；一个都没有就是「不设限」。"""
    values = [limit for limit in limits if limit is not None]
    return min(values) if values else None


def _bot_by_military(raw: str) -> bool:
    """这个 bot 任务是不是走「军力优先」那一支。默认 False = 老的区域攻击。

    默认关是刻意的：军力优先会把目标散到全宇宙，而区域攻击的范围是用户自己
    圈的。悄悄换掉一条已经在跑的链路的选靶口径，比多一个开关危险得多。
    """
    return bool(_params(raw).get("by_military", False))


def _bot_top_n(raw: str) -> int:
    """军力优先时取前几名。默认 `TOP_BY_MILITARY`（用户口径：50）。"""
    value = _params(raw).get("top_n")
    return (
        int(value)
        if isinstance(value, int | float | str) and str(value).strip()
        else TOP_BY_MILITARY
    )


def _bot_max_score(raw: str) -> float | None:
    """军力上限，超过就不打。没配就是不设限。

    用户 2026-08-14 要求过「军力确实要设置上限」——太强的目标不是当前预设
    打得动的。留成可配而不是写死：上限取决于用的哪个预设，而预设是用户维护的。
    """
    value = _params(raw).get("max_score")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value)


def _bot_rescan_after_hours(data: dict[str, Any]) -> float:
    """仅提示军力池陈旧；默认六小时，但绝不以此阻塞派遣。"""
    value = data.get("rescan_after_hours", 6)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise MissionParamError("rescan_after_hours 必须是正数")
    return float(value)


def _bot_tiers(data: dict[str, Any]) -> tuple[MilitaryTier, ...]:
    """解析用户明确配置的档位；空配置回落 BBB，绝不偷偷造阈值。"""
    raw = data.get("tiers")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MissionParamError("tiers 必须是数组")
    tiers: list[MilitaryTier] = []
    for item in raw:
        if not isinstance(item, dict):
            raise MissionParamError("tiers 的每一项必须是对象")
        minimum, preset = item.get("min_score"), item.get("preset")
        if isinstance(minimum, bool) or not isinstance(minimum, int | float):
            raise MissionParamError("tiers.min_score 必须是数字")
        if not isinstance(preset, str) or not preset.strip():
            raise MissionParamError("tiers.preset 必须是非空预设标题")
        tiers.append(MilitaryTier(float(minimum), preset))
    if tiers != sorted(tiers, key=lambda tier: tier.min_score, reverse=True):
        raise MissionParamError("tiers 必须按 min_score 从高到低排列")
    return tuple(tiers)


def _bot_range(raw: str) -> dict[str, int]:
    data = _params(raw)
    return {
        "galaxy": _int_param(data, "galaxy"),
        "first_system": _int_param(data, "first_system"),
        "last_system": _int_param(data, "last_system"),
    }
