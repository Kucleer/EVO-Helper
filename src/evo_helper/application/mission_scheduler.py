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
from collections.abc import Callable, Mapping, Sequence
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
    top_up_with_unrated,
)
from evo_helper.domain.missions import (
    ORIGIN,
    MissionIdle,
    MissionParamError,
    NoFreeLineError,
    bot_command,
    bot_targets_in_range,
    pirate_command,
    pirate_systems,
    ranking_command,
    scan_command,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import (
    BOT_AREA_REACHED_PREFIX,
    bot_area_scrolls,
    calibrated_blind_scrolls,
    is_bot_coordinate,
)
from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
from evo_helper.domain.report_wait import (
    MAX_REPORT_AGE,
    REPORT_SCAN_HOURS_MAX,
    UNKNOWN_LINE_HOLD,
    ReportWaitPlanner,
    WaitAction,
)
from evo_helper.domain.scheduler import (
    Action,
    Decision,
    DisabledRecovery,
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
    DEFAULT_SCORE_MAX_AGE,
    TOP_BY_MILITARY,
    FreshnessSplit,
    ScoredTarget,
    split_by_freshness,
    strongest_then_nearest,
)
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN,
    BLIND_SCROLL_SAMPLES,
    BLIND_SCROLLS,
)
from evo_helper.infrastructure.system_log import (
    child_environment,
    record_knob_override,
    record_system_log,
)
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
#:
#: 分类（2026-08-17 审计）：**低优先级旋钮**——「撑多久算撑不过去」有主观成分，
#: 但这个数不是独立可调的：它的物理含义是「6 × `RESTART_COOLDOWN` ≈ 半小时」，
#: 而重启冷却本身在库里可配。真要让豁免时长可配，该配的是**时长**、由它反推次数，
#: 不是直接开一个次数框——开了之后两个数会各说各话。留待有人真的需要时再做。
MAX_ENVIRONMENT_EXEMPTIONS = 6

#: 同一个 bot 坐标多久之内不重复打。用户口径（2026-08-15）。
#:
#: ⚠️ **这是「没配置时」的默认值。** 它是一个**运维旋钮**：24 小时是用户定的策略，
#: 不是游戏规则（游戏那侧的硬限制是海盗每日 32 发，在 `scheduler_config`）。
#: 活动期间想多榨几轮就调小，已知 bot 多、想摊得更开就调大——没有唯一正确答案。
#: 攻击配置页上有一个框（`military_attack_config.bot_revisit_hours`），
#: 留空才走这里。
DEFAULT_BOT_REVISIT = timedelta(hours=24)

#: 用户能填进去的重复攻击间隔上界（小时）。
#: 一周：bot 军力每周一 UTC+0 刷新，跨过一个刷新周期之后，「上周打过」拦住的是
#: 一批军力已经变了的目标——那不再是「别重复打」，而是把候选池越锁越小。
BOT_REVISIT_MAX_HOURS = 168

#: `scheduler_config.report_grace_minutes` 的默认值，抄在这里只为了在配置行还没
#: 建出来时给冷却上界一个说法（见 `MissionScheduler._report_grace_minutes`）。
#: ⚠️ 改 `storage.models.SchedulerConfigRow.report_grace_minutes` 的默认值时要一起改。
DEFAULT_REPORT_GRACE_MINUTES = 30

#: 冷却窗口离宽限期至少要留出来的那一段（分钟）。
#:
#: 冷却窗口逼近宽限期就会**自己制造「战报缺失」**：战报最多晚这么久才入库，
#: 而过了预计时间再等一个宽限期还读不到就判缺失。留一半余量是
#: `RECONCILE_COOLDOWN` 那个 15 分钟（宽限期 30）当初的取法，这里把它写成规则，
#: 好让宽限期被用户改过之后上界跟着走。
RECONCILE_COOLDOWN_GRACE_RATIO = 2

#: 军力候选池连着这么久一个能打的都筛不出来，就往 `system_log` 写一条 WARNING。
#:
#: **它是为「攻击悄悄停摆」准备的。** 候选的军力分数全都过期时，这条链路会被判成
#: 没活干——那是对的，调度器会去跑军力榜扫描把池子刷新——但如果扫描本身跟不上
#: 有效期（扫得太慢、榜单页读不出来、或者有效期被调得比一轮扫描还短），这个状态
#: 会一直维持下去，而页面上只是一句不痛不痒的状态，一整夜一发不派也没人知道。
#:
#: **为什么按时长而不是按 tick 数。** tick 每秒一次，「连续 3 轮」等于三秒，
#: 那挡不住任何东西（榜单刚开始写第一屏时分数本来就会短暂全过期）。取半小时：
#: 约等于半轮扫描，长到不会被一次采集中途的空档触发，短到还来得及在一夜里补救。
#:
#: 分类（2026-08-17 审计）：**低优先级旋钮**——它只决定日志里那条 WARNING 什么时候
#: 出现，不参与任何调度判据；调错了最坏也只是告警早一点或晚一点。没做成可配置。
STALE_POOL_WARNING_AFTER = timedelta(minutes=30)

