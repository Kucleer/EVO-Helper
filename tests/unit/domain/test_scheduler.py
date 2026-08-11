"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、不看屏。用户描述的四个场景在这里逐条钉死。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from evo_helper.domain.scheduler import (
    RESTART_COOLDOWN,
    Action,
    Decision,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskSnapshot,
    came_back_empty,
    decide,
    has_work,
    quota_day_start_utc,
    waiting_for_a_line,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
DWELL = timedelta(seconds=60)
SHANGHAI = timezone(timedelta(hours=8))

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


# -- 重启冷却 ------------------------------------------------------------------


def test_a_chain_that_just_ran_is_held_back_by_the_restart_cooldown() -> None:
    """堵的是「立即收取」的空转。

    `expected_report_at_utc` 为 NULL 时战报判据恒为「该去收」，而战报可能只是
    还没到：runner 进信箱、扑空、退出、下一 tick 判据仍为真、再起一次。不是
    死循环，但每轮几十秒的导航全白费，还一直占着鼠标不让扫描进来。
    """
    just_ran = facts(
        free_lines=0,
        pirate_reports_due=True,
        last_started_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=1)},
    )

    assert not has_work(MissionKind.PIRATE, just_ran, restart_cooldown=RESTART_COOLDOWN)


def test_the_cooldown_expires_and_the_chain_comes_back() -> None:
    """冷却是节流不是停用——过了就该照常起。"""
    cooled = facts(
        free_lines=0,
        pirate_reports_due=True,
        last_started_at_utc={MissionKind.PIRATE: NOW - RESTART_COOLDOWN - timedelta(seconds=1)},
    )

    assert has_work(MissionKind.PIRATE, cooled, restart_cooldown=RESTART_COOLDOWN)


def test_the_cooldown_only_holds_back_the_chain_that_just_ran() -> None:
    """冷却按 kind 分。海盗刚跑完，不该连累 bot。"""
    mixed = facts(last_started_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=1)})

    assert not has_work(MissionKind.PIRATE, mixed, restart_cooldown=RESTART_COOLDOWN)
    assert has_work(MissionKind.BOT, mixed, restart_cooldown=RESTART_COOLDOWN)


def test_a_cooling_chain_yields_its_turn_to_the_next_one() -> None:
    """冷却期内该 kind 视为「没活干」，顺位让给下一个——这正是让扫描挤进来的口子。"""
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN),
        facts(last_started_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=1)}),
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    )

    assert decision == Decision(Action.START, MissionKind.SCAN)


def test_scan_is_never_held_back_by_the_cooldown() -> None:
    """**扫描跳过冷却。** 刚被抢占、冷却期远未过，而攻击任务已经没活干了——
    这一刻应当立刻回到扫描，不是空转等满五分钟。

    冷却堵的 churn 是收战报特有的（NULL expected → 恒判「该去收」→ 进信箱扑空
    → 再来）。扫描没有这种循环，游标持久化、随起随停没有代价。套上去只会制造
    纯空转：攻击轮两分钟跑完、扫描还得再等三分钟，而填这种空隙正是扫描存在的
    全部理由。秒级来回归 `MIN_DWELL` 管，两者不重复。
    """
    just_preempted = facts(
        # 航线占满、没有到期战报 → 两条攻击链路都没活干。
        free_lines=0,
        last_started_at_utc={MissionKind.SCAN: NOW - timedelta(seconds=10)},
    )

    assert has_work(MissionKind.SCAN, just_preempted, restart_cooldown=RESTART_COOLDOWN)
    assert decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        just_preempted,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    ) == Decision(Action.START, MissionKind.SCAN)


def test_a_cooling_chain_does_not_preempt_the_running_scan() -> None:
    """冷却中的海盗不算「有活干」，因此不足以打断扫描。

    少了这一条，抢占那一路就绕过了冷却：扫描被打断、海盗因冷却起不来，
    结果是谁都没在跑。
    """
    running = RunningProcess(kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(minutes=5))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN),
        facts(last_started_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=1)}),
        running=running,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    )

    assert decision == Decision(Action.IDLE, None)


# -- 航线占满之后不要再一轮轮地起 ----------------------------------------------
#
# 实机 2026-08-11 01:12–01:34 UTC（本地 09:12–09:34）：`free_lines` 一路报 3，
# 游戏那边 6 条航线全满，海盗与 bot 交替起了九轮，每轮几十秒导航之后撞上
# 「同时派遣的舰队数量已达上限。」退出，冷却五分钟，再来。
#
# 成因不是判据写错，是 `free_lines` 这个估算错了而且**没有回写路径**：runner
# 在屏上看到了真相，可它撞上限之后的退出码（0）和跑完一轮正常收尾一模一样。

#: 上一轮启动之后再没派出去过任何一发——「空手而归」的最小事实组合。
_EMPTY_ROUND = {
    "last_started_at_utc": {MissionKind.PIRATE: NOW - RESTART_COOLDOWN - timedelta(seconds=1)},
    "last_dispatch_at_utc": {MissionKind.PIRATE: NOW - timedelta(hours=2)},
}


def test_a_round_that_dispatched_nothing_is_recognised_as_empty() -> None:
    """判据就是两个时刻比大小：上一次启动之后再没有过一条被接受的派遣记录。"""
    assert came_back_empty(MissionKind.PIRATE, facts(**_EMPTY_ROUND))


def test_a_round_that_actually_dispatched_is_not_empty() -> None:
    """派出去了就不算空手而归——这一刻没有任何理由怀疑航线估算。"""
    productive = facts(
        last_started_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=10)},
        last_dispatch_at_utc={MissionKind.PIRATE: NOW - timedelta(minutes=9)},
    )

    assert not came_back_empty(MissionKind.PIRATE, productive)


