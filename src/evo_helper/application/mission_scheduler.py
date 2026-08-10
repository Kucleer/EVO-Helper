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
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from evo_helper.application.mission_supervisor import (
    MissionExit,
    MissionSupervisor,
    RunningChild,
    StopReason,
)
from evo_helper.domain.bot_round import BotPhase, phase_of
from evo_helper.domain.missions import (
    ORIGIN,
    MissionParamError,
    bot_command,
    bot_targets_in_range,
    pirate_command,
    pirate_systems,
    scan_command,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
from evo_helper.domain.report_wait import MAX_REPORT_AGE, ReportWaitPlanner, WaitAction
from evo_helper.domain.scheduler import (
    Action,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskSnapshot,
    decide,
    quota_day_start_utc,
)
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

#: 同一任务连续这么多次异常退出就自动停用。
#:
#: 没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环：起、崩、
#: 下一 tick 判据仍为真、再起。失败多半是「窗口抢不到前台」或「甩鼠标触发
#: FAILSAFE」，重试只会再来一遍，所以三次就够——再多只是多刷几行日志。
MAX_CONSECUTIVE_FAILURES = 3

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
    config: orm.SchedulerConfigRow
    facts: SchedulerFacts


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
    ) -> None:
        self._repository = repository
        self._supervisor = supervisor
        self._clock = clock
        #: 「该等还是该收」只能有一份实现，所以复用 runner 那一套 planner，
        #: 不在这里另写一遍 SQL 判据。
        self._planner = planner or ReportWaitPlanner()
        self._enabled = False
        self._started_at_utc: datetime | None = None
        #: 开机时认出的孤儿进程号。只显示，不据此杀进程。用户点了「强制结束」
        #: 就清掉——那一下的含义是「我知道了，别再提醒我」。
        self._orphan_pid: int | None = None
        self._run_id: UUID | None = None
        #: tick 跑在后台线程里，而页面的「开始 / 结束」来自请求线程。没有这把锁，
        #: 一次「结束」可能正好落在 tick 的「起进程」中间——supervisor 停掉的是
        #: 上一个，紧接着 tick 又起了一个新的，于是控制台以为已经停了，实际还有
        #: 一个 runner 在点鼠标。这直接违反「任何时刻最多一个子进程」。
        #: 可重入锁：`stop()` 与 `tick()` 内部都会再调 `_finish()`。
        self._lock = threading.RLock()

    # -- 对外 ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def current(self) -> RunningChild | None:
        return self._supervisor.running

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

    def start(self) -> None:
        with self._lock:
            # 已经在跑就不要把秒表按回零：连点两下「开始」不该让页面显示成
            # 刚刚才启动。
            if not self._enabled:
                self._started_at_utc = self._clock()
            self._enabled = True

    def stop(self) -> None:
        """用户点「结束」。立刻杀，不等它跑完手上这一个。"""
        with self._lock:
            self._enabled = False
            self._started_at_utc = None
            self._finish(self._supervisor.stop(StopReason.USER))

    def shutdown(self) -> None:
        """控制台关闭时清场，覆盖「正常重启」这条最常见的路径。"""
        with self._lock:
            self._enabled = False
            self._started_at_utc = None
            self._finish(self._supervisor.stop(StopReason.SHUTDOWN))

    def force_kill(self) -> None:
        """页面顶部那条红条上的「强制结束」。

        只做两件事：**停掉我们自己手上的那个子进程**，**把台账里还没闭合的行
        闭合掉**。绝不按 pid 去杀一个不认识的进程——pid 会被系统回收复用，
        那一枪可能打在别人身上。

        它顺带把调度器停掉（走 `stop()`）：只杀不停的话，下一个 tick 立刻又起
        一个新的，按钮看上去毫无作用。「强制结束」的用户口径是全停。
        """
        with self._lock:
            self.stop()
            self._repository.mark_orphan_mission_runs(ended_at_utc=self._clock())
            self._orphan_pid = None

    def snapshot(self) -> SchedulerSnapshot:
        """当前的完整现状。页面每几秒问一次，桌面悬浮窗也问同一个。

        走的是和 `tick()` 同一套 `_facts`，所以页面上看到的判据依据与调度器
        下一步据以行动的是同一份事实。
        """
        with self._lock:
            tasks = self._repository.mission_tasks()
            config = self._repository.scheduler_config()
            return SchedulerSnapshot(
                enabled=self._enabled,
                started_at_utc=self._started_at_utc,
                running=self._supervisor.running,
                orphan_pid=self._orphan_pid,
                tasks=tuple(tasks),
                config=config,
                facts=self._facts(tasks, config, self._clock()),
            )

    def begin_bot_round(self) -> None:
        """页面上的「重开一轮」：把 `round_started_at_utc` 推到当前。

        走调度器的时钟而不是调用方自己取一个 `now()`：本轮的起点和判定完成度
        时用的「现在」必须同源，否则两个时钟差一点，刚开的一轮就可能把边界上
        那条战报算成本轮的。
        """
        with self._lock:
            self._repository.begin_bot_round(now_utc=self._clock())

    def command_for(self, kind: MissionKind, params_json: str) -> list[str]:
        """把一份参数换算成命令行，换不出来就抛 `MissionParamError`。

        对外开放是为了让 API 能在**写库之前**用调度器自己的那把尺子量一遍：
        范围内一个 bot 都没有、半径 ≤ 0、系号区间首尾颠倒，这些配置存下来只会
        让调度器起一个必然空转的 runner，或者干脆在启动时把任务自动停用——
        两种都要等用户下次看页面才发现。校验必须和启动走同一段代码，否则
        「页面收下了、调度器起不来」这种分歧迟早出现。
        """
        return self._command_for(kind, params_json)

    def tick(self) -> None:
        """每秒一次。收退出码、看判据、该起就起。

        收退出码不能只在页面轮询时做——没人开着页面时，那条记录会一直挂在
        「运行中」，而连续失败也就永远数不到三。
        """
        with self._lock:
            self._finish(self._supervisor.poll())
            if not self._enabled:
                return
            # 一个任务因参数不合格被就地停用后要能立刻让位给下一个，否则这一秒
            # 谁都不跑。上限取任务条数：每转一圈至少停用一个，不可能无限转。
            for _ in range(len(MissionKind)):
                if not self._step():
                    return

    # -- 一次决策 --------------------------------------------------------------

    def _step(self) -> bool:
        """走一遍「读事实 → 判 → 起」。返回 True 表示刚停用了谁，值得再算一次。"""
        now = self._clock()
        tasks = self._repository.mission_tasks()
        config = self._repository.scheduler_config()
        facts = self._facts(tasks, config, now)
        running = self._supervisor.running
        decision = decide(
            [task_snapshot(row) for row in tasks if _known(row.kind)],
            facts,
            running=(
                None
                if running is None
                else RunningProcess(kind=running.kind, started_at_utc=running.started_at_utc)
            ),
            min_dwell=timedelta(seconds=config.min_dwell_seconds),
            restart_cooldown=timedelta(seconds=config.restart_cooldown_seconds),
        )
        if decision.action is Action.IDLE or decision.kind is None:
            return False
        if decision.action is Action.PREEMPT:
            # 只有扫描会被抢占（判据保证），它的游标持久化，随时可断。
            self._finish(self._supervisor.stop(StopReason.PREEMPTED))
        return not self._launch(decision.kind, tasks)

    def _launch(self, kind: MissionKind, tasks: Sequence[orm.MissionTaskRow]) -> bool:
        """组命令行、起进程、记账。参数不合格则停用该任务并返回 False。

        `MissionParamError` 必须在这里被接住：让它冒出去就是整个调度循环停摆，
        而它表达的只是「这条链路的配置填错了」——另外两条没有理由跟着停。
        """
        row = _row_for(tasks, kind)
        try:
            command = self._command_for(kind, row.params_json)
        except MissionParamError as exc:
            self._repository.disable_mission_task(kind, str(exc))
            return False
        child = self._supervisor.start(kind, command)
        self._run_id = self._repository.begin_mission_run(
            kind,
            command=command,
            pid=child.pid,
            started_at_utc=child.started_at_utc,
            log_path=str(child.log_path),
        )
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
        if exited.failed:
            self._repository.record_mission_failure(
                exited.kind, exit_code=exited.exit_code, limit=MAX_CONSECUTIVE_FAILURES
            )
        elif exited.stopped_by is StopReason.SELF:
            # 跑完一轮且退出码为 0。抢占和用户点停不动这个计数——那是我们
            # 自己动的手，任务本身没毛病。
            self._repository.clear_mission_failures(exited.kind)

    # -- 事实 ------------------------------------------------------------------

    def _facts(
        self,
        tasks: Sequence[orm.MissionTaskRow],
        config: orm.SchedulerConfigRow,
        now: datetime,
    ) -> SchedulerFacts:
        """一次调度所需的全部事实，全部来自数据库。

        没在参与调度的链路一律不去查：bot 的完成判据要按目标逐个问库，
        而 tick 每秒一次。查了也只是丢掉。
        """
        active = {
            MissionKind(row.kind)
            for row in tasks
            if _known(row.kind) and row.enabled and row.disabled_reason is None
        }
        grace = timedelta(minutes=config.report_grace_minutes)
        pirate_row = _row_for(tasks, MissionKind.PIRATE)
        return SchedulerFacts(
            now_utc=now,
            free_lines=self._free_lines(config, now),
            pirate_dispatches_today=(
                self._repository.count_dispatches_since(
                    TARGET_KIND_PIRATE, since=quota_day_start_utc(now)
                )
                if MissionKind.PIRATE in active
                else 0
            ),
            pirate_quota=config.pirate_daily_quota,
            pirate_blocked_until_utc=pirate_row.quota_exhausted_until_utc,
            pirate_reports_due=(
                self._reports_due(MissionKind.PIRATE, now, grace)
                if MissionKind.PIRATE in active
                else False
            ),
            bot_reports_due=(
                self._reports_due(MissionKind.BOT, now, grace)
                if MissionKind.BOT in active
                else False
            ),
            bot_targets_remaining=(
                self._bot_remaining(_row_for(tasks, MissionKind.BOT))
                if MissionKind.BOT in active
                else 0
            ),
            last_started_at_utc=self._repository.last_mission_starts(),
        )

    def _free_lines(self, config: orm.SchedulerConfigRow, now: datetime) -> int:
        """空闲航线的**乐观估算**——不含用户自己派出去的舰队。

        权威闸门仍在 runner 的 `game.capacity.LineCapacityGate`（它看屏）。
        这里估高了，最坏结果是 runner 起来发现没位子、空跑一轮就退，不会误派；
        `reserved_lines` 正是为这段误差留的缓冲。**不要把看屏搬进调度器。**
        """
        usable = max(config.fleet_line_limit - config.reserved_lines, 0)
        return max(usable - self._repository.count_inflight(now_utc=now), 0)

    def _reports_due(self, kind: MissionKind, now: datetime, grace: timedelta) -> bool:
        """这条链路有没有到期未收的战报。

        **`grace` 与 `max_age` 是两档完全不同的规则，不能互换也不能同值。**
        `grace` 管「飞行时间读到了」的那些：过了预计时间再等这么久还没战报就
        判缺失。`max_age` 管「读不到」的那些：`ReportWaitPlanner` 见到任何一条
        NULL 就无条件返回 `COLLECT`，没有按派出时刻算的放弃阈值，这一档就既
        永远「可收」又永远不被判缺失——调度器每个 tick 都去收一封永远不会到的
        战报，扫描永远抢不到空隙。
        """
        pending = self._repository.pending_reports_for_kind(
            _TARGET_KIND[kind], now_utc=now, grace=grace, max_age=MAX_REPORT_AGE
        )
        return self._planner.plan(pending, now_utc=now).action is WaitAction.COLLECT

    def _bot_remaining(self, row: orm.MissionTaskRow) -> int:
        """本轮范围内还有几个 bot 没走完。

        完成 = 收到**攻击发**（非探路预设）的战报。分档判为「不值得打」而没派
        攻击的目标同样算完成——它已经走完该走的流程。这两条都在
        `domain.bot_round.phase_of` 里，这里只负责把事实喂给它。
        """
        try:
            targets = bot_targets_in_range(self._bot_targets(), **_bot_range(row.params_json))
        except MissionParamError as exc:
            self._repository.disable_mission_task(MissionKind.BOT, str(exc))
            return 0
        return sum(
            1
            for target in targets
            if phase_of(self._repository.bot_dispatch_facts(target, since=row.round_started_at_utc))
            is not BotPhase.DONE
        )

    def _bot_targets(self) -> list[Coordinate]:
        return [
            Coordinate(row.galaxy, row.system, row.position)
            for row in self._repository.list_bot_targets()
        ]

    # -- 参数换算 --------------------------------------------------------------

    def _command_for(self, kind: MissionKind, params_json: str) -> list[str]:
        """三条链路各有各的换算，`domain.missions` 里是纯函数。

        刻意不做成一个 `mission_command(kind, params)`：三条链路的参数类型本来
        就不通，合成一个入口就得让 `params` 退化成 `dict[str, Any]`，在 strict
        mypy 下等于放弃检查。
        """
        if kind is MissionKind.SCAN:
            return scan_command()
        if kind is MissionKind.PIRATE:
            return pirate_command(pirate_systems(ORIGIN, _pirate_radius(params_json)))
        return bot_command(bot_targets_in_range(self._bot_targets(), **_bot_range(params_json)))


