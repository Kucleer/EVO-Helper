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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from evo_helper.application.ai_targeting import AiShadowObserver
from evo_helper.application.backfill import (
    BACKFILL_KINDS,
    REASON_STARTUP,
    BackfillCoordinator,
    BackfillCounts,
    BackfillMeasurement,
    BackfillPhase,
    BackfillRequest,
    BackfillState,
    SqlAlchemyBackfillCounts,
    default_since,
)
from evo_helper.application.mission_freeze import (
    FrozenOrigin,
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
    MILITARY_SPARE_FACTOR,
    AssignedTarget,
    AttackOrigin,
    MilitaryTier,
    assign_by_capacity_and_value,
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
    bot_area_rows,
    bot_area_scrolls,
    calibrated_blind_rows,
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
from evo_helper.domain.rules import cycle_start_utc
from evo_helper.domain.scheduler import (
    SCAN_YIELD_PATIENCE,
    Action,
    Decision,
    DisabledRecovery,
    MilitaryWindowPool,
    MissionKind,
    RunningProcess,
    ScanCooldown,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    account_free_lines,
    decide,
    fills_gaps,
    free_lines_for,
    has_work,
    looks_like_an_environment_fault,
    quota_day_start_utc,
    scan_cooldown_verdict,
    tasks_failing_together,
    within_schedule_window,
)
from evo_helper.domain.target_order import (
    DEFAULT_PROTECTION_EXCLUSION,
    DEFAULT_SCORE_MAX_AGE,
    DEFAULT_UNREADABLE_EXCLUSION,
    PROTECTION_EXCLUSION_MAX_HOURS,
    SCORE_MAX_AGE_MAX_HOURS,
    UNREADABLE_EXCLUSION_MAX_HOURS,
    WINDOW_POOL_FLOOR,
    MilitaryChoice,
    ScoredTarget,
    choose_by_military,
    most_valuable_first,
    score_is_fresh,
)
from evo_helper.domain.uptime import due_for_a_beat, opens_a_new_segment
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN,
    BLIND_SCROLL_MARGIN_ROWS,
    BLIND_SCROLL_ROWS,
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

#: 「从来没从这颗星球派过」在轮换排序里算作**最久远**。
#: 用 `datetime.min` 而不是 `None`：排序键里混着 `None` 会在 strict mypy 下要额外
#: 的分支，而一颗从没出过兵的星球，语义上本来就该排在所有出过兵的前面。
_NEVER = datetime.min.replace(tzinfo=UTC)

#: 用户能填进去的全账号航线上限的上界，纯防手滑。
#: 游戏那侧的真实上限在 9 附近（用户口径 2026-08-18），但助手不该替游戏写死一个数
#: ——版本会变、道具会变。这个数只挡住明显不可能的取值。
ACCOUNT_LINE_LIMIT_MAX = 99

#: 调度器**每 tick 都可能触发的那几条日志**共用的默认限流窗口。
#:
#: **它是为「反复跃迁」准备的，去重挡不住那一档。** 2026-08-18 01:00 那一小时里，
#: 任务「扫描+攻击 bot」自动停用 447 次、自动恢复 447 次、写了 1368 行日志，
#: 每一下都是真跃迁（库里那两列每次都在变），所以按「只在变化时写」去重一条都拦
#: 不下来。判据不是「有没有打日志」，是**出事时能不能只靠库里的日志定位**——
#: 一小时 1368 行同一句话定位不了任何东西。
#:
#: 取 120 秒是抄 `record_unrecognised_screen` 的先例（同一类问题：一个每 tick 都
#: 可能触发的东西）。它是**运维旋钮**，可在攻击配置页上改
#: （`military_attack_config.auto_toggle_log_seconds`）：调小排障时看得密、日志吵；
#: 调大库干净，代价是一次真实的反复跃迁被合并成看不出频率的一条。
#:
#: ⚠️ **2026-08-18 起它管的不止「自动停用 / 自动恢复」那一对。** 「军力候选池」
#: 与「军力读数放宽窗口」这两条走的是同一道闸（见 `_log_a_repeated_line`）。
#: 只做一个旋钮而不是两个，是因为两边的取舍**完全同向**：想把排障看密的人两边
#: 都想密，嫌库吵的人两边都嫌吵。旋钮多一个就多一个要解释、要配、要配错的地方。
#: 数据库那一列仍叫 `auto_toggle_log_seconds`（历史名，改名要迁移、要动页面，
#: 换不来任何用户可见的好处），页面上的标题已经跟着改成「调度器重复日志窗口」。
REPEATED_LOG_WINDOW = timedelta(seconds=120)

#: 用户能填进去的日志窗口上界（秒）。一小时——再长就把一整夜的抖动合并成寥寥几条。
REPEATED_LOG_MAX_SECONDS = 3600

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
class _RepeatedLine:
    """一条高频日志上一次**真的落库**时的账。见 `MissionScheduler._log_a_repeated_line`。

    ⚠️ `signature` 必须覆盖那条消息里出现的**每一个会变的数**。少覆盖一个，
    「和上一条一样」这句判断就是错的，而错的那一刻会被限流压成沉默——
    库里留下的是一条**内容已经不对了却假装还没变**的日志。这条不变量由
    `test_the_signature_covers_every_number_in_the_message` 钉住。
    """

    #: 上一次落库那条的状态签名。相等 = 这一次要写的和上一条一字不差。
    signature: tuple[object, ...]
    #: 上一次真的落库的时刻。限流窗口从它起算。
    written_at: datetime
    #: 上一次落库之后被压掉了几次。写下一条时必须交代出去，否则就是撒谎。
    suppressed: int


def _line_signature(message: str, payload: Mapping[str, Any]) -> tuple[object, ...]:
    """一条日志的内容签名：**消息全文 + payload 的全部键值**。

    刻意**不手写**一份「哪几个数算数」的清单。清单漏掉一个，限流就会把一条内容
    已经变了的日志压成沉默，而库里留着的上一条还在假装现状没变——那是本仓库最
    忌讳的那种缺陷（「日志说假话比不说更糟」）。由结构保证「凡是会被写出去的东西
    都在签名里」，比由纪律保证可靠得多。
    """
    return (message, tuple(sorted((key, repr(value)) for key, value in payload.items())))


def _spoken_span(span: timedelta) -> str:
    """把一段时长说成人话。给日志读，不参与任何判据。"""
    seconds = round(span.total_seconds())
    if seconds < 90:
        return f"{seconds} 秒"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.1f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def _merged_note(suppressed: int, span: timedelta, *, repeat_noun: str, changed: bool) -> str:
    """被限流压掉的那些**在下一条里的交代**。一条都没压掉时是空串。

    ⚠️ **两种情形分开措辞，因为主语不同。** 被压掉的那些按构造与**上一条落库的**
    一字不差（签名相等才会被压）：

    - `changed=False`（这一条与上一条内容相同，只是窗口到了）：主语就是眼前这条，
      说「已持续」是照实说。
    - `changed=True`（判定变了）：被压掉的是**旧**状态。这时候再说「这一判定已持续」
      就是把旧账算到新状态头上——所以措辞明确指回上一条。
    """
    if suppressed == 0:
        return ""
    if changed:
        return (
            f"（在此之前，上一条同样的{repeat_noun}又原样重复了 {suppressed} 次、"
            f"横跨 {_spoken_span(span)}，已合并；这一条是判定变了才写的）"
        )
    return f"（这一{repeat_noun}已持续 {_spoken_span(span)}，其间原样重复 {suppressed} 次，已合并）"


@dataclass(frozen=True)
class MilitaryPoolReading:
    """军力候选池这一次的账：**四步流水线每一步之后各剩多少，以及这一池有多旧**。

    做成一个结构而不是只返回一个列表，是因为日志得说实话。原先那句
    「军力候选池数据已过期（最旧读数 …）」既不说这一轮还剩多少能打，也不说被跳过
    的是哪一批——实机 2026-08-17 就是被它误导的：它报的「最旧读数」是三天前的
    某一条，而正要打的那个目标超期 3.6 小时，日志里一个字都没提。

    ⚠️ **它把整条选靶算一遍并把结果留下来**，选靶不许在别处再算第二份：
    「命令行按一份口径算、页面按另一份显示」这种分家 2026-08-15 已经撞过一次，
    症状是**一发都不派而且不报错**。
    """

    #: 第 1 步之后：排除本轮走完的、重复攻击间隔内打过的、以及刚撞上过保护期的，
    #: 剩下的全部候选。
    candidates: tuple[ScoredTarget, ...]
    #: 第 2--3 步与安全线的结果，**连「窗口有没有被放弃」一起带**（见
    #: `domain.target_order.MilitaryChoice`）。选靶的每一个中间量都从它里面取，
    #: 这一层不再自己算第二份。
    choice: MilitaryChoice
    #: 这一次用的**窗口门限**，写进日志好让用户对得上自己配的那个数
    #: （2026-08-23 起它是全局的一格：`military_attack_config.window_floor`，
    #: 从前是任务参数 `top_n`）。
    #:
    #: ⚠️ **它不再是「打前几名」。** 2026-08-18 起第 4 步按 `军力 ÷ 往返小时`
    #: 排序、军力硬截断取消，这个数只剩一个身份：第 3 步「窗口内够不够用」的尺子。
    window_floor: int
    #: 这一次用的窗口宽度（军力分数有效期）。**2026-08-18 起它真的会挡目标**，
    #: 只是挡不住整轮：窗口内不足 `window_floor` 个时窗口会被放弃，见 `widened`。
    max_age: timedelta
    #: 算这一次账的时刻。
    now: datetime

    @property
    def with_readings(self) -> tuple[ScoredTarget, ...]:
        """第 2 步之后：有军力读数的那些（分数与读取时刻都在）。"""
        return self.choice.with_readings

    @property
    def in_window(self) -> tuple[ScoredTarget, ...]:
        """第 3 步划出来的窗口内那批。**放宽与否都记**——「窗口内只有几个」
        正是告警里最要紧的那个数。
        """
        return self.choice.in_window

    @property
    def eligible(self) -> tuple[ScoredTarget, ...]:
        """过完安全线之后**这一轮有资格被打的全部**。

        ⚠️ **不是「选中的这几个」。** 军力硬截断 2026-08-18 取消之后，真正打谁由
        第 4 步按 `军力 ÷ 往返小时` 的得分连同航线预算一起定
        （`domain.military_attack.assign_by_capacity_and_value`），而那一步要知道
        从哪颗星球出发，这里还不知道。
        """
        return self.choice.eligible

    @property
    def widened(self) -> bool:
        """**这一轮的池子里混进了窗口外的旧读数吗。** 判据在 `MilitaryChoice.widened` 上。

        它同时喂两处：日志里那条 WARNING，和页面上
        `TaskStatus.WIDENED_SCORE_WINDOW` 那一档。**两处必须同源**——
        「日志里报了警而页面若无其事」和「页面标红了却查不到是哪一轮」
        都是同一种失败：用户还是得从攻击日志里一条一条对。
        """
        return self.choice.widened

    @property
    def attackable(self) -> int:
        """第 1 步之后还剩多少个。"""
        return len(self.candidates)

    @property
    def usable(self) -> int:
        """这一轮**还有多少个能打**：有军力读数的候选数（第 2 步之后）。

        ⚠️ **数的是全库还能打的总数，不是「这一轮派了几发」。** 后者由航线预算
        定（一轮几发），拿它当「还剩几个」会让页面上那个数几乎不动。
        页面据此判「有没有活干」（`domain.scheduler.bot_round_complete`），
        而「还有能打的」正是这个量。

        ⚠️ **没有军力读数的不算在内**（用户 2026-08-18 决定，理由见
        `domain.target_order`）。它们不再参与攻击，算进来等于说「还有活干」，
        而实际一个都派不出去。
        """
        return len(self.with_readings)

    @property
    def dropped_unrated(self) -> int:
        """第 2 步里**「从没上过军力榜、或说不清什么时候读的」**剔掉了几个。

        ⚠️ **上周期那一批不算在内**（`dropped_last_cycle` 单独数）。写成
        `attackable - usable` 的话，周一凌晨这个数会把一整库读过分数的目标算成
        「从未上榜」，而日志正文里写的就是「N 个从未上榜」——**那是句假话**，
        而且它会把人引到完全错的善后上（去查军力榜为什么漏了这些 bot，
        而真相只是该重扫一轮了）。
        """
        return self.attackable - self.usable - self.dropped_last_cycle

    @property
    def dropped_last_cycle(self) -> int:
        """第 2 步按**周期边界**剔掉了几个：读到过分数，只是那份读数属于上一个周期。

        bot 军力每周一 UTC+0 刷新，刷新那一刻全库读数同时作废
        （`domain.target_order.reading_is_from_this_cycle`）。
        """
        return len(self.choice.from_previous_cycles)

    @property
    def cycle_start(self) -> datetime:
        """本周期的起点（本周一 00:00 UTC）。只用于日志，让人对得上「作废的是哪一批」。"""
        return cycle_start_utc(self.now)

    @property
    def stale(self) -> int:
        """**这一池**里有几个分数已经超期，也就是**放宽窗口多捞到了几个**。

        窗口没被放弃时它恒为 0（池子只在窗口内取），所以这个数同时是
        「这一轮偏离了配置多远」的量度：`widened` 说的是「有没有」，它说的是「几个」。
        """
        return sum(
            1
            for target in self.eligible
            if not score_is_fresh(target, now=self.now, max_age=self.max_age)
        )

    @property
    def oldest_eligible_at(self) -> datetime | None:
        """这一池里最旧的那条读数。"""
        return min(
            (
                target.military_score_at_utc
                for target in self.eligible
                if target.military_score_at_utc is not None
            ),
            default=None,
        )

    @property
    def starved(self) -> bool:
        """有候选，却一个**本周期**的军力读数都没有。

        ⚠️ **2026-08-19 起它有两个成因，善后不同，日志里必须分开说**
        （`starved_by_the_cycle_boundary` 就是用来分的）：

        1. **从没上过军力榜**——军力榜还没扫到它们；
        2. **读数全属于上一个周期**——bot 军力每周一 UTC+0 刷新，刷新那一刻全库
           读数同时作废（`domain.target_order` 模块头第 2 步）。周一凌晨整池都是
           这一档。

        两者的补救其实是同一件事（等军力榜扫一轮），所以页面上共用
        `TaskStatus.MISSING_MILITARY_SCORES` 这一档；但**日志必须说清是哪一种**，
        否则周一凌晨那条会写着「N 个从未上榜」——一句假话，而且会把人引到
        「军力榜为什么漏了这些 bot」这条错路上。

        ⚠️ **和「一个候选都没有」必须分开。** 后者是完全正常的一档（已知 bot 全在
        24 小时冷却里或还在飞），拿它去报「军力榜没跟上」是句假话。

        ⚠️ **2026-08-18 之前这个判据是空转的。** 那时 `usable = 有读数的 + 没读数的`,
        而库里从来都有没读数的行（实测 628 个），所以它恒为假：那条 WARNING
        在生产库里一次都没响过，`MISSING_MILITARY_SCORES` 那个页面状态也基本
        显示不出来。没有读数的目标退出攻击之后，这个判据才第一次有了真的含义
        ——「军力榜还没扫过（或者刚清过一次坏读数），此刻一个都派不出去」。

        ⚠️ **窗口筛选（第 3 步）不参与这个判据，这是有意的。** 数的是第 2 步的
        余量：窗口把人筛光了不等于「没采集」——那种情形下窗口会被放弃、照样打得
        出去，该说的是 `widened`。把窗口算进来的话，页面会在「读数都旧了」时报
        「军力数据未采集」，用户于是去等一轮扫描，而助手其实正在正常派遣。
        """
        return self.attackable > 0 and self.usable == 0

    @property
    def starved_by_the_cycle_boundary(self) -> bool:
        """整池被挡光了，**而且挡它的是「读数早于本周期起点」这一条**。

        判据带上 `dropped_last_cycle > 0`，是为了不去替另一个成因说话：一池纯粹
        「从没上过军力榜」的候选也会 `starved`，那时报「上周期的读数作废了」
        就是在描述一件没发生过的事。
        """
        return self.starved and self.dropped_last_cycle > 0


@dataclass(frozen=True)
class ConfiguredOrigin:
    """`mission_task_origins` 上配着的一颗出发星球，**连「有没有勾上」一起带**。

    和 `domain.military_attack.AttackOrigin` 的区别只在这一个字段，但那个区别是
    要紧的：判据只该看到启用的那几颗（停用的星球不该分到目标），而**固化记录要连
    停用的一起记**——「用户把 2 号星停掉了」这件事在账里必须留得下来，否则事后
    翻记录只看得见「少了一颗星球」，分不清是停用还是删掉了。
    """

    coordinate: Coordinate
    fleet_lines: int
    enabled: bool


@dataclass(frozen=True)
class BlindRowChoice:
    """盲滚**行数**这一次判成了什么，**以及凭什么**。

    与 `BlindScrollChoice` 同形（下面每条理由都是从那边搬过来的，换成行之后一条
    都没失效），区别只在单位：滚轮没有「屏」这个概念，拨的是格，而行是唯一同时
    量得住慢拖和滚轮的单位。

    做成一个结构而不是只返回一个 `int | None`，是因为答案本身分不清三种来源，
    而三种的善后完全不同：手填的要去攻击配置页上改，标定出来的说明这条反馈回路
    还活着，**「没给出答案」则可能是刚上线、也可能是反解规则已经失效**——后者
    正是 `domain.ranking.bot_area_reached_rows_message` 上警告过的那种静默退化。
    `samples` 就是分开这两者的那个数：刚上线时它会一天天涨，失效时它恒为 0。

    ⚠️ 行版这里还多一档静默失效：库里存着一整年**屏版**正文，前缀和行版一模一样，
    只差单位那个字。屏版样本会被 `domain.ranking.bot_area_rows` 整条丢掉（有意的），
    于是切换口径之后 `samples` 会先掉回 0 再重新涨——那一段看起来和「反解失效」
    长得一样，唯一分得开的办法就是这个数在往上走。
    """

    #: 判定结果。`None` = 不往命令行上加 `--blind-rows`，采集用写死的默认值。
    rows: int | None
    #: `manual`（攻击配置页手填）/ `calibrated`（按实测标定）/ `default`（没答案）。
    source: str
    #: 从 `system_log` 里反解出来的**行版**实测样本条数。手填那一支不查库，恒为 0。
    samples: int


def _blind_row_verdict(choice: BlindRowChoice) -> str:
    """把一次判定念成人话。**三种来源各一句，绝不含糊成一句通用的。**"""
    if choice.source == "manual":
        return f"{choice.rows} 行（攻击配置页上手填的，标定不再参与）"
    if choice.source == "calibrated":
        return (
            f"{choice.rows} 行（按最近 {BLIND_SCROLL_SAMPLES} 次实测标定，"
            f"当前共有 {choice.samples} 条行版实测样本）"
        )
    return (
        f"「不指定」，采集将用写死的默认值 {BLIND_SCROLL_ROWS} 行"
        f"（行版实测样本只有 {choice.samples} 条，自动标定要 {BLIND_SCROLL_SAMPLES} 条）"
    )


@dataclass(frozen=True)
class BlindScrollChoice:
    """盲拖屏数这一次判成了什么，**以及凭什么**。

    ⚠️ **口径已改行（2026-08-22），这一套眼下没有调用点**，留着的理由见
    `MissionScheduler._blind_scrolls`：`military_attack_config.blind_scrolls`
    那一列和攻击配置页上那个框都还在，它们是这次改动的回滚杠杆。

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


class LaunchOutcome(Enum):
    """`MissionScheduler._launch()` 这一次的结果。**四态，因为那里真的有四件事。**

    原先这四件事全都挤在一个 `bool` 里（起来了 = True，其余三件 = False），而
    `_act()` 的 `return not self._launch(...)` 把「其余三件」一律翻成「值得再算一
    次」。后果是**每一个「没活干」的 tick 都把 `_step` 转满 `len(MissionKind)` = 4
    圈，每圈一次完整的 `_facts()`**（本地 16 条 SQL / 约 194 ms，生产实测 0.32 秒），
    而那 3 圈额外的**一发都派不出去**——理由见 `worth_another_round`。

    ⚠️ **`VOID` 和 `IDLE` 对 `_act` 是同一个答案，仍然不许合并。** 「决策指向的任务
    已经不在了」和「这条链路此刻正常地没活干」在排障时是两回事：前者只该在用户刚删
    过任务的那一瞬出现，出现在别的时候说明决策与起进程之间还有别的东西在改库；后者
    每分钟都可能有几十次。合成一个成员，日志和用例就都分不出来了。
    """

    #: 子进程真的起来了，`mission_runs` 里也已经落了一行。
    STARTED = "started"
    #: 决策已作废：`mission_task(...)` 读回来是空的，用户在这期间把它删了。
    VOID = "void"
    #: 这会儿没活干（`MissionIdle`）：军力池凑不出目标、航线预算刚好用完。
    #: **不停用、不记失败、不起进程**，下一 tick 拿新事实重算即可。
    IDLE = "idle"
    #: 刚把这条链路**就地停用**了（`MissionParamError`）：参数不合格，用户不点
    #: 一次「恢复」它就不再参与调度。
    DISABLED = "disabled"

    @property
    def worth_another_round(self) -> bool:
        """本 tick 值得再走一遍「读事实 → 判 → 起」吗。

        **只有 `DISABLED`。** 判据是「候选集变了吗」，而只有停用会真的改它：
        `disabled_reason` 落了库，`decide()` 下一圈的候选里就少一个，顺位该立刻让给
        下一条链路（`test_a_bad_radius_yields_its_turn_in_the_same_tick`）。

        另外三档都不改候选集，于是**再走一遍必然挑中同一个任务、得到同一个结果**
        ——`decide()` 是 `(tasks, facts)` 的纯函数，而这三档一个字都没往库里写。
        `IDLE` 那一档 2026-08-18 实测过：4 圈跑完 `launcher.kinds == []`，排在后面的
        SCAN 一次都没顶上。所以「`IDLE` 之后本 tick 就到此为止」**不是把让位取消了**，
        今天那个位本来就没让出去；下一条链路等下一个 tick（1 秒）。

        ⚠️ **写成白名单（`is DISABLED`）而不是黑名单（`is not IDLE`）。** 将来加第五
        个成员时，漏改这里的后果必须是「少重算一次」——浪费一个 tick，页面上看不出
        来；黑名单漏改的后果是每秒空转四圈重算全库候选池，也就是这次修的东西原样
        复发。
        """
        return self is LaunchOutcome.DISABLED


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
        ai_shadow: AiShadowObserver | None = None,
    ) -> None:
        self._repository = repository
        self._supervisor = supervisor
        self._clock = clock
        #: AI 选靶影子观测器。**组装点注入**（`web.app.create_persistent_app`
        #: 拿真 repository 和 Settings 建）：默认 None = 整条观测不存在，
        #: `_observe_ai_shadow` 第一行就返回，零开销（需求第八节第 5 条）。
        self._ai_shadow = ai_shadow
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
        #: 挂机心跳：现在正往哪一段里落拍、上一拍是什么时候。
        #:
        #: **只记在内存里，而且进程一起来就是空的**（判据见
        #: `domain.uptime.opens_a_new_segment`）：从库里找上一段接回来，会把控制台
        #: 重启那几十秒算成挂机。宁可少算一拍，也不让这个数说大话。
        self._uptime_segment_id: int | None = None
        self._uptime_last_beat: datetime | None = None
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
        #: 不能在写入前几行后就被新出现的 bot 候选抢占；采够**窗口门限**
        #: （`military_attack_config.window_floor`，全局一格）后反过来
        #: 也必须先启动该任务，再交还普通优先级排序。
        self._military_ranking_batch_task_id: int | None = None
        # 点「开始」后军力任务只使用这一份档位；运行中修改全局配置不会让
        # 固化记录与实际派遣分家。停掉后才允许下一轮取新配置。
        self._active_military_tiers_json: str | None = None
        #: 每个军力 bot 任务这一 tick 数出来的候选池账目。由 `_facts` 整份重新赋值
        #: （不是原地改），因为页面线程也会调 `_facts`：整份换掉的话，读的人拿到的
        #: 要么是上一份、要么是新的一份，不会撞见改到一半的中间态。
        self._military_pool_readings: dict[int, MilitaryPoolReading] = {}
        #: **按任务**记：上一次判「让位还有用」时这个任务窗口内的数量。
        #: 账在 `_scan_can_still_help` 与 `domain.scheduler.yields_to_a_scan` 上。
        self._yield_watermark: dict[int, int] = {}
        #: **按任务**记：数量停滞是从哪一刻开始的。缺键 = 上一趟还在涨。
        self._yield_stalled_since: dict[int, datetime] = {}
        #: 每个军力 bot 任务「池子全超期」这一段是从什么时候开始的，以及连着看到了
        #: 几个 tick。只记在内存里，理由同上面那几份：判据每 tick 现算，这里记的
        #: 只是「这一段持续多久了」，好让 WARNING 不至于每秒刷一条。
        self._stale_pool_since: dict[int, datetime] = {}
        self._stale_pool_rounds: dict[int, int] = {}
        #: 上一次为这一段写过 WARNING 的时刻。用来把重复告警压到每
        #: `STALE_POOL_WARNING_AFTER` 一条——不是只报一次：一整夜的停摆该在日志里
        #: 留下持续的痕迹，只报一次的话，翻日志的人会以为它早就恢复了。
        self._stale_pool_warned_at: dict[int, datetime] = {}
        #: 上一次判定出来的盲滚行数取值与它的来源，用来把日志压成「只在变化时写」。
        #: 见 `_blind_rows`。
        self._blind_row_choice: BlindRowChoice | None = None
        #: 屏口径那一份同样的账。**眼下没有调用点**（口径已改行），随
        #: `_blind_scrolls` 一起留着当回滚杠杆。
        self._blind_scroll_choice: BlindScrollChoice | None = None
        #: 每 tick 都可能触发的那几条日志的限流账：`(任务, 日志种类)` → `_RepeatedLine`。
        #: 见 `_log_a_repeated_line`。眼下住着四种：自动停用、自动恢复、军力候选池、
        #: 军力读数放宽窗口。
        #: 只记在内存里——重启之后的第一条本来就该落库，它是新一轮运行里的第一手事实。
        self._repeated_lines: dict[tuple[int, str], _RepeatedLine] = {}

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
                        # ⚠️ **军力攻击的真相在这里，不在上面那两个字段里。**
                        # `mission_tasks.origin_*` / `fleet_lines` 是加多出发点之前
                        # 留下的残值，`replace_mission_origins` 从不回写它们；照着
                        # 它们记账，固化记录会写出「出发 4:277:15 · 航线 7」，
                        # 而用户配的是 `4:277:15=6` + `9:250:8=2`（生产实证
                        # 2026-08-18）。其余链路没有多出发点，记 `()`（「确实没有」，
                        # 与旧行那个 `None`「没得比」是两回事）。
                        origins=self._frozen_origins(row),
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
        acknowledged_from = self._backfill.state()
        self._backfill.acknowledge()
        self._log_backfill_transition(acknowledged_from, "用户点了「开始」，顺带确认上一批摘要")
        if reconcile:
            requested_from = self._backfill.state()
            self._backfill.request_batch(
                [
                    BackfillRequest(kind=kind, since=default_since(now), reason=REASON_STARTUP)
                    for kind in BACKFILL_KINDS
                ]
            )
            self._log_backfill_transition(requested_from, "用户点了「开始」，先排一批启动对账")
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
            self._cancel_backfill("控制台关闭时清场")

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
            self._cancel_backfill("用户点了红条上的「强制结束」")
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

        ⚠️ **这里不再校验有效期与窗口门限。** 2026-08-23 起那两格是全局的
        （`military_attack_config`），由 `validate_score_max_age_hours` /
        `validate_window_floor` 在攻击配置页那条路上把关。**留在这里反而有害**：
        存量任务的 `params_json` 里还存着那两个旧键，照旧校验等于让一个**已经不
        生效**的值把保存整个拦下来，而报出来的错说的是一个用户在任务页上再也看不到
        的框。旧键的善后在 `_legacy_window_keys`：忽略，并在派遣时告警。
        """
        if not _bot_by_military(params_json):
            return
        maximum = _bot_max_score(params_json)
        if maximum is not None and maximum < 0:
            raise MissionParamError("max_score 不能小于 0")

    def validate_military_tiers(self, tiers: list[dict[str, Any]]) -> tuple[MilitaryTier, ...]:
        """校验全局攻击档位；任务参数不再携带档位。"""
        return _bot_tiers({"tiers": tiers})

    def validate_blind_scroll_rows(self, value: object) -> int | None:
        """校验攻击配置页上那个「盲滚行数」。同 `validate_military_tiers`：
        页面在**写库之前**用调度器自己这把尺子量一遍。

        返回 `None` 表示留空——那不是 0，是「跟着 `BLIND_SCROLL_ROWS`（700 行）
        的默认值走」。

        形状与 `validate_blind_scrolls` 逐字一致，只是单位从屏换成行：页面那一侧
        两个框并排放着，校验入口的形状不同只会让保存那条路上多一处特例。
        """
        return _blind_scroll_rows(value)

    def validate_blind_scrolls(self, value: object) -> int | None:
        """校验攻击配置页上那个「盲拖屏数」。同 `validate_military_tiers`：
        页面在**写库之前**用调度器自己这把尺子量一遍。

        返回 `None` 表示留空——那不是 0，是「跟着 `BLIND_SCROLLS` 的默认值走」。

        ⚠️ **屏口径已被行口径取代（2026-08-22），但这个校验还是活的**：那一列和
        页面上那个框都留着当回滚杠杆，页面照旧要在写库之前量一遍。眼下写进去的
        值不再上命令行——真正驱动盲滚的是 `validate_blind_scroll_rows` 那一个。
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

    def validate_protection_exclusion_hours(self, value: object) -> int | None:
        """校验攻击配置页上那个「撞上保护期之后排除多久」。留空返回 `None`。"""
        return _protection_exclusion_hours(value)

    def validate_unreadable_exclusion_hours(self, value: object) -> int | None:
        """校验攻击配置页上那个「面板名读不出之后排除多久」。留空返回 `None`。"""
        return _unreadable_exclusion_hours(value)

    def validate_score_max_age_hours(self, value: object) -> float | None:
        """校验攻击配置页上那个「军力分数有效期」。留空返回 `None`。

        **允许小数**（1.5 小时是合法取值），其余同 `validate_blind_scrolls`：
        页面在写库之前用调度器自己这把尺子量一遍，两边不许各判一次。
        """
        return _score_max_age_hours(value)

    def validate_window_floor(self, value: object) -> int | None:
        """校验攻击配置页上那个「窗口门限」。留空返回 `None`。"""
        return _window_floor_value(value)

    def validate_account_line_limit(self, value: object) -> int | None:
        """校验攻击配置页上那个「全账号航线上限」。留空返回 `None`。"""
        return _account_line_limit(value)

    def validate_auto_toggle_log_seconds(self, value: object) -> int | None:
        """校验攻击配置页上那个「自动停用/恢复日志的限流窗口」。留空返回 `None`。"""
        return _auto_toggle_log_seconds(value)

    def account_line_limit(self) -> int | None:
        """全账号此刻认的航线上限，**没配就是 `None`**。页面显示那句提示时读它。

        公开出来是为了让页面上那句提示和调度判据量**同一把尺子**：页面另读一次
        的话，用户在攻击配置页把上限改成 6 之后，任务页可能还写着别的数，
        而那句提示存在的全部意义就是让他一眼看出配超了没有。
        """
        return self._account_line_limit()

    def unknown_line_hold(self) -> timedelta:
        """飞行时间读不到时，一条航线占多久。**读侧的唯一入口。**

        公开出来是给「清理航线占用」那条路用的（`web.persistent_service`）：
        它和 `count_inflight` 必须量同一把尺子，否则页面上写着「占着 3 条」、
        按钮却报「放开了 0 条」，而那个数字是这个按钮唯一的可见回执。

        数据概览页也读它：那一页画的航线格子必须按调度器认的 `hold` 去判占用，
        写死 90 分钟的话，用户在攻击配置页把它改成 45 之后，页面会继续把一批
        早该放手的派遣画成「占着」。
        """
        return self._unknown_line_hold()

    def configured_line_origins(self) -> tuple[ConfiguredOrigin, ...]:
        """**参与调度的军力攻击任务**此刻配着的那几颗出发星球，含停用的。
        **读侧的公开入口**，同 `unknown_line_hold`。

        公开出来是给数据概览页画航线格子用的（需求文档 8.3）：格子数必须按
        **每颗星球各自配置的航线数**画，而那个数只有一个来源
        （`mission_task_origins.fleet_lines`，实测 `4:277:15` 是 5 条、
        `9:250:8` 是 4 条）。⚠️ 页面**不许**按占用数画格子——原型第一版按
        「在飞 + 时长未知」画，于是一颗配了 4 条的星球画出了 7 格；也**不许**
        自己去读那张表，`planet_id` 与坐标快照谁优先这条规则只该有一份
        （见 `_configured_origins`）。

        同一颗星球被两个任务配着时，航线数**相加**：它们抢的是同一颗星球上的
        同一批航线，分成两行画等于把一条航线画两遍。眼下只有一个军力任务，
        这一条是为多任务那天先把语义定死。

        按坐标排序，好让页面上的卡片次序不随库里的行序抖动。
        """
        merged: dict[Coordinate, ConfiguredOrigin] = {}
        for row in self._repository.mission_tasks():
            if MissionKind(row.kind) is not MissionKind.BOT or not _bot_by_military(
                row.params_json
            ):
                continue
            for item in self._configured_origins(row):
                seen = merged.get(item.coordinate)
                if seen is None:
                    merged[item.coordinate] = item
                    continue
                merged[item.coordinate] = ConfiguredOrigin(
                    coordinate=item.coordinate,
                    fleet_lines=seen.fleet_lines + item.fleet_lines,
                    # 任一处启用就算启用：停用的那一份不该把另一个任务真的会派
                    # 舰的那颗星球说成「没在用」。
                    enabled=seen.enabled or item.enabled,
                )
        return tuple(
            merged[key]
            for key in sorted(merged, key=lambda item: (item.galaxy, item.system, item.position))
        )

    def _configured_line_total(self) -> int | None:
        """此刻账号一共配着几条航线（只数启用的出发星球）。**一条都没配就 None。**

        起子进程时把它写进 `mission_runs.configured_lines`，页面据此算利用率的分母。

        ⚠️ **返回 None 而不是 0**：没有任何配着的出发星球时，「配了 0 条」这句话
        和「不知道」在库里长得一模一样，而下游拿 0 当分母的乘数会把那一天的利用率
        整段抹成「—」。None 的意思是「这一行说不出线数」，页面会退回下界推算
        （`domain.overview.period_lines`）。

        走 `configured_line_origins()` 而不是自己读表：`planet_id` 与坐标快照谁
        优先、同一颗星球被两个任务配着要不要相加，这些规则只该有一份。
        """
        total = sum(item.fleet_lines for item in self.configured_line_origins() if item.enabled)
        return total or None

    def military_candidate_pool(self) -> tuple[ScoredTarget, ...]:
        """此刻**还打得动**的那些 bot 目标。**读侧的公开入口**，同上。

        公开出来是给数据概览页那块「候选池」用的。⚠️ 页面**不许**自己写一遍
        筛选（需求文档 8.7）：排除近期打过的、刚撞过 8 小时保护期的、本轮已走
        完的，这三条都是策略（两个窗口都能在攻击配置页上改），页面另算一份的话
        它显示的池子和调度器下一轮真的会挑的那批不是同一个东西。

        没有军力攻击任务时返回空元组——那是「没有这条链路」，不是「池子空了」，
        页面上要分得开。
        """
        for row in self._repository.mission_tasks():
            if MissionKind(row.kind) is MissionKind.BOT and _bot_by_military(row.params_json):
                return tuple(self._military_candidates(row))
        return ()

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

    def _protection_exclusion_window(self) -> timedelta:
        """撞上保护期之后，这个坐标多久之内不进候选池。

        ⚠️ 它排除的起点是**撞上的时刻**（`bot_targets.protection_seen_at_utc`），
        不是保护期的起点——后者根本不可知。整段取舍在 `DEFAULT_PROTECTION_EXCLUSION`。
        """
        hours = self._knob("protection_exclusion_hours")
        if hours is None:
            return DEFAULT_PROTECTION_EXCLUSION
        window = timedelta(hours=hours)
        record_knob_override(
            "protection_exclusion",
            source=__name__,
            effective=window,
            default=DEFAULT_PROTECTION_EXCLUSION,
            detail="撞上保护期的坐标在这段时间内不进候选池",
        )
        return window

    def _unreadable_exclusion_window(self) -> timedelta:
        """面板名读不出之后，这个坐标多久之内不进候选池。

        ⚠️ 起点是**读不出的那一刻**（`bot_targets.unreadable_seen_at_utc`）。
        整段取舍（为什么是 6 小时、为什么排除有尽头）在
        `domain.target_order.DEFAULT_UNREADABLE_EXCLUSION`。
        """
        hours = self._knob("unreadable_exclusion_hours")
        if hours is None:
            return DEFAULT_UNREADABLE_EXCLUSION
        window = timedelta(hours=hours)
        record_knob_override(
            "unreadable_exclusion",
            source=__name__,
            effective=window,
            default=DEFAULT_UNREADABLE_EXCLUSION,
            detail="面板名读不出的坐标在这段时间内不进候选池",
        )
        return window

    def _score_max_age(self) -> timedelta:
        """军力读数「算不算新」的门槛，也就是选靶第 3 步那扇**窗口**的宽度。

        ⚠️ **它是全局的**（`military_attack_config.score_max_age_hours`），
        2026-08-23 起不再按任务、也就是不再按出发点银河系各配一份。用户口径
        （2026-08-23）：「军力攻击的有效期 门限 改为全局设置，不再根据单个星系进行
        调整」。整段理由在 `domain.target_order.DEFAULT_SCORE_MAX_AGE`——最要紧的
        一句是：这个数约束的是**军力榜扫描的节奏**，而榜是一趟扫完全宇宙的，
        与舰队从哪个银河系出发无关。

        ⚠️ **读侧只能有这一处。** 从前它是从 `params_json` 里各读一份，于是「页面
        显示的窗口」和「派遣真正用的窗口」有两处算式；这条链路每一次事故都是从
        「同一个数有两份算法」开始的。

        留空 = `DEFAULT_SCORE_MAX_AGE`（2 小时）；填了就往 `system_log` 落一条
        「配置生效」，好让排障的人不必照着代码里那个 2 小时去推现场。
        """
        hours = self._float_knob("score_max_age_hours")
        if hours is None:
            return DEFAULT_SCORE_MAX_AGE
        window = timedelta(hours=hours)
        record_knob_override(
            "score_max_age",
            source=__name__,
            effective=window,
            default=DEFAULT_SCORE_MAX_AGE,
            detail="军力读数落在这段时间之内才算新（选靶第 3 步的窗口宽度）",
        )
        return window

    def _window_floor(self) -> int:
        """选靶第 3 步的**窗口门限**：窗口内至少几个，这一轮才肯只用窗口内的。

        ⚠️ **它不决定打谁**（军力硬截断 2026-08-18 就取消了），只决定「这一轮肯不肯
        只信新数据」。整段理由在 `domain.target_order.WINDOW_POOL_FLOOR`。

        ⚠️ **它和上面那个有效期是同一道判据的两半，所以一起搬成了全局。**
        一半全局一半按星系，等于让同一道判据的两半各说一套——「窗口多宽」按全局、
        「窗口内够不够用」按星系，那种组合谁也解释不清。

        留空 = `WINDOW_POOL_FLOOR`（100）。
        """
        floor = self._knob("window_floor")
        if floor is None:
            return WINDOW_POOL_FLOOR
        record_knob_override(
            "window_floor",
            source=__name__,
            effective=floor,
            default=WINDOW_POOL_FLOOR,
            detail="窗口内至少这么多个目标，这一轮才肯只用窗口内的（选靶第 3 步）",
        )
        return floor

    def _account_line_limit(self) -> int | None:
        """全账号同时能在飞的舰队上限。**留空 = 不施加这道闸，而不是某个默认值。**

        用户口径（2026-08-18）：「账号的默认权限不应在代码中进行配置，直接用航线
        限制就可以了，因为实际通过科技升级，使用道具，人为占用，都会影响到留给你的
        航线数量」。整段理由写在 `domain.scheduler.account_free_lines` 上，这里只重复
        最要紧的那一条：**别顺手补一个代码默认值**——真实可用航线是浮动的，
        写死 9 是错的，写死 6 也是错的。

        ⚠️ **也不是 `scheduler_config.fleet_line_limit`。** 那一列的含义早已降级成
        「任务没填航线数时用几条」，复用会造出第二份真相。
        """
        limit = self._knob("account_line_limit")
        if limit is None:
            return None
        record_knob_override(
            "account_line_limit",
            source=__name__,
            # `default=None` 是照实说：这个旋钮**没有**代码默认值。所以「用户填了
            # 一个数」本身就是要留痕的那件事——排障的人得知道助手这一夜是按几条
            # 在算的，而代码里翻不出这个数。
            effective=limit,
            default=None,
            detail="全账号同时在飞的舰队上限；留空则不施加账号那道闸，只按每颗星球的预算算",
        )
        return limit

    def _repeated_log_window(self) -> timedelta:
        """调度器高频日志的限流窗口。**留空 = `REPEATED_LOG_WINDOW`。**

        一个旋钮管四条（自动停用、自动恢复、军力候选池、军力读数放宽窗口）：
        理由写在 `REPEATED_LOG_WINDOW` 上——两边的取舍完全同向，拆成两个旋钮
        只是多一个要配错的地方。
        """
        seconds = self._knob("auto_toggle_log_seconds")
        if seconds is None:
            return REPEATED_LOG_WINDOW
        window = timedelta(seconds=seconds)
        record_knob_override(
            "repeated_log_window",
            source=__name__,
            effective=window,
            default=REPEATED_LOG_WINDOW,
            detail="同一个任务的同一条重复日志在这段时间里最多落一条",
        )
        return window

    def _account_free_lines(
        self, now: datetime, *, hold: timedelta, reserved_lines: int
    ) -> int | None:
        """全账号这一刻还剩几条航线。**账号那道闸的唯一算处。** `None` = 这道闸不生效。

        散成两份的话，`has_work` 与 `_launch` 会因为一个多减了 `reserved_lines`、
        另一个没减而慢慢走散——那正是 `_free_lines_from` 的文档一直在说的那件事。

        **没配上限时连查都不查。** `count_inflight_total` 是一次全表 count，而
        tick 每秒一次；上限留空时那个数不参与任何判据，查了只是白付一次查询。
        这也让「没配这个旋钮」的库的查询次数与加这道闸之前**完全一致**。
        """
        limit = self._account_line_limit()
        if limit is None:
            return None
        return account_free_lines(
            account_limit=limit,
            inflight_total=self._repository.count_inflight_total(now_utc=now, hold=hold),
            reserved_lines=reserved_lines,
        )

    def _knob(self, column: str) -> int | None:
        """全局攻击配置上某个旋钮的原始值；没配 / 配置行不存在都返回 None。"""
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return None
        value = getattr(row, column, None)
        return None if value is None else int(value)

    def _float_knob(self, column: str) -> float | None:
        """同 `_knob`，但**不取整**。

        ⚠️ **有效期那一格必须走这一条。** 它一直允许填 1.5 小时（页面上步长 0.5），
        而 `_knob` 里那个 `int()` 会把 1.5 悄悄变成 1——用户配的窗口窄了三分之一，
        日志里写的却是 1.0，看起来完全正常。
        """
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return None
        value = getattr(row, column, None)
        return None if value is None else float(value)

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
            before = self._backfill.state()
            self._backfill.poll(self._measure_backfill)
            self._log_backfill_transition(before, "补录子进程自己退了")
            self._advance_backfill()
            if not self._enabled:
                return
            # 挂机心跳落在这道闸**之后**：这个指标问的是「调度器开着多久」，
            # 而不是「控制台的进程活着多久」。用户按了「停止」之后页面开着一整夜，
            # 那不是挂机——那段时间一发都不会派出去，把它算进挂机时长，
            # 「利用率为什么低」这个问题就又没人回答了。
            self._beat_uptime()
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

    # -- 挂机心跳 --------------------------------------------------------------
    #
    # 「挂机运行时长」的唯一写入点。判据全在 `domain.uptime`（该不该落一拍、
    # 要不要另开一段），这里只负责按判据落行。
    #
    # ⚠️ **为什么必须有这个**：现在库里查不出「那段时间到底开没开机」。
    # `state_events` 全表只有 1 行、写它的路径早删了；而拿 `mission_runs` 的轮次
    # 覆盖去冒充挂机时长会说假话——实测 2026-08-20 有 6 小时的空隙是「开着但没活
    # 干」（扫描间隔挡住 RANKING、`waiting_for_a_line` 压住 BOT）。整段在
    # `domain.uptime` 的模块头上。

    def _beat_uptime(self) -> None:
        """落一拍挂机心跳。**每 tick 调一次，但按间隔限流。**

        ⚠️ **写失败一律吞掉**（只记本地日志、不往上抛）：这是个观测指标，绝不能
        因为它写不进去就把调度停了。`system_log` 也是同一个库，库都写不进去的时候
        往那里记日志同样写不进去；而漏掉的那几拍**本身就是证据**——挂机时长上会
        留一个和故障时段对得上的缺口。
        """
        now = self._clock()
        previous = self._uptime_last_beat
        if not due_for_a_beat(last_beat=previous, now=now):
            return
        try:
            if opens_a_new_segment(last_beat=previous, now=now):
                self._open_uptime_segment(now, after=previous, reason="心跳断了一段")
            elif self._uptime_segment_id is None or not self._repository.beat_uptime_segment(
                self._uptime_segment_id, now_utc=now
            ):
                # 行没了（换库、被清过）。当成新的一段，别把拍子丢进空里。
                self._open_uptime_segment(now, after=previous, reason="那一段的行不见了")
            self._uptime_last_beat = now
        except Exception:  # noqa: BLE001 - 监控不许把调度器弄死，理由见上
            _LOGGER.exception("挂机心跳落库失败，本拍跳过")

    def _open_uptime_segment(self, now: datetime, *, after: datetime | None, reason: str) -> None:
        """另开一段，**只在「断过」那一刻**写一条 `system_log`。

        心跳本身每分钟一拍，一律不记日志（CLAUDE.md：每 tick 可能触发的要限流、
        只在状态变化时写）。

        ⚠️ **本进程的第一段（`after is None`）也不写。** 那不是异常——用户按了
        「开始」、控制台刚起来，都会走到这里，而这两件事在别处已经看得见
        （`mission_runs`、页面上的运行态）。真正值得一行日志的是**断过**：
        上一拍还在，中间那段空档却超过了阈值。那说明机器睡了、tick 卡了、
        或者进程死过一次——而挂机时长上正好会缺那一截，日志得说出那一截是什么。
        """
        self._uptime_segment_id = self._repository.open_uptime_segment(now_utc=now)
        if after is None:
            return
        record_system_log(
            "WARNING",
            __name__,
            f"挂机心跳：{reason}，已另开一段运行段",
            payload={
                "reason": reason,
                "segment_id": self._uptime_segment_id,
                "now_utc": now.isoformat(),
                "previous_beat_utc": after.isoformat(),
                # 这一截就是挂机时长里缺掉的那段。
                "gap_seconds": (now - after).total_seconds(),
            },
        )

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
        before = self._backfill.state()
        self._backfill.request(request)
        self._log_backfill_transition(before, "用户点了「开始补录」")
        self._advance_backfill()
        return self._backfill.state()

    def cancel_backfill(self) -> BackfillState:
        """排队中就撤销，跑着就杀掉。取消之后立刻放行。"""
        return self._cancel_backfill("用户点了「取消补录」")

    def acknowledge_backfill(self) -> BackfillState:
        """用户看过摘要，点了「继续任务」。**这一下才放行。**"""
        before = self._backfill.state()
        state = self._backfill.acknowledge()
        self._log_backfill_transition(before, "用户点了「继续任务」")
        return state

    # -- 补录的痕迹 ------------------------------------------------------------
    #
    # ⚠️ **这一段是踩出来的**（2026-08-19）。那天一趟正在跑的手动补录变成了
    # 「已取消」，事后翻库想知道是谁把它取消的——`system_log` 里一条都没有。
    # 补录会独占游戏窗口十几分钟、会拦下所有任务、还有三个按钮能改它的态
    # （取消 / 继续 / 强制结束），却整条链路一行日志都不留，于是「它为什么停了」
    # 这个问题只能靠猜。判据是 CLAUDE.md 那条：**出事时能不能只靠库里的日志定位。**
    #
    # 只在**态真的变了**的那一刻写（同 `_log_schedule_window_changes`）：
    # `_advance_backfill` 每秒被调一次，每 tick 刷一行的话一晚上几万行，
    # 真正要看的那几条会被淹掉。

    def _cancel_backfill(self, trigger: str) -> BackfillState:
        """取消，并留下**是谁按的**。三个入口各有各的口径，混作一条就白记了。"""
        before = self._backfill.state()
        state = self._backfill.cancel(self._measure_backfill)
        self._log_backfill_transition(before, trigger)
        return state

    def _log_backfill_transition(self, before: BackfillState, trigger: str) -> None:
        """态变了就记一条，附上足够复现的那份证据。"""
        after = self._backfill.state()
        if (
            after.phase is before.phase
            and after.queued == before.queued
            # 「确认」不动 phase，只翻这一位——而它正是放不放任务出来的那一下。
            and after.acknowledged == before.acknowledged
        ):
            return
        summary = after.summary
        record_system_log(
            "WARNING" if after.phase is BackfillPhase.FAILED else "INFO",
            "application.mission_scheduler",
            f"战报补录 {before.phase.value} → {after.phase.value}（{trigger}）",
            payload={
                "trigger": trigger,
                "phase_from": before.phase.name,
                "phase_to": after.phase.name,
                "kind": after.kind,
                "since": None if after.since is None else after.since.isoformat(),
                "reason": after.reason,
                "queued": after.queued,
                "pid": after.pid,
                "exit_code": after.exit_code,
                "acknowledged": after.acknowledged,
                # 任务此刻起不起得来，就看这一位。
                "blocking": after.blocking,
                "log_path": None if after.log_path is None else str(after.log_path),
                "reports_ingested": None if summary is None else summary.reports_ingested,
                "dispatches_claimed": None if summary is None else summary.dispatches_claimed,
                "bot_targets_settled": None if summary is None else summary.bot_targets_settled,
            },
            logged_at_utc=self._clock(),
        )

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
            was = self._backfill.state()
            self._backfill.launch_if_pending(before)
            self._log_backfill_transition(was, "窗口空出来了，起补录子进程")

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
        self._log_a_pool_stalled_on_the_cycle_boundary(now)
        self._log_the_scan_cooldown(snapshots, facts)
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
        # 在两个判据里占着的航线数不一样。全账号那道闸同理，而且它连星球都不分，
        # 一趟只查一次。
        hold = self._unknown_line_hold()
        account_free = self._account_free_lines(
            now, hold=hold, reserved_lines=config.reserved_lines
        )
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
                account_free=account_free,
            )
            if free < 1:
                continue
            if not self._repository.resume_mission_task(
                row.id, recovery=DisabledRecovery.FREE_LINES
            ):
                # 这期间用户自己点了「恢复」，或者它已经被别的原因重新停用。
                continue
            name = task.name or task.kind.value
            self._log_auto_toggle(
                task_id=row.id,
                mission_kind=task.kind.value,
                event="resumed",
                level="INFO",
                message=(
                    f"任务「{name}」曾因空闲航线不足被自动停用，"
                    f"当前空闲航线 {free} 条，已自动恢复参与调度"
                ),
                payload={
                    "task_id": row.id,
                    "mission_kind": task.kind.value,
                    "free_lines": free,
                    "disabled_recovery": DisabledRecovery.FREE_LINES.value,
                },
                now=now,
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

        ⚠️ **跃迁去重挡不住「反复跃迁」**，所以外面还罩着一层限流
        （`_log_auto_toggle`）：停用 → 恢复 → 停用，每一下都是**真的**跃迁，
        库里那两列每次都在变，去重一条都拦不下来。2026-08-18 01:00 那一小时
        写了 1368 行正是这一档。
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
        self._log_auto_toggle(
            task_id=row.id,
            mission_kind=task.kind.value,
            event="disabled",
            level="WARNING",
            message=f"任务「{name}」已被自动停用：{reason}；{aftermath}",
            payload={
                "task_id": row.id,
                "mission_kind": task.kind.value,
                "disabled_reason": reason,
                "disabled_recovery": recovery.value,
                "previous_disabled_reason": previous[0],
                "previous_disabled_recovery": previous[1],
            },
            now=self._clock(),
        )

    def _log_auto_toggle(
        self,
        *,
        task_id: int,
        mission_kind: str,
        event: str,
        level: str,
        message: str,
        payload: dict[str, Any],
        now: datetime,
    ) -> None:
        """「自动停用 / 自动恢复」这一对日志的**限流**闸门。

        **为什么去重不够。** `_disable_task` 已经按库里那两列做了跃迁去重，
        `_resume_tasks_waiting_for_a_line` 也只在真的放出来时才写。可 2026-08-18
        01:00 那一小时里，同一个任务**自动停用 447 次、自动恢复 447 次**——每一下
        都是真跃迁，两处去重一条都拦不住，结果 1368 行日志把那一小时里别的东西
        全埋了，而这条链路一发未派。判据不是「有没有打日志」，是**出事时能不能只
        靠库里的日志定位**；一小时 1368 行同一句话，定位不了任何东西。

        ⚠️ **签名恒为空元组，也就是「只按窗口限流，不看内容」**——这一条与军力那
        两条**刻意不同**。这里每一次调用本身就是一次货真价实的跃迁，「内容变了没」
        对它毫无意义；换成按内容签名的话，`previous_disabled_reason` 在
        停用→恢复→停用的抖动里一变，447 次里就有一大半会绕开窗口重新刷出来，
        而那正是 #179 花力气按下去的东西。
        """
        self._log_a_repeated_line(
            key=(task_id, event),
            mission_kind=mission_kind,
            signature=(),
            level=level,
            message=message,
            payload=payload,
            now=now,
            repeat_noun="跃迁",
        )

    def _log_an_idle_round(self, task: TaskSnapshot, facts: TaskFacts, exc: MissionIdle) -> None:
        """「这一轮没活干」——**每 tick 都可能触发，所以必须限流。**

        ⚠️ **`has_work` 对齐之后它仍然可达，别以为限流是多余的。** 对齐挡掉的是
        「航线满了却说有活干」那一类；剩下的一类是**航线有、池子挑不出人**——
        候选全在保护期里、全在重复攻击间隔里、或者全被军力上限挡在外面。那一档
        `can_dispatch` 为真、`_military_command` 照样空手而归，而它同样是每 tick
        一次。`tests/integration/application/test_idle_tick_recompute.py` 里那个
        `ALL_TOO_STRONG` 夹具就是它。

        **为什么从 `_LOGGER.info` 换成 `record_system_log` 这条路。** 原先那句
        `_LOGGER.info` 经 `infrastructure.system_log.SystemLogHandler` 落库，
        那座桥上既没有限流、也认不出是哪个任务（`task_id` 列取的是**进程**身份，
        而控制台进程不属于任何一轮，于是恒为 NULL）。生产实测
        （2026-08-18 16:13 → 08-19 00:04）：6,661 行同一句话、占 `system_log`
        全表 22%、`task_id` **全部为 NULL**——只能靠消息正文里的任务名去认它是谁。
        走 `_log_a_repeated_line` 两件事一起解决：限流，以及 `task_id` 落到列上。

        **签名覆盖那句话里会变的一切**：任务名、原因（`MissionIdle` 的消息里带
        出发点坐标）、以及**当时看到的航线余量**。少覆盖一个，「和上一条一样」
        这句判断就会把一个已经变了的现场压成沉默——而仓库的规矩是
        「日志说假话比不说更糟」。
        """
        reason = str(exc)
        message = f"{task.name} 这一轮没活干：{reason}"
        payload: dict[str, Any] = {
            "task_id": task.task_id,
            "mission_kind": task.kind.value,
            "reason": reason,
            "free_lines": facts.free_lines,
            # 说清「为什么这不是去收战报」：航线满而战报欠着正是本条最常见的现场，
            # 而这一档**故意不起轮**（见 `domain.scheduler.has_work`）。
            "reports_due": facts.reports_due,
        }
        self._log_a_repeated_line(
            key=(task.task_id, "mission_idle"),
            mission_kind=task.kind.value,
            signature=_line_signature(message, payload),
            level="INFO",
            message=message,
            payload=payload,
            now=self._clock(),
            repeat_noun="判定",
        )

    def _log_a_repeated_line(
        self,
        *,
        key: tuple[int, str],
        mission_kind: str,
        signature: tuple[object, ...],
        level: str,
        message: str,
        payload: dict[str, Any],
        now: datetime,
        repeat_noun: str,
    ) -> None:
        """调度器高频日志的公共闸门：**状态变了就立刻写，没变就一个窗口最多一条。**

        两条规则缺一不可，各自挡的是不同的东西：

        - **状态变了立刻写，不受窗口约束。** 跃迁本身就是要看的那件事；被时间窗
          压掉的话，「窗口 16:13 开始被放弃」这种时刻就永远读不出来了。
        - **状态没变时按窗口压。** 2026-08-18 16:00 那一小时，「军力候选池」
          6,078 行、「军力读数放宽窗口」6,077 行，两条合起来占了 `system_log`
          全表的 44%；而按内容去重之后各只剩 38 / 37 条——**其余 12,080 行一个新
          事实都没带来**。成因是 `_step` 一个 tick 里会转好几圈，每圈都要组一次
          命令行，于是同一秒里同一句话能落四遍。

        ⚠️ **限流不许把信息丢掉。** 被压掉的次数与它们横跨的时长都写进下一条
        （`suppressed_since_last_log` / `suppressed_span_seconds`，消息里也说一遍）。
        被压掉的那些**按构造与上一条落库的一字不差**（签名相等才会被压），所以那句
        补充说的是「**上一条**在那之后又原样重复了 N 次」——主语是上一条，不是眼前
        这一条。两种情形的措辞因此**分开写**（`_merged_note`）：判定变了的那一条要是
        也说成「这一判定持续了 N 次」，那就是把被压掉的旧状态算到新状态头上，
        而仓库的规矩是「日志说假话比不说更糟」——压缩可以，撒谎不行。

        窗口**可配**（`military_attack_config.auto_toggle_log_seconds`，留空 =
        `REPEATED_LOG_WINDOW`）：调小排障时看得密、日志吵；调大库干净，代价是一次
        真实的反复抖动会被合并成看不出频率的一条。先例是
        `record_unrecognised_screen` 的 120 秒。

        **状态只在内存里**，进程一重启就忘掉——那是对的：重启之后的第一条本来就该
        落库，它是新一轮运行里的第一手事实。

        ⚠️ **`key[0]` 就是 `task_id`，而且它要一路写到 `system_log.task_id` 那一列
        上去。** 那一列平时由**进程**身份填（`system_log.current_context()`，
        runner 靠环境变量认领自己那一轮），而控制台进程不属于任何一轮，于是这里
        每一条本来都落成 NULL——排障时只能从消息正文里的任务名去认「这是谁」，
        按任务过滤根本做不到（生产实测 2026-08-18：6,661 行「这一轮没活干」的
        `task_id` **全部为 NULL**）。payload 里那份是给人读的，列上这份是给
        `WHERE` 用的，两份都要。
        """
        previous = self._repeated_lines.get(key)
        changed = previous is None or previous.signature != signature
        if not changed:
            assert previous is not None  # noqa: S101 - `changed` 已经把 None 排掉了
            if now - previous.written_at < self._repeated_log_window():
                self._repeated_lines[key] = replace(previous, suppressed=previous.suppressed + 1)
                return
        suppressed = 0 if previous is None else previous.suppressed
        span = timedelta() if previous is None else now - previous.written_at
        self._repeated_lines[key] = _RepeatedLine(signature=signature, written_at=now, suppressed=0)
        record_system_log(
            level,
            "application.mission_scheduler",
            message + _merged_note(suppressed, span, repeat_noun=repeat_noun, changed=changed),
            payload={
                **payload,
                "suppressed_since_last_log": suppressed,
                "suppressed_span_seconds": round(span.total_seconds()),
                "signature_changed": changed,
            },
            logged_at_utc=now,
            task_id=key[0],
            mission_kind=mission_kind,
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

        **为什么非有这条不可。** 「候选一个军力读数都没有」会落成「此刻没活干」，
        那是对的——调度器会去跑军力榜扫描。但如果扫描本身跑不起来（扫得太慢、
        榜单页读不出来、军力榜任务被停用），这个状态会一直维持，而页面上只有一句
        不痛不痒的状态：**攻击悄悄停摆一整夜，没人知道。**

        ⚠️ 这一档 2026-08-18 起不再由「分数全都过期」造成：超期的分数不再挡任何
        目标（窗口不够就放宽），所以措辞跟着从「分数全都过期、扫描跟不上有效期」
        改成「一个军力读数都没有」。**不改的话这条警告会指着一个不存在的原因**，
        而用户照它去把有效期调长，调完照样一发不派。

        ⚠️ **2026-08-19 起它有两个成因，措辞必须跟着分岔**（`cause` 那一段）：
        「从没上过军力榜」和「读数全属于上一个周期」——后者是周一 UTC+0 刷新那一刻
        全库读数同时作废造成的。两者在 `usable == 0` 上长得一模一样，而说错的代价
        是把人引到「军力榜为什么漏了这些 bot」这条错路上。**日志说假话比不说更糟。**

        ⚠️ 顺带记一笔：**这条警告在 2026-08-18 之前一次都没响过。** 那时
        `usable = 有读数的 + 没读数的`，而库里从来都有没读数的行（实测 628 个），
        `starved` 恒为假。没有读数的目标退出攻击之后它才第一次有真的含义。

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
            # ⚠️ **成因必须说对。** 「上周期的读数整批作废」和「军力榜还没扫到
            # 它们」在 `usable == 0` 上长得一模一样，而后一句会把人引到
            # 「军力榜为什么漏了这些 bot」这条错路上——真相只是该重扫一轮了。
            cause = (
                f"{reading.dropped_last_cycle} 个的军力读数早于本周期起点 "
                f"{reading.cycle_start:%Y-%m-%d %H:%M} UTC（每周一 UTC+0 刷新时全部作废）、"
                f"另有 {reading.dropped_unrated} 个从未上榜，本周期一条新读数都没有"
                if reading.starved_by_the_cycle_boundary
                else "一个军力读数都没有，军力榜还没扫到它们"
            )
            record_system_log(
                "WARNING",
                "application.mission_scheduler",
                f"「{task.name if task else task_id}」的军力候选池已连续 "
                f"{rounds} 轮（自 {since:%Y-%m-%d %H:%M} UTC 起）"
                f"筛不出能打的目标：{reading.attackable} 个候选{cause}。"
                "攻击已停在这里，请确认军力榜扫描是否还在跑",
                payload={
                    "task_id": task_id,
                    "mission_kind": MissionKind.BOT.value,
                    "attackable": reading.attackable,
                    "with_readings": 0,
                    "dropped_unrated": reading.dropped_unrated,
                    "dropped_last_cycle": reading.dropped_last_cycle,
                    "cycle_start_utc": reading.cycle_start.isoformat(),
                    "starved_since_utc": since.isoformat(),
                    "starved_rounds": rounds,
                },
                logged_at_utc=now,
            )

    def _log_a_pool_stalled_on_the_cycle_boundary(self, now: datetime) -> None:
        """**「读数早于本周期起点」这一条把整池挡光了的那一刻，留一条痕。**

        为什么这条日志非有不可，而上面那两条都盖不住它：

        - `_log_the_military_pipeline`（每一步余量那条）只在**组命令行**时才写，
          而整池被挡光时这条链路根本轮不到组命令行（`has_work` 已经是假），
          于是那一刻在库里一个字都没有；
        - `_log_a_starved_military_pool` 要**连续半小时**才开口——周一凌晨那半小时
          正是最该看清「刚才发生了什么」的半小时。

        说清三件事：**本周期起点是什么时候、被筛掉多少条、剩多少**。少任何一个，
        看见日志的人还是得回库里查——而那正是「没人告诉你」的另一种写法。

        ⚠️ **限流：判定变了立刻写，没变就一个窗口最多一条**（`_log_a_repeated_line`）。
        这一条位于**每 tick 都走**的那条路上（`_facts` → `_military_pool_reading`
        每 tick 算一次账），不限流的话它就是下一个 PR #188——那次两条日志占了
        `system_log` 全表的 44%。

        ⚠️ **只在整池被挡光时才开口**，不是「丢掉一条就说一句」。周一上午军力榜
        一边扫一边把这个数往下带，逐条报会把一整个上午刷满，而那期间攻击本来就在
        正常跑——**每轮都响的告警和不响的一样没用**。

        ⚠️ **恢复时补一条 INFO 收口，且只补一条。** 只报开头不报结尾的话，翻日志的
        人读不出这一段停了多久——而那正是判断「军力榜扫得够不够快」的那个数。
        收口只在跃迁那一下写，不吃窗口兜底，否则一个长期正常的任务会每
        `REPEATED_LOG_WINDOW` 刷一句「已恢复」。
        """
        for task_id, reading in self._military_pool_readings.items():
            key = (task_id, "military_cycle")
            cleared: tuple[object, ...] = ("cleared",)
            if not reading.starved_by_the_cycle_boundary:
                previous = self._repeated_lines.get(key)
                # 没报过就不必「恢复」：没响过的告警去收口，读日志的人只会以为
                # 刚才出过事。
                if previous is None or previous.signature == cleared:
                    continue
                self._log_a_repeated_line(
                    key=key,
                    mission_kind=MissionKind.BOT.value,
                    signature=cleared,
                    level="INFO",
                    message=(
                        f"军力读数跨周期作废：已恢复。本周期（{reading.cycle_start:%Y-%m-%d %H:%M}"
                        f" UTC 起）已经采到 {reading.usable} 个读数，这一轮起重新打得出去"
                    ),
                    payload={
                        "task_id": task_id,
                        "mission_kind": MissionKind.BOT.value,
                        "cycle_start_utc": reading.cycle_start.isoformat(),
                        "attackable": reading.attackable,
                        "with_readings": reading.usable,
                        "dropped_last_cycle": reading.dropped_last_cycle,
                        "stalled": False,
                    },
                    now=now,
                    repeat_noun="告警",
                )
                continue
            message = (
                f"军力读数跨周期作废：本周期起点是 {reading.cycle_start:%Y-%m-%d %H:%M} UTC"
                f"（bot 军力每周一 UTC+0 刷新，刷新那一刻上周的读数全部作废）。"
                f"{reading.attackable} 个候选里 {reading.dropped_last_cycle} 个的读数早于这个时刻、"
                f"另有 {reading.dropped_unrated} 个从未上榜，"
                f"本周期读到的只剩 {reading.usable} 个——这一轮一个都打不了。"
                f"等军力榜扫过一轮就会自己恢复"
            )
            payload: dict[str, Any] = {
                "task_id": task_id,
                "mission_kind": MissionKind.BOT.value,
                "cycle_start_utc": reading.cycle_start.isoformat(),
                "attackable": reading.attackable,
                "with_readings": reading.usable,
                "dropped_last_cycle": reading.dropped_last_cycle,
                "dropped_unrated": reading.dropped_unrated,
                "stalled": True,
            }
            self._log_a_repeated_line(
                key=key,
                mission_kind=MissionKind.BOT.value,
                signature=_line_signature(message, payload),
                level="WARNING",
                message=message,
                payload=payload,
                now=now,
                repeat_noun="告警",
            )

    def _forget_a_starved_military_pool(self, task_id: int) -> None:
        """池子恢复（或这个任务不再参与调度）时把那一段的账清掉。"""
        self._stale_pool_since.pop(task_id, None)
        self._stale_pool_rounds.pop(task_id, None)
        self._stale_pool_warned_at.pop(task_id, None)

    # -- 扫描间隔 ----------------------------------------------------------------

    def _log_the_scan_cooldown(
        self, snapshots: Sequence[TaskSnapshot], facts: SchedulerFacts
    ) -> None:
        """扫描间隔**把活儿挡掉的那一刻**、以及**安全阀让路的那一刻**，各留一条痕。

        判据不是「有没有打日志」，而是**出事时能不能只靠库里的日志说清
        「军力榜这一夜为什么只扫了两轮」**。所以两个时刻都要写，而且两条都要说清
        「为什么 + 当时看到了什么」：上一轮什么时候开始的、冷却配了多久、还差多久、
        窗口内当时还剩几个、门限是多少。少任何一个，读日志的人都得回去查库。

        - **挡掉**（`BLOCKING`）：INFO。它是这个旋钮**正常工作**的样子，不是异常。
        - **安全阀让路**（`OVERRIDDEN`）：WARNING。用户口径里这一条最要紧——它意味着
          上一轮扫描失败或被打断，池子正在饿，再挡下去选靶就要回落到上周期的陈旧
          读数（整段理由在 `domain.scheduler.scan_cooldown_verdict` 上）。淹在 INFO
          里等于没说。

        ⚠️ **限流走 `_log_a_repeated_line`**：`has_work` 每 tick 都会走这条判据，
        而 `_step` 一个 tick 里会转好几圈（先例与实测数字在 `REPEATED_LOG_WINDOW`
        与 `record_unrecognised_screen` 上）。不限流的话，一个配了 2 小时间隔的
        军力榜任务能在两小时里写出七千行同一句话。

        ⚠️ **签名只认「状态」，不认「还差几分钟」。** 那个数每 tick 都在变，
        放进签名等于让限流整个失效（`_line_signature` 覆盖消息里的每一个数，
        而那正是它平时该做的事）。这里改喂一个显式签名：
        `(挡不挡, 从哪一刻起算, 冷却多长)`——它们全变了才叫状态变了。
        代价是被压掉的那一段里「还差几分钟」在减少而库里不记，那没有信息量：
        起算时刻和冷却时长都在同一条日志里，差多少一减就有。

        ⚠️ **写在 `_step` 里，不在 `_facts` 或 `has_work` 里。** 后两者页面轮询也会
        走（`snapshot`），挪过去会让「页面开着」和「页面关着」写出不一样的日志。
        """
        for task in snapshots:
            if task.kind is not MissionKind.RANKING or not _participating(task):
                continue
            verdict = scan_cooldown_verdict(task, facts)
            if verdict.state is ScanCooldown.BLOCKING:
                level, headline, noun = "INFO", "扫描间隔生效", "判定"
            elif verdict.state is ScanCooldown.OVERRIDDEN:
                level, headline, noun = "WARNING", "扫描间隔让路", "告警"
            else:
                # 没配、或者已经过完了。**一个字都不写**：每轮都响的日志和不响的
                # 一样没用，而「这一轮放行了」由 `mission_runs` 里那条记录说得更准。
                continue
            assert verdict.cooldown is not None  # noqa: S101 - 这两档必然配了冷却
            assert verdict.last_started_at_utc is not None  # noqa: S101 - 也必然跑过
            hours = verdict.cooldown.total_seconds() / 3600
            pool = verdict.pool
            message = (
                f"{headline}：上次扫描开始于 {verdict.last_started_at_utc:%Y-%m-%d %H:%M} UTC"
                f"（{_spoken_span(verdict.elapsed)}前），扫描间隔配的是 {hours:.1f} 小时，"
                f"还差 {_spoken_span(verdict.remaining)}；"
                + (
                    "此刻没有军力优先的攻击任务在等这份读数"
                    if pool is None
                    else f"窗口内还有 {pool.in_window} 个候选、窗口门限 {pool.floor} 个"
                )
                + (
                    "。窗口内已经低于门限，再挡下去选靶就会放弃窗口、回落到"
                    "上一周期的陈旧读数，所以这一轮放行"
                    if verdict.state is ScanCooldown.OVERRIDDEN
                    else "，够用，这一轮不开新的扫描"
                )
            )
            self._log_a_repeated_line(
                key=(task.task_id, "scan_cooldown"),
                mission_kind=MissionKind.RANKING.value,
                signature=(
                    verdict.state.value,
                    verdict.last_started_at_utc.isoformat(),
                    round(verdict.cooldown.total_seconds()),
                ),
                level=level,
                message=message,
                payload={
                    "task_id": task.task_id,
                    "mission_kind": MissionKind.RANKING.value,
                    "last_started_at_utc": verdict.last_started_at_utc.isoformat(),
                    "cooldown_hours": hours,
                    "elapsed_minutes": round(verdict.elapsed.total_seconds() / 60, 1),
                    "remaining_minutes": round(verdict.remaining.total_seconds() / 60, 1),
                    "in_window_count": None if pool is None else pool.in_window,
                    "window_floor": None if pool is None else pool.floor,
                    "safety_valve_released": verdict.state is ScanCooldown.OVERRIDDEN,
                },
                now=facts.now_utc,
                repeat_noun=noun,
            )

    def _log_a_scan_round_that_outlived_its_cooldown(self, exited: MissionExit) -> None:
        """**边界留痕**：这一轮扫描本身就跑得比扫描间隔还久。

        冷却从「上一轮开始」算（用户明确要的，理由在
        `domain.scheduler.TaskSnapshot.scan_cooldown` 上），所以一旦某一轮的时长
        追平了冷却，这道闸门在它结束的那一刻就已经过完——**等于没有生效**。
        那不是缺陷，是这个起算点必然的推论；但它必须查得出来，否则用户看着
        「间隔 1 小时」却发现扫描一轮接一轮，只会以为旋钮坏了。

        实测均值 19.3 分钟、最长 29 分钟（`domain.target_order.DEFAULT_SCORE_MAX_AGE`
        那一段记的采集速率），离 1 小时还远，所以这条日常一条都不该出现。

        ⚠️ **不限流，因为它一轮最多一条**——`_finish` 每个子进程只走一次。
        套上 `_log_a_repeated_line` 反而会把「连着三轮都超时」压成一条。
        """
        row = self._repository.mission_task(exited.task_id)
        if row is None:
            return
        try:
            cooldown = _ranking_scan_cooldown(row.params_json)
        except MissionParamError:
            # 参数这会儿填错了不该顺带把收退出码这条路搞崩。那件事由页面校验与
            # `_launch` 各自负责，这里只是记账。
            return
        if cooldown is None:
            return
        duration = exited.ended_at_utc - exited.started_at_utc
        if duration < cooldown:
            return
        hours = cooldown.total_seconds() / 3600
        record_system_log(
            "WARNING",
            "application.mission_scheduler",
            f"军力榜这一轮扫描耗时 {_spoken_span(duration)}，已经不短于配置的扫描间隔 "
            f"{hours:.1f} 小时——间隔是从「上一轮开始」算的，所以它在这一轮结束时就已经"
            "过完，下一轮不会被它挡住。要真的拉开两轮之间的距离，得把间隔调到比一轮"
            "扫描的时长更大",
            payload={
                "task_id": exited.task_id,
                "mission_kind": MissionKind.RANKING.value,
                "started_at_utc": exited.started_at_utc.isoformat(),
                "ended_at_utc": exited.ended_at_utc.isoformat(),
                "duration_minutes": round(duration.total_seconds() / 60, 1),
                "cooldown_hours": hours,
            },
            logged_at_utc=exited.ended_at_utc,
            task_id=exited.task_id,
            mission_kind=MissionKind.RANKING.value,
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
        变了，顺位该立刻让给下一条。**这句话现在由 `LaunchOutcome` 保证**——
        判据在 `LaunchOutcome.worth_another_round` 上，连同它为什么只认
        `DISABLED` 一档的全部理由。

        2026-08-18 之前这段规格和最后一行是对不上的：`_launch` 用一个 `False` 表达
        了任务被删、`MissionIdle`、`MissionParamError` **三**件事，`not
        self._launch(...)` 把三件都翻成「值得再算」。于是每一个「没活干」的 tick 都把
        `_step` 转满 `len(MissionKind)` = 4 圈，每圈一次完整的 `_facts()`——
        实测（本地 SQLite，体量按生产摆到约两倍：`bot_targets` 10,725 行）一次
        `_military_pool_reading` 4 条 SQL / 约 179 ms，一次 `_facts` 16 条 SQL /
        约 194 ms（`_facts` 自己的注释记的生产实测是 0.32 秒），乘以 4 就是每个空转
        tick 近一秒钟全花在重算全库候选池上。它同时是 2026-08-18 16:00 那一小时日志
        刷屏的成因——实测「同一秒内最多重复 4 次」，与这 4 圈对得上（PR #188 压住了
        日志，成因留到这里才修）。

        **修法不是加缓存。** 真正的失效条件（新军力读数写入、新派遣写入、配置变化、
        窗口边界随时间移动）说不清，而缓存会让调度器拿着过期的候选池去派遣，比多花
        CPU 糟得多。
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
            return self._launch(decision.task, facts.of(decision.task)).worth_another_round

    def _launch(self, task: TaskSnapshot, facts: TaskFacts) -> LaunchOutcome:
        """组命令行、起进程、记账。四种结局各有各的成员，见 `LaunchOutcome`。

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
            return LaunchOutcome.VOID
        try:
            batch_task = self._military_batch_task() if task.kind is MissionKind.RANKING else None
            if task.kind is MissionKind.RANKING:
                # 两个上限，取**小**的那个：任务上配的「扫描数量」是用户给这条
                # 链路划的天花板（留空 = 不划），军力批次要的**窗口门限**是「这一批
                # 攻击要在窗口内看到多少个目标才肯只信新数据」。取大的会越过用户
                # 划的线，取任务那个又会让批次采不满——`min` 是唯一同时守得住两条的。
                #
                # ⚠️ 窗口门限 2026-08-18 起不再是「打前几名」（军力硬截断取消了），
                # 但它在这里的用法没变：扫够那么多个，这一轮才不必放弃窗口。
                #
                # ⚠️ 2026-08-23 起它是**全局**的一个数（`_window_floor`），所以这里
                # 不再看 `batch_task` 的参数——但 `batch_task is None` 这个分支必须
                # 留着：它表达的是「这一趟没有任何军力任务在等这批目标」，那时不该
                # 拿一个攻击侧的门限去卡用户给扫描链路划的上限。
                command = ranking_command(
                    bot_limit=_smallest_limit(
                        _ranking_bot_limit(row.params_json),
                        None if batch_task is None else self._window_floor(),
                    ),
                    blind_rows=self._blind_rows(),
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
            # 不停用、不记失败、不起进程，**而且本 tick 不再重算**：候选集一个字都
            # 没变，再走一遍必然挑中同一个任务、抛同一个 `MissionIdle`
            # （见 `LaunchOutcome.worth_another_round`）。下一 tick 拿新事实重算。
            self._log_an_idle_round(task, facts, exc)
            return LaunchOutcome.IDLE
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
            # **唯一值得本 tick 再算一次的一档**：候选集真的少了一个，顺位该立刻让给
            # 下一条链路，否则这一秒谁都不跑。
            return LaunchOutcome.DISABLED
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
            # 这一轮开始时账号一共配着几条航线。⚠️ **必须在这里记下来**：数据概览页
            # 的利用率分母是「周期总时长 × 航线数」，而航线数会变（用户 2026-08-20
            # 把 4 条加到 9 条）。读页面时现取「此刻配着几条」去乘历史那些天，
            # 会把 08-15（当时 4 条）低估到 44%，而页面上一点异样都看不出来。
            configured_lines=self._configured_line_total(),
        )
        if task.kind is MissionKind.RANKING:
            self._military_ranking_batch_task_id = None if batch_task is None else batch_task.id
        elif task.kind is MissionKind.BOT and task.task_id == self._military_ranking_batch_task_id:
            # 这一批已经真正交给带 --attack 的 runner，后续排程恢复常规优先级。
            self._military_ranking_batch_task_id = None
        return LaunchOutcome.STARTED

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
        if exited.kind is MissionKind.RANKING:
            self._log_a_scan_round_that_outlived_its_cooldown(exited)
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
        # 全账号那道闸的余量。**一趟只查一次**：它与出发星球无关，按任务查等于
        # 把同一个数乘上任务数，而 tick 每秒一次。没有任何一条派遣链路参与调度时
        # 连查都不查——这一趟本来就没人会用到它。
        #
        # ⚠️ **早退那一支必须是 `None`（「这道闸不生效」）而不是 `0`（「一条不剩」）。**
        # 写 `0` 的话，一旦以后有人把这个早退条件放宽，所有任务的可用航线会被这个
        # 假的「账号已满」整个压成 0，而症状是助手一发不派、页面上一切正常。
        account_free = (
            self._account_free_lines(now, hold=hold, reserved_lines=config.reserved_lines)
            if any(_participating(task) and not fills_gaps(task.kind) for task in tasks)
            else None
        )
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
                    account_free=account_free,
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
                # ⚠️ 这里算的是**还有多少个能打**（有军力读数的候选数），不是
                # 「这一轮派了几发」——后者由航线预算定。军力优先这一支的
                # 「有没有活干」就是这个数（`domain.scheduler.bot_round_complete`），
                # 于是「候选一个军力读数都没有」自然落成「此刻没活干」，调度器会去跑
                # 军力榜扫描把池子刷新——而**不是**抛异常。抛出去的话 `_launch` 会把
                # 任务停用，用户不点「恢复」它就永远不跑（见 `MissionIdle`）。
                reading = self._military_pool_reading(row)
                readings[task.task_id] = reading
                per_task[task.task_id] = replace(
                    base,
                    free_lines=free,
                    reports_due=self._reports_due(task, now, grace),
                    targets_remaining=reading.usable,
                    scores_are_missing=reading.starved,
                    # 页面那一半的「大声说出来」。日志那一半在
                    # `_warn_about_a_widened_window`，**两处同源**：只报一处的话，
                    # 用户还是得从攻击日志里一条一条对——那正是这次要修的形状。
                    scores_window_widened=reading.widened,
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
                    account_free=account_free,
                ),
                reports_due=self._reports_due(task, now, grace),
                targets_remaining=(
                    self._bot_remaining(task) if task.kind is MissionKind.BOT else 0
                ),
                last_dispatch_at_utc=self._repository.last_dispatch_at(
                    target_kind, origin=task.origin
                ),
                next_line_free_at_utc=next_free[task.origin],
                # 这个任务自己的窗口存货，以及「让位补货还有没有用」。
                # ⚠️ 两格都按任务算——口径是「轮到**该星系**时它自己够不够」，
                # 整段账在 `domain.scheduler.yields_to_a_scan` 上。
                military_window=(window := _window_of(readings.get(task.task_id))),
                scan_can_still_help=self._scan_can_still_help(task.task_id, window, now),
            )

        # 整份换上去而不是原地改：页面线程也会调 `_facts`（`snapshot`），
        # 原地改的话读的人可能撞见只填了一半的那一刻。
        self._military_pool_readings = readings
        return SchedulerFacts(
            now_utc=now,
            # 扫描间隔那道安全阀读的就是它。**用的是这一趟已经算好的
            # `MilitaryPoolReading`，不另查一遍库**：选靶口径只能有一份，
            # 各算一份的结果是安全阀在「其实还够用」时乱放行（或者反过来），
            # 而两种走样在页面上都看不出来。
            military_window=_most_starved_window(readings.values()),
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

    def _scan_can_still_help(
        self, task_id: int, window: MilitaryWindowPool | None, now: datetime
    ) -> bool:
        """这个任务让位给军力榜还有没有用——**看它窗口内的数量还在不在涨**。

        用户口径（2026-08-24）：「轮到该星系 bot 攻击时，如果不足就去采集
        （现在的采集效率很高）采集够了就开始攻击，而不是轮空星系」。

        ⚠️ **判据是「还在涨」，不是「扫过没有」。** 池子是**逐屏写库**的
        （日志「逐屏写入 N 条」），所以扫描一开跑这个数就往上爬 —— 拿涨势当判据
        比去查「上一趟扫描几点跑的」简单得多，也不必新加一份运行记录的读法。

        ⚠️ **停滞要给足时间。** 军力榜落下第一行之前要先开榜、盲滚、检测 bot 区，
        实测约 50 秒；这段时间数量一动不动，而它并不是「补不进来」。
        所以停滞计时到 `SCAN_YIELD_PATIENCE`（3 分钟）才判死。

        ⚠️ **这是唯一的防死锁闸**：门限配得比榜上能采到的还高时（今天差点这样：
        门限 200、本周期总共才采到 227 个），扫描每趟都跑、池子每趟都不涨，
        没有这道闸的话 BOT 会永远让位、一发不打，而页面显示的是「没活干」
        ——一句听起来正常、实际相反的话。

        ⚠️ **按任务记账。** 每个军力任务有自己的出发点，能打到的目标不一样，
        所以「够不够」「补得进来吗」都是各自的事（同 `TaskFacts.military_window`）。
        """
        if window is None or not window.below_floor:
            self._yield_watermark.pop(task_id, None)
            self._yield_stalled_since.pop(task_id, None)
            return False

        seen = self._yield_watermark.get(task_id)
        if seen is None or window.in_window > seen:
            if seen is None:
                self._log_the_yield(task_id, window, now, stalled=None)
            self._yield_watermark[task_id] = window.in_window
            self._yield_stalled_since.pop(task_id, None)
            return True

        since = self._yield_stalled_since.get(task_id)
        if since is None:
            self._yield_stalled_since[task_id] = now
            return True

        stalled = now - since
        if stalled < SCAN_YIELD_PATIENCE:
            return True
        self._log_the_yield(task_id, window, now, stalled=stalled)
        return False

    def _log_the_yield(
        self,
        task_id: int,
        window: MilitaryWindowPool,
        now: datetime,
        *,
        stalled: timedelta | None,
    ) -> None:
        """让位 / 停止让位都走**同一个**闸门，因为它们是同一个状态机的两个态。

        ⚠️ 走 `_log_a_repeated_line` 而不是自己判「只在跃迁时说」：那个闸门已经是
        「状态变了立刻写、没变就一个窗口最多一条」，而 `_facts` 每 tick 都跑
        （页面线程也会调），自己写一份必然要么刷屏、要么漏掉跃迁那一刻。

        ⚠️ **停止让位那一条必须是 WARNING**，而且要把「门限可能配得太高」写进正文
        ——那是这一档唯一的可行动信息。让位本身是正常运转，INFO 就够。
        """
        if stalled is None:
            message = (
                f"窗口内只剩 {window.in_window} 个候选、门限 {window.floor} 个："
                "这一跳让给军力榜去补货，补够了再打"
            )
            level = "INFO"
        else:
            message = (
                f"让位 {stalled.total_seconds() / 60:.1f} 分钟，窗口内仍停在 "
                f"{window.in_window} 个、够不着门限 {window.floor} 个："
                "不再让位，放弃窗口照旧打——**门限可能配得比榜上能采到的还高**"
            )
            level = "WARNING"
        payload: dict[str, Any] = {
            "task_id": task_id,
            "mission_kind": MissionKind.BOT.value,
            "in_window": window.in_window,
            "floor": window.floor,
            "stalled_seconds": None if stalled is None else round(stalled.total_seconds()),
        }
        self._log_a_repeated_line(
            key=(task_id, "military_yield_to_scan"),
            mission_kind=MissionKind.BOT.value,
            signature=_line_signature(message, payload),
            level=level,
            message=message,
            payload=payload,
            now=now,
            repeat_noun="告警" if stalled is not None else "提示",
        )

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
                # 只数还有多少个能打（有军力读数的候选）：军力优先这一支
                # 「有没有活干」就是这个数。
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
            return most_valuable_first(
                self._scored_bot_targets(),
                origin,
                # 时钟与窗口宽度都从这一层喂进去：领域层不许自己去问「现在几点」，
                # 那会让页面算出来的一批和调度器算出来的一批差上几秒钟的窗口边界，
                # 而边界上的目标恰恰是最容易两边不一致的那些。
                now=self._clock(),
                # ⚠️ 有效期与窗口门限**从全局配置读**（2026-08-23 起），不再从
                # `params_json` 里各读一份。整段理由在 `_score_max_age` 上。
                max_age=self._score_max_age(),
                window_floor=self._window_floor(),
                max_score=_bot_max_score(params_json),
            )
        in_range = bot_targets_in_range(self._bot_targets(), **_bot_range(params_json))
        return nearest_first(in_range, origin)

    def _military_command(
        self, row: orm.MissionTaskRow, *, max_dispatches: int | None = None
    ) -> list[str]:
        """只起一颗出发星球的一组目标，避免 runner 中途切星球留下半组状态。

        ⚠️ **「整轮只跑一颗星球」不是可以放宽的实现细节。** runner 一轮只能站一颗
        星球：一个游戏窗口、一只鼠标，开工时 `ensure_origin_planet` 真的把当前星球
        切过去。中途换星球会留下半组状态。

        ⚠️ **「这一轮凑不出目标」抛的是 `MissionIdle` 而不是 `MissionParamError`。**
        后者会让 `_launch` 去调 `disable_mission_task`：任务被停用、挂上
        `disabled_reason`，用户不去页面点一次「恢复」就永远不跑。而这里的空手而归
        （池子里没有读数新鲜的目标、航线预算刚好耗尽）全都是**会自己好起来**的一档
        ——扫描刷新池子、舰队飞回来，下一 tick 就成立了。判成参数错误的代价是
        一整夜一发不派，比拿旧数据打糟得多。

        ⚠️ **`NoFreeLineError` 在这条路上一次都不该出现。** 它说的是「配置让我一发
        都派不出去」，而多出发点场景里「这一颗此刻满了」是**正常的间歇**。
        2026-08-18 01:00 那一小时把它当成配置错误处理，代价是自动停用 447 次、
        自动恢复 447 次、bot 链路空转一小时。所以这里一律 `MissionIdle`。
        """
        assignments = self._military_assignments(row)
        if not assignments:
            raise MissionIdle("本轮没有可派遣的军力攻击目标")
        origin = self._origin_taking_its_turn(assignments)
        group = [item for item in assignments if item.origin == origin]
        # ⚠️ **`budget` 只数正选，备胎一个都不算。**
        #
        # 备胎是 2026-08-24 加的（`MILITARY_SPARE_FACTOR`）：分配阶段按
        # 「航线数 × 2」放行，多出来的那些标了 `reserve=True`，用来顶替撞上保护期
        # 的目标。它们**绝不能让这一轮多派几发**——而 `max_dispatches` 有默认值
        # `None`，那条路上 budget 会退回 `len(group)`，若拿整组去数就等于翻倍。
        #
        # 正选的个数已经被这颗星球的两道闸预算卡过一次了
        # （`_military_assignments` 把预算喂给了 `assign_by_capacity_and_value`），
        # 所以这里不必、也不该再去查一次库：再查一次就是第二把尺子。
        primaries = [item for item in group if not item.reserve]
        budget = min(
            max_dispatches if max_dispatches is not None else len(primaries), len(primaries)
        )
        if budget < 1:
            # 结构上到不了（`facts.free_lines` 是各出发点里最能派的那一个，
            # 而这颗星球恰恰是分到了目标的那些之一）。留着它是为了让「万一走到了」
            # 也走 `MissionIdle` 那条路——不停用、不记失败、下一 tick 重算。
            raise MissionIdle(f"出发点 {origin} 此刻没有可用航线")
        # ⚠️ **坐标交整组（含备胎），派出数交 `budget`（只含正选）。**
        # runner 按这个次序往下试，撞上保护期弹窗就跳过那一个、拿下一个顶上
        # （`pirate_loop._handle_dialog`），直到派满 `max_dispatches` 发。
        # 交整组是「这一轮的攻击必须发出去」唯一的兑现方式：只交正选的话，
        # 一个被保护的目标就白白吃掉一条航线。
        return bot_command(
            [item.coordinate for item in group],
            origin=origin,
            presets={item.coordinate: item.preset for item in group},
            max_dispatches=budget,
        )

    def _origin_taking_its_turn(self, assignments: Sequence[AssignedTarget]) -> Coordinate:
        """这一轮跑哪颗星球：**分到了目标的那些里，上次出兵最久远的那颗。**

        ## 为什么必须轮换

        原先取的是 `assignments[0].origin`，而 `assign_by_capacity_and_value`
        末尾按 `(origin, distance)` 排序、`Coordinate` 是 `order=True` 的 dataclass
        ——`4:277:15 < 9:250:8` 恒成立。于是只要 1 号星拿到哪怕一个目标，第一组
        永远是它，**第二颗星永远轮不到**。那不是「优先级」，是结构性不可达。

        ## ⚠️ 判据绝不能是军力 / 价值

        实测（生产库，2026-08-18）：1 号星邻域最高 47,170，2 号星 38,330。按价值排的
        话，邻域强的那颗**恒赢**——饿死只是换了个判据复发，而且这一次连
        「排序恒定」这个线索都没有了，看起来像是「它就是更该打」。所以判据只能是
        **公平性本身**：谁等得最久谁上。

        ## 事实从库里取，不在内存里记

        `last_dispatch_at(origin=...)` 已经在库里。调度器进程会重启（实机上重开
        Chrome、重启控制台都发生过），内存里的「上次轮到谁」一重启就没了，
        而库里那个时刻重启之后照样答得出来。

        从没派过的那颗排最前（`_NEVER`）：它等得比任何人都久。同刻时按坐标定序，
        只为让结果确定——否则同一份事实能选出两颗不同的星球。
        """
        candidates = sorted({item.origin for item in assignments})
        return min(
            candidates,
            key=lambda origin: (
                self._repository.last_dispatch_at(TARGET_KIND_BOT, origin=origin) or _NEVER,
                origin,
            ),
        )

    def _military_assignments(self, row: orm.MissionTaskRow) -> tuple[AssignedTarget, ...]:
        """这一轮打谁、从哪出发。**四步的先后是判据的一部分，不能重排。**

        1. 排除本轮已走完的、重复攻击间隔内打过的、刚撞上过保护期的
           （`_military_candidates`）；
        2. 只留有军力读数的（`with_a_military_reading`）；
        3. 只留读数落在有效期**窗口**内的（`within_score_window`），窗口内不够
           **窗口门限**那么多个时**放弃窗口并告警**（`choose_by_military`）；
        4. 过军力上限这道安全线，按 `军力 ÷ 往返小时` 降序分给各出发星球出击
           （`assign_by_capacity_and_value`）。

        前三步在 `_military_pool_reading` 里一次算完，这里只取结果——**选靶口径
        只能有这一份**。每一步的理由写在 `domain.target_order` 的模块头上，
        这里只重复最容易搞反的两条：

        - 第 1 步必须在最前，否则首批刚好都打过时候选池会缩成空集；
        - 第 4 步只有**一条**判据（得分），不再是「先按军力截断、再按距离出击」
          那两条——旧的那两条互相矛盾，而它们之间那道墙（`top_n`）是拍出来的。

        ⚠️ **第 4 步吃的是「两道闸算完之后的预算」，不是 `mission_task_origins`
        里那个原样的航线数**（见 `_origin_budgets`）。这一点是「`has_work` 与
        `_launch` 用同一把尺子」的结构性保证：预算为 0 的星球压根拿不到目标，
        于是凡是分到了目标的出发点一定还派得出去。喂原样的航线数进去，
        2026-08-18 01:00 那一小时的 447 次抖动就会原封不动地回来。
        """
        reading = self._military_pool_reading(row)
        origins = self._military_origins(row)
        if not origins:
            raise MissionParamError("军力攻击没有启用的出发星球")
        # 说实话的那一句：**每一步之后各剩多少**，让人只靠库里的日志就能复盘
        # 「为什么打的是这几个」。落库不落文件——实机跑在另一台机器上。
        #
        # ⚠️ 仍然**不从这里启动 RANKING**：两条链路会争同一只鼠标。刷新交给调度器
        # 的填空隙机制。
        self._log_the_military_pipeline(row, reading)
        pool = reading.eligible
        try:
            tiers_json = self._active_military_tiers_json
            if tiers_json is None:
                tiers_json = self._repository.military_attack_config().tiers_json
            global_tiers = json.loads(tiers_json)
        except json.JSONDecodeError as exc:  # pragma: no cover - 写侧已校验
            raise MissionParamError("全局军力档位配置损坏") from exc
        dispatchable = self._dispatchable_origins(origins)
        assignments = assign_by_capacity_and_value(
            pool,
            dispatchable,
            fallback_preset=BOT_ATTACK_PRESET,
            tiers=self.validate_military_tiers(global_tiers),
            # 每条航线多配一个备胎，用来顶替撞上保护期的目标（用户口径 2026-08-24）。
            # ⚠️ 备胎与正选出自**同一个 `pool`**，而 `pool` 是
            # `reading.eligible`——已经过完窗口那一步。所以「必须是新鲜的数据」
            # 这半句是结构性成立的，不靠调用方自觉。
            spare_factor=MILITARY_SPARE_FACTOR,
        )
        # AI 选靶（影子）观测：在 `assign_by_capacity_and_value` 算完之后、组命令行
        # 之前插一次。**只读，返回值一个字不动**——这一行是纯观测，AI 挂掉、
        # 超时、返回垃圾都不影响派遣（需求第八节，用例钉死「逐字不变」）。
        self._observe_ai_shadow(row, reading, origins, dispatchable, assignments)
        return assignments

    def _observe_ai_shadow(
        self,
        row: orm.MissionTaskRow,
        reading: MilitaryPoolReading,
        origins: Sequence[AttackOrigin],
        dispatchable: Sequence[AttackOrigin],
        assignments: Sequence[AssignedTarget],
    ) -> None:
        """把这一轮喂给影子观测器。**任何路径都不许改 `assignments` 或抛异常。**

        ## 喂进去的是 `candidates`（全池），不是 `eligible`

        ⚠️ **这一点是整个一期成不成立的地方。** `eligible` 是四步流水线筛完的
        结果：第 3 步按有效期的窗口和窗口门限裁过（两格 2026-08-23 起住在
        `military_attack_config`），第 4 步过了 `max_score` 军力上限（仍是任务参数）。
        而这三个旋钮的**数值一个都没进 prompt**——
        方案第一节的整段理由就是「AI 不该参考旋钮，AI 就是去调这些旋钮的」。
        喂 `eligible` 等于旋钮的值不给、筛选效果照给，**把答案先塞给它**的另一种
        形态。所以这里取 `reading.candidates`。

        （`candidates` 本身仍带着第 1 步的两条排除——`bot_revisit_hours` 与
        保护期排除窗口。那两条不是「哪个目标更值」的判据，而是「这个坐标此刻
        打不了」，属于事实一侧；而且第 1 步的结果就是需求文档 §3 点名要给的
        那一份。）

        ## 关掉时的开销

        ⚠️ **「零开销」指的是不组 prompt、不起线程、不发任何网络请求**，
        **不是「一次库都不查」**：这里要读一行 `military_attack_config` 才知道
        开关的状态。那张表在同一次派遣里本来就要读好几次（每个旋钮一次，见
        `_knob`），多这一行读不出量级差别；而把它缓存起来会让「用户在页面上
        点开开关」延迟生效，那才是真正会误事的。
        """
        if self._ai_shadow is None:
            return
        if not self._ai_shadow_enabled():
            return
        now = reading.now
        hold = self._unknown_line_hold()
        dispatchable_list = list(dispatchable)
        total_budget = sum(item.fleet_lines for item in dispatchable_list)
        if total_budget < 1:
            return
        account_limit = self._account_line_limit()
        try:
            account_inflight = self._repository.count_inflight_total(now_utc=now, hold=hold)
        except Exception as error:  # noqa: BLE001 - 影子观测查库失败只跳过，不动派遣
            self._log_the_ai_shadow_was_skipped(row, "count_inflight_total 查询失败", error, now)
            return
        try:
            self._ai_shadow.observe(
                task_id=row.id,
                now=now,
                run_id=self._run_id,
                budget=total_budget,
                candidates=reading.candidates,
                origins=[item.coordinate for item in origins],
                configured_lines={item.coordinate: item.fleet_lines for item in origins},
                budgets_by_origin={item.coordinate: item.fleet_lines for item in dispatchable_list},
                account_inflight=account_inflight,
                account_limit=account_limit,
                hold=hold,
                presets=_ai_presets(assignments),
                assignments=assignments,
            )
        except Exception as error:  # noqa: BLE001 - 观测侧的异常绝不连锁到派遣
            self._log_the_ai_shadow_was_skipped(row, "observe() 抛异常", error, now)
            return

    def _log_the_ai_shadow_was_skipped(
        self, row: orm.MissionTaskRow, what: str, error: BaseException, now: datetime
    ) -> None:
        """影子观测被一句 `except` 挡掉时留个痕。

        ⚠️ **这两条路以前一个字都不记。** 用户把开关打开、库里什么都没多出来，
        排障时无从下手——正是 CLAUDE.md 那条「日志不说话，故障拖了两天」的
        复发形态。判据不是「有没有打日志」，是**出事时能不能只靠库里的日志定位**，
        所以异常的 `repr` 要带上。

        ⚠️ **限流走 `_log_a_repeated_line`**：这一段每一轮派遣都会走到，
        一个反复失败的查询能在一夜里刷出上万行。签名里带上异常类型，
        **换了一种失败立刻写一条**（状态跃迁不受窗口约束）。
        """
        detail = f"{type(error).__name__}: {error}"
        self._log_a_repeated_line(
            key=(row.id, "ai_shadow_skipped"),
            mission_kind=row.kind,
            signature=(what, type(error).__name__),
            level="WARNING",
            message=(
                f"AI 选靶影子：任务「{row.name}」这一轮跳过——{what}（{detail}）。"
                "派遣不受影响，照常按算法进行。"
            ),
            payload={"task": row.name, "what": what, "error": detail},
            now=now,
            repeat_noun="跳过",
        )

    def _ai_shadow_enabled(self) -> bool:
        """AI 影子观测的开关。**默认关**（同 `AUTO_ENABLED` 的惯例），
        没配 / 表没初始化一律当关。

        ⚠️ **它读一行库。** `_observe_ai_shadow` 的文档串里写清了为什么不缓存。
        observer 自己在 `_read_knobs` 里还会再确认一次同一个开关——那是
        防御性的第二道，用的是它本来就要读的同一行，不多花查询。
        """
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return False
        return bool(row.ai_shadow_enabled)

    def _dispatchable_origins(self, origins: Sequence[AttackOrigin]) -> tuple[AttackOrigin, ...]:
        """各出发点此刻**真的**能派几发，两道闸都算过（见 `_origin_budgets`）。

        这一层负责查库（每颗星球的在飞数、全账号在飞数、保留数），算式本身在
        `_origin_budgets` 里——**算式只能有一份**，`_facts` 与这里问的是同一个函数。
        """
        now = self._clock()
        hold = self._unknown_line_hold()
        config = self._repository.scheduler_config()
        inflight = {
            item.coordinate: self._repository.count_inflight(
                now_utc=now, origin=item.coordinate, hold=hold
            )
            for item in origins
        }
        return _origin_budgets(
            origins,
            inflight=inflight,
            account_free=self._account_free_lines(
                now, hold=hold, reserved_lines=config.reserved_lines
            ),
        )

    def _military_pool_reading(self, row: orm.MissionTaskRow) -> MilitaryPoolReading:
        """把选靶的前四步**一次算完**，每一步的中间结果都留着。

        ⚠️ **选靶只能在这里算一次。** 页面上的「还剩几个」、日志里的每一步余量、
        真正下发的那批目标，全都从这一份结果里取。三处各算一遍的话，最先分家的
        是「军力优先」那一支——2026-08-15 撞过一次，症状是一发都不派而且不报错。

        ⚠️ **有效期（`max_age`）在这里是一道真的筛选**，但它挡不住整轮：窗口内
        不足**窗口门限**那么多个时 `choose_by_military` 会放弃窗口、改用全部有读数
        的目标，并把这件事记在 `widened` 上。两条历史都写在 `domain.target_order`
        的模块头第 3 步上——挡整轮的那一版让实机停摆 2.5 小时，换成「取最新 N 个」
        的那一版把全库最弱的一批选了出来。
        """
        # ⚠️ **两个数都从全局配置读**（2026-08-23 起），不再看 `row.params_json`。
        # 用户口径（2026-08-23）：「军力攻击的有效期 门限 改为全局设置，不再根据
        # 单个星系进行调整」。存量任务里那两个旧键的善后在 `_legacy_window_keys`。
        max_age = self._score_max_age()
        window_floor = self._window_floor()
        candidates = self._military_candidates(row)
        now = self._clock()
        return MilitaryPoolReading(
            candidates=tuple(candidates),
            choice=choose_by_military(
                candidates,
                now=now,
                max_age=max_age,
                window_floor=window_floor,
                max_score=_bot_max_score(row.params_json),
            ),
            window_floor=window_floor,
            max_age=max_age,
            now=now,
        )

    def _log_the_military_pipeline(
        self, row: orm.MissionTaskRow, reading: MilitaryPoolReading
    ) -> None:
        """把这一轮选靶的**每一步余量**写进 `system_log`。

        判据不是「有没有打日志」，而是**出事时能不能只靠库里的日志复盘
        「为什么打的是这几个」**。所以这几个数一个都不能省：剔除后 / 有本周期读数 /
        从未上榜的 / 读数属于上周期的 / 窗口内 / 过完安全线——少任何一个，读日志的人
        就分不清是「没候选」「没读数」「读数上周就作废了」「窗口太窄」还是
        「被军力上限挡在外面」，而这几种的善后完全不同。

        ⚠️ **「从未上榜」和「读数属于上一个周期」必须各占一个数。** 合成一个的话，
        周一凌晨这条日志会写着「N 个从未上榜」，而那是句假话——它会把人引到
        「军力榜为什么漏了这些 bot」这条错路上，真相只是该重扫一轮了。

        ⚠️ **这一池不等于「这一轮打的那几个」**：真正打谁由第 4 步按得分连同航线
        预算定，那一步在 `_military_assignments` 里。所以这条日志的措辞是
        「有资格被打的有几个」，不是「选中了几个」——**日志说假话比不说更糟**。

        ⚠️ **限流：状态变了立刻写，没变就一个窗口最多一条**（`_log_a_repeated_line`）。

        原先这里写着「不限流：一轮出击一条」——**那句规格是错的**。`_step` 一个
        tick 里会转好几圈（`tick()` 里那个 `for _ in range(len(MissionKind))`），
        每圈都要组一次命令行，于是同一秒里同一句话能落四遍。实机 2026-08-18 16:00
        那一小时：这一条 6,078 行、放宽窗口那条 6,077 行，两条合起来占了
        `system_log` 全表的 44%；而按内容去重之后各只剩 38 / 37 条。

        ⚠️ 仍然**别把它挪到 `_facts` 里去**：那里页面轮询也会走。限流只是把重复
        压掉，挪过去会让「页面开着」和「页面关着」写出不一样的日志。
        """
        oldest = reading.oldest_eligible_at
        message = (
            f"军力候选池：排除近期打过的与撞上过保护期的之后剩 {reading.attackable} 个，"
            f"其中 {reading.usable} 个有本周期军力读数"
            f"（{reading.dropped_unrated} 个从未上榜，"
            f"{reading.dropped_last_cycle} 个的读数早于本周期起点 "
            f"{reading.cycle_start:%Y-%m-%d %H:%M} UTC，都不参与）；"
            f"读数在 {reading.max_age.total_seconds() / 3600:.1f} 小时窗口内的有 "
            f"{len(reading.in_window)} 个（窗口门限 {reading.window_floor}）；"
            f"过完军力上限之后有资格被打的 {len(reading.eligible)} 个，"
            f"其中 {reading.stale} 个来自窗口外"
            f"（最旧读数 {'无' if oldest is None else f'{oldest:%Y-%m-%d %H:%M} UTC'}）；"
            "真正打谁由「军力 ÷ 往返小时」的得分连同航线预算定"
        )
        payload: dict[str, Any] = {
            "task_id": row.id,
            "mission_kind": MissionKind.BOT.value,
            "attackable": reading.attackable,
            "with_readings": reading.usable,
            "dropped_unrated": reading.dropped_unrated,
            "dropped_last_cycle": reading.dropped_last_cycle,
            "cycle_start_utc": reading.cycle_start.isoformat(),
            "in_window": len(reading.in_window),
            "window_floor": reading.window_floor,
            "eligible": len(reading.eligible),
            "stale_eligible": reading.stale,
            "widened": reading.widened,
            "score_max_age_hours": reading.max_age.total_seconds() / 3600,
            "oldest_eligible_at_utc": None if oldest is None else oldest.isoformat(),
        }
        self._log_a_repeated_line(
            key=(row.id, "military_pool"),
            mission_kind=MissionKind.BOT.value,
            signature=_line_signature(message, payload),
            level="INFO",
            message=message,
            payload=payload,
            now=reading.now,
            repeat_noun="账目",
        )
        self._warn_about_a_widened_window(row, reading)
        self._warn_about_legacy_window_params(row, reading)

    def _warn_about_legacy_window_params(
        self, row: orm.MissionTaskRow, reading: MilitaryPoolReading
    ) -> None:
        """这个任务的 `params_json` 里还存着**已经不生效**的有效期/窗口门限。

        2026-08-23 那两格搬进了全局攻击配置（用户口径：「军力攻击的有效期 门限
        改为全局设置，不再根据单个星系进行调整」），存量任务里那几个键**一律忽略**。

        ⚠️ **忽略必须说出来，这一条不是可选的附注。** 这次改动会让实机的行为在
        某些任务上**当场变掉**：一个从前配着 6 小时有效期的任务，这一轮起用的是
        全局的 2 小时（或用户在攻击配置页填的那个数）。不说的话，症状是「某个银河
        突然打得少了 / 突然开始报放宽窗口了」，而页面上、日志里都找不到任何解释
        ——那正是这个仓栽过好几次的那种静默走样。

        ⚠️ **级别是 WARNING**，理由同 `_warn_about_a_widened_window`：淹在每轮都写的
        INFO 里的一句话等于没说。而且这一条是**有尽头的**——用户在任务页保存一次
        就会把那几个旧键清掉，告警随之消失。所以它不会永远吵。

        ⚠️ **说清「旧值是多少」和「这一轮实际用的是多少」两个数。** 只报「有旧值」
        的告警回答不了用户唯一想问的那个问题：那我现在到底按几小时在打。

        ⚠️ **限流走同一道闸**（`_log_a_repeated_line`）：这句话每一轮派遣都会算到，
        不限流就是又一条一小时六千行。
        """
        legacy = _legacy_window_keys(row.params_json)
        if not legacy:
            return
        hours = reading.max_age.total_seconds() / 3600
        stale = "、".join(f"{key}={value!r}" for key, value in sorted(legacy.items()))
        message = (
            f"军力选靶窗口已改为全局设置：任务「{row.name}」的参数里还存着 {stale}，"
            "**已忽略**——有效期与窗口门限 2026-08-23 起不再按星系分别配。"
            f"这一轮实际用的是全局值：有效期 {hours:.1f} 小时、窗口门限 "
            f"{reading.window_floor} 个。要改就去攻击配置页改那一份；"
            "在任务页保存一次会把这几个旧键清掉，这条告警随之消失。"
        )
        payload: dict[str, Any] = {
            "task_id": row.id,
            "mission_kind": MissionKind.BOT.value,
            "ignored_params": {key: str(value) for key, value in legacy.items()},
            "score_max_age_hours": hours,
            "window_floor": reading.window_floor,
        }
        self._log_a_repeated_line(
            key=(row.id, "military_legacy_window_params"),
            mission_kind=MissionKind.BOT.value,
            signature=_line_signature(message, payload),
            level="WARNING",
            message=message,
            payload=payload,
            now=reading.now,
            repeat_noun="告警",
        )

    def _warn_about_a_widened_window(
        self, row: orm.MissionTaskRow, reading: MilitaryPoolReading
    ) -> None:
        """窗口不够用、被放弃了——**大声说出来**。

        用户口径（2026-08-18）：「今晚这件事的真正问题不是『用了旧数据』，而是
        **用了旧数据却没人告诉你**——你是从攻击日志里一条一条对出来的」。所以：

        - **级别是 WARNING，不是 INFO。** 上面那条流水线日志每轮都写，淹在
          几千行 INFO 里的一句「其中 N 个来自窗口外」等于没说。降成 INFO 就等于
          把这次改动最要紧的那一半退回去了。
        - **四个数一个都不能少**：窗口多宽、窗口内只有几个、截断要几个、放宽之后
          用到的最旧读数是什么时候。少了任何一个，看见告警的人还是得回去查库才
          知道该把有效期调成多少——而那正是「没人告诉你」的另一种写法。

        ⚠️ **正常走窗口时一个字都不写**（第一次见到这个任务就正常的话，连
        「恢复」都不写）。每轮都响的告警和不响的告警一样没用。

        ⚠️ **限流：判定变了立刻写，没变就一个窗口最多一条**（`_log_a_repeated_line`）。
        原先这里写着「不必也不该加限流，因为一轮一条」——**那句规格是错的**，实机
        2026-08-18 16:00 那一小时它写了 6,077 行，其中只有 37 条内容不同。要看的
        「连着几轮都在放宽」并没有因此丢掉：它现在由下一条里的
        `suppressed_since_last_log` / `suppressed_span_seconds` 说出来，而且说得比
        「数一数有几行」更准。

        ⚠️ **从「放宽」跌回「正常」时补一条 INFO 收口。** 只报开头不报结尾的话，
        翻日志的人读不出这一段有多长——而「放宽持续了多久」正是判断该不该调
        有效期的那个数。这一条**只在跃迁那一下写**，不参与窗口兜底：否则一个
        长期正常的任务会每 `REPEATED_LOG_WINDOW` 刷一句「已恢复」，那是另一种刷屏。
        """
        key = (row.id, "military_widened")
        recovered: tuple[object, ...] = ("recovered",)
        oldest = reading.oldest_eligible_at
        hours = reading.max_age.total_seconds() / 3600
        if not reading.widened:
            previous = self._repeated_lines.get(key)
            if previous is None or previous.signature == recovered:
                return
            self._log_a_repeated_line(
                key=key,
                mission_kind=MissionKind.BOT.value,
                signature=recovered,
                level="INFO",
                message=(
                    f"军力读数放宽窗口：已恢复。{hours:.1f} 小时窗口内有 "
                    f"{len(reading.in_window)} 个目标，够窗口门限要的 {reading.window_floor} 个了，"
                    "这一轮起重新只在窗口内选靶"
                ),
                payload={
                    "task_id": row.id,
                    "mission_kind": MissionKind.BOT.value,
                    "score_max_age_hours": hours,
                    "in_window": len(reading.in_window),
                    "window_floor": reading.window_floor,
                    "with_readings": reading.usable,
                    "widened": False,
                },
                now=reading.now,
                repeat_noun="告警",
            )
            return
        message = (
            f"军力读数放宽窗口：{hours:.1f} 小时窗口内只有 {len(reading.in_window)} 个目标，"
            f"不够窗口门限要的 {reading.window_floor} 个，于是放弃窗口、"
            f"改用全部 {reading.usable} 个有读数的目标。"
            f"这一轮有资格被打的 {len(reading.eligible)} 个里有 {reading.stale} 个来自窗口外，"
            f"最旧读数 {'无' if oldest is None else f'{oldest:%Y-%m-%d %H:%M} UTC'}。"
            "要么等军力榜再扫一轮，要么把「军力分数有效期」调大到与扫描周期相称。"
        )
        payload: dict[str, Any] = {
            "task_id": row.id,
            "mission_kind": MissionKind.BOT.value,
            "score_max_age_hours": hours,
            "in_window": len(reading.in_window),
            "window_floor": reading.window_floor,
            "with_readings": reading.usable,
            "eligible": len(reading.eligible),
            "stale_eligible": reading.stale,
            "oldest_eligible_at_utc": None if oldest is None else oldest.isoformat(),
        }
        self._log_a_repeated_line(
            key=key,
            mission_kind=MissionKind.BOT.value,
            signature=_line_signature(message, payload),
            level="WARNING",
            message=message,
            payload=payload,
            now=reading.now,
            repeat_noun="告警",
        )

    def _military_candidates(self, row: orm.MissionTaskRow) -> list[ScoredTarget]:
        """**第 1 步，必须在最前**：排除本轮已走完的、「重复攻击间隔」之内已攻击的、
        **刚撞上过保护期**的、以及**刚刚面板名读不出**的 bot。

        若先挑一批再排除，首批刚好都打过时军力任务会把候选池缩成空集，排名靠后、
        从未攻击的目标永远轮不到。排除必须在窗口筛选与得分排序的前面，随后再按得分
        给各出发星球分配。

        ⚠️ **这三条排除和 24 小时那一条排在同一处、同一优先级，不是顺手加的。**
        它们是同一档判据——「这个坐标此刻打不了」——而把任何一条挪到**航线预算花掉
        之后**，缩成空集的失败模式会原样复发：打不了的高分目标先把航线占满，
        再被筛掉，这一轮一发不派，而排在它后面那些明明能打。
        （PR #194 合并第 ④⑤ 步之前，这句话说的是「挪到取前 N 之后」——那道硬截断
        没有了，收窄候选池的闸口换成了航线预算，不变量本身没有放宽。）

        三个窗口都是策略、都可在攻击配置页上改，不是游戏规则：
        见 `DEFAULT_BOT_REVISIT`、`DEFAULT_PROTECTION_EXCLUSION` 与
        `DEFAULT_UNREADABLE_EXCLUSION`。

        ## 保护期这一条在修什么

        游戏的保护期是 8 小时，**任何人打过都会触发，而且只能撞上了才知道**
        （`game.pirate_ui.DIALOG_NO_MISSION`）。在 `bot_targets.protection_seen_at_utc`
        出现之前，「撞上了」只存在于 `system_log` 的纯文本里，选靶查不到——实机
        2026-08-18 20:29 那一轮当场确认四个目标全在保护期、11.5 分钟一发没派，
        20:41 结算完，**一秒之后的下一轮又把同样的四个挑了出来**，直到 8 小时自然
        过去。每个目标每轮约 2.9 分钟鼠标时间，而鼠标时间才是这台机器的瓶颈。

        ## 「面板名读不出」这一条在修什么

        **同一个形状，第二次。** 站到目标星球上，面板归属名有时读不出来，判据只能
        说「这不是 bot」，于是这一发不派——而这件事同样一个字都没落库。生产库实测
        （2026-08-20，近 24 小时）：「不是 bot（面板名 None）」40 次、**只涉及 3 个
        坐标**，而「不是 bot」但真读出了名字的 **0 次**；这 3 个坐标历史上成功派出
        0 次。军力高（39,030 / 20,960 / 20,630）→ 排在候选池最前 → 读不出 → 跳过 →
        这一轮 0 发 → `came_back_empty` 让 `waiting_for_a_line` 把那颗球压到下一条
        航线空出（实测一次 117 分钟）→ 候选池一个字没变，下一轮又挑中同一个。
        65 轮里 16 轮空手而归（25%）。

        ⚠️ **这一条只修「失败不留记录」，不碰面板名为什么读不出**（识别层，根因
        未知，要实机才查得动）。也不碰 `waiting_for_a_line`——它有它的道理。
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
        protected = self._repository.protected_bot_targets_since(
            now - self._protection_exclusion_window()
        )
        unreadable = self._repository.unreadable_bot_targets_since(
            now - self._unreadable_exclusion_window()
        )
        return [
            target
            for target in targets
            if target.coordinate not in attacked_last_day
            and target.coordinate not in protected
            and target.coordinate not in unreadable
            and phase_of(facts_by_target[target.coordinate]) is BotPhase.NEEDS_ATTACK
        ]

    def _frozen_origins(self, row: orm.MissionTaskRow) -> tuple[FrozenOrigin, ...]:
        """点「开始」那一刻，这个任务配着哪几颗出发星球。**只有军力攻击有。**

        其余链路返回 `()`——它们的出发星球就是 `FrozenTask.origin` 那一个，
        再抄一份到 `origins` 里只会让「改动」列把同一件事说两遍。

        ⚠️ **`()` 不是 `None`。** 前者是「记录了，确实没有多出发点」，后者是
        「这一行本轮之前写的，没有这个字段」，逐条对比对两者的处理完全不同，
        见 `mission_freeze.FrozenTask.origins`。
        """
        if MissionKind(row.kind) is not MissionKind.BOT or not _bot_by_military(row.params_json):
            return ()
        return tuple(
            FrozenOrigin(
                origin=str(item.coordinate), fleet_lines=item.fleet_lines, enabled=item.enabled
            )
            for item in self._configured_origins(row)
        )

    def _configured_origins(self, row: orm.MissionTaskRow) -> tuple[ConfiguredOrigin, ...]:
        """`mission_task_origins` 的**唯一读处**（连停用的那些一起带出来）。

        新表为空才回落旧单 origin，区域攻击永远不读新表。

        ⚠️ **判据侧与固化侧读的必须是同一份。** 判据只看启用的
        （`_military_origins`），而固化记录要连停用的一起记——不然「用户把 2 号星
        停掉了」这件事在账里一个字都不剩，而那正是事后要查的东西。各写一个读法的
        话，两边对「这个任务配了哪几颗星球」的理解迟早分家。
        """
        configured = self._repository.mission_task_origins(row.id)
        if configured:
            origins: list[ConfiguredOrigin] = []
            for item in configured:
                planet = None
                if item.planet_id is not None:
                    planet = self._repository.attack_planet(item.planet_id)
                coordinate = (
                    Coordinate(item.galaxy, item.system, item.position)
                    if planet is None
                    else Coordinate(planet.galaxy, planet.system, planet.position)
                )
                origins.append(ConfiguredOrigin(coordinate, item.fleet_lines, item.enabled))
            return tuple(origins)
        config = self._repository.scheduler_config()
        return (ConfiguredOrigin(self._origin_of(row), self._fleet_lines_of(row, config), True),)

    def _military_origins(self, row: orm.MissionTaskRow) -> tuple[AttackOrigin, ...]:
        """这个军力任务此刻**参与派遣**的那几颗出发星球。停用的一律不在内。"""
        return tuple(
            AttackOrigin(item.coordinate, item.fleet_lines)
            for item in self._configured_origins(row)
            if item.enabled
        )

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
            # ⚠️ **扫描间隔不上命令行，仍然要在这里量一遍。** 页面保存参数之前那道
            # 校验走的正是 `command_for`（`web.persistent_service._validate`）；
            # 不量，一个填错的值就会静默落库，而它下一次现身是在**每个 tick** 的
            # `task_snapshot` 里抛出来——那时错的是调度循环，不是那一次保存。
            _ranking_scan_cooldown(params_json)
            return ranking_command(
                bot_limit=_ranking_bot_limit(params_json),
                blind_rows=self._blind_rows(),
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

    def _blind_rows(self) -> int | None:
        """军力榜盲滚**行数**。**填了数就锁死，留空则按实测自动标定。**

        取值顺序与屏口径那一份（`_blind_scrolls`）逐条一致，下面每条理由都是从
        那边搬过来的，换成行之后一条都没失效：

        取自**全局攻击配置**（攻击配置页），不是任务参数——用户口径
        （2026-08-17）：「盲拖数量需在攻击配置页可配置」。

        返回 `None` 的意思是「命令行上不带 `--blind-rows`」，runner 用
        `game.ranking_ui.BLIND_SCROLL_ROWS`（700 行）那个写死的默认值。样本攒不够
        时就走这条。**不在这里自己回落成一个数字**：默认值只该有一处，写第二遍
        日后必然漏改，而漏改之后两个默认值各自生效，谁也不知道用的是哪个。

        手填的值优先于自动标定：它是覆盖，不是初值。

        配置行还没建出来时（老库、或者 `ensure_mission_rows()` 还没跑）当成留空：
        一个还没初始化的配置表说明不了「用户想改盲滚行数」，为它把整条采集链路
        停掉是不成比例的。⚠️ 这里更不能抛 `MissionParamError`——那个异常的后果是
        **自动停用到用户手动恢复为止**，不只是「这一轮不跑」。

        ⚠️ **一个上界都不设**（用户口径 2026-08-22）：盲滚行数由用户定，尤其不许
        拿 `FIRST_BOT_RANK`(587) 当边界——那个「bot 起点」是玩家改名伪装出来的，
        真 bot 区在更后面。理由整段写在 `game.ranking_ui.BLIND_SCROLL_ROWS` 上。
        """
        choice = self._blind_row_decision()
        self._log_blind_row_change(choice)
        return choice.rows

    def _blind_row_decision(self) -> BlindRowChoice:
        """这一刻盲滚行数判成了什么，**以及凭什么**。判定本身不写任何日志。"""
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return BlindRowChoice(None, source="default", samples=0)
        manual = _blind_scroll_rows(row.blind_scroll_rows)
        if manual is not None:
            # 手填时不去查库要样本：那次查询只为凑一句日志，而这条路上的答案
            # 与样本无关。
            return BlindRowChoice(manual, source="manual", samples=0)
        return self._calibrated_blind_rows()

    def _log_blind_row_change(self, choice: BlindRowChoice) -> None:
        """盲滚行数的取值或来源变了才写一条。

        ⚠️ **补的是自动标定唯一的哑点。** `domain.ranking.bot_area_reached_rows_message`
        上写着：那句实测日志的措辞一改，攒下的样本一次性作废，标定就**静悄悄退回
        写死的默认值**——页面上、日志里都看不出任何异常。采集那头照样打「盲滚 700
        行」，看上去和「本来就没攒够样本」一模一样。所以差别只能由**判定这一侧**
        说出来：这个数是手填的、是标定出来的、还是因为样本不够而根本没给出答案
        （连带说清此刻攒到了几条）。

        ⚠️ **只在变化时写**，同 `infrastructure.system_log.record_knob_override`
        那条先例。`_blind_rows` 每次组军力榜命令行时都会走，而 `command_for` 那条
        公开路径**页面保存配置时也会走**——每次都写的话，一天几十条重复的「盲滚
        行数还是 515 行」会把真正的那一次变化埋掉，而这条日志存在的全部意义就是
        那一次变化。

        ⚠️ **措辞只说判定，不说「这一趟滚了多少行」。** 走到这里未必真会起一轮
        采集：`command_for` 是页面拿来校验参数的，组出来的命令行随手就丢了。说成
        「本趟盲滚 N 行」就是替一件没发生的事作证。真正「这一趟滚了多少行」那句
        话在 `tools.ranking_scan` 里，由**真的滚完了**的那一侧打出来。
        """
        if choice == self._blind_row_choice:
            return
        self._blind_row_choice = choice
        record_system_log(
            "INFO",
            "application.mission_scheduler",
            f"军力榜盲滚行数判定为 {_blind_row_verdict(choice)}",
            payload={
                "blind_scroll_rows": choice.rows,
                "source": choice.source,
                "measurements": choice.samples,
                "samples_required": BLIND_SCROLL_SAMPLES,
                "margin": BLIND_SCROLL_MARGIN_ROWS,
                "hard_coded_default": BLIND_SCROLL_ROWS,
            },
            logged_at_utc=self._clock(),
        )

    def _calibrated_blind_rows(self) -> BlindRowChoice:
        """从 `system_log` 里那些「翻了 N **行**到达 bot 区」反推盲滚行数。

        ⚠️ **实测记录刻意没有自己的表或列。** 每趟采集本来就会把这句话写进
        `system_log`，那里已经攒着全部历史；再加一张表等于让同一件事有两份账，
        而两份账迟早对不上（其中一份还只有新版本才写）。

        多读一些行再筛：那句话不是每条日志都是，而 `recent_messages` 只做前缀
        匹配。⚠️ **切了口径之后这一点更要紧**：库里那一年**屏版**正文的前缀和
        行版一模一样，只差单位那个字，它们会占满前缀匹配的额度，然后被
        `bot_area_rows` 整条丢掉（有意的——78 屏 ≈ 647 行，当成 78 行算出来的
        盲滚荒谬地小）。读 `BLIND_SCROLL_SAMPLES` 的若干倍足以让新攒的行版样本
        在一天之内就浮上来，同时仍然只碰几十行。

        **样本条数要一起交出去**，那是日志唯一能分开「这台机器刚上线（或刚切完
        口径）」和「反解规则失效了」的凭据：前者样本会一天天涨上去，后者恒为 0。
        """
        raw = self._repository.recent_system_log_messages(
            starts_with=BOT_AREA_REACHED_PREFIX, limit=BLIND_SCROLL_SAMPLES * 8
        )
        measurements = [value for value in map(bot_area_rows, raw) if value is not None]
        rows = calibrated_blind_rows(
            measurements, sample_size=BLIND_SCROLL_SAMPLES, margin=BLIND_SCROLL_MARGIN_ROWS
        )
        return BlindRowChoice(
            rows,
            source="calibrated" if rows is not None else "default",
            samples=len(measurements),
        )

    def _blind_scrolls(self) -> int | None:
        """军力榜盲拖屏数。**填了数就锁死，留空则按实测自动标定。**

        ⚠️ **眼下没有调用点**：口径 2026-08-22 改成行，组命令行走的是 `_blind_rows`。
        这一套（连 `BlindScrollChoice` / `_blind_scroll_verdict` /
        `_calibrated_blind_scrolls` / `_log_blind_scroll_change`）留着是因为
        `military_attack_config.blind_scrolls` 那一列和攻击配置页上那个框都还在，
        它们合起来是这次改动的**回滚杠杆**：把上面两处 `_blind_rows()` 换回
        `_blind_scrolls()`、`ranking_command` 的参数换回 `--blind-scrolls`
        （`tools.ranking_scan` 那个开关也还留着），盲滚就退回慢拖，不用重写判据。
        等实机复测确认不回滚了，再连同 `domain.ranking` 里屏版那三个函数一起删。

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


def _window_of(reading: MilitaryPoolReading | None) -> MilitaryWindowPool | None:
    """把一份任务级的池子账目折成调度判据要的那两个数。

    ⚠️ 和 `_most_starved_window` 共用同一个 `MilitaryWindowPool`，但问的问题不同：
    那个跨任务取最饿的（扫描安全阀要的），这个只看一个任务（让位判据要的）。
    共用类型是有意的——两个数的口径必须完全一致，各写一份迟早分家。
    """
    if reading is None:
        return None
    return MilitaryWindowPool(in_window=len(reading.in_window), floor=reading.window_floor)


def _most_starved_window(readings: Iterable[MilitaryPoolReading]) -> MilitaryWindowPool | None:
    """这一趟里**最饿的**那一池：`窗口内个数 - 窗口门限` 最小的那一个。

    扫描间隔的安全阀防的是「整池归零」，而最先归零的就是余量最小的那一个。
    取最饿的（而不是求和、也不是取第一个）是唯一守得住这句话的选法：求和会让
    一个宽裕的任务把另一个已经见底的任务盖过去，取第一个则取决于任务的排列顺序
    ——那个顺序换一次查询就会变。

    `None` 表示**这一趟一个军力优先的 bot 任务都没参与调度**：那时没人等这份
    读数，冷却该照常生效。⚠️ 别回落成 `MilitaryWindowPool(0, 0)`——那个值的
    `below_floor` 是假（`0 < 0` 不成立），看起来结果一样，但它是在陈述一个
    「量到了、窗口内 0 个」的事实，而实际上一次都没量。
    """
    pools = [
        MilitaryWindowPool(in_window=len(reading.in_window), floor=reading.window_floor)
        for reading in readings
    ]
    # 空清单先早退，不用 `min(..., default=None)`：那个重载会把 `key` 里的形参
    # 推成 `MilitaryWindowPool | None`，strict mypy 当场报 union-attr。
    if not pools:
        return None
    return min(pools, key=lambda pool: pool.in_window - pool.floor)


def task_snapshot(row: orm.MissionTaskRow, *, origin: Coordinate, fleet_lines: int) -> TaskSnapshot:
    """一行 `mission_tasks` → 领域层认识的那个不可变快照。

    公开是给 API 用的：页面要按 `domain.scheduler` 的判据算状态和展示次序，
    而它拿到的只有 ORM 行。转换只能有一份，否则两边对「什么算已停用」的
    理解迟早分家。

    `origin` 与 `fleet_lines` 是**解析完默认值之后**的取值，由调用方传进来
    （`MissionScheduler._snapshots`）：那两条回落规则要用到 Settings 与
    `scheduler_config`，而这个函数不该去碰它们中的任何一个。

    ⚠️ **扫描间隔在这里解析，而不是在调度器里另读一遍 `params_json`。**
    页面算状态（`web.persistent_service._view`）和调度器判「起不起」走的是同一个
    `TaskSnapshot`，各读一份的结果必然是「页面说待命、调度器在按住它」。
    只对 `RANKING` 解析：`SCAN` 压根不吃参数，两条攻击链路的 `params_json` 里
    也不该长出一个不生效的键。
    """
    kind = MissionKind(row.kind)
    return TaskSnapshot(
        task_id=row.id,
        kind=kind,
        name=row.name,
        enabled=row.enabled,
        priority=row.priority,
        origin=origin,
        fleet_lines=fleet_lines,
        disabled_reason=row.disabled_reason,
        enabled_from_utc=row.enabled_from_utc,
        enabled_until_utc=row.enabled_until_utc,
        scan_cooldown=(
            _ranking_scan_cooldown(row.params_json) if kind is MissionKind.RANKING else None
        ),
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


def _origin_budgets(
    origins: Sequence[AttackOrigin],
    *,
    inflight: Mapping[Coordinate, int],
    account_free: int | None,
) -> tuple[AttackOrigin, ...]:
    """每颗出发星球此刻**真的**能派几发。**两道闸同时生效，取小。**

        某出发点此刻可用 = min( 该星预算 − 该星在飞 ,  全账号上限 − 全部在飞 − 保留数 )

    用户口径（2026-08-18）：「我的总航线数是所有星球共享的，在启动加成道具情况下
    最高是到 9 条」「星球的航线是我来配置的，我配置时已经手动确认了不会超过总航线数，
    **两者均需要约束**」。

    `account_free` 为 `None` = 用户没在攻击配置页上填账号上限，那道闸整个不生效，
    只剩每星预算这一道。**这不是「用某个默认值」**——真实可用航线随科技、道具、
    人为占用浮动，代码里写死哪个数都是错的，整段理由在
    `domain.scheduler.account_free_lines` 上。

    ⚠️ **返回的仍是 `AttackOrigin`，而且这个预算就是要喂给
    `assign_by_capacity_and_value` 的那一个**，不是原样的
    `mission_task_origins.fleet_lines`。这一点是「`has_work` 与 `_launch` 用同一把
    尺子」的结构性保证：预算为 0 的星球拿不到任何目标，于是**凡是分到了目标的
    出发点，一定至少还能派一发**，`_military_command` 那一路不可能再算出
    `max_dispatches < 1`。2026-08-18 01:00 那一小时的 447 次「自动停用 / 自动恢复」
    正是因为这两处各算各的：`has_work` 看所有出发点之和（2 条），而真正要跑的那颗
    星球上是 0 条。
    """
    return tuple(
        AttackOrigin(
            item.coordinate, _capped(item.fleet_lines - inflight[item.coordinate], account_free)
        )
        for item in origins
    )


def _capped(free: int, account_free: int | None) -> int:
    """`max(0, free)`，再按账号余量收一次（没配上限就不收）。"""
    available = max(0, free)
    return available if account_free is None else min(available, account_free)


def _free_lines_from(
    task: TaskSnapshot,
    *,
    origins: Sequence[AttackOrigin] | None,
    inflight: Mapping[Coordinate, int],
    reserved_lines: int,
    account_free: int | None,
) -> int:
    """这个任务此刻估算还剩几条空闲航线。

    **只有这一份判据。** `_facts`（决定要不要起一轮、`--max-dispatches` 传几）
    与「因航线不足停用后自动恢复」都问它。各写一份的话，放它出来用的尺子会和
    当初停用它的那把慢慢走散——走散之后要么放不出来，要么放出来就立刻再被停用，
    每 tick 一次。

    `origins` 非 None = 军力多出发点那一路。这一档返回的是**各出发点里最能派的
    那一个**（`max`），不是它们的合计（`sum`）：runner 一轮只能站一颗星球
    （一个游戏窗口、一只鼠标，`ensure_origin_planet`），所以「这一轮能不能起」
    问的只能是「有没有**某一颗**星球还派得出去」。

    ⚠️ **2026-08-18 之前这里是 `sum`，那正是 447 次抖动的成因**：1 号星占满、
    2 号星还剩 2 条时，合计 2 > 0 把任务放行，而真正要跑的是 1 号星、它是 0 条，
    于是 `bot_command` 抛 `NoFreeLineError` → 停用 → 下一 tick 合计仍是 2 → 恢复。
    一小时 447 个来回、1368 行日志、一发未派。

    `origins` 为 None 则走单出发星球那条，`reserved_lines` 在 `free_lines_for` 里
    按星球生效；账号那道闸对它同样有效——总数是**所有星球共享**的，海盗那条链路
    一样占里面的位子。

    `account_free` 为 `None` = 用户没填账号上限，那道闸不生效（不是「用默认值」，
    见 `domain.scheduler.account_free_lines`），于是行为与只有每星预算那一道时
    完全一致。

    `inflight` 由调用方按出发星球缓存好（同一颗星球一次 tick 只查一次），
    `account_free` 也由调用方算好（一次 tick 只查一次全账号在飞数），
    这一层不查库。
    """
    if origins is not None:
        budgets = _origin_budgets(origins, inflight=inflight, account_free=account_free)
        return max((item.fleet_lines for item in budgets), default=0)
    return _capped(
        free_lines_for(
            task, inflight_from_origin=inflight[task.origin], reserved_lines=reserved_lines
        ),
        account_free,
    )


def _known(kind: str) -> bool:
    """库里出现不认识的 kind（手改或旧版本留下的）就跳过，不让调度器崩掉。"""
    return kind in {item.value for item in MissionKind}


def _ai_presets(assignments: Sequence[AssignedTarget]) -> frozenset[str]:
    """AI 只能从本轮算法实际用到的预设里选——那些才在游戏的预设条上。"""
    return frozenset(item.preset for item in assignments)


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


def _ranking_scan_cooldown(raw: str) -> timedelta | None:
    """两轮军力榜扫描之间至少隔多久，**从上一轮开始的那一刻算起**。留空 = 不限。

    用户口径（2026-08-20）：「比如在周四，我会把 bot 攻击的军力范围选择为 6 小时。
    但是我又不希望太多的扫描打断派出攻击。所以我会设定扫描间隔为 2 小时。当新的
    扫描发起时，检查上次开始扫描的时候是否大于 2 小时。当周一时，我会将军力范围
    选择为 2 小时，扫描冷却为 1 小时，这样尽快的轮转。」

    ## 为什么住在 `mission_tasks.params_json`，而不是 `military_attack_config`

    ⚠️ **它仍然是任务级的**，理由来自用户 2026-08-20 那段话的第二半：**扫描任务
    将来可能不止一个**，一个全局值配不了「这个扫得勤、那个扫得稀」。

    ⚠️ **那段话的第一半 2026-08-23 已经不成立了，别再照它推理。** 它说的是「它和
    同样按周内相位调的『军力分数有效期』是配套的一对，两个数分居两张表，改一次要
    跑两个页面」——而有效期那一格当日搬成了全局设置（用户口径：「军力攻击的有效期
    门限 改为全局设置，不再根据单个星系进行调整」），于是这两个数**本来就分居两处
    了**：扫描间隔在任务上，有效期在攻击配置页。那个「配套的一对」的论据因此只剩
    「记得一起调」这一句，不再能用来推「它们该住在同一张表」。

    ⚠️ 「一起调」这件事本身没有消失，而且更要紧了：扫完一轮的时间必须短于有效期，
    否则池子永远追不上。守它的不是「同页」，是本函数的安全阀（窗口内候选低于门限时
    这个间隔立刻让路）加上放宽窗口那条 WARNING。

    ## 为什么没有代码默认值

    ⚠️ **留空 = 不施加冷却，行为与加这个旋钮之前逐字相同。** 理由照抄
    `military_attack_config.blind_scrolls` 那一列：给了默认值就分不开「没配」
    和「恰好配成当前默认」，而这两件事在默认值将来被改动时的处置完全相反——
    前者该跟着新默认走，后者该纹丝不动。

    ⚠️ **`0` 与负数一律拒掉，不当成「不限」。** 同 `_ranking_bot_limit` 那个 0：
    「不限」有一个明明白白的表达方式（把框留空），用一个看起来像时长的数字去
    表达它，只会让下一个读库的人分不清那是「用户想不限」还是「用户填错了」。

    没有为它加数据库列：它和 `bot_limit` 一样是任务参数，住在 `params_json` 里
    （见 `storage.models` 那一行的注释）。
    """
    value = _params(raw).get("scan_cooldown_hours")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # `bool` 是 `int` 的子类，得单独排掉（同 `_int_param` 那条）：`True` 会被当成
    # 冷却 1 小时，而用户敲进去的根本不是一个时长。
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError("扫描间隔必须是小时数；不想限制就把它留空")
    try:
        hours = float(value)
    except ValueError as exc:
        raise MissionParamError(f"扫描间隔不是数字：{value!r}") from exc
    if hours <= 0:
        raise MissionParamError("扫描间隔必须大于 0 小时；不想限制就把它留空，别填 0")
    return timedelta(hours=hours)


def _blind_scroll_rows(value: object) -> int | None:
    """军力榜开榜后先盲滚几**行**。**留空 = 用 `BLIND_SCROLL_ROWS` 的默认值 700。**

    形状与 `_blind_scrolls` 逐字一致（页面上两个框并排放着，校验形状不同只会让
    保存那条路上多一处特例），单位从屏换成行。

    ⚠️ **「没配」和「配了 0」是两回事，两个都合法。** 留空是「跟着默认走」；
    `0` 是用户真的敲进去的「一行都别盲滚，从第一屏就开始检测 bot」——那是
    **最保守**的取值（多花几屏廉价检测，绝不可能滚过头），所以它必须放行，
    而不是像 `bot_limit` 那个 0 一样当成「把链路关掉」而拒绝。

    ⚠️ **不设上界，一个都不加**（用户口径 2026-08-22：盲滚行数由用户定，助手不做
    越界判断）。尤其不许拿 `game.ranking_ui.FIRST_BOT_RANK`(587) 当上界：那个
    「bot 起点」是**玩家改名伪装**出来的（判据只看名字前缀 `bot_`，改名的真人一样
    命中），真 bot 区在更后面，所以 700 行并不越界。拿一个被伪装污染的边界报警，
    比不报警更坏——而这里报警的代价还格外高：`MissionParamError` 的后果是**自动
    停用到用户手动恢复为止**，一个「拦一下」就能把整夜的采集关掉。

    滚过头的代价仍然是真的（**静悄悄少采一截**，页面和日志都看不出），所以那句
    警告留在界面上；但它是**提示**，不是拦路。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    # `bool` 是 `int` 的子类，得单独排掉（同 `_int_param` 那条）：`True` 会被
    # 当成盲滚 1 行，而用户敲进去的根本不是一个行数。
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError("盲滚行数必须是整数；要用默认值就把它留空")
    try:
        rows = int(value)
    except ValueError as exc:
        raise MissionParamError(f"盲滚行数不是整数：{value!r}") from exc
    if isinstance(value, float) and rows != value:
        raise MissionParamError(f"盲滚行数必须是整数：{value!r}")
    if rows < 0:
        raise MissionParamError("盲滚行数不能是负数；要用默认值就把它留空")
    return rows


def _blind_scrolls(value: object) -> int | None:
    """军力榜开榜后先盲拖几屏。**留空 = 用 `BLIND_SCROLLS` 的默认值 40。**

    ⚠️ **口径 2026-08-22 改行之后，驱动盲滚的是 `_blind_scroll_rows`。** 这一个
    仍然是活的：页面上那个框和 `military_attack_config.blind_scrolls` 那一列都
    留着当回滚杠杆，保存前照旧要用同一把尺子量一遍。

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


def _protection_exclusion_hours(value: object) -> int | None:
    """撞上保护期之后排除多久（小时）。**留空 = 默认 8 小时。**

    ## 两条边界

    - **至少 1 小时。** 0 等于取消排除，而这正是这条功能要修的那个缺陷本身：
      被排除掉的目标会被每一轮重新挑中、每轮每个白烧约 2.9 分钟鼠标时间，直到
      保护期自然过去。填 0 的人多半以为自己在「放宽一点」，实际是把它关掉。
    - **最多 `PROTECTION_EXCLUSION_MAX_HOURS`（24 小时）。** 理由在那个常量上：
      保护期最长 8 小时，8 以上纯属保守余量；越过 24 就开始和
      `bot_revisit_hours` 争同一件事。
    """
    hours = _optional_int(value, label="保护期排除时长（小时）")
    if hours is None:
        return None
    if hours < 1:
        raise MissionParamError(
            "保护期排除时长至少 1 小时：填 0 等于取消排除，而撞上保护期的目标"
            "会被下一轮原样挑中，每轮每个白跑约 2.9 分钟鼠标时间。"
        )
    if hours > PROTECTION_EXCLUSION_MAX_HOURS:
        raise MissionParamError(
            f"保护期排除时长最多 {PROTECTION_EXCLUSION_MAX_HOURS} 小时："
            "游戏的保护期最长 8 小时，再往上只是保守余量；超过一天就和"
            "「bot 重复攻击间隔」争同一件事，排障时分不清目标是被哪一条挡住的。"
        )
    return hours


def _unreadable_exclusion_hours(value: object) -> int | None:
    """面板名读不出之后排除多久（小时）。**留空 = 默认 6 小时。**

    ## 两条边界

    - **至少 1 小时。** 0 等于取消排除，而这正是这条功能要修的缺陷本身：读不出的
      坐标会被每一轮重新挑中，每轮白花 21--44 秒鼠标时间，更贵的是整轮空手而归
      之后 `waiting_for_a_line` 把那颗球压住 1--2 小时。填 0 的人多半以为自己在
      「放宽一点」，实际是把它关掉。而且实测那几个坐标约**每小时**就会被重新挑中
      一次，窗口不明显大于 1 小时等于没排。
    - **最多 `UNREADABLE_EXCLUSION_MAX_HOURS`（24 小时）。** 理由在那个常量上：
      越过一天就和 `bot_revisit_hours` 争同一件事；而且「读不出」的根因至今不明，
      按一个还没查清的现象把高军力目标锁掉超过一天，赌的是一个没有证据的结论。
    """
    hours = _optional_int(value, label="面板名读不出排除时长（小时）")
    if hours is None:
        return None
    if hours < 1:
        raise MissionParamError(
            "面板名读不出排除时长至少 1 小时：填 0 等于取消排除，而读不出的坐标会被"
            "下一轮原样挑中，白跑一趟之后整轮空手而归、把那颗球的航线压住一两个小时。"
        )
    if hours > UNREADABLE_EXCLUSION_MAX_HOURS:
        raise MissionParamError(
            f"面板名读不出排除时长最多 {UNREADABLE_EXCLUSION_MAX_HOURS} 小时："
            "超过一天就和「bot 重复攻击间隔」争同一件事；而且面板名为什么读不出至今"
            "没查清，按它把一个高军力目标锁掉一整天，赌的是一个还没有证据的结论。"
        )
    return hours


def _optional_number(value: object, *, label: str) -> float | None:
    """同 `_optional_int`，但**不要求是整数**；留空返回 `None`。

    专为「军力分数有效期」准备：它一直允许 1.5 小时（页面上步长 0.5），
    拿 `_optional_int` 去量会把一个合法取值当场判成非法，而错误话术里说的是
    「必须是整数」——用户看不出这句话是从哪来的。

    `bool` 一样单独排掉（理由同 `_optional_int`）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise MissionParamError(f"{label}必须是数字；要用默认值就把它留空")
    try:
        return float(value)
    except ValueError as exc:
        raise MissionParamError(f"{label}不是数字：{value!r}") from exc


def _score_max_age_hours(value: object) -> float | None:
    """军力分数有效期（小时）。**留空 = 默认 `DEFAULT_SCORE_MAX_AGE`（2 小时）。**

    ## 两条边界

    - **必须是正数。** 0（和负数）等于「一条读数都不算新」，那时窗口内恒为 0 个、
      永远不足门限，于是**每一轮都放弃窗口**——这个旋钮被填成了它的反面：
      看起来是「只用最新数据」，实际是「一律拿全部旧读数打」，而页面上只会显示
      那句正常的「军力读数已放宽窗口」。填 0 的人多半以为自己在收紧。
    - **最多 `SCORE_MAX_AGE_MAX_HOURS`（168 小时 = 一周）。** 理由在那个常量上：
      第 2 步的周期边界已经把上周期的读数整批挡在外面，超过一周之后这个数**再也
      挡不掉任何东西**——一个填了却什么都不做的旋钮比没有它更坏。
    """
    hours = _optional_number(value, label="军力分数有效期（小时）")
    if hours is None:
        return None
    if hours <= 0:
        raise MissionParamError(
            "军力分数有效期必须是正数：填 0 等于「没有一条读数算新」，"
            "于是每一轮都会放弃窗口、改用全部旧读数——和你想要的正好相反。"
            "要用默认值就把它留空。"
        )
    if hours > SCORE_MAX_AGE_MAX_HOURS:
        raise MissionParamError(
            f"军力分数有效期最多 {SCORE_MAX_AGE_MAX_HOURS} 小时（一周）："
            "bot 军力每周一 UTC+0 刷新，上周期的读数本来就整批不参与选靶，"
            "再往上填这个数挡不掉任何目标。"
        )
    return hours


def _window_floor_value(value: object) -> int | None:
    """选靶第 3 步的窗口门限。**留空 = 默认 `WINDOW_POOL_FLOOR`（100）。**

    ## 一条边界

    - **至少 1。** 0 等于「窗口内一个都不用有也算够」，于是窗口**永远不会被放弃**
      ——听起来像是「更严格」，实际后果相反：窗口内真的一个都没有的夜里（周一凌晨
      正是如此），这一轮就在一个空池子上选靶、一发不派，而那句本该响的
      「军力读数已放宽窗口」一个字都不会写。**这道闸存在的意义就是别悄悄停摆。**

    上界**刻意不设**：门限该多大取决于候选池此刻有多少个（实测 3000+ 个 bot，
    而窗口内能有多少又取决于扫描节奏），写死一个数就是拿一个凭空的上界去卡用户
    真实的处境。填得比池子还大只会让窗口每轮都被放弃，而那件事**会告警**，
    从日志里一眼看得出来。
    """
    floor = _optional_int(value, label="窗口门限（个）")
    if floor is None:
        return None
    if floor < 1:
        raise MissionParamError(
            "窗口门限至少为 1：填 0 等于窗口永远不会被放弃，"
            "于是窗口内真的一个目标都没有时这一轮会在空池子上选靶、一发不派，"
            "而那句「军力读数已放宽窗口」的告警一个字都不会写。"
        )
    return floor


def _account_line_limit(value: object) -> int | None:
    """全账号同时能在飞的舰队上限。**留空 = 不施加这道闸**（不是「用某个默认值」）。

    用户口径（2026-08-18）：「账号的默认权限不应在代码中进行配置，直接用航线限制
    就可以了，因为实际通过科技升级，使用道具，人为占用，都会影响到留给你的航线
    数量」。整段理由在 `domain.scheduler.account_free_lines` 上。

    ## 两条边界

    - **至少 1。** 0 等于「一发都不许派」，而那是用复选框表达的意思，不该用一个
      看起来像容量的数字表达；填了 0 之后整台助手会安静地一夜不动。要「不限制」
      就留空。
    - **最多 `ACCOUNT_LINE_LIMIT_MAX`**，防手滑。它不是策略上的界——助手不该替
      游戏写死一个数（科技会升、道具会开），只挡住明显不可能的取值。

    ⚠️ **超过这个数的「已配航线」不在这里拦。** 用户口径（2026-08-18）：
    「星球的航线是我来配置的，我配置时已经手动确认了不会超过总航线数」——他可能
    正在编辑中途，也可能先配好再去开道具。页面只把「已配 X 条 / 上限 Y 条」摆出来
    让他一眼看见（`web.persistent_service._summary`），不硬拦。
    """
    limit = _optional_int(value, label="全账号航线上限")
    if limit is None:
        return None
    if limit < 1:
        raise MissionParamError(
            "全账号航线上限至少是 1：填 0 等于一发都不许派，而那要用复选框表达；"
            "要「不额外限制账号总数」就把它留空。"
        )
    if limit > ACCOUNT_LINE_LIMIT_MAX:
        raise MissionParamError(f"全账号航线上限最多 {ACCOUNT_LINE_LIMIT_MAX} 条；填错了吧？")
    return limit


def _auto_toggle_log_seconds(value: object) -> int | None:
    """调度器重复日志的限流窗口（秒）。**留空 = `REPEATED_LOG_WINDOW`（120 秒）。**

    函数名与数据库那一列都还叫 `auto_toggle_log_seconds`：那是它 2026-08-18 之前
    只管「自动停用 / 自动恢复」时留下的历史名。改名要迁移、要动页面、要动 API 字段，
    换不来任何用户可见的好处，所以只把**用户看得见的措辞**跟着改了。

    ## 两条边界

    - **0 合法，而且它不是「关掉日志」**：0 表示不限流，也就是加这道闸之前的行为
      ——每一次都落一条。排障时想看清抖动的真实频率就填 0，代价是一次反复抖动能
      像 2026-08-18 01:00 那样写 1368 行、或者像同日 16:00 那样写 12,155 行。
    - **最多 `REPEATED_LOG_MAX_SECONDS`（一小时）。** 再长就把一整夜的抖动合并
      成寥寥几条，「抖了几百次」这个事实只剩 payload 里一个数字撑着，翻日志的人
      按时间线读不出任何频率。
    """
    seconds = _optional_int(value, label="调度器重复日志窗口（秒）")
    if seconds is None:
        return None
    if seconds < 0:
        raise MissionParamError("调度器重复日志窗口不能是负数；要每次都记就填 0")
    if seconds > REPEATED_LOG_MAX_SECONDS:
        raise MissionParamError(
            f"调度器重复日志窗口最多 {REPEATED_LOG_MAX_SECONDS} 秒（一小时）："
            "再长就把一整夜的抖动合并成寥寥几条，按时间线读不出频率。"
        )
    return seconds


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


def _bot_max_score(raw: str) -> float | None:
    """军力上限，超过就不打。没配就是不设限。

    用户 2026-08-14 要求过「军力确实要设置上限」——太强的目标不是当前预设
    打得动的。留成可配而不是写死：上限取决于用的哪个预设，而预设是用户维护的。
    """
    value = _params(raw).get("max_score")
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value)


#: 存量任务的 `params_json` 里可能还存着的那三个键——**有效期与窗口门限从前是
#: 按任务配的**。2026-08-23 改成全局之后它们**一律被忽略**（用户口径：「军力攻击的
#: 有效期 门限 改为全局设置，不再根据单个星系进行调整」）。
#:
#: `rescan_after_hours` 是有效期最早的名字；`score_max_age_hours` 是它改名之后、
#: 搬家之前的名字；`top_n` 是窗口门限在 `params_json` 里的键。
_LEGACY_WINDOW_KEYS = ("score_max_age_hours", "rescan_after_hours", "top_n")


def _legacy_window_keys(raw: str) -> dict[str, object]:
    """存量任务参数里还留着的**已失效**的有效期/窗口门限值，键 → 值。

    ## 为什么是「忽略并告警」，而不是迁移，也不是照旧读

    改成全局之后这两格有唯一的一份取值（`military_attack_config`）。存量的
    `params_json` 里每个军力任务各存着一份自己的，三条路各有代价：

    - **照旧读**：那就不是全局设置，用户改了攻击配置页也不生效——需求本身没做到。
    - **自动迁移**（把某个任务的值搬进全局）：库里有多个军力任务，搬哪一份都是替
      用户拍一个数。拍错的症状是**所有星系一起换了个有效期**，而页面上看不出这个
      数是从哪来的。这个数该是多少只有用户知道，所以不替他决定。
    - **忽略并告警**（现在这条）：全局那两列留空 = 跟着代码默认走（2 小时 / 100 个），
      同时每一轮派遣往 `system_log` 落一条 WARNING，说清「这个任务里存着一个
      X 小时的旧值，已忽略，这一轮实际用的是全局的 Y 小时」。

    ⚠️ **判据必须是「这个键在不在」，不能是「它等不等于默认值」。** 后者会把
    「用户当年就配的 2 小时」判成没配过，于是那条任务永远不告警——而它同样存着
    一个再也不生效的值，用户同样需要知道这件事。

    ⚠️ **不在这里抛异常，一条路都不许。** 旧值不合法（负数、字符串）只是被忽略；
    `params_json` 整个读不出来也只是返回空，不往外抛 `MissionParamError`——抛出去
    会一路走到 `disable_mission_task` 把任务停用、挂上 `disabled_reason`，用户不去
    页面点一次「恢复」就永远不跑。**这是一条日志用的辅助判据，它没有资格成为派遣
    链路的一个失败点。** 坏 JSON 在别处（`_bot_by_military` 那条路）已经有自己的
    善后，那才是该报它的地方。
    """
    try:
        params = _params(raw)
    except MissionParamError:
        return {}
    return {key: params[key] for key in _LEGACY_WINDOW_KEYS if key in params}


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
