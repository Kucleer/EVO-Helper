"""战报补录 / 对账：一批**优先于所有任务**的一次性子进程。

信箱里躺着没读进库的战报时，这条路把它们捞回来
（`python -m evo_helper.tools.backfill_reports`）。两个入口，同一套机制：

- **手动补录**：用户在页面上点一次，选链路与起始日期。
- **启动对账**：用户点「开始」时自动排一批（海盗一趟、bot 一趟——两条链路的
  信箱主题不同，一趟只读得了一种），**跑完才放行任务**。

它不是一条链路，不进调度：`domain.scheduler.MissionKind` 里没有、也不该有一档
`BACKFILL`（它不派遣、没有配额、没有优先级）。

## 为什么它的优先级高于任务，而不是给任务让路

补录改的正是任务读来做决策的那批数据。用户口径（2026-08-13）：

> 因为你补录之后，你就知道你还剩下几次海盗攻击机会，有哪些 bot 已经攻击了，
> 不要再重复攻击

> 启动调度台之后，先检查有多少应读未读战报 → 读完所有应读未读战报 → 结合当前
> 任务状态的进度，优先发出应读未读战报的后续操作 → 继续执行任务，但是已攻击的
> 海盗/BOT 不再重复侦查/攻击

机制上确实如此，而且是**会白打舰队**的那一档：

- bot 每个目标的态由 `domain.bot_round.phase_of` 判。战报没回来时它是
  `AWAITING_ATTACK_REPORT`（等着，不重打），**但** `bot_dispatch_facts` 会按
  `MAX_REPORT_AGE`（6 小时）把过期派遣整条剔掉，于是这个目标退回成「本轮一发
  都没打过」→ `NEEDS_ATTACK` → **被重打一遍**。2026-08-12 那 15 发丢掉战报的
  bot 攻击此刻正坐在这个状态上。
- 海盗当日配额数的是 `count_dispatches_since`。战报没入库不影响派遣计数，但
  「这一发打赢没有」影响的是要不要再打——同一个道理。

所以「先补录、再跑任务」是硬要求，不是优化。落地成四段：

1. 请求进来时正在跑**扫描** → 立刻抢占（`StopReason.PREEMPTED`，扫描的游标
   持久化，随时可断）。
2. 正在跑**海盗 / bot** → 进 `PENDING`，**等它自己跑完，绝不硬杀**。
   它们可能正卡在「点了出发」和「把这一发记进库」之间，硬杀会留下一发飞出去了
   却没记账的舰队，而那正是战报永远配不上的成因。页面上要看得出在等谁。
3. 窗口空出来 → 起补录，**期间一律不许起任何任务**（闸门在
   `MissionScheduler._act`）。一批里有几趟就依次跑几趟。
4. 整批跑完**不自动放行**：把「改了什么」摆到页面上，等用户点一下「继续任务」。
   见 `BackfillState.blocking` 上那段。

第 1、3、4 段由 `MissionScheduler._advance_backfill` 驱动，判据在这里
（`active` / `blocking` 两个属性），动手在那边——同 `MissionSupervisor` 与调度
循环的分工。

## 启动对账为什么不怕慢

那趟信箱**单子一空就早停**（`tools.pirate_loop` 里已有的逻辑：撞见一封库里已有
的、且单子上没有到点没战报的派遣了，就收工）。所以 `--max-opens` 给大是安全的
——**它是封顶，不是指标**。没有欠账时几十秒走完，有欠账时才真的开封。因此启动
那趟直接用 CLI 自己的默认预算，不另调小。

## 一个游戏窗口，一只鼠标

`MissionSupervisor` 那条不变量在这里同样成立，而且是**跨两个进程管理器**的：
补录的子进程不由 supervisor 管（它只认 `MissionKind`）。两边谁都不知道对方的
存在，让它们不打架的唯一办法就是那道闸门：`blocking` 为真时调度器一个任务都不
起，而补录只在 `supervisor.running is None` 时才起。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_supervisor import LOG_DIR, TERMINATE_TIMEOUT_S, Process
from evo_helper.storage import models as orm

#: 能补录的两条链路。取值就是 CLI 的 `--kind`，页面上那个下拉框也用同一批词——
#: 两边各写一份，哪天多一条链路就会有一边漏掉。
#:
#: **次序就是启动对账的次序**：海盗在前（每天 32 次配额是账号级的，先把它数准），
#: bot 在后。
BACKFILL_KINDS: tuple[str, ...] = ("pirate", "bot")

#: 页面上日志尾巴默认给多少行。补录最坏要跑十几分钟（60 封 × 约 15 秒），
#: 按钮点下去之后页面不能只是「没反应」，而进度的唯一来源就是这份日志。
LOG_TAIL_LINES = 40

#: 起始日期默认往回退几天。
#:
#: **默认「今天」是反的。** 游戏时间按 UTC+0 显示，UTC 的今天要到现实时间
#: （UTC+8）早上 8 点才开始——早上打开控制台点一次补录，`--since 今天` 会把
#: 昨夜那批漏掉的战报整批排除在外，而漏掉的恰恰就是昨夜的（`/logs` 那一页的
#: 「默认不按日期筛」踩的是同一个坑）。
#:
#: 退一天足以覆盖「昨晚出的事、今早来补」这条唯一的实际路径，也不至于让每次
#: 启动对账都去翻一整周的信箱。想补更早的，页面上那个日期框可以随便往前调。
DEFAULT_SINCE_DAYS_BACK = 1


def default_since(now: datetime) -> date:
    """补录起始日期的默认值（UTC 日）。页面与启动对账**共用这一个**。

    两边各算一份的话，页面上显示的和实际跑的可以是两天——而那种错静默。
    """
    return (now.astimezone(UTC) - timedelta(days=DEFAULT_SINCE_DAYS_BACK)).date()


class BackfillPhase(Enum):
    """补录此刻处在哪一段。

    值直接是中文，同 `domain.scheduler.TaskStatus`：显示层拿到就能用，不必再
    维护一张「枚举 → 文案」的映射表。色调与字形在 `web.display`——**色永远配
    一个字形和一个词**（控制台要在灰度下、对色盲用户也读得懂）。
    """

    #: 没有补录请求。
    IDLE = "未在补录"
    #: 已经排上了，正在等当前那一轮海盗 / bot 自己跑完。扫描不会停在这一档——
    #: 它当场就被抢占了。
    PENDING = "等任务结束"
    #: 补录子进程正在跑。这期间一个任务都不许起。
    RUNNING = "补录中"
    #: 整批跑完了，最后一趟退出码 0。
    DONE = "补录完成"
    #: 跑完了，退出码非 0。**留在页面上**：静默地回到「未在补录」，用户会以为
    #: 补录成功了，而那批战报其实还躺在信箱里。
    FAILED = "补录失败"
    #: 用户按了「取消」。和失败分开：一次主动取消不是故障，不该让人去翻日志。
    CANCELLED = "已取消"


#: 一次补录是谁要的。只为在页面上说清楚，判据一个字都不看它。
REASON_MANUAL = "手动补录"
REASON_STARTUP = "启动对账"


class BackfillBusyError(RuntimeError):
    """这条链路已经在排队或在跑了。

    一个游戏窗口，一只鼠标。同一条链路排两趟只会让第二趟读一遍刚读过的信箱。
    """


@dataclass(frozen=True)
class BackfillRequest:
    """排队里的一趟。"""

    kind: str
    since: date
    reason: str = REASON_MANUAL
    max_pages: int | None = None
    max_opens: int | None = None

    @property
    def command(self) -> tuple[str, ...]:
        return build_command(
            self.kind, self.since, max_pages=self.max_pages, max_opens=self.max_opens
        )


@dataclass(frozen=True)
class BackfillMeasurement:
    """整批补录**前后各量一次**的那份底数。差值就是这一批改了什么。

    量两次而不是让补录自己报数：报数得由 `tools/backfill_reports.py` 打印、再由
    这边解析它的输出，两个进程之间凭一行文本约定格式——那种约定断了不会报错，
    只会让页面上永远显示 0。数据库是两边共同的事实，量它不需要任何约定。
    """

    #: `battle_reports` 的总行数。
    reports: int
    #: 其中**认领上了一发派遣**的（`dispatch_id` 非空）。认领上了才会影响任务
    #: 决策——一份挂在那里没认领的战报，`phase_of` 根本看不见。
    claimed: int
    #: (task_id, 目标坐标) → 那一刻的态。只含参与调度的 bot 任务。
    #: 值是 `domain.bot_round.BotPhase` 的名字，拿字符串是为了让这一层不必
    #: 依赖那个枚举（差值只需要比较相等）。
    bot_phases: Mapping[tuple[int, str], str] = field(default_factory=dict)


@dataclass(frozen=True)
class BackfillSummary:
    """这一批补录到底改了什么。**跑完摆在页面上，等用户看过再放行。**

    只报差值，不报绝对值：「库里一共 580 份战报」回答不了「刚才那一趟有用吗」。
    """

    reports_ingested: int
    dispatches_claimed: int
    #: 从「本轮还要打」变成「本轮已完成」的 bot 目标数。**这就是省下来的重复
    #: 攻击**，也是这个功能眼下最直接的价值。
    bot_targets_settled: int
    #: 一共量了多少个 bot 目标。0 表示没有参与调度的 bot 任务，那时上面那个 0
    #: 的含义是「没量」而不是「一个都没变」——两者在页面上必须分得开。
    bot_targets_measured: int

    @classmethod
    def between(cls, before: BackfillMeasurement, after: BackfillMeasurement) -> BackfillSummary:
        """两次测量之差。

        目标态的变化**只数「变完成」那一侧**：反向（完成 → 还要打）在补录这条
        路上不该发生（它只入库、不删），真出现了也不是一句摘要能说清的事。
        """
        settled = sum(
            1
            for key, was in before.bot_phases.items()
            if was != "DONE" and after.bot_phases.get(key) == "DONE"
        )
        return cls(
            reports_ingested=max(after.reports - before.reports, 0),
            dispatches_claimed=max(after.claimed - before.claimed, 0),
            bot_targets_settled=settled,
            bot_targets_measured=len(before.bot_phases),
        )


@dataclass(frozen=True)
class BackfillState:
    """补录的完整现状，供 API 搬给页面。

    请求的参数（`kind` / `since` / `reason`）在跑完之后**照旧留着**：页面上那句
    「补录完成」要说清楚补的是哪条链路、从哪天起、是谁要的，否则它只是一个没有
    主语的对勾。
    """

    phase: BackfillPhase
    kind: str | None = None
    since: date | None = None
    reason: str = REASON_MANUAL
    command: tuple[str, ...] = ()
    pid: int | None = None
    requested_at_utc: datetime | None = None
    started_at_utc: datetime | None = None
    ended_at_utc: datetime | None = None
    exit_code: int | None = None
    log_path: Path | None = None
    #: 这一批里还有几趟排在后面。页面要说「海盗那趟跑完还有 bot 一趟」。
    queued: int = 0
    #: 整批跑完之后那份「改了什么」。没量到底数时为 None。
    summary: BackfillSummary | None = None
    #: 用户看过摘要、点了「继续任务」。**跑完到点这一下之间，任务一个都不起。**
    acknowledged: bool = False
    #: 整批开跑前量的那一份底数，只为算差值。第二趟起沿用第一趟量的那份——
    #: 摘要说的是「这一批」，不是「最后那一趟」。
    before: BackfillMeasurement | None = None

    @property
    def active(self) -> bool:
        """这一批还在排队或在跑。

        跑完但还没确认的那一档**不算**：那时用户完全可以接着补另一条链路，
        拦住它只会逼用户先点一下「继续任务」——而那一下的含义是「放任务出来」，
        正好相反。
        """
        return self.phase in (BackfillPhase.PENDING, BackfillPhase.RUNNING) or self.queued > 0

    @property
    def blocking(self) -> bool:
        """补录此刻是不是扣着游戏窗口。**调度器据此一个任务都不起。**

        三档都算，各有各的理由：

        - `PENDING`：那一刻窗口还在海盗 / bot 手上，但**下一个**窗口归补录。
          不算的话，正在跑的那一轮一结束，调度器立刻起下一个任务，补录就永远
          排在后面等不到——而它等的正是「这一轮结束」。
        - `RUNNING`：它正在点鼠标翻信箱。
        - `DONE` / `FAILED` **且还没确认**：用户口径是要在放行前看一眼摘要。
          失败那一档尤其不能自动放行——补录失败意味着数据仍然不全，而「拿不全
          的数据做决策」正是这整件事要防的东西。

        `CANCELLED` 不算：用户主动取消的含义就是「放任务出来」。
        """
        if self.active:
            return True
        return self.phase in (BackfillPhase.DONE, BackfillPhase.FAILED) and not self.acknowledged


class BackfillCounts(Protocol):
    """量一次那两个数。抽出来是为了让差值算得了，不必真连库。"""

    def read(self) -> tuple[int, int]: ...


class SqlAlchemyBackfillCounts:
    """真的去数。**只读，两个 `COUNT(*)`，一个字都不写。**

    自带 session 工厂而不是走 `SqlAlchemyRepository`，与
    `application.mission_progress.SqlAlchemyMissionProgress` 同一个形状：这两个
    计数只有补录摘要一个读者，塞进那个已经近两千行的仓库类只会让它更长。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(self) -> tuple[int, int]:
        with self._session_factory() as session:
            total = session.scalar(select(func.count()).select_from(orm.BattleReportRow)) or 0
            claimed = (
                session.scalar(
                    select(func.count())
                    .select_from(orm.BattleReportRow)
                    .where(orm.BattleReportRow.dispatch_id.is_not(None))
                )
                or 0
            )
            return int(total), int(claimed)