def task_snapshot(row: orm.MissionTaskRow) -> TaskSnapshot:
    """一行 `mission_tasks` → 领域层认识的那个不可变快照。

    公开是给 API 用的：页面要按 `domain.scheduler` 的判据算状态和展示次序，
    而它拿到的只有 ORM 行。转换只能有一份，否则两边对「什么算已停用」的
    理解迟早分家。
    """
    return TaskSnapshot(
        kind=MissionKind(row.kind),
        enabled=row.enabled,
        priority=row.priority,
        disabled_reason=row.disabled_reason,
    )


def _known(kind: str) -> bool:
    """库里出现不认识的 kind（手改或旧版本留下的）就跳过，不让调度器崩掉。"""
    return kind in {item.value for item in MissionKind}


def _row_for(tasks: Sequence[orm.MissionTaskRow], kind: MissionKind) -> orm.MissionTaskRow:
    for row in tasks:
        if row.kind == kind.value:
            return row
    raise ValueError(f"mission_tasks 里没有 {kind.value} 这一行；先调 prepare()")


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


def _bot_range(raw: str) -> dict[str, int]:
    data = _params(raw)
    return {
        "galaxy": _int_param(data, "galaxy"),
        "first_system": _int_param(data, "first_system"),
        "last_system": _int_param(data, "last_system"),
    }