def test_a_chain_that_never_ran_is_not_treated_as_empty() -> None:
    """没跑过就没有「上一轮」。开机第一轮不该被自己的空白历史压住。"""
    assert not came_back_empty(MissionKind.PIRATE, facts(last_dispatch_at_utc={}))


def test_an_empty_round_stops_the_chain_while_a_fleet_is_still_out() -> None:
    """**这就是用户说的「航路上限到达后，不应继续海盗任务」。**

    估算说还有一条空闲航线，可上一轮从头跑到尾一发都没派出去，而且还有舰队在
    外面没回来——照着同一个估算再起一轮，只会把上一轮原样重演一遍。
    """
    blocked = facts(
        free_lines=3,
        next_line_free_at_utc=NOW + timedelta(minutes=3),
        **_EMPTY_ROUND,
    )

    assert waiting_for_a_line(MissionKind.PIRATE, blocked)
    assert not has_work(MissionKind.PIRATE, blocked, restart_cooldown=RESTART_COOLDOWN)


def test_the_chain_comes_back_once_a_line_actually_frees_up() -> None:
    """**不许做成永久不起。** 压到的那个时刻是库里查出来的，到点自动解除。"""
    freed = facts(
        free_lines=3,
        next_line_free_at_utc=NOW - timedelta(seconds=1),
        **_EMPTY_ROUND,
    )

    assert not waiting_for_a_line(MissionKind.PIRATE, freed)
    assert has_work(MissionKind.PIRATE, freed, restart_cooldown=RESTART_COOLDOWN)


def test_an_empty_round_with_nothing_in_flight_is_not_blocked() -> None:
    """一支在飞的都没有时，这一层对「航线满不满」没有任何证据，那就不猜。

    空手而归还有别的成因（这一圈没有海盗、目标都在保护期里）。单凭它就压着
    链路，等于把一条与航线无关的规则塞进航线判据，而且没有任何时刻可以解除。
    这一档照旧交给 `RESTART_COOLDOWN` 节流。
    """
    no_anchor = facts(free_lines=3, next_line_free_at_utc=None, **_EMPTY_ROUND)

    assert not waiting_for_a_line(MissionKind.PIRATE, no_anchor)
    assert has_work(MissionKind.PIRATE, no_anchor, restart_cooldown=RESTART_COOLDOWN)


def test_waiting_for_a_line_never_holds_back_report_collection() -> None:
    """只挡「去派」那半边判据。收报告不占航线，压着它只会让战报烂在信箱里。"""
    blocked = facts(
        free_lines=3,
        next_line_free_at_utc=NOW + timedelta(minutes=3),
        pirate_reports_due=True,
        **_EMPTY_ROUND,
    )

    assert waiting_for_a_line(MissionKind.PIRATE, blocked)
    assert has_work(MissionKind.PIRATE, blocked, restart_cooldown=RESTART_COOLDOWN)


def test_an_empty_pirate_round_does_not_hold_back_the_bot_chain() -> None:
    """空手而归按 kind 分。海盗那轮什么都没派出去，不该连累 bot。"""
    mixed = facts(free_lines=3, next_line_free_at_utc=NOW + timedelta(minutes=3), **_EMPTY_ROUND)

    assert waiting_for_a_line(MissionKind.PIRATE, mixed)
    assert not waiting_for_a_line(MissionKind.BOT, mixed)


def test_scanning_fills_the_gap_while_the_attack_chains_wait_for_a_line() -> None:
    """两条攻击链路都在等航线时，扫描顶上——那正是它存在的理由。

    这一条盯的是整轮里最贵的那件事：实机上那九轮不只是白跑，它们一直占着鼠标，
    扫描一次都挤不进来。
    """
    stuck = facts(
        free_lines=3,
        next_line_free_at_utc=NOW + timedelta(minutes=3),
        last_started_at_utc={
            MissionKind.PIRATE: NOW - RESTART_COOLDOWN - timedelta(seconds=1),
            MissionKind.BOT: NOW - RESTART_COOLDOWN - timedelta(seconds=1),
        },
        last_dispatch_at_utc={
            MissionKind.PIRATE: NOW - timedelta(hours=2),
            MissionKind.BOT: NOW - timedelta(hours=2),
        },
    )

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        stuck,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    )

    assert decision == Decision(Action.START, MissionKind.SCAN)


# -- 配额的起算时刻 ------------------------------------------------------------


def test_the_quota_day_starts_at_utc_midnight_not_local_midnight() -> None:
    """重置点是 UTC 00:00，本地（UTC+8）是每天早上 8 点。

    本地时间早上 3 点这一刻，UTC 还停在前一天，当日配额已经起算了 19 小时。
    按本地日历天截断会把起算点推到本地 0 点（= 那个 UTC 日的 16:00），于是该
    UTC 日 00:00–16:00 这整段真实的派遣被漏数，海盗以为还有额度，白飞一趟舰队。
    """
    local_early_morning = datetime(2026, 8, 9, 3, 0, tzinfo=SHANGHAI)

    assert quota_day_start_utc(local_early_morning) == datetime(2026, 8, 8, 0, 0, tzinfo=UTC)


def test_the_quota_day_start_is_the_same_instant_expressed_in_utc() -> None:
    utc_noon = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)

    assert quota_day_start_utc(utc_noon) == datetime(2026, 8, 9, 0, 0, tzinfo=UTC)


def test_a_naive_timestamp_is_refused_rather_than_guessed() -> None:
    """没有时区的时刻无从判断它属于哪个 UTC 日，猜错就是整段配额算错。"""
    with pytest.raises(ValueError):
        quota_day_start_utc(datetime(2026, 8, 9, 3, 0))