#: 调度器的任务种类 → `attack_intents.target_kind` 的取值。
#: 两套词汇本来就不同（一个是链路，一个是打谁），映射写明白比两边硬凑一致好。
_TARGET_KIND = {
    MissionKind.PIRATE: TARGET_KIND_PIRATE,
    MissionKind.BOT: TARGET_KIND_BOT,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MilitaryPoolReading:
    """军力候选池这一次数出来的账：**能打的有几个、被新鲜度跳过了几个**。

    做成一个结构而不是只返回一个列表，是因为日志得说实话。原先那句
    「军力候选池数据已过期（最旧读数 …）」既不说这一轮还剩多少能打，也不说被跳过
    的是哪一批——实机 2026-08-17 就是被它误导的：它报的「最旧读数」是三天前的
    某一条，而正要打的那个目标超期 3.6 小时，日志里一个字都没提。
    """

    #: 三堆：主力（有分数且新鲜）、补位（没有分数）、跳过（有分数但过期）。
    split: FreshnessSplit
    #: 这一次用的有效期，写进日志好让用户对得上自己配的那个数。
    max_age: timedelta

    @property
    def rated(self) -> tuple[ScoredTarget, ...]:
        return self.split.rated

    @property
    def unrated(self) -> tuple[ScoredTarget, ...]:
        return self.split.unrated

    @property
    def usable(self) -> int:
        """这一轮真的可以打的个数：**主力 + 补位**。

        ⚠️ 补位必须算进来。不算的话，一个「全库都没有分数」的正常夜晚会被判成
        「没活干」，而那些目标打起来毫无风险（实测最高战力只有 70 多 K，离打不动
        还很远），只是排不了序而已。
        """
        return len(self.split.rated) + len(self.split.unrated)

    @property
    def attackable(self) -> int:
        """过新鲜度闸门**之前**还剩多少个（已排除近 24 小时打过的与本轮走完的）。"""
        return self.usable + len(self.split.expired)

    @property
    def skipped(self) -> int:
        return len(self.split.expired)

    @property
    def oldest_skipped_at(self) -> datetime | None:
        """被跳过的那批里最旧的那条读数。"""
        return min(
            (
                target.military_score_at_utc
                for target in self.split.expired
                if target.military_score_at_utc is not None
            ),
            default=None,
        )

    @property
    def starved(self) -> bool:
        """有候选，却一个能打的都没有——也就是**全都是「有分数但过期」那一档**。

        ⚠️ **和「一个候选都没有」必须分开。** 后者是完全正常的一档（已知 bot 全在
        24 小时冷却里或还在飞），拿它去报「军力榜扫描跟不上」是句假话。
        """
        return self.attackable > 0 and self.usable == 0


@dataclass(frozen=True)
class BlindScrollChoice:
    """盲拖屏数这一次判成了什么，**以及凭什么**。

    做成一个结构而不是只返回一个 `int | None`，是因为答案本身分不清三种来源，
    而三种的善后完全不同：手填的要去攻击配置页上改，标定出来的说明这条反馈回路
    还活着，**「没给出答案」则可能是刚上线、也可能是反解规则已经失效**——后者
    正是 `domain.ranking.bot_area_reached_message` 上警告过的那种静默退化。
    `samples` 就是分开这两者的那个数：刚上线时它会一天天涨，失效时它恒为 0。
    """

    #: 判定结果。`None` = 不往命令行上加 `--blind-scrolls`，采集用写死的默认值。
    scrolls: int | None
    #: `manual`（攻击配置页手填）/ `calibrated`（按实测标定）/ `default`（没答案）。
    source: str
    #: 从 `system_log` 里反解出来的实测样本条数。手填那一支不查库，恒为 0。
    samples: int


def _blind_scroll_verdict(choice: BlindScrollChoice) -> str:
    """把一次判定念成人话。**三种来源各一句，绝不含糊成一句通用的。**"""
    if choice.source == "manual":
        return f"{choice.scrolls} 屏（攻击配置页上手填的，标定不再参与）"
    if choice.source == "calibrated":
        return (
            f"{choice.scrolls} 屏（按最近 {BLIND_SCROLL_SAMPLES} 次实测标定，"
            f"当前共有 {choice.samples} 条实测样本）"
        )
    return (
        f"「不指定」，采集将用写死的默认值 {BLIND_SCROLLS} 屏"
        f"（实测样本只有 {choice.samples} 条，自动标定要 {BLIND_SCROLL_SAMPLES} 条）"
    )


@dataclass(frozen=True)
class SchedulerSnapshot:
    """一眼看全的调度器现状，供 API 搬给页面。

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
        #: 实测一次 0.32 秒；而 tick 每秒一次、页面每 2 秒问一次状态（2026-08-11
        #: 那会儿还有个桌面悬浮窗在问第三遍，那个窗口已经删了，但一台机器上开几个
        #: 浏览器标签就能把这个数补回来）。这些活儿一旦压在同一把锁上，
        #: 用户点「结束」就得排在它们后面
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
        #: 每个军力 bot 任务这一 tick 数出来的候选池账目。由 `_facts` 整份重新赋值
        #: （不是原地改），因为页面线程也会调 `_facts`：整份换掉的话，读的人拿到的
        #: 要么是上一份、要么是新的一份，不会撞见改到一半的中间态。
        self._military_pool_readings: dict[int, MilitaryPoolReading] = {}
        #: 每个军力 bot 任务「池子全超期」这一段是从什么时候开始的，以及连着看到了
        #: 几个 tick。只记在内存里，理由同上面那几份：判据每 tick 现算，这里记的
        #: 只是「这一段持续多久了」，好让 WARNING 不至于每秒刷一条。
        self._stale_pool_since: dict[int, datetime] = {}
        self._stale_pool_rounds: dict[int, int] = {}
        #: 上一次为这一段写过 WARNING 的时刻。用来把重复告警压到每
        #: `STALE_POOL_WARNING_AFTER` 一条——不是只报一次：一整夜的停摆该在日志里
        #: 留下持续的痕迹，只报一次的话，翻日志的人会以为它早就恢复了。
        self._stale_pool_warned_at: dict[int, datetime] = {}
        #: 上一次判定出来的盲拖屏数取值与它的来源，用来把日志压成「只在变化时写」。
        #: 见 `_blind_scrolls`。
        self._blind_scroll_choice: BlindScrollChoice | None = None

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
        """当前的完整现状。页面每几秒问一次。

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
        _bot_score_max_age(params)

    def validate_military_tiers(self, tiers: list[dict[str, Any]]) -> tuple[MilitaryTier, ...]:
        """校验全局攻击档位；任务参数不再携带档位。"""
        return _bot_tiers({"tiers": tiers})

    def validate_blind_scrolls(self, value: object) -> int | None:
        """校验攻击配置页上那个「盲拖屏数」。同 `validate_military_tiers`：
        页面在**写库之前**用调度器自己这把尺子量一遍。

        返回 `None` 表示留空——那不是 0，是「跟着 `BLIND_SCROLLS` 的默认值走」。
        """
        return _blind_scrolls(value)

    def validate_report_scan_hours(self, value: object) -> int | None:
        """校验攻击配置页上那个「翻信箱时长」。同 `validate_blind_scrolls`：
        页面在**写库之前**用调度器自己这把尺子量一遍。

        返回 `None` 表示留空 = 跟着 `DEFAULT_REPORT_SCAN_FLOOR`（6 小时）走。
        """
        return _report_scan_hours(value)

    def validate_unknown_line_hold_minutes(self, value: object) -> int | None:
        """校验攻击配置页上那个「读不到飞行时间时占多久航线」。留空返回 `None`。"""
        return _unknown_line_hold_minutes(value)

    def validate_reconcile_cooldown_minutes(self, value: object) -> int | None:
        """校验攻击配置页上那个「两次翻信箱之间的冷却」。留空返回 `None`。

        上界**读的是库里当下的 `report_grace_minutes`**，不是写死的 30：
        那条边界本身就是可配的，拿一个写死的数去卡它，用户把宽限期调大之后
        照样填不进合法的冷却值。理由整段写在
        `domain.reconcile_cooldown.RECONCILE_COOLDOWN` 上。
        """
        return _reconcile_cooldown_minutes(value, grace_minutes=self._report_grace_minutes())

    def reconcile_cooldown_ceiling(self) -> int:
        """页面上那个框能填的最大分钟数。**和校验用的是同一条算式。**

        页面必须显示同一个上界：显示一个数、校验用另一个数，用户会填进一个
        `max` 允许、后端却 400 的值——而那种不一致读起来像是保存功能坏了。
        """
        return _reconcile_cooldown_ceiling(self._report_grace_minutes())

    def validate_bot_revisit_hours(self, value: object) -> int | None:
        """校验攻击配置页上那个「同一个 bot 多久之内不重复打」。留空返回 `None`。"""
        return _bot_revisit_hours(value)

    def unknown_line_hold(self) -> timedelta:
        """飞行时间读不到时，一条航线占多久。**读侧的唯一入口。**

        公开出来是给「清理航线占用」那条路用的（`web.persistent_service`）：
        它和 `count_inflight` 必须量同一把尺子，否则页面上写着「占着 3 条」、
        按钮却报「放开了 0 条」，而那个数字是这个按钮唯一的可见回执。
        """
        return self._unknown_line_hold()

    def _report_grace_minutes(self) -> int:
        """库里当下的战报宽限期。配置行还没建出来时按默认 30 分钟算——
        校验一个旋钮时不该因为另一张表没初始化就把整条保存路径弄死。
        """
        try:
            return int(self._repository.scheduler_config().report_grace_minutes)
        except ValueError:
            return DEFAULT_REPORT_GRACE_MINUTES

    # -- 行为旋钮的读侧 --------------------------------------------------------
    #
    # 三个读法完全同构：问库要那一行 → 空就用代码默认值 → 用了非默认值就往
    # `system_log` 留一条痕迹。**那条痕迹是硬要求**：一个被改过的阈值最阴的失败
    # 方式是日志里一切都像默认行为，排障的人照着代码里的数去推，怎么算都对不上。

    def _unknown_line_hold(self) -> timedelta:
        """读不到飞行时间时，一条航线按派出时刻起算占多久。

        配置行还没建出来时（老库、或 `ensure_mission_rows()` 还没跑）当成留空：
        一个没初始化的配置表说明不了「用户想改这个数」，为它把航线记账停掉
        是不成比例的。同 `_blind_scrolls`。
        """
        minutes = self._knob("unknown_line_hold_minutes")
        if minutes is None:
            return UNKNOWN_LINE_HOLD
        hold = timedelta(minutes=minutes)
        record_knob_override(
            "unknown_line_hold",
            source=__name__,
            effective=hold,
            default=UNKNOWN_LINE_HOLD,
            detail="飞行时间读不到的派遣按这个时长占航线",
        )
        return hold

    def _bot_revisit_window(self) -> timedelta:
        """同一个 bot 坐标多久之内不重复打。"""
        hours = self._knob("bot_revisit_hours")
        if hours is None:
            return DEFAULT_BOT_REVISIT
        window = timedelta(hours=hours)
        record_knob_override(
            "bot_revisit",
            source=__name__,
            effective=window,
            default=DEFAULT_BOT_REVISIT,
            detail="这段时间内打过的 bot 坐标不进候选池",
        )
        return window

    def _knob(self, column: str) -> int | None:
        """全局攻击配置上某个旋钮的原始值；没配 / 配置行不存在都返回 None。"""
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return None
        value = getattr(row, column, None)
        return None if value is None else int(value)

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
            # 放在 `_step` **之前**：刚被放回来的任务这一秒就该参与排队，不必
            # 白等一个 tick。放在循环外面是因为它按 tick 算一次就够——`_step`
            # 一个 tick 里会转好几圈，每圈都去数一遍在飞舰队纯属白付。
            self._resume_tasks_waiting_for_a_line(self._clock())
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
        self._log_a_starved_military_pool(snapshots, now)
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

    # -- 因航线不足停用的自动恢复 --------------------------------------------------

    def _resume_tasks_waiting_for_a_line(self, now: datetime) -> None:
        """把「因空闲航线不足而自动停用」的任务放回来——**只在此刻真的有空闲航线时**。

        **为什么这一类不该要人工恢复。** 触发它的条件会自愈：舰队总会飞回来，
        航线总会空出来（占用判据是纯时间的，见 `storage.repository` 的
        `_still_holding_a_line`）。而 `disabled_reason` 一旦写下就只有两条清除
        路径——用户点「恢复」，或者用户改一次任务配置。于是条件早就不成立了，
        任务却一直挂着「已停用」，一发都不派。2026-08-17 11:19 生产库实测：一个
        配了 9 条航线的 bot 攻击任务只占着 2 条，7 条空着，仍然停用着。

        **别的停用原因绝不能顺带被放出来。** 连续失败到上限说的是「这不是暂时
        的」，自动放出来只会让调度循环退回那个满速空转的重启循环；参数填错也一样
        ——改之前重试一万次都是同一个结果。所以这里认的是
        `DisabledRecovery.FREE_LINES` 这个标记，不是 `disabled_reason` 里那句
        中文（措辞改一次判据就静默失效）。最终那一下由
        `repository.resume_mission_task` 在同一个事务里再确认一遍标记。

        **判据现算，不挂定时器。** 每 tick 拿此刻的在飞舰队重新算一次空闲航线，
        不是「过了 N 分钟就试试」：调度器进程会重启，内存里的闹钟一重启就没了，
        而「有没有空闲航线」重启后照样算得出来。空闲航线用的是
        `_free_lines_from`——`_facts` 那一份同一个函数，所以放它出来的这一刻，
        它一定过得了 `_launch` 里那道让它停用的闸门，不会一放出来就再停一次。

        **恢复要写 `system_log`。** 任务突然又开始跑而日志里一个字都没有，
        事后没人查得出是谁放的它。
        """
        rows = [
            row
            for row in self._repository.mission_tasks()
            if row.disabled_reason is not None
            and row.disabled_recovery == DisabledRecovery.FREE_LINES.value
            and _known(row.kind)
        ]
        if not rows:
            # 绝大多数 tick 走这里：一次 `mission_tasks()` 之外一个查询都不多付。
            return
        config = self._repository.scheduler_config()
        snapshots = {task.task_id: task for task in self._snapshots(rows, config)}
        inflight: dict[Coordinate, int] = {}
        # 一次读齐，整段复用：航线记账的每一处都必须用同一个值，否则同一颗星球
        # 在两个判据里占着的航线数不一样。
        hold = self._unknown_line_hold()
        for row in rows:
            task = snapshots[row.id]
            origins = (
                self._military_origins(row)
                if task.kind is MissionKind.BOT and _bot_by_military(row.params_json)
                else None
            )
            coordinates = (
                [task.origin] if origins is None else [item.coordinate for item in origins]
            )
            for coordinate in coordinates:
                if coordinate not in inflight:
                    inflight[coordinate] = self._repository.count_inflight(
                        now_utc=now, origin=coordinate, hold=hold
                    )
            free = _free_lines_from(
                task,
                origins=origins,
                inflight=inflight,
                reserved_lines=config.reserved_lines,
            )
            if free < 1:
                continue
            if not self._repository.resume_mission_task(
                row.id, recovery=DisabledRecovery.FREE_LINES
            ):
                # 这期间用户自己点了「恢复」，或者它已经被别的原因重新停用。
                continue
            name = task.name or task.kind.value
            record_system_log(
                "INFO",
                "application.mission_scheduler",
                f"任务「{name}」曾因空闲航线不足被自动停用，"
                f"当前空闲航线 {free} 条，已自动恢复参与调度",
                payload={
                    "task_id": row.id,
                    "mission_kind": task.kind.value,
                    "free_lines": free,
                    "disabled_recovery": DisabledRecovery.FREE_LINES.value,
                },
                logged_at_utc=now,
            )

    # -- 自动停用 ------------------------------------------------------------

    def _disable_task(
        self,
        row: orm.MissionTaskRow,
        task: TaskSnapshot,
        reason: str,
        *,
        recovery: DisabledRecovery,
    ) -> None:
        """把任务自动停用，**并在真正发生跃迁的那一刻写一条 `system_log`**。

        全仓「调度器自己把任务关掉」只走这一处，理由和 `_resume_tasks_waiting_for_a_line`
        那一条对称：**任务突然不动了而日志里一个字都没有，事后没人查得出是谁关的它。**

        ⚠️ **`disabled_reason` 那一列不算留痕。** 它只留得住**当前**这一次：
        `resume_mission_task`（航线一空就自动恢复）与 `update_mission_task`
        （用户改一次配置）都会把它清成 NULL。于是「昨晚三点因为范围里一个 bot
        都没有被关掉、四点又被自动放回来」这段经过，在库里一个字都不剩——而那
        正是要查的东西。日志是只增不改的，它才留得住。

        ⚠️ **只在跃迁那一下写。** `_targets_remaining` 每 tick 都会走（页面轮询
        也会），停用一条配置填错的链路会在那里被重复调用；无条件写就是每秒一条、
        一夜八万行，把真正要看的那一条淹掉，而且事后按日志对时间会对出一个假的
        「停用时刻」——真正的那一刻在八万行的最前面。所以判据是**库里此刻的那
        两列**，不是内存里的记忆：进程重启之后再看到同一个已停用的任务，那不是
        新的跃迁，不该再记一条。

        `recovery` 一起进比较：措辞没变而恢复方式从「等航线」变成「要人工」，
        对用户是完全不同的两件事，漏掉它就等于把一次真的跃迁说成没发生。
        """
        previous = (row.disabled_reason, row.disabled_recovery)
        self._repository.disable_mission_task(row.id, reason, recovery=recovery)
        if previous == (reason, recovery.value):
            return
        name = task.name or task.kind.value
        aftermath = (
            "空闲航线一空出来就会自动恢复"
            if recovery is DisabledRecovery.FREE_LINES
            else "在用户点「恢复」或改一次任务配置之前，它不会再被起起来"
        )
        record_system_log(
            "WARNING",
            "application.mission_scheduler",
            f"任务「{name}」已被自动停用：{reason}；{aftermath}",
            payload={
                "task_id": row.id,
                "mission_kind": task.kind.value,
                "disabled_reason": reason,
                "disabled_recovery": recovery.value,
                "previous_disabled_reason": previous[0],
                "previous_disabled_recovery": previous[1],
            },
            logged_at_utc=self._clock(),
        )

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

    def _log_a_starved_military_pool(
        self, snapshots: Sequence[TaskSnapshot], now: datetime
    ) -> None:
        """池子连着一段时间一个能打的都筛不出来时，往 `system_log` 写一条 WARNING。

        **为什么非有这条不可。** 新鲜度闸门把「候选全都顶着过期分数」变成了
        「此刻没活干」，那是对的——调度器会去跑军力榜扫描。但如果扫描本身跟不上
        有效期（扫得太慢、榜单读不出来、或者用户把有效期调得比一轮扫描还短），
        这个状态会一直维持，而页面上只有一句不痛不痒的状态：**攻击悄悄停摆一整夜，
        没人知道。**

        ⚠️ 这一档现在**只可能由「有分数但过期」造成**：没有分数的目标走补位池，
        照样能打（`MilitaryPoolReading.usable`）。所以措辞说的是「分数全都过期」，
        不能再写成笼统的「读不到数据」——后者会把一个全库都没扫过的正常夜晚
        也说成故障。

        写在 `_step` 里而不是 `_military_pool_reading` 里，因为后者页面线程也会走
        （`snapshot` → `_facts`），按它计数等于把页面轮询算成调度轮次。

        **每 `STALE_POOL_WARNING_AFTER` 最多一条**，池子一恢复就清账。只报一次是
        不够的：一整夜的停摆该在日志里留下持续的痕迹，否则翻日志的人会以为它早就
        恢复了。
        """
        readings = self._military_pool_readings
        by_id = {task.task_id: task for task in snapshots}
        for task_id in [known for known in self._stale_pool_since if known not in readings]:
            self._forget_a_starved_military_pool(task_id)
        for task_id, reading in readings.items():
            if not reading.starved:
                self._forget_a_starved_military_pool(task_id)
                continue
            since = self._stale_pool_since.setdefault(task_id, now)
            rounds = self._stale_pool_rounds.get(task_id, 0) + 1
            self._stale_pool_rounds[task_id] = rounds
            # 头一条按「这一段开始」起算，之后每隔同样长再补一条。
            warned_at = self._stale_pool_warned_at.get(task_id)
            if now < (since if warned_at is None else warned_at) + STALE_POOL_WARNING_AFTER:
                continue
            self._stale_pool_warned_at[task_id] = now
            task = by_id.get(task_id)
            hours = reading.max_age.total_seconds() / 3600
            record_system_log(
                "WARNING",
                "application.mission_scheduler",
                f"「{task.name if task else task_id}」的军力候选池已连续 "
                f"{rounds} 轮（自 {since:%Y-%m-%d %H:%M} UTC 起）"
                f"筛不出能打的目标：{reading.attackable} 个候选的军力分数全部过期，"
                f"军力榜扫描可能跟不上 {hours:.1f} 小时的有效期。"
                f"攻击已停在这里，请确认扫描是否还在跑、或把有效期放宽",
                payload={
                    "task_id": task_id,
                    "mission_kind": MissionKind.BOT.value,
                    "attackable": reading.attackable,
                    "usable": 0,
                    "score_max_age_hours": hours,
                    "starved_since_utc": since.isoformat(),
                    "starved_rounds": rounds,
                    "oldest_skipped_at_utc": (
                        None
                        if reading.oldest_skipped_at is None
                        else reading.oldest_skipped_at.isoformat()
                    ),
                },
                logged_at_utc=now,
            )

    def _forget_a_starved_military_pool(self, task_id: int) -> None:
        """池子恢复（或这个任务不再参与调度）时把那一段的账清掉。"""
        self._stale_pool_since.pop(task_id, None)
        self._stale_pool_rounds.pop(task_id, None)
        self._stale_pool_warned_at.pop(task_id, None)

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

        ⚠️ **`MissionIdle` 走另一条路：什么都不做，绝不停用。** 它说的是「这会儿
        没活干」（军力池里没有读数新鲜的目标、航线刚好用完），是一档正常的间歇。
        按参数错误处理的话，一次正常的间歇会把整条链路自动停用到用户手动恢复为止。
        它也**不进连续失败计数**——那个计数只数「起来了却异常退出」的子进程，
        而这里连进程都没起。
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
                    ),
                    blind_scrolls=self._blind_scrolls(),
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
        except MissionIdle as exc:
            # 不停用、不记失败、不起进程。下一 tick 拿新事实重算即可。
            _LOGGER.info("%s 这一轮没活干：%s", task.name, exc)
            return False
        except MissionParamError as exc:
            # 类别按**异常类型**认，不按那句中文认：`NoFreeLineError` 说的是
            # 「这一刻没航线」，舰队飞回来就好了；别的都是配置填错，改之前重试
            # 一万次都一样。判据见 `domain.scheduler.DisabledRecovery`。
            self._disable_task(
                row,
                task,
                str(exc),
                recovery=(
                    DisabledRecovery.FREE_LINES
                    if isinstance(exc, NoFreeLineError)
                    else DisabledRecovery.MANUAL
                ),
            )
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
            #
            # ⚠️ **`exit_code is None` 必须落在「没采满」这一侧。** 手动停掉的那几档
            # 现在一律记 None（见 `MissionSupervisor.stop`），而 `None != 0` 为真，
            # 所以这句话本身已经是对的——但凡把它写成 `(exited.exit_code or 0) != 0`
            # 或者 `exited.exit_code in (None, 0)` 之类「None 当 0 看」的形状，
            # 就等于把一趟半截的榜单当成采满了，接着按它去派攻击。
            # 判据只认一件事：**只有 runner 自己报的 0 才算采满。**
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
        # 同上：一趟只读一次，整段共用。
        hold = self._unknown_line_hold()
        inflight: dict[Coordinate, int] = {}
        next_free: dict[Coordinate, datetime | None] = {}
        per_task: dict[int, TaskFacts] = {}
        # 这一趟数出来的军力候选池账目，末尾整份换上去（见 `_military_pool_readings`）。
        readings: dict[int, MilitaryPoolReading] = {}

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
                            now_utc=now, origin=item.coordinate, hold=hold
                        )
                        next_free[item.coordinate] = self._repository.next_line_free_at(
                            now_utc=now, origin=item.coordinate
                        )
                free = _free_lines_from(
                    task,
                    origins=origins,
                    inflight=inflight,
                    reserved_lines=config.reserved_lines,
                )
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
                # ⚠️ 这里算的是**这一轮真的能打的**那几个（主力 + 补位），不含
                # 「有分数但过期」那一堆。军力优先这一支的「有没有活干」就是这个数
                # （`domain.scheduler.bot_round_complete`），于是「候选全都顶着过期
                # 分数」自然落成「此刻没活干」，调度器会去跑军力榜扫描把池子刷新
                # ——而**不是**抛异常。抛出去的话 `_launch` 会把任务停用，用户不点
                # 「恢复」它就永远不跑，比拿旧数据打糟得多（见 `MissionIdle`）。
                reading = self._military_pool_reading(row)
                readings[task.task_id] = reading
                per_task[task.task_id] = replace(
                    base,
                    free_lines=free,
                    reports_due=self._reports_due(task, now, grace),
                    targets_remaining=reading.usable,
                    last_dispatch_at_utc=max(
                        (item for item in last_dispatches if item is not None), default=None
                    ),
                    next_line_free_at_utc=min(free_moments, default=None),
                )
                continue
            if task.origin not in inflight:
                inflight[task.origin] = self._repository.count_inflight(
                    now_utc=now, origin=task.origin, hold=hold
                )
                next_free[task.origin] = self._repository.next_line_free_at(
                    now_utc=now, origin=task.origin
                )
            target_kind = _TARGET_KIND[task.kind]
            per_task[task.task_id] = replace(
                base,
                free_lines=_free_lines_from(
                    task,
                    origins=None,
                    inflight=inflight,
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

        # 整份换上去而不是原地改：页面线程也会调 `_facts`（`snapshot`），
        # 原地改的话读的人可能撞见只填了一半的那一刻。
        self._military_pool_readings = readings
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
                # 只数这一轮真能打的（主力 + 补位）：军力优先这一支「有没有活干」
                # 就是这个数。
                return self._military_pool_reading(row).usable
            targets = self._bot_selection(row.params_json, self._origin_of(row))
        except MissionParamError as exc:
            # ⚠️ 这一处每 tick 都会走（页面轮询也会），所以停用必须走
            # `_disable_task`——它只在库里那两列真的变了时才写日志。
            self._disable_task(row, task, str(exc), recovery=DisabledRecovery.MANUAL)
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
        """只起一颗出发星球的一组目标，避免 runner 中途切星球留下半组状态。

        ⚠️ **「这一轮凑不出目标」抛的是 `MissionIdle` 而不是 `MissionParamError`。**
        后者会让 `_launch` 去调 `disable_mission_task`：任务被停用、挂上
        `disabled_reason`，用户不去页面点一次「恢复」就永远不跑。而这里的空手而归
        （池子里没有读数新鲜的目标、航线预算刚好耗尽）全都是**会自己好起来**的一档
        ——扫描刷新池子、舰队飞回来，下一 tick 就成立了。判成参数错误的代价是
        一整夜一发不派，比拿旧数据打糟得多。
        """
        assignments = self._military_assignments(row)
        if not assignments:
            raise MissionIdle("本轮没有可派遣的军力攻击目标")
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
            - self._repository.count_inflight(
                now_utc=self._clock(),
                origin=first_origin,
                hold=self._unknown_line_hold(),
            ),
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
        """军力池先排除本轮已处理目标，否则前 N 打完会静默卡住。

        ⚠️ **四步的先后是判据的一部分，不能重排**：

        1. 排除近 24 小时打过的与本轮已走完的（`_military_candidates`）；
        2. 按分数的新鲜度分成主力 / 补位 / 跳过（`_military_pool_reading`）；
        3. **主力**按军力取前 N（`military_pool`）；
        4. 前 N 没取满时，**补位**按距离补齐（`top_up_with_unrated`）。

        第 2 步必须在第 3 步之前：反过来的话，前 N 里若大半超期，这一轮实际可打的
        就只剩零星几个，而用户配的「候选 500 名」在页面上看不出任何差别。

        第 4 步必须在第 3 步**之后**、且不参与第 3 步的排序：补位没有分数，混进
        `strongest_first` 会让它们占掉前 N 的名额，于是「军力优先」在补位多的夜里
        退化成「随便打」。两条理由都写在 `domain.military_attack.top_up_with_unrated`
        与 `domain.target_order.split_by_freshness` 上。
        """
        reading = self._military_pool_reading(row)
        origins = self._military_origins(row)
        if not origins:
            raise MissionParamError("军力攻击没有启用的出发星球")
        take = _bot_top_n(row.params_json)
        pool = top_up_with_unrated(
            military_pool(
                reading.rated,
                take=take,
                maximum_score=_bot_max_score(row.params_json),
            ),
            reading.unrated,
            [item.coordinate for item in origins],
            take=take,
        )
        # 说实话的那一句：这一轮**还剩多少能打**、补位补了几个，而不是
        # 「整池里最旧的那条是哪年的」。
        #
        # ⚠️ 仍然**不从这里启动 RANKING**：两条链路会争同一只鼠标。刷新交给调度器
        # 的填空隙机制。变的只是「顶着假分数的不再拿来排序」，那一只鼠标都不多占。
        if reading.skipped:
            _LOGGER.info(
                "军力候选池：%d 个候选中 %d 个分数在 %.1f 小时内，"
                "%d 个从未读到分数（按距离补位），%d 个分数已过期并跳过（最旧 %s）",
                reading.attackable,
                len(reading.rated),
                reading.max_age.total_seconds() / 3600,
                len(reading.unrated),
                reading.skipped,
                reading.oldest_skipped_at,
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

    def _military_pool_reading(self, row: orm.MissionTaskRow) -> MilitaryPoolReading:
        """这一轮的候选池账目：**按分数的新鲜度分三堆，并数清楚跳过了多少**。

        这道闸门刻意放在**取前 N 名之前**（`_military_assignments` 里那段注释写了
        为什么），所以它住在这里而不是 `military_pool` 后面。

        ⚠️ **跳过的只有「有分数但过期」那一堆。** 完全没有分数的进补位池——它们
        不参与按军力排序，挤不掉任何人，判据与理由在
        `domain.target_order.split_by_freshness` 上。
        """
        max_age = _bot_score_max_age(_params(row.params_json))
        return MilitaryPoolReading(
            split=split_by_freshness(
                self._military_candidates(row), now=self._clock(), max_age=max_age
            ),
            max_age=max_age,
        )

    def _military_candidates(self, row: orm.MissionTaskRow) -> list[ScoredTarget]:
        """取前 N 名前，先排除本轮与「重复攻击间隔」之内已攻击的 bot。

        若先拿前 N 再排除已攻击目标，首批刚好都打过时军力任务会把候选池缩成
        空集，较低排名、从未攻击的目标永远轮不到。排除必须在 ``military_pool``
        的前面，随后再由距离给各出发星球分配。

        间隔默认 24 小时（用户口径 2026-08-15），可在攻击配置页上改——
        它是策略不是游戏规则，见 `DEFAULT_BOT_REVISIT`。
        """
        targets = self._scored_bot_targets()
        now = self._clock()
        facts_by_target = self._repository.bot_dispatch_facts_many(
            [target.coordinate for target in targets],
            since=row.round_started_at_utc,
            now_utc=now,
        )
        attacked_last_day = self._repository.attacked_bot_targets_since(
            now - self._bot_revisit_window()
        )
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
            return ranking_command(
                bot_limit=_ranking_bot_limit(params_json),
                blind_scrolls=self._blind_scrolls(),
            )
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

    def _blind_scrolls(self) -> int | None:
        """军力榜盲拖屏数。**填了数就锁死，留空则按实测自动标定。**

        取自**全局攻击配置**（攻击配置页），不是任务参数——用户口径
        （2026-08-17）：「盲拖数量需在攻击配置页可配置」。

        返回 `None` 的意思是「命令行上不带 `--blind-scrolls`」，runner 用
        `game.ranking_ui.BLIND_SCROLLS` 那个写死的默认值。样本攒不够时就走这条，
        行为与加这个框之前完全一致。**不在这里自己回落成一个数字**：默认值只该
        有一处，写第二遍日后必然漏改。

        手填的值优先于自动标定：它是覆盖，不是初值。

        配置行还没建出来时（老库、或者 `ensure_mission_rows()` 还没跑）当成留空：
        一个还没初始化的配置表说明不了「用户想改盲拖屏数」，为它把整条采集链路
        停掉是不成比例的。
        """
        choice = self._blind_scroll_decision()
        self._log_blind_scroll_change(choice)
        return choice.scrolls

    def _blind_scroll_decision(self) -> BlindScrollChoice:
        """这一刻盲拖屏数判成了什么，**以及凭什么**。判定本身不写任何日志。"""
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return BlindScrollChoice(None, source="default", samples=0)
        manual = _blind_scrolls(row.blind_scrolls)
        if manual is not None:
            # 手填时不去查库要样本：那次查询只为凑一句日志，而这条路上的答案
            # 与样本无关。
            return BlindScrollChoice(manual, source="manual", samples=0)
        return self._calibrated_blind_scrolls()

    def _log_blind_scroll_change(self, choice: BlindScrollChoice) -> None:
        """盲拖屏数的取值或来源变了才写一条。

        ⚠️ **补的是自动标定唯一的哑点。** `domain.ranking.bot_area_reached_message`
        上写着：那句实测日志的措辞一改，库里全部历史样本一次性作废，标定就
        **静悄悄退回写死的默认值**——页面上、日志里都看不出任何异常。采集那头
        照样打「盲拖 40 屏」，看上去和「本来就没攒够样本」一模一样。所以差别只能
        由**判定这一侧**说出来：这个数是手填的、是标定出来的、还是因为样本不够
        而根本没给出答案（连带说清此刻攒到了几条）。

        ⚠️ **只在变化时写。** `_blind_scrolls` 每次组军力榜命令行时都会走，而
        `command_for` 那条公开路径**页面保存配置时也会走**——每次都写的话，一天
        几十条重复的「盲拖屏数还是 62 屏」会把真正的那一次变化埋掉。

        ⚠️ **措辞只说判定，不说「这一趟拖了几屏」。** 走到这里未必真会起一轮采集：
        `command_for` 是页面拿来校验参数的，组出来的命令行随手就丢了。说成
        「本趟盲拖 N 屏」就是替一件没发生的事作证。
        """
        if choice == self._blind_scroll_choice:
            return
        self._blind_scroll_choice = choice
        record_system_log(
            "INFO",
            "application.mission_scheduler",
            f"军力榜盲拖屏数判定为 {_blind_scroll_verdict(choice)}",
            payload={
                "blind_scrolls": choice.scrolls,
                "source": choice.source,
                "measurements": choice.samples,
                "samples_required": BLIND_SCROLL_SAMPLES,
                "margin": BLIND_SCROLL_MARGIN,
                "hard_coded_default": BLIND_SCROLLS,
            },
            logged_at_utc=self._clock(),
        )

    def _calibrated_blind_scrolls(self) -> BlindScrollChoice:
        """从 `system_log` 里那些「翻了 N 屏到达 bot 区」反推盲拖屏数。

        ⚠️ **实测记录刻意没有自己的表或列。** 每趟采集本来就会把这句话写进
        `system_log`，那里已经攒着全部历史；再加一张表等于让同一件事有两份账，
        而两份账迟早对不上（其中一份还只有新版本才写）。

        多读一些行再筛：那句话不是每条日志都是，而 `recent_messages` 只做前缀
        匹配。读 `BLIND_SCROLL_SAMPLES` 的若干倍足以覆盖前缀相同但不是这句话的
        邻居，同时仍然只碰几十行。

        **样本条数要一起交出去**，那是日志唯一能分开「这台机器刚上线」和
        「反解规则失效了」的凭据：前者样本会一天天涨上去，后者恒为 0。
        """
        raw = self._repository.recent_system_log_messages(
            starts_with=BOT_AREA_REACHED_PREFIX, limit=BLIND_SCROLL_SAMPLES * 8
        )
        measurements = [value for value in map(bot_area_scrolls, raw) if value is not None]
        scrolls = calibrated_blind_scrolls(
            measurements, sample_size=BLIND_SCROLL_SAMPLES, margin=BLIND_SCROLL_MARGIN
        )
        return BlindScrollChoice(
            scrolls,
            source="calibrated" if scrolls is not None else "default",
            samples=len(measurements),
        )

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


def _free_lines_from(
    task: TaskSnapshot,
    *,
    origins: Sequence[AttackOrigin] | None,
    inflight: Mapping[Coordinate, int],
    reserved_lines: int,
) -> int:
    """这个任务此刻估算还剩几条空闲航线。

    **只有这一份判据。** `_facts`（决定要不要起一轮、`--max-dispatches` 传几）
    与「因航线不足停用后自动恢复」都问它。各写一份的话，放它出来用的尺子会和
    当初停用它的那把慢慢走散——走散之后要么放不出来，要么放出来就立刻再被停用，
    每 tick 一次。

    `origins` 非 None = 军力多出发点那一路：**每颗星球各算各的预算**，绝不拿
    全局保留数把它们合计校验一次；游戏的真实硬上限仍由 runner 的看屏闸门兜底。
    None 则走单出发星球那条，`reserved_lines` 在 `free_lines_for` 里生效。

    `inflight` 由调用方按出发星球缓存好（同一颗星球一次 tick 只查一次），
    这一层不查库。
    """
    if origins is not None:
        return sum(max(0, item.fleet_lines - inflight[item.coordinate]) for item in origins)
    return free_lines_for(
        task, inflight_from_origin=inflight[task.origin], reserved_lines=reserved_lines
    )


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


def _blind_scrolls(value: object) -> int | None:
    """军力榜开榜后先盲拖几屏。**留空 = 用 `BLIND_SCROLLS` 的默认值 40。**

    用户口径（2026-08-17）：「盲拖数量需在攻击配置页可配置」。

    ⚠️ **「没配」和「配了 0」是两回事，两个都合法。** 留空是「跟着默认走」；
    `0` 是用户真的敲进去的「一屏都别盲拖，从第一屏就开始检测 bot」——那是
    **最保守**的取值（多花几十次廉价检测，绝不可能拖过头），所以它必须放行，
    而不是像 `bot_limit` 那个 0 一样当成「把链路关掉」而拒绝。

    ⚠️ **不设上界**（用户口径 2026-08-17：「不需要这个限制」）。

    这里曾经拒掉大于 `BLIND_SCROLLS_MAX` 的值，理由是「再往上就证不出盲拖那一段
    够不到 bot 起点」。那个上界是从**已记录的最小实测屏数减余量**推出来的——
    也就是说它只反映**我们碰巧量到过什么**，不是游戏的事实。榜会随玩家增加变长，
    实测值也在涨，把一个观测下界当成硬闸门，结果就是用户明明知道该填 70 却填不进去。

    调大的代价仍然是真的（拖过 bot 起点会**静悄悄少采一截**，页面和日志都看不出），
    所以那句警告留在界面上；但它是**提示**，不是拦路。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # `bool` 是 `int` 的子类，得单独排掉（同 `_int_param` 那条）：`True` 会被
    # 当成盲拖 1 屏，而用户敲进去的根本不是一个屏数。
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError("盲拖屏数必须是整数；要用默认值就把它留空")
    try:
        scrolls = int(value)
    except ValueError as exc:
        raise MissionParamError(f"盲拖屏数不是整数：{value!r}") from exc
    if isinstance(value, float) and scrolls != value:
        raise MissionParamError(f"盲拖屏数必须是整数：{value!r}")
    if scrolls < 0:
        raise MissionParamError("盲拖屏数不能是负数；要用默认值就把它留空")
    return scrolls


def _optional_int(value: object, *, label: str) -> int | None:
    """把页面送上来的东西读成一个整数；留空返回 `None`。

    ⚠️ **「没配」和「配了某个数」是两回事，两个都合法。** 留空是「跟着代码里的
    默认值走」，所以空串、空白串、`None` 一律返回 `None`，而不是当成 0。

    `bool` 单独排掉（同 `_blind_scrolls`）：它是 `int` 的子类，`True` 会被读成
    1 分钟——而用户敲进去的根本不是一个时长。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError(f"{label}必须是整数；要用默认值就把它留空")
    try:
        number = int(value)
    except ValueError as exc:
        raise MissionParamError(f"{label}不是整数：{value!r}") from exc
    if isinstance(value, float) and number != value:
        raise MissionParamError(f"{label}必须是整数：{value!r}")
    return number


def _unknown_line_hold_minutes(value: object) -> int | None:
    """读不到飞行时间时，一条航线按派出时刻起算占多久（分钟）。
    **留空 = 用 `UNKNOWN_LINE_HOLD` 的默认值 90。**

    ## 两条边界

    - **至少 1 分钟。** 0 等于「读不到飞行时间就当没占航线」，而那正是被实机
      推翻掉的旧口径：每一发读不出飞行时间的派遣都让调度器凭空多出一条空闲
      航线，到点就起一轮、导航几十秒、撞上游戏的「同时派遣的舰队数量已达
      上限。」、退出、冷却、再来。整段理由在 `domain.report_wait.line_free_at`。
    - **必须严格小于 `MAX_REPORT_AGE`。** 那是「等一封战报等到什么时候就死心」的
      上界；航线占用超过它，就会出现「战报早就被判缺失、航线还锁着」的死角，
      而那条航线再没有任何事件能把它放开，只能等人来点「清理航线占用」。

    两条边界之间**故意留得很宽**，因为这个值调大调小都不会「错」，只是取舍不同：
    调小提高吞吐（估短了的代价有界且自纠，runner 的 `LineCapacityGate` 看屏复核
    兜着），调大更保守（代价是一次读不到就能把一条链路压住那么久）。
    """
    minutes = _optional_int(value, label="航线占用时长（分钟）")
    if minutes is None:
        return None
    if minutes < 1:
        raise MissionParamError(
            "航线占用时长至少 1 分钟：填 0 等于「读不到飞行时间就当没占航线」，"
            "而那会让调度器凭空多出空闲航线、反复撞游戏的舰队数量上限。"
        )
    ceiling = int(MAX_REPORT_AGE.total_seconds() // 60)
    if minutes >= ceiling:
        raise MissionParamError(
            f"航线占用时长必须短于 {ceiling} 分钟（放弃等战报的上界）："
            "再长就会出现「战报已判缺失、航线还锁着」的死角，只能靠人手动清理。"
        )
    return minutes


def _reconcile_cooldown_ceiling(grace_minutes: int) -> int:
    """翻信箱冷却的上界（分钟）：战报宽限期的一半，且至少 1。

    **写成一个函数而不是两处各算一遍**：页面上显示的上界和校验用的上界必须是
    同一个数，否则用户会填进一个输入框允许、后端却拒绝的值。
    """
    return max(1, grace_minutes // RECONCILE_COOLDOWN_GRACE_RATIO)


def _reconcile_cooldown_minutes(value: object, *, grace_minutes: int) -> int | None:
    """两次开工翻信箱之间至少隔多久（分钟）。
    **留空 = 用 `RECONCILE_COOLDOWN` 的默认值 15。**

    ## 两条边界

    - **0 合法**，而且它不是「关掉」：0 表示每一轮开工都翻信箱，也就是加这道
      冷却之前的行为。那是**最安全**的一侧（战报绝不会因为冷却而晚入库），
      代价只是每轮多花约 83 秒，所以必须放行。
    - **上界由宽限期定**：冷却窗口逼近 `report_grace_minutes` 就会自己制造
      「战报缺失」——一份战报最多晚一个冷却窗口才入库，而过了预计时间再等一个
      宽限期还读不到就判缺失。取宽限期的一半（`RECONCILE_COOLDOWN_GRACE_RATIO`），
      正是默认那对数（15 / 30）当初的取法。

    ⚠️ **上界跟着库里的宽限期走，不是写死的 15。** 用户把宽限期调到 60，
    冷却就该能填到 30；拿写死的数去卡，用户会发现两个框互相矛盾却看不出为什么。
    """
    minutes = _optional_int(value, label="翻信箱冷却（分钟）")
    if minutes is None:
        return None
    if minutes < 0:
        raise MissionParamError("翻信箱冷却不能是负数；要每轮都翻就填 0，要用默认值就留空")
    ceiling = _reconcile_cooldown_ceiling(grace_minutes)
    if minutes > ceiling:
        raise MissionParamError(
            f"翻信箱冷却最多 {ceiling} 分钟（= 战报宽限期 {grace_minutes} 分钟的一半）："
            "再长就会把战报拖到被判缺失，等于自己制造缺失。要翻得更疏就先把宽限期调大。"
        )
    return minutes


def _bot_revisit_hours(value: object) -> int | None:
    """同一个 bot 坐标多久之内不重复打（小时）。**留空 = 默认 24 小时。**

    ## 两条边界

    - **至少 1 小时。** 0 等于取消排除，而候选池是**军力降序**排的
      （`domain.target_order.strongest_first`）：排除一取消，榜首那一个就会被
      反复挑中、一夜的航线全烧在同一个目标上，而页面上只会显示一切正常。
      这跟「调小一点多榨几轮」不是一回事，所以 0 当场拒掉。
    - **最多 168 小时（一周）。** 再长就超过 bot 军力的刷新周期（周一 UTC+0），
      上一周的「打过」拦住这一周的候选，等于把候选池越锁越小。
    """
    hours = _optional_int(value, label="bot 重复攻击间隔（小时）")
    if hours is None:
        return None
    if hours < 1:
        raise MissionParamError(
            "bot 重复攻击间隔至少 1 小时：填 0 等于取消排除，而候选池按军力降序排，"
            "那会让榜首那一个被反复打、一夜的航线全烧在同一个目标上。"
        )
    if hours > BOT_REVISIT_MAX_HOURS:
        raise MissionParamError(
            f"bot 重复攻击间隔最多 {BOT_REVISIT_MAX_HOURS} 小时（一周）："
            "再长就跨过了 bot 军力的刷新周期，上一周打过的会一直拦着这一周的候选。"
        )
    return hours


def _report_scan_hours(value: object) -> int | None:
    """对账那一趟翻信箱最多往回读几个小时。**留空 = 用默认的 6 小时。**

    用户口径（2026-08-17）：「可能我的希望是，不要读那么多，毕竟数量是大几百封」
    「这个参数改为可配置，这样遇到活动我可以灵活调整」。

    ⚠️ **0 不是合法取值，这一点与 `_blind_scrolls` 相反。** 那边的 0 是最保守的
    一侧（多花几十次廉价检测）；这里的 0 意味着下界就是「此刻」，而信箱里每一封
    都比此刻旧——于是对账那一趟**一封都翻不到**，还一声不响。留空才是「跟着默认
    走」，0 只可能是手滑。

    上界 `REPORT_SCAN_HOURS_MAX` 不是策略上的界，是**防手滑与防溢出**：
    `now - timedelta(hours=值)` 在几十万年那个量级上会直接 `OverflowError`，
    把一趟对账变成 traceback。「配多大才有意义」那条留在页面上说（超过 6 小时之后
    多读回来的战报，对应的派遣早就掉出了 `due_attack_dispatches` 的追踪窗口，
    救它们是 `--exhaustive` 补录的活），这里不拦。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # `bool` 是 `int` 的子类，得单独排掉（同 `_blind_scrolls` 那条）。
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError("翻信箱时长必须是整数小时；要用默认值就把它留空")
    try:
        hours = int(value)
    except ValueError as exc:
        raise MissionParamError(f"翻信箱时长不是整数：{value!r}") from exc
    if isinstance(value, float) and hours != value:
        raise MissionParamError(f"翻信箱时长必须是整数小时：{value!r}")
    if hours < 1:
        raise MissionParamError("翻信箱时长至少 1 小时；要用默认值就把它留空，别填 0")
    if hours > REPORT_SCAN_HOURS_MAX:
        raise MissionParamError(
            f"翻信箱时长最多 {REPORT_SCAN_HOURS_MAX} 小时（{REPORT_SCAN_HOURS_MAX // 24} 天）；"
            "要救更早的战报请用手动补录（那一条不受这个下限约束）。"
        )
    return hours


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


#: 这个参数从前的名字。**只读不写**，为的是不动生产库里已经存着的那批
#: `params_json`——页面保存一次就会换成新名字，没保存过的照旧读得出来。
_LEGACY_SCORE_MAX_AGE_KEY = "rescan_after_hours"


def _bot_score_max_age(data: dict[str, Any]) -> timedelta:
    """军力**分数**的有效期。**它现在是硬判据，分数过期的目标一律不打。**

    ⚠️ **名字换过一次，别按旧名字理解它。** 它原先叫 `rescan_after_hours`，
    界面上写着「榜单超过 N 小时提示重扫」——那时它确实只是提示：日志里记一句，
    然后照样拿旧读数派遣。实机 2026-08-17 就栽在这上面：用户设的是 1 小时，
    而 `4:293:6` 顶着 3.6 小时前的读数被打了出去。现在它决定一个**分数**还能不能
    用来排序（`domain.target_order.score_is_fresh`），文案与字段名必须跟着变，
    否则同一个数字在页面上和判据里说的是两件事。

    ⚠️ **它管不到没有分数的目标。** 那些走补位池，照打不误——理由（旧分数害的是
    排序，不是战果）在 `domain.target_order` 的模块头上。

    旧名字仍然读得出来（`_LEGACY_SCORE_MAX_AGE_KEY`）：生产库里已经存着一批带旧
    键的 `params_json`，读不出来就会静默回落到默认值，把用户配好的数悄悄改掉。
    """
    value = data.get("score_max_age_hours", data.get(_LEGACY_SCORE_MAX_AGE_KEY))
    if value is None:
        return DEFAULT_SCORE_MAX_AGE
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise MissionParamError("军力分数有效期（小时）必须是正数")
    return timedelta(hours=float(value))


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
