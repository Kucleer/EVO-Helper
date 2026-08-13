"""这一轮到底还在不在干活：外部可观测的「进展」，以及据此判死的看门狗。

**实机 `var/logs/overnight-0812.log` 最后 1.5 小时**（心跳每半小时一行）：

    05:14:51 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    05:45:13 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    06:15:36 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    06:45:59 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...

六次心跳、七个计数一个都没变，而状态一直是「运行中」。调度器当时知道的唯一
事实就是「子进程还活着」——而那正是当时唯一为真的事。一个半小时白丢。

**「进展」的定义是这条修复的全部要害，所以它只认库里多出来的行。**

- **不能是「进程还在」。** 那恰恰是出事时唯一成立的条件。
- **不能是「有日志输出」。** 卡在重试循环里的 runner 一样不停打日志：
  `_settle` 每轮四次、`_goto_checked` 每失败一次都是一行。按日志判活，
  出事那一晚照样判成「在干活」。
- **不能是墙上时间。** 一轮里合法的长等待是存在的（`SCOUT_REPORT_WAIT_S`
  是 45 秒、翻一趟信箱实测 83 秒、`_settle` 各处还有等待），只按运行时长掐，
  掐掉的是正常的轮次。

剩下的就只有**产出**：派遣、战报、侦察报告、坐标扫描。这四张表是三条链路各自
唯一的产物，也正是页面上那几个计数的来源——判据和用户看到的东西同源。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, assert_never

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_supervisor import RunningChild
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base

#: 一轮里「一件事都没做成」持续这么久，就判死并收掉。
#:
#: **上界来自用户口径**（2026-08-13）：「比如 1 小时未读取到邮件需要采用兜底
#: 重启机制」。所以阈值必须在 1 小时以内。
#:
#: **下界来自一轮合法的、确实产不出任何行的最长一段。** 按默认参数算：
#:
#: - 海盗默认 `{"radius": 10}` → 21 个恒星系 × 4 个行星位 = 84 次导航+核对，
#:   实测每次 6–12 秒 → 最坏约 17 分钟；这一路一个海盗都没有时确实一行不产。
#: - 开工那趟 `reconcile_today` 翻信箱：实测一趟 83 秒，最多 8 页 → 数分钟，
#:   而信箱里的报告要是都已入库，同样一行不产（去重不插入）。
#:
#: 两段加起来约 20 分钟，45 分钟留了一倍以上余量，同时比用户给的 1 小时上界
#: 更早止损。取值有代价：真有人把半径调到把这两段撑过 45 分钟，那一轮会被误杀
#: ——误杀的代价是重跑一轮（游标、派遣、战报都已经落库，不丢数据），
#: 而漏杀的代价是这一晚剩下的时间全部白费。
STALL_TIMEOUT = timedelta(minutes=45)

#: 隔多久去数一次那四张表。
#:
#: tick 每秒一次，而这四个 `COUNT(*)` 在生产库上是四次全表/全索引扫描。
#: 判据的分辨率本来就是分钟级（阈值 45 分钟），每秒去数一遍纯属白付钱。
PROGRESS_POLL_INTERVAL = timedelta(seconds=30)


@dataclass(frozen=True)
class ProgressReading:
    """四张产出表各有多少行。**只用来和上一次比大小，绝对值没有意义。**"""

    dispatches: int
    battle_reports: int
    scout_reports: int
    coordinate_scans: int

    def for_kind(self, kind: MissionKind) -> tuple[int, ...]:
        """这条链路的进展由哪几个数字说了算。

        按链路挑而不是四个一起看，是因为「谁产出的」这件事必须说得清：
        控制台自己也会写库（页面上手动补录、`--import-debug` 回灌），
        四个一起看的话，别处写进来的一行会被记到卡死的那一轮头上，
        看门狗就此哑火——而它存在的全部意义就是不被这种事骗过去。

        - `PIRATE`：侦察与攻击都记 `attack_dispatches`；它自己读信箱，所以
          战报和侦察报告也是它的产出。
        - `BOT`：探路发与攻击发同样记 `attack_dispatches`，战报同理；
          它不侦察，`scout_reports` 与它无关。
        - `SCAN`：只产 `coordinate_scans`，它压根不派遣。
        """
        if kind is MissionKind.PIRATE:
            return (self.dispatches, self.battle_reports, self.scout_reports)
        if kind is MissionKind.BOT:
            return (self.dispatches, self.battle_reports)
        if kind is MissionKind.SCAN:
            return (self.coordinate_scans,)
        # 穷举到这里说明 MissionKind 加了新成员却没人补分支——新链路静默套用
        # 别人的进展判据，等于给它配了一个永远不会响的看门狗。
        assert_never(kind)


class MissionProgress(Protocol):
    """数一次那四张表。抽出来是为了让看门狗的判据测得了，不必真连库。"""

    def read(self) -> ProgressReading: ...


class SqlAlchemyMissionProgress:
    """真的去数。**只读，四个 `COUNT(*)`，一个字都不写。**

    自带 session 工厂而不是走 `SqlAlchemyRepository`，与
    `application.bindings`、`storage.intel` 同一个形状：这四个计数是看门狗
    一个人的判据，没有别的调用方，塞进那个已经近两千行的仓库类只会让它更长。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def read(self) -> ProgressReading:
        with self._session_factory() as session:
            return ProgressReading(
                dispatches=_count(session, orm.AttackDispatchRow),
                battle_reports=_count(session, orm.BattleReportRow),
                scout_reports=_count(session, orm.ScoutReportRow),
                coordinate_scans=_count(session, orm.CoordinateScanRow),
            )