def log_path_for(kind: str, *, log_dir: Path = LOG_DIR) -> Path:
    """补录日志的落脚处。**和任务日志同一个目录，但不是同一个文件。**

    混进 `mission-pirate.log` 的话，事后翻「那一轮海盗到底干了什么」会读到一段
    根本不是它写的输出。按链路分而不是按次分，同 `mission_supervisor.log_path_for`：
    任何时刻只有一个补录在跑，同一条链路的两趟在时间上天然不重叠。
    """
    return log_dir / f"backfill-{kind}.log"


def build_command(
    kind: str,
    since: date,
    *,
    max_pages: int | None = None,
    max_opens: int | None = None,
) -> tuple[str, ...]:
    """补录命令行。

    `sys.executable` 而不是写死 `"python"`，同 `domain.missions` 那三条：写死会
    走 PATH 解析，控制台若跑在 venv 外的系统解释器下，拉起的补录就会跟着跑到
    系统解释器上，找不到本仓的依赖。

    `-u` 不是可有可无的：子进程的 stdout 重定向到文件之后是**全缓冲**的，
    4KB 攒满才落盘。补录要跑十几分钟，页面上那份日志尾巴是唯一的进度来源——
    少了这一个字母，用户会盯着一个空文件看十分钟，然后得出「点了没反应」。

    `--max-pages` / `--max-opens` 不填就不出现在 argv 里，由 CLI 自己的默认值
    决定。**不在这里替它填一份默认值**：两处各写一份，改了一边就是另一边悄悄
    地按旧值跑。启动对账正是走这一条（它要的就是 CLI 那份大预算）。
    """
    command = [
        sys.executable,
        "-u",
        "-m",
        "evo_helper.tools.backfill_reports",
        "--kind",
        kind,
        "--since",
        since.isoformat(),
    ]
    if max_pages is not None:
        command += ["--max-pages", str(max_pages)]
    if max_opens is not None:
        command += ["--max-opens", str(max_opens)]
    return tuple(command)


