"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、不看屏。用户描述的四个场景在这里逐条钉死。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from evo_helper.domain.scheduler import (
    ENVIRONMENT_FAULT_WINDOW,
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
    kinds_failing_together,
    looks_like_an_environment_fault,
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


def test_scan_is_not_held_back_after_a_clean_round() -> None:
    """**扫描没崩就跳过冷却。** 刚被抢占、冷却期远未过，而攻击任务已经没活干了
    ——这一刻应当立刻回到扫描，不是空转等满五分钟。

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


def test_scan_does_cool_down_after_it_crashed() -> None:
    """**崩掉的那一档不一样。**

    实机 2026-08-11 08:40:30 / 08:40:45 / 08:40:59：同一个「游戏窗口抢不到前台」
    把扫描连崩三次，每次 14 秒——不冷却的话，`MAX_CONSECUTIVE_FAILURES` 只需要
    **43 秒**就把这条链路自动停用，而另外两条有冷却的链路要撞满 10 分钟才落到
    同一个下场。于是最该一直有活干的那条，最容易被一阵前台争抢误判成坏掉。
    """
    just_crashed = facts(
        free_lines=0,
        last_started_at_utc={MissionKind.SCAN: NOW - timedelta(seconds=15)},
        last_failure_at_utc={MissionKind.SCAN: NOW - timedelta(seconds=14)},
    )

    assert not has_work(MissionKind.SCAN, just_crashed, restart_cooldown=RESTART_COOLDOWN)
    assert decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        just_crashed,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    ) == Decision(Action.IDLE, None)


def test_scan_comes_back_once_the_crash_cooldown_expires() -> None:
    """**不许做成崩一次就再也不起。** 冷却是节流，不是墓碑。"""
    cooled = facts(
        free_lines=0,
        last_failure_at_utc={MissionKind.SCAN: NOW - RESTART_COOLDOWN - timedelta(seconds=1)},
    )

    assert has_work(MissionKind.SCAN, cooled, restart_cooldown=RESTART_COOLDOWN)


def test_a_scan_in_its_crash_cooldown_is_never_called_waiting_for_a_line() -> None:
    """扫描压根不派遣，航线满不满与它无关。

    `came_back_empty` 对它恒为真（它永远不会出现在 `last_dispatch_at_utc` 里），
    不挡一道的话，一条只是在崩溃冷却里的扫描会被 `status_of` 说成「等航线」
    ——一句用户照着去调航线数、调完也不会有任何变化的假话。
    """
    stuck = facts(
        free_lines=0,
        last_started_at_utc={MissionKind.SCAN: NOW - timedelta(seconds=15)},
        next_line_free_at_utc=NOW + timedelta(minutes=20),
    )

    assert not waiting_for_a_line(MissionKind.SCAN, stuck)


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


# -- 环境故障：多条链路在同一时间窗里一起倒 ------------------------------------
#
# **实机 2026-08-12。** 01:55「BOT 已停用（连续 3 次异常退出，退出码 1）」，
# 04:37 三条**全部**已停用。BOT 从 01:55 停到 04:37，近三个小时一发没派。
# 三条链路共用一个游戏窗口、一个鼠标、一份连接和一台机器，同时坏掉几乎必然是
# 那些共用的东西坏了，而不是三处互不相干的代码在同一晚一起长出 bug。


def test_a_chain_failing_by_itself_is_its_own_problem() -> None:
    """**豁免不能退化成「所有失败都不算失败」。**

    只有它一条在倒的时候，那就是它自己的毛病。这一条为假，自动停用整个失效，
    调度循环会在一个坏掉的任务上一轮轮空转。
    """
    assert not looks_like_an_environment_fault(MissionKind.PIRATE, NOW, {})


def test_two_chains_failing_minutes_apart_read_as_one_environment_fault() -> None:
    """环境坏掉时三条链路是**接连**倒下的：起来就崩、崩完等一个冷却、再来。"""
    recent = {MissionKind.SCAN: NOW - timedelta(seconds=30)}

    assert looks_like_an_environment_fault(MissionKind.PIRATE, NOW, recent)


def test_failures_far_apart_are_two_separate_faults() -> None:
    """**这是「怎么区分」那道题的另一半。**

    隔了大半个钟头才轮到第二条，那不是同一阵故障——环境坏掉时每条链路
    五分钟就撞一次，不会等那么久。时间窗一放开，这条豁免就会开始吃掉真正的故障。

    ⚠️ 这里的 40 分钟**写死**，不许写成 `ENVIRONMENT_FAULT_WINDOW + 1 分钟`：
    那样的话时间窗改多大，这个时刻就跟着挪多远，用例永远绿——变异验证时正是
    这么发现的，把窗口放大到一整天它照样通过。
    """
    recent = {MissionKind.SCAN: NOW - timedelta(minutes=40)}

    assert not looks_like_an_environment_fault(MissionKind.PIRATE, NOW, recent)


def test_the_window_covers_a_burst_but_not_a_night() -> None:
    """窗口得比一次重启冷却宽、比一整夜窄，两头都会坏事。

    - **窄于 `RESTART_COOLDOWN`**：环境坏掉时第二条链路要等前一条的冷却过去才
      轮得到再崩一次，「接连倒下」根本落不进同一个窗口，豁免形同虚设。
    - **宽到按小时算**：一整夜里两处互不相干的真故障必然会挤进同一个窗口，
      于是自动停用被这条豁免整个吃掉。
    """
    assert RESTART_COOLDOWN < ENVIRONMENT_FAULT_WINDOW < timedelta(hours=1)


def test_a_chain_repeating_its_own_crash_never_corroborates_itself() -> None:
    """同一条链路崩两次不构成「多条一起倒」。

    自己给自己作证的话，任何一条高频复发的真故障都会自动豁免掉，
    `MAX_CONSECUTIVE_FAILURES` 从此永远数不到。
    """
    recent = {MissionKind.PIRATE: NOW - timedelta(seconds=30)}

    assert not looks_like_an_environment_fault(MissionKind.PIRATE, NOW, recent)


def test_the_kinds_in_one_fault_include_the_one_that_just_failed() -> None:
    """调用方拿这一组去清计数：刚倒下的那条也记错了账，不能漏掉自己。"""
    recent = {MissionKind.SCAN: NOW - timedelta(seconds=30), MissionKind.BOT: NOW}

    together = kinds_failing_together(MissionKind.PIRATE, NOW, recent)

    assert together == {MissionKind.PIRATE, MissionKind.SCAN, MissionKind.BOT}


def test_a_record_from_the_future_is_ignored_rather_than_trusted() -> None:
    """出现比「现在」还晚的记录只能是时钟被调过。

    那时宁可少认一次环境故障，也不要凭一个说不清的差值去豁免一次真正的崩溃。

    ⚠️ 这里的 5 分钟**必须落在时间窗以内**：取一个窗口以外的未来时刻，判据写成
    `abs(at - moment) <= window` 也照样能通过——那样这条用例就只是在重测窗口宽度，
    根本没碰「未来」这件事。
    """
    recent = {MissionKind.SCAN: NOW + timedelta(minutes=5)}

    assert not looks_like_an_environment_fault(MissionKind.PIRATE, NOW, recent)