def _count(session: Session, model: type[Base]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


class StallWatchdog:
    """盯住**当前这一个**子进程：它上一次让库里多出一行是多久以前。

    只记「在盯谁」和「上次见到变化是什么时候」，没有别的状态。子进程一换
    （`RunningChild.started_at_utc` 变了）就整个重新起表——上一轮的进展说明不了
    新一轮的死活。

    ⚠️ **`check()` 会查库，所以调用方必须在 `MissionScheduler._lock` 外面调。**
    那把锁只护「起停子进程」那几行，把查库压进去就是给用户的「结束」排队，
    而那正是 2026-08-11「点了结束毫无反应」那一轮修复刚拆开的东西。
    """

    def __init__(
        self,
        progress: MissionProgress,
        *,
        timeout: timedelta = STALL_TIMEOUT,
        poll_interval: timedelta = PROGRESS_POLL_INTERVAL,
    ) -> None:
        self._progress = progress
        self._timeout = timeout
        self._poll_interval = poll_interval
        #: 正在盯的那个子进程的启动时刻，也是「在不在盯」的标记。
        self._watching: datetime | None = None
        self._fingerprint: tuple[int, ...] = ()
        self._moved_at: datetime | None = None
        self._polled_at: datetime | None = None

    def check(self, running: RunningChild | None, now: datetime) -> timedelta | None:
        """判死了就返回「已经多久没进展」，否则返回 None。

        节流命中、刚换子进程、以及**任何一个数字动过**都返回 None——「有进展就
        不许掐」比「卡死了要掐」更要紧：误杀一轮正常的长等待，丢的是真实的舰队
        和真实的当日配额。
        """
        if running is None:
            self._watching = None
            return None
        if self._watching != running.started_at_utc:
            self._start_watching(running, now)
        elif self._polled_at is None or now - self._polled_at >= self._poll_interval:
            self._polled_at = now
            current = self._progress.read().for_kind(running.kind)
            if current != self._fingerprint:
                self._fingerprint = current
                self._moved_at = now
        idle = now - (self._moved_at or now)
        return idle if idle >= self._timeout else None

    def _start_watching(self, running: RunningChild, now: datetime) -> None:
        """换了一个子进程：重新起表。

        表从**它启动那一刻**起算，不是从「我第一次看见它」起算。两者在生产里
        只差一个 tick（子进程是在 `_step()` 里起的，而这一段跑在它前面，所以
        第一次看见它总是下一秒），但按「第一次看见」算，会让这个判据依赖调用
        频率——把 tick 调慢一点，阈值就跟着悄悄变长。
        """
        self._watching = running.started_at_utc
        self._fingerprint = self._progress.read().for_kind(running.kind)
        self._moved_at = running.started_at_utc
        self._polled_at = now


def watchdog_for(
    read: Callable[[], ProgressReading],
    *,
    timeout: timedelta = STALL_TIMEOUT,
    poll_interval: timedelta = PROGRESS_POLL_INTERVAL,
) -> StallWatchdog:
    """拿一个普通函数当进展来源，省得测试为四个计数专门写一个类。"""

    class _Adapter:
        def read(self) -> ProgressReading:
            return read()

    return StallWatchdog(_Adapter(), timeout=timeout, poll_interval=poll_interval)
