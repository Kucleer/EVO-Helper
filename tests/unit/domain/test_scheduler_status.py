"""状态文案的判据。

页面要显示「这个任务现在是什么状态」，而那句话必须和调度器
真正的行为出自同一份判据——否则页面写着「等航线」、调度器其实是在冷却，
用户会去改航线数，改完还是不动。

所以它和 `has_work` 放在一起，是纯函数：`status_of` 复用 `has_work`，
只在它返回 False 时再去问「为什么」。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    TaskStatus,
    status_of,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
COOLDOWN = timedelta(minutes=5)
HOME = Coordinate(2, 137, 18)

#: 每条链路一个固定的 task_id。判据按 id 认人，用例也就得给它们各自的身份。
_IDS = {MissionKind.PIRATE: 1, MissionKind.BOT: 2, MissionKind.SCAN: 3}

_BASE = TaskFacts(free_lines=1, targets_remaining=5)


def task(kind: MissionKind, **overrides: Any) -> TaskSnapshot:
    base = TaskSnapshot(
        task_id=_IDS[kind],
        kind=kind,
        name="",
        enabled=True,
        priority=0,
        origin=HOME,
        fleet_lines=6,
    )
    return replace(base, **overrides)


def status(
    kind: MissionKind,
    *,
    running: RunningProcess | None = None,
    task_overrides: dict[str, Any] | None = None,
    **task_facts: Any,
) -> TaskStatus:
    """这个任务此刻显示成什么。

    关键字里 `pirate_dispatches_today` / `pirate_quota` / `pirate_blocked_until_utc`
    是**账号级**事实，其余落到这个任务自己的 `TaskFacts` 上。
    """
    account = {
        key: task_facts.pop(key)
        for key in ("pirate_dispatches_today", "pirate_quota", "pirate_blocked_until_utc")
        if key in task_facts
    }
    subject = task(kind, **(task_overrides or {}))
    return status_of(
        subject,
        SchedulerFacts(
            now_utc=NOW,
            per_task={subject.task_id: replace(_BASE, **task_facts)},
            **account,
        ),
        running=running,
        restart_cooldown=COOLDOWN,
    )


def running_scan() -> RunningProcess:
    return RunningProcess(task_id=_IDS[MissionKind.SCAN], kind=MissionKind.SCAN, started_at_utc=NOW)


def test_the_running_chain_says_so() -> None:
    assert status(MissionKind.SCAN, running=running_scan()) is TaskStatus.RUNNING


def test_a_task_that_is_not_the_running_one_is_not_marked_running() -> None:
    """一个鼠标一次只有一个任务在跑，别的任务不能跟着显示「运行中」。"""
    assert status(MissionKind.PIRATE, running=running_scan()) is TaskStatus.READY


def test_another_task_of_the_same_kind_is_not_marked_running() -> None:
    """**认的是 task_id，不是 kind。**

    同一 `kind` 现在可以有多行（多个 bot 攻击任务），按 kind 认的话，主星那个
    一跑起来，2 号星那个也会跟着显示「运行中」——而任何时刻只有一个子进程。
    """
    other_bot = TaskSnapshot(
        task_id=99,
        kind=MissionKind.BOT,
        name="2 号星",
        enabled=True,
        priority=0,
        origin=HOME,
        fleet_lines=2,
    )
    running = RunningProcess(
        task_id=_IDS[MissionKind.BOT], kind=MissionKind.BOT, started_at_utc=NOW
    )

    assert (
        status_of(
            other_bot,
            SchedulerFacts(now_utc=NOW, per_task={other_bot.task_id: _BASE}),
            running=running,
            restart_cooldown=COOLDOWN,
        )
        is TaskStatus.READY
    )


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
    assert status(MissionKind.BOT, targets_remaining=0) is TaskStatus.DONE


def test_no_free_lines_reads_as_waiting_for_lines() -> None:
    assert status(MissionKind.BOT, free_lines=0) is TaskStatus.WAITING_LINES


def test_a_chain_inside_its_restart_cooldown_says_cooling_down() -> None:
    """冷却和没航线看起来都是「不动」，但用户能做的事不一样：冷却只要等，
    没航线得看是不是航线数配小了。混成一句会让人去改不该改的那个。"""
    assert (
        status(MissionKind.PIRATE, last_started_at_utc=NOW - timedelta(minutes=1))
        is TaskStatus.COOLING_DOWN
    )


def test_a_chain_waiting_for_a_line_says_so_rather_than_cooling_down() -> None:
    """两者同时成立时说「等航线」——那是更长、也更该让用户看到的原因。

    反过来显示成「冷却中」，用户会以为再等五分钟就动，然后眼看着它到点也不动
    （航线还没空出来）。页面上那句话必须能预测调度器接下来干什么。
    """
    empty_round = status(
        MissionKind.PIRATE,
        free_lines=3,
        next_line_free_at_utc=NOW + timedelta(minutes=20),
        last_started_at_utc=NOW - timedelta(minutes=1),
        last_dispatch_at_utc=NOW - timedelta(hours=2),
    )

    assert empty_round is TaskStatus.WAITING_LINES


def test_scanning_is_not_cooled_down_just_for_having_run() -> None:
    """跑完就冷却的话就是纯空转——填空隙正是它存在的理由。"""
    assert (
        status(MissionKind.SCAN, last_started_at_utc=NOW - timedelta(seconds=1)) is TaskStatus.READY
    )


def test_a_scan_that_just_crashed_says_cooling_down_not_waiting_for_lines() -> None:
    """崩过之后它也要等一轮冷却，而那句话必须说对是哪一种等。

    「等航线」对扫描是句假话（它压根不派遣），用户照着去调航线数只会白调。
    """
    just_crashed = status(
        MissionKind.SCAN,
        free_lines=0,
        # 它刚跑过、且从来不派遣，所以「上一轮空手而归」对它恒为真——正是这一点
        # 会把它误判成「等航线」。
        last_started_at_utc=NOW - timedelta(seconds=15),
        last_failure_at_utc=NOW - timedelta(seconds=14),
        next_line_free_at_utc=NOW + timedelta(minutes=20),
    )

    assert just_crashed is TaskStatus.COOLING_DOWN


def test_scanning_is_ready_even_with_no_lines() -> None:
    assert status(MissionKind.SCAN, free_lines=0) is TaskStatus.READY
