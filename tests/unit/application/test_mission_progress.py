"""「跑着不动」的判据。

**实机 `var/logs/overnight-0812.log` 最后 1.5 小时**（心跳每半小时一行）：

    05:14:51 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    05:45:13 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    06:15:36 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...
    06:45:59 运行=True 当前=PIRATE | 580/92/84/83/86/126/4536 | PIRATE:运行中 ...

六次心跳、七个计数一个没变，状态一直是「运行中」。调度器只知道子进程还活着，
不知道它已经不干活了，白丢一个半小时。

这里守的两条方向相反、缺一不可：

1. **有进展就绝不掐。** 一轮里合法的长等待是存在的（`SCOUT_REPORT_WAIT_S`
   是 45 秒、翻一趟信箱实测 83 秒），误杀丢的是真实的舰队和当日配额。
2. **一直没进展就要掐。** 这才是这条修复本身。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.application.mission_progress import (
    STALL_TIMEOUT,
    ProgressReading,
    watchdog_for,
)
from evo_helper.application.mission_supervisor import RunningChild
from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 12, 5, 0, tzinfo=UTC)
IDLE = ProgressReading(dispatches=580, battle_reports=92, scout_reports=84, coordinate_scans=4536)


def child(kind: MissionKind = MissionKind.PIRATE, *, started_at: datetime = NOW) -> RunningChild:
    from pathlib import Path

    return RunningChild(
        task_id=1,
        kind=kind,
        name="",
        command=("python",),
        pid=7001,
        started_at_utc=started_at,
        log_path=Path("x.log"),
    )


class _Counts:
    """一份可以现改的进展读数，外加「被数了几次」。"""

    def __init__(self, reading: ProgressReading = IDLE) -> None:
        self.reading = reading
        self.reads = 0

    def __call__(self) -> ProgressReading:
        self.reads += 1
        return self.reading


# -- 有进展就不许掐 -------------------------------------------------------------


def test_a_round_that_keeps_producing_rows_is_never_cut_off() -> None:
    """**这条最要紧。** 误杀一轮正常的长等待，丢的是真实的舰队和当日配额。"""
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child()

    assert watchdog.check(running, NOW) is None
    for minutes in range(1, 240, 5):
        # 每五分钟多派一发，正是一轮健康的海盗该有的样子。
        counts.reading = ProgressReading(
            dispatches=IDLE.dispatches + minutes,
            battle_reports=IDLE.battle_reports,
            scout_reports=IDLE.scout_reports,
            coordinate_scans=IDLE.coordinate_scans,
        )
        assert watchdog.check(running, NOW + timedelta(minutes=minutes)) is None


def test_a_long_legitimate_wait_is_not_a_stall() -> None:
    """一轮里合法的长等待是存在的：一趟信箱实测 83 秒、侦察报告等 45 秒。

    阈值不能小到把它们当成卡死——而这条用例钉的正是「阈值之前一律不掐」。
    """
    watchdog = watchdog_for(_Counts())
    running = child()
    watchdog.check(running, NOW)

    assert watchdog.check(running, NOW + STALL_TIMEOUT - timedelta(seconds=1)) is None


def test_progress_resets_the_clock_rather_than_merely_delaying_it() -> None:
    """进展要把表**归零**，不是往后推一格。

    只推一格的话，一轮跑满阈值的健康轮次照样会在某个时刻被掐掉。
    """
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child()
    watchdog.check(running, NOW)
    watchdog.check(running, NOW + STALL_TIMEOUT - timedelta(minutes=1))

    counts.reading = ProgressReading(581, 92, 84, 4536)
    assert watchdog.check(running, NOW + STALL_TIMEOUT - timedelta(seconds=30)) is None
    # 归零之后，重新走满一整个阈值才算数。
    assert watchdog.check(running, NOW + STALL_TIMEOUT + timedelta(seconds=1)) is None
    assert watchdog.check(running, NOW + STALL_TIMEOUT * 2) is not None


# -- 一直没进展就要掐 -----------------------------------------------------------


def test_a_round_with_no_progress_at_all_is_cut_off_at_the_threshold() -> None:
    """出事那一晚的形状：计数一个不动，而状态一直是「运行中」。"""
    watchdog = watchdog_for(_Counts())
    running = child()
    watchdog.check(running, NOW)

    idle = watchdog.check(running, NOW + STALL_TIMEOUT)

    assert idle is not None
    assert idle >= STALL_TIMEOUT


def test_the_threshold_stays_inside_the_hour_the_user_asked_for() -> None:
    """**用户口径（2026-08-13）：「比如 1 小时未读取到邮件需要采用兜底重启机制」。**

    1 小时是他给的直觉上界，不是目标值。阈值涨过它就等于把用户明说不能忍的那段
    时间又还了回去。
    """
    assert STALL_TIMEOUT <= timedelta(hours=1)


# -- 盯的是「当前这一个」子进程 -------------------------------------------------


def test_a_new_child_starts_the_clock_over() -> None:
    """上一轮的进展说明不了新一轮的死活。

    不重新起表的话，一轮刚起来就会背上上一轮攒下的整段空转，第一次巡检就被掐。
    """
    watchdog = watchdog_for(_Counts())
    watchdog.check(child(started_at=NOW), NOW)
    watchdog.check(child(started_at=NOW), NOW + STALL_TIMEOUT - timedelta(seconds=1))

    later = NOW + STALL_TIMEOUT
    assert watchdog.check(child(started_at=later), later) is None
    assert watchdog.check(child(started_at=later), later + timedelta(minutes=1)) is None


def test_nothing_running_means_nothing_to_judge() -> None:
    counts = _Counts()
    watchdog = watchdog_for(counts)

    assert watchdog.check(None, NOW) is None
    assert counts.reads == 0, "没有子进程在跑时不该白查一次库"


# -- 每条链路只认自己的产出 -----------------------------------------------------


def test_each_chain_is_judged_by_the_tables_it_actually_writes() -> None:
    """扫描不派遣、不产战报，拿别人的行给它记进展就等于给它配了个哑火的看门狗。

    这里让扫描卡死，同时让派遣和战报一路涨（比如控制台在手动补录）：
    扫描照样要被掐。
    """
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child(MissionKind.SCAN)
    watchdog.check(running, NOW)

    counts.reading = ProgressReading(9999, 9999, 9999, IDLE.coordinate_scans)

    assert watchdog.check(running, NOW + STALL_TIMEOUT) is not None


def test_a_scan_that_keeps_writing_coordinates_is_left_alone() -> None:
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child(MissionKind.SCAN)
    watchdog.check(running, NOW)

    counts.reading = ProgressReading(0, 0, 0, IDLE.coordinate_scans + 1)

    assert watchdog.check(running, NOW + STALL_TIMEOUT) is None


def test_a_pirate_round_counts_its_scout_reports_as_progress() -> None:
    """海盗自己读信箱，侦察报告是它这一轮实打实的产出。

    漏掉这一样，一轮「只侦察、还没轮到派攻击」的海盗会被误判成卡死。
    """
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child(MissionKind.PIRATE)
    watchdog.check(running, NOW)

    counts.reading = ProgressReading(
        IDLE.dispatches, IDLE.battle_reports, IDLE.scout_reports + 1, IDLE.coordinate_scans
    )

    assert watchdog.check(running, NOW + STALL_TIMEOUT) is None


# -- 别每秒去数一遍 -------------------------------------------------------------


def test_the_counts_are_not_re_read_on_every_tick() -> None:
    """tick 每秒一次，而这四个计数在生产库上是四次全表扫描。

    判据的分辨率本来就是分钟级，每秒数一遍纯属白付钱。
    """
    counts = _Counts()
    watchdog = watchdog_for(counts)
    running = child()
    watchdog.check(running, NOW)
    reads_after_first_look = counts.reads

    for second in range(1, 25):
        watchdog.check(running, NOW + timedelta(seconds=second))

    assert counts.reads == reads_after_first_look


def test_the_ranking_scan_reports_progress_by_time_not_by_row_count() -> None:
    """⚠️⚠️ **拿行数当信号会把一条正在干活的链路当卡死杀掉。**

    军力榜重扫同一批 bot 时只更新不新增（`bot_targets` 上有坐标唯一约束），
    所以第二趟开始 `COUNT(*)` 就再也不动。看门狗看到「计数没变」就判卡死。

    时刻则是只要写了任何一行就往前走。变异测试当场抓到过这条：把
    `_latest_epoch` 换成 `_count(BotTargetRow)` 时，所有用例照样绿。
    """
    same_rows_later_write = ProgressReading(
        dispatches=0,
        battle_reports=0,
        scout_reports=0,
        coordinate_scans=0,
        ranking_written_at=1_755_000_000,
    )
    earlier = ProgressReading(
        dispatches=0,
        battle_reports=0,
        scout_reports=0,
        coordinate_scans=0,
        ranking_written_at=1_754_000_000,
    )

    assert same_rows_later_write.for_kind(MissionKind.RANKING) != earlier.for_kind(
        MissionKind.RANKING
    )
    # 而其它链路的信号一个都没动——「谁产出的」必须说得清。
    assert same_rows_later_write.for_kind(MissionKind.SCAN) == earlier.for_kind(MissionKind.SCAN)