def launch_backfill(command: Sequence[str], log_path: Path) -> Process:
    """真的拉起一个补录进程。**测试里绝不调它。**

    照 `mission_supervisor.launch_mission` 长：`stderr` 并进 `stdout`，
    两条流分开写同一个文件会互相截断，而这份日志是出事之后唯一能看的东西。
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    return subprocess.Popen(  # noqa: S603 - 命令行全由 `build_command` 构造
        list(command),
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class BackfillCoordinator:
    """补录的状态机：**只判、只管自己那个子进程，不碰任务、不碰数据库。**

    「什么时候能起」由 `MissionScheduler` 问这里的 `active` / `blocking`，动手
    （抢占扫描、等海盗跑完、量底数）在那边——同 `MissionSupervisor` 与调度循环
    的分工。底数由调用方**以函数的形式**传进来，而不是在这里查库：那两个
    `COUNT(*)` 加上逐目标的态，只该在真的要用时量一次，不能每个 tick 都量。

    `launch` 与 `clock` 都可注入：起停逻辑是这里唯一有分支的地方，把它和真实的
    `Popen`、真实的时钟隔开才测得了。**绝不能在 CI 上真的拉起一个补录**，
    那会去点真实的鼠标翻信箱。
    """

    launch: Callable[[Sequence[str], Path], Process] = launch_backfill
    clock: Callable[[], datetime] = _utc_now
    log_dir: Path = LOG_DIR

    def __post_init__(self) -> None:
        self._state = BackfillState(phase=BackfillPhase.IDLE)
        self._process: Process | None = None
        #: 还没轮到的那几趟。启动对账一次排两趟（海盗、bot）。
        self._queue: list[BackfillRequest] = []
        #: 请求来自 HTTP 线程，`poll` / `launch_if_pending` 来自 tick 线程。
        #: 没有这把锁，一次「取消」可能正好落在「起进程」中间。
        self._lock = threading.RLock()

    # -- 读 --------------------------------------------------------------------

    def state(self) -> BackfillState:
        with self._lock:
            return self._state

    @property
    def blocking(self) -> bool:
        """补录此刻扣着窗口。**调度器据此一个任务都不起。**"""
        return self.state().blocking

    @property
    def active(self) -> bool:
        return self.state().active

    @property
    def pending(self) -> bool:
        return self.state().phase is BackfillPhase.PENDING

    @property
    def running(self) -> bool:
        return self.state().phase is BackfillPhase.RUNNING

    def log_tail(self, lines: int = LOG_TAIL_LINES) -> str:
        """日志的最后几行。文件还没有就返回空串。

        补录跑十几分钟，页面上除了这个没有别的进度来源，所以**读不到不能报错**
        ——一次读文件失败把整个状态接口打成 500，页面连「在跑」都显示不出来了。
        """
        path = self.state().log_path
        if path is None:
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    # -- 写 --------------------------------------------------------------------

    def request(self, request: BackfillRequest) -> BackfillState:
        """排一趟补录。**这里不起进程**——窗口可能还在别人手上。

        真正的启动由 `launch_if_pending()` 在窗口空出来之后完成。分开是因为
        「等海盗那一轮跑完」可能要半小时，而 HTTP 请求不能等在那里。

        同一条链路已经排着就拒：第二趟只会去读一遍刚读过的信箱，而它得先占着
        游戏窗口十几分钟。
        """
        if request.kind not in BACKFILL_KINDS:
            raise ValueError(
                f"补录链路只能是 {' / '.join(BACKFILL_KINDS)}（收到 {request.kind!r}）"
            )
        with self._lock:
            if request.kind in self._scheduled_kinds():
                raise BackfillBusyError(f"「{request.kind}」这一趟补录已经在排队或在跑了")
            if not self._state.active:
                self._begin(request)
            else:
                self._queue.append(request)
                self._state = replace(self._state, queued=len(self._queue))
            return self._state

    def request_batch(self, requests: Sequence[BackfillRequest]) -> BackfillState:
        """排一整批（启动对账用）。**已经排着的那条链路跳过，不报错。**

        报错的话，「刚手动补完海盗、紧接着点开始」这条完全正常的路会把整个
        「开始」打成 409，而用户想要的只是「顺带把 bot 那趟也跑了」。
        """
        with self._lock:
            for request in requests:
                if request.kind in self._scheduled_kinds():
                    continue
                self.request(request)
            return self._state

    def launch_if_pending(self, before: BackfillMeasurement) -> bool:
        """窗口空出来了，起这一趟。返回「这一下真的起了吗」。

        调用方（`MissionScheduler._advance_backfill`）必须已经确认
        `supervisor.running is None`：这里会真的拉起一个去点鼠标翻信箱的子进程。

        `before` 是**刚刚**量的那份底数，只在整批的第一趟采纳：摘要说的是这一批
        改了什么，第二趟拿它自己的底数去比，第一趟的战果就凭空消失了。要在起进程
        之前量——补录一开跑就会往库里写，晚一步量到的已经掺进了它自己的产出。
        """
        with self._lock:
            if self._state.phase is not BackfillPhase.PENDING:
                return False
            log_path = self._state.log_path
            if log_path is None:  # pragma: no cover - PENDING 一定带着它
                return False
            process = self.launch(self._state.command, log_path)
            self._process = process
            self._state = replace(
                self._state,
                phase=BackfillPhase.RUNNING,
                pid=getattr(process, "pid", None),
                started_at_utc=self.clock(),
                before=self._state.before or before,
            )
            return True

    def poll(self, measure: Callable[[], BackfillMeasurement]) -> BackfillState:
        """收退出码。没在跑（或还没退）就原样返回，**也不量底数**。

        `measure` 是个函数而不是一份现成的测量：它要跑两个 `COUNT(*)` 外加逐个
        bot 目标问库，而这个方法每秒被调一次。只有真的收到退出码那一次才量。

        退出只会被收一次：`RUNNING` 之外一律直接返回，所以重复调用是安全的。
        """
        with self._lock:
            if self._state.phase is not BackfillPhase.RUNNING or self._process is None:
                return self._state
            exit_code = self._process.poll()
            if exit_code is None:
                return self._state
            self._process = None
            self._settle(
                BackfillPhase.DONE if exit_code == 0 else BackfillPhase.FAILED,
                exit_code=exit_code,
                measure=measure,
            )
            return self._state

    def cancel(self, measure: Callable[[], BackfillMeasurement]) -> BackfillState:
        """用户改主意了。**整批一起取消**：排队中的撤掉，跑着的杀掉。

        排队那一档必须有：正在等一轮 30 分钟的海盗跑完时，用户唯一的退路本来是
        「把整台调度器停掉」——而那会把另外两条正常的链路一起停掉。

        跑着那一档记成 `CANCELLED` 而不是 `FAILED`：一次主动取消不是故障，不该
        让人去翻日志找原因。补录只读、不删邮件、不领奖励，中途杀掉最坏也只是少
        补几封，下次再点一次就是了。**已经补进去的那些照样算数**，所以这一档也
        出摘要。

        取消之后**立刻放行**（`CANCELLED` 不 blocking）：这一下的用户意图就是
        「别占着窗口了」。
        """
        with self._lock:
            if not self._state.active:
                return self._state
            self._queue.clear()
            process, self._process = self._process, None
            exit_code: int | None = None
            if process is not None:
                process.terminate()
                try:
                    exit_code = process.wait(timeout=TERMINATE_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - 收不到退出码也不该让控制台卡住
                    exit_code = None
            self._settle(
                BackfillPhase.CANCELLED,
                exit_code=exit_code,
                measure=measure if process is not None else None,
            )
            return self._state

    def acknowledge(self) -> BackfillState:
        """用户看过摘要，点了「继续任务」。**这一下才放行。**

        摘要**留在页面上**（`phase` 不动，只翻 `acknowledged`）：放行之后那几个
        数字仍然是「刚才那一批干了什么」的唯一答案。

        **只认已经跑完的那一批。** 还在排队或在跑的时候「确认」没有意义——摘要
        都还不存在。而且它会一路留在状态上：`_settle` 是在现状上 `replace`，一次
        提前的确认会让这一批跑完的那一刻直接放行，用户永远看不到那几个数。
        `MissionScheduler.start()` 顺手确认上一批时正好会撞上这一条。
        """
        with self._lock:
            if self._state.phase in (BackfillPhase.DONE, BackfillPhase.FAILED):
                self._state = replace(self._state, acknowledged=True)
            return self._state

    # -- 内部 ------------------------------------------------------------------

    def _scheduled_kinds(self) -> set[str]:
        """已经排上的链路：当前这一趟（还没结束的）加上队里那些。"""
        kinds = {item.kind for item in self._queue}
        if self._state.active and self._state.kind is not None:
            kinds.add(self._state.kind)
        return kinds

    def _begin(self, request: BackfillRequest, *, carry: BackfillState | None = None) -> None:
        """把一趟请求变成当前状态。调用方必须已经持有 `_lock`。

        `carry` 是同一批里上一趟的状态：底数与已经攒下的摘要要跟着走，否则整批
        的摘要会缩水成「最后那一趟」。
        """
        self._state = BackfillState(
            phase=BackfillPhase.PENDING,
            kind=request.kind,
            since=request.since,
            reason=request.reason,
            command=request.command,
            requested_at_utc=self.clock(),
            log_path=log_path_for(request.kind, log_dir=self.log_dir),
            queued=len(self._queue),
            before=None if carry is None else carry.before,
            summary=None if carry is None else carry.summary,
        )

    def _settle(
        self,
        phase: BackfillPhase,
        *,
        exit_code: int | None,
        measure: Callable[[], BackfillMeasurement] | None,
    ) -> None:
        """一趟结束了：记账、算摘要，队里还有就接着排下一趟。"""
        finished = replace(
            self._state,
            phase=phase,
            exit_code=exit_code,
            ended_at_utc=self.clock(),
            queued=len(self._queue),
            summary=self._summarize(measure),
        )
        self._state = finished
        # 只有正常跑完才轮下一趟。失败或取消之后接着跑，等于在一个已经不对劲的
        # 环境里再占十几分钟窗口，而用户此刻多半想看的是那条失败。
        if phase is BackfillPhase.DONE and self._queue:
            self._begin(self._queue.pop(0), carry=finished)

    def _summarize(
        self, measure: Callable[[], BackfillMeasurement] | None
    ) -> BackfillSummary | None:
        """跟整批开跑前那份底数比一比。没量到底数就不出摘要。

        出不来时返回 None 而不是一串 0：0 的意思是「一份都没补进来」，那是一句
        会让人白跑一趟信箱的假话。
        """
        before = self._state.before
        if before is None or measure is None:
            return self._state.summary
        return BackfillSummary.between(before, measure())


__all__ = [
    "BACKFILL_KINDS",
    "DEFAULT_SINCE_DAYS_BACK",
    "LOG_TAIL_LINES",
    "REASON_MANUAL",
    "REASON_STARTUP",
    "BackfillBusyError",
    "BackfillCoordinator",
    "BackfillCounts",
    "BackfillMeasurement",
    "BackfillPhase",
    "BackfillRequest",
    "BackfillState",
    "BackfillSummary",
    "SqlAlchemyBackfillCounts",
    "build_command",
    "default_since",
    "launch_backfill",
    "log_path_for",
]
