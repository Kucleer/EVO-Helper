"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、不看屏。用户描述的四个场景在这里逐条钉死。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from evo_helper.domain.scheduler import (
    Action,
    Decision,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskSnapshot,
    decide,
    has_work,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
DWELL = timedelta(seconds=60)

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


def tasks(*kinds: MissionKind) -> tuple[TaskSnapshot, ...]:
    return tuple(
        TaskSnapshot(kind=kind, enabled=True, priority=index) for index, kind in enumerate(kinds)
    )


# -- has_work ------------------------------------------------------------------


def test_scanning_always_has_work() -> None:
    """扫描不派遣，因此永远有活干——它正是用来填空隙的。"""
    assert has_work(MissionKind.SCAN, facts(free_lines=0))


def test_pirates_stop_when_the_daily_quota_is_used_up() -> None:
    """每天 32 次是游戏硬限制，超了会被强制返回。"""
    assert not has_work(MissionKind.PIRATE, facts(pirate_dispatches_today=32))


def test_pirates_stop_when_the_game_said_the_quota_is_gone() -> None:
    """收到超限邮件时 runner 会写下封锁截止时刻，那是比计数更硬的信号。"""
    blocked = facts(pirate_blocked_until_utc=NOW + timedelta(hours=3))

    assert not has_work(MissionKind.PIRATE, blocked)


def test_pirates_resume_once_the_blocked_until_time_is_in_the_past() -> None:
    """封锁截止时刻只在未来才生效；昨天的封锁不能把海盗永久停掉。"""
    expired = facts(pirate_blocked_until_utc=NOW - timedelta(hours=1))

    assert has_work(MissionKind.PIRATE, expired)


def test_a_full_line_pool_does_not_stop_a_task_that_owes_a_report() -> None:
    """航线满了也要能回去收战报——收报告不占航线。"""
    assert has_work(MissionKind.PIRATE, facts(free_lines=0, pirate_reports_due=True))


def test_a_full_line_pool_stops_a_task_with_nothing_due() -> None:
    """这就是「前序占满航线时不开下一个」。"""
    assert not has_work(MissionKind.PIRATE, facts(free_lines=0))


def test_bots_are_done_when_no_target_remains() -> None:
    assert not has_work(MissionKind.BOT, facts(bot_targets_remaining=0))


def test_a_full_line_pool_does_not_stop_a_bot_task_that_owes_a_report() -> None:
    """BOT 链路和海盗一样：收报告不占航线，否则任务会卡住永远退不出去。"""
    assert has_work(MissionKind.BOT, facts(free_lines=0, bot_reports_due=True))


# -- decide --------------------------------------------------------------------


def test_the_highest_priority_task_with_work_starts() -> None:
    """勾了 1-2-3：海盗优先。"""
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        facts(),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.START, MissionKind.PIRATE)


def test_priority_order_is_honored_not_just_input_order() -> None:
    """`tasks()` 用 enumerate 下标当 priority，输入顺序恒等于优先级顺序——
    这里手写 priority，故意把输入顺序和优先级顺序反过来，防住排序被删掉。
    """
    snapshot = (
        TaskSnapshot(kind=MissionKind.BOT, enabled=True, priority=5),
        TaskSnapshot(kind=MissionKind.PIRATE, enabled=True, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, MissionKind.PIRATE)


def test_scan_never_wins_even_with_the_smallest_priority_number() -> None:
    """规格变更：扫描恒排最后，不可拖。即使数据库里出现一条坏行，把 SCAN
    的 priority 设成全场最小，攻击任务仍然优先——领域层结构性兜底。
    """
    snapshot = (
        TaskSnapshot(kind=MissionKind.SCAN, enabled=True, priority=0),
        TaskSnapshot(kind=MissionKind.PIRATE, enabled=True, priority=99),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, MissionKind.PIRATE)


def test_scanning_fills_the_gap_when_the_attack_tasks_are_blocked() -> None:
    """勾了 1-3：海盗配额用尽后，扫描顶上。"""
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN),
        facts(pirate_dispatches_today=32),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.START, MissionKind.SCAN)


def test_two_attack_tasks_yield_to_the_one_with_a_due_report_when_lines_are_full() -> None:
    """规格第九节场景：两个攻击任务在航线占满时的让位——PIRATE 有到期战报，
    BOT 没有，该起 PIRATE（收报告不占航线）。
    """
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        facts(free_lines=0, pirate_reports_due=True),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.START, MissionKind.PIRATE)


def test_two_attack_tasks_both_yield_to_scan_when_lines_are_full_and_nothing_is_due() -> None:
    """同一场景的另一半：航线占满且谁都没有到期战报——两个攻击任务都没活干，
    扫描顶上填空隙。
    """
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        facts(free_lines=0),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.START, MissionKind.SCAN)


def test_a_disabled_task_never_starts() -> None:
    snapshot = (
        TaskSnapshot(kind=MissionKind.PIRATE, enabled=False, priority=0),
        TaskSnapshot(kind=MissionKind.SCAN, enabled=True, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, MissionKind.SCAN)


def test_an_auto_disabled_task_never_starts() -> None:
    """连续失败被自动停用的任务不该把调度循环拖成满速空转。"""
    snapshot = (
        TaskSnapshot(
            kind=MissionKind.PIRATE, enabled=True, priority=0, disabled_reason="连续 3 次异常退出"
        ),
        TaskSnapshot(kind=MissionKind.SCAN, enabled=True, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, MissionKind.SCAN)


def test_scanning_is_preempted_once_an_attack_task_has_work() -> None:
    running = RunningProcess(kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(seconds=90))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN), facts(), running=running, min_dwell=DWELL
    )

    assert decision == Decision(Action.PREEMPT, MissionKind.PIRATE)


def test_scanning_is_not_preempted_before_the_minimum_dwell() -> None:
    """航线一空一占会引起秒级反复切换，而每次切换都要校几何 + 认屏。"""
    running = RunningProcess(kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(seconds=10))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN), facts(), running=running, min_dwell=DWELL
    )

    assert decision == Decision(Action.IDLE, None)


def test_an_attack_round_is_never_preempted() -> None:
    """中途杀掉可能正停在派遣面板上。攻击轮一旦启动就跑完。"""
    running = RunningProcess(kind=MissionKind.BOT, started_at_utc=NOW - timedelta(minutes=30))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT), facts(), running=running, min_dwell=DWELL
    )

    assert decision == Decision(Action.IDLE, None)


def test_nothing_to_do_is_idle_not_an_error() -> None:
    decision = decide(
        tasks(MissionKind.PIRATE),
        facts(pirate_dispatches_today=32),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.IDLE, None)
