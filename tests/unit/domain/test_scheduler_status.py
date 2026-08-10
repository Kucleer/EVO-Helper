"""状态文案的判据。

页面和桌面悬浮窗都要显示「这条链路现在是什么状态」，而那句话必须和调度器
真正的行为出自同一份判据——否则页面写着「等航线」、调度器其实是在冷却，
用户会去改航线数，改完还是不动。

所以它和 `has_work` 放在一起，是纯函数：`status_of` 复用 `has_work`，
只在它返回 False 时再去问「为什么」。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evo_helper.domain.scheduler import (
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskSnapshot,
    TaskStatus,
    status_of,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
COOLDOWN = timedelta(minutes=5)

_DEFAULT_FACTS = SchedulerFacts(
    now_utc=NOW,
    free_lines=1,
    pirate_dispatches_today=0,
    pirate_quota=32,
    pirate_blocked_until_utc=None,
    pirate_reports_due=False,
    bot_reports_due=False,
    bot_targets_remaining=5,
)


def facts(**overrides: object) -> SchedulerFacts:
    return replace(_DEFAULT_FACTS, **overrides)


def task(kind: MissionKind, **overrides: object) -> TaskSnapshot:
    base = TaskSnapshot(kind=kind, enabled=True, priority=0)
    return replace(base, **overrides)


def status(
    kind: MissionKind,
    *,
    running: RunningProcess | None = None,
    task_overrides: dict[str, object] | None = None,
    **fact_overrides: object,
) -> TaskStatus:
    return status_of(
        task(kind, **(task_overrides or {})),
        facts(**fact_overrides),
        running=running,
        restart_cooldown=COOLDOWN,
    )


def test_the_running_chain_says_so() -> None:
    running = RunningProcess(kind=MissionKind.SCAN, started_at_utc=NOW)
    assert status(MissionKind.SCAN, running=running) is TaskStatus.RUNNING


def test_a_chain_that_is_not_the_running_one_is_not_marked_running() -> None:
    """一个鼠标一次只有一条链路在跑，别的链路不能跟着显示「运行中」。"""
    running = RunningProcess(kind=MissionKind.SCAN, started_at_utc=NOW)
    assert status(MissionKind.PIRATE, running=running) is TaskStatus.READY


def test_an_unchecked_task_says_so_instead_of_pretending_to_wait() -> None:
    """复选框没勾就是不参与调度。显示「待命」是句谎话——它永远不会被起起来。"""
    assert status(MissionKind.SCAN, task_overrides={"enabled": False}) is TaskStatus.OFF


def test_an_auto_disabled_task_says_disabled() -> None:
    assert (
        status(MissionKind.PIRATE, task_overrides={"disabled_reason": "连续 3 次异常退出"})
        is TaskStatus.DISABLED
    )


def test_disabled_wins_over_unchecked() -> None:
    """两者同时成立时先说停用原因——那是用户真正需要知道的那一条。"""
    assert (
        status(
            MissionKind.PIRATE,
            task_overrides={"enabled": False, "disabled_reason": "参数不合格"},
        )
        is TaskStatus.DISABLED
    )


def test_pirates_report_the_quota_rather_than_a_generic_wait() -> None:
    """配额用尽和没航线的处置完全不同：一个要等到 UTC 00:00，一个等舰队回来。"""
    assert (
        status(MissionKind.PIRATE, pirate_dispatches_today=32, free_lines=0)
        is TaskStatus.QUOTA_EXHAUSTED
    )


def test_the_hard_quota_signal_also_reads_as_quota_exhausted() -> None:
    blocked = NOW + timedelta(hours=3)
    assert (
        status(MissionKind.PIRATE, pirate_blocked_until_utc=blocked) is TaskStatus.QUOTA_EXHAUSTED
    )


def test_a_finished_bot_round_says_done_not_waiting() -> None:
    """本轮打完就是打完，等下去也不会有事发生——页面据此给出「重开一轮」。"""
    assert status(MissionKind.BOT, bot_targets_remaining=0) is TaskStatus.DONE


def test_no_free_lines_reads_as_waiting_for_lines() -> None:
    assert status(MissionKind.BOT, free_lines=0) is TaskStatus.WAITING_LINES


def test_a_chain_inside_its_restart_cooldown_says_cooling_down() -> None:
    """冷却和没航线看起来都是「不动」，但用户能做的事不一样：冷却只要等，
    没航线得看是不是航线数配小了。混成一句会让人去改不该改的那个。"""
    recent = {MissionKind.PIRATE: NOW - timedelta(minutes=1)}
    assert status(MissionKind.PIRATE, last_started_at_utc=recent) is TaskStatus.COOLING_DOWN


def test_scanning_is_never_cooled_down() -> None:
    """冷却只管攻击链路。套在扫描上就是纯空转——填空隙正是它存在的理由。"""
    recent = {MissionKind.SCAN: NOW - timedelta(seconds=1)}
    assert status(MissionKind.SCAN, last_started_at_utc=recent) is TaskStatus.READY


def test_scanning_is_ready_even_with_no_lines() -> None:
    assert status(MissionKind.SCAN, free_lines=0) is TaskStatus.READY
