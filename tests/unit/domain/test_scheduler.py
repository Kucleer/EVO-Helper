"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、不看屏。用户描述的四个场景在这里逐条钉死。

**判据按任务认人，不按链路认人**（用户口径 2026-08-13：可以有多个 bot 攻击任务），
所以这里的每个用例都拿 `TaskSnapshot` 说话，事实按 `task_id` 挂在
`SchedulerFacts.per_task` 上。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    ENVIRONMENT_FAULT_WINDOW,
    RESTART_COOLDOWN,
    Action,
    Decision,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    came_back_empty,
    decide,
    free_lines_for,
    has_work,
    looks_like_an_environment_fault,
    quota_day_start_utc,
    tasks_failing_together,
    waiting_for_a_line,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
DWELL = timedelta(seconds=60)
SHANGHAI = timezone(timedelta(hours=8))

#: 用户的三颗星球（实机截图确认 2026-08-12）。主星是奥格瑞玛。
HOME = Coordinate(2, 137, 18)
SECOND = Coordinate(9, 250, 8)


def task(
    kind: MissionKind,
    *,
    task_id: int,
    priority: int = 0,
    enabled: bool = True,
    disabled_reason: str | None = None,
    origin: Coordinate = HOME,
    fleet_lines: int = 6,
    name: str = "",
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id,
        kind=kind,
        name=name,
        enabled=enabled,
        priority=priority,
        origin=origin,
        fleet_lines=fleet_lines,
        disabled_reason=disabled_reason,
    )


PIRATE = task(MissionKind.PIRATE, task_id=1, priority=0)
BOT = task(MissionKind.BOT, task_id=2, priority=1)
SCAN = task(MissionKind.SCAN, task_id=3, priority=2)
RANKING = task(MissionKind.RANKING, task_id=4, priority=3)

#: 一个「什么都不挡」的任务事实：有一条空闲航线，本轮还剩目标。
_BASE = TaskFacts(free_lines=1, targets_remaining=5)


def facts(
    *,
    per: Mapping[TaskSnapshot, dict[str, Any]] | None = None,
    every: dict[str, Any] | None = None,
    **account: Any,
) -> SchedulerFacts:
    """搭一份事实。

    `every` 落到每个任务上（「航线全满」这类），`per` 再逐个覆盖（「只有海盗有
    到期战报」这类），其余关键字是账号级事实（海盗配额）。
    """
    base = replace(_BASE, **(every or {}))
    per_task = {item.task_id: base for item in (PIRATE, BOT, SCAN)}
    for item, fields in (per or {}).items():
        per_task[item.task_id] = replace(base, **fields)
    return SchedulerFacts(now_utc=NOW, per_task=per_task, **account)


# -- 按出发星球记账 ------------------------------------------------------------
#
# 用户口径（2026-08-13，追问确认）：「航线上限是按星球各一份的，不是账号共享」。


def test_lines_run_out_when_this_planet_has_that_many_fleets_out() -> None:
    """在飞数达到这个任务的航线数，就没位子了。"""
    five_lines = task(MissionKind.BOT, task_id=10, fleet_lines=5)

    assert free_lines_for(five_lines, inflight_from_origin=4, reserved_lines=0) == 1
    assert free_lines_for(five_lines, inflight_from_origin=5, reserved_lines=0) == 0


def test_another_planets_fleets_do_not_eat_this_planets_lines() -> None:
    """**不同出发星球互不影响。**

    主星 5 条打满的时候，2 号星那 2 条一条都没被占——调用方只把同一颗星球上的
    在飞数喂进来，这里就自然分开了。跨星球一起数的话，2 号星那个任务会以为自己
    也没位子，一发都不派。

    ⚠️ 两个任务的航线数**故意不同**（5 与 2）：填成同一个数的话，把
    `free_lines_for` 改成「谁的在飞数都算」也未必露馅。
    """
    main = task(MissionKind.BOT, task_id=10, origin=HOME, fleet_lines=5)
    second = task(MissionKind.BOT, task_id=11, origin=SECOND, fleet_lines=2)

    assert free_lines_for(main, inflight_from_origin=5, reserved_lines=0) == 0
    assert free_lines_for(second, inflight_from_origin=0, reserved_lines=0) == 2


def test_reserved_lines_are_kept_free_on_each_planet() -> None:
    """`reserved_lines` 是给用户自己留的缓冲，按星球生效。"""
    five_lines = task(MissionKind.BOT, task_id=10, fleet_lines=5)

    assert free_lines_for(five_lines, inflight_from_origin=0, reserved_lines=2) == 3


def test_over_reserving_yields_zero_rather_than_a_negative_count() -> None:
    """留的比总数还多时是 0，不是负数——负数会让「> 0」之外的比较全部走样。"""
    two_lines = task(MissionKind.BOT, task_id=10, fleet_lines=2)

    assert free_lines_for(two_lines, inflight_from_origin=0, reserved_lines=5) == 0
    assert free_lines_for(two_lines, inflight_from_origin=99, reserved_lines=0) == 0


def test_a_bot_task_stops_dispatching_once_its_own_lines_are_full() -> None:
    """把上面那条接到 `has_work` 上：自己那颗星球满了就不再派。"""
    full = facts(per={BOT: {"free_lines": 0}})

    assert not has_work(BOT, full)


def test_two_bot_tasks_on_different_planets_are_accounted_for_separately() -> None:
    """**多个 BOT 任务各自独立记账。** 主星那个满了，2 号星那个照样能派。"""
    main = task(MissionKind.BOT, task_id=10, priority=0, origin=HOME, fleet_lines=5)
    second = task(MissionKind.BOT, task_id=11, priority=1, origin=SECOND, fleet_lines=2)
    mixed = SchedulerFacts(
        now_utc=NOW,
        per_task={
            main.task_id: TaskFacts(free_lines=0, targets_remaining=3),
            second.task_id: TaskFacts(free_lines=2, targets_remaining=3),
        },
    )

    assert not has_work(main, mixed)
    assert has_work(second, mixed)
    assert decide([main, second], mixed, running=None, min_dwell=DWELL) == Decision(
        Action.START, second
    )


# -- has_work ------------------------------------------------------------------


def test_scanning_always_has_work() -> None:
    """扫描不派遣，因此永远有活干——它正是用来填空隙的。"""
    assert has_work(SCAN, facts(every={"free_lines": 0}))


def test_pirates_stop_when_the_daily_quota_is_used_up() -> None:
    """每天 32 次是游戏硬限制，超了会被强制返回。"""
    assert not has_work(PIRATE, facts(pirate_dispatches_today=32))


def test_the_pirate_quota_is_per_account_not_per_planet() -> None:
    """**配额不跟着航线一起改成按星球。**

    航线上限是按星球各一份的，而每天 32 次是游戏对**账号**的硬限制。跟着改成
    按星球，等于凭空把配额翻倍——超了会收到超限邮件、舰队被强制返回。
    """
    elsewhere = task(MissionKind.PIRATE, task_id=12, origin=SECOND)

    assert not has_work(elsewhere, facts(pirate_dispatches_today=32))


def test_pirates_stop_when_the_game_said_the_quota_is_gone() -> None:
    """收到超限邮件时 runner 会写下封锁截止时刻，那是比计数更硬的信号。"""
    blocked = facts(pirate_blocked_until_utc=NOW + timedelta(hours=3))

    assert not has_work(PIRATE, blocked)


def test_pirates_resume_once_the_blocked_until_time_is_in_the_past() -> None:
    """封锁截止时刻只在未来才生效；昨天的封锁不能把海盗永久停掉。"""
    expired = facts(pirate_blocked_until_utc=NOW - timedelta(hours=1))

    assert has_work(PIRATE, expired)


def test_a_full_line_pool_does_not_stop_a_task_that_owes_a_report() -> None:
    """航线满了也要能回去收战报——收报告不占航线。"""
    assert has_work(PIRATE, facts(every={"free_lines": 0}, per={PIRATE: {"reports_due": True}}))


def test_a_full_line_pool_stops_a_task_with_nothing_due() -> None:
    """这就是「前序占满航线时不开下一个」。"""
    assert not has_work(PIRATE, facts(every={"free_lines": 0}))


def test_bots_are_done_when_no_target_remains() -> None:
    assert not has_work(BOT, facts(per={BOT: {"targets_remaining": 0}}))


def test_one_bot_task_finishing_its_round_does_not_finish_the_other() -> None:
    """两个 bot 任务各打各的范围、各开各的轮，完成态也就各算各的。"""
    main = task(MissionKind.BOT, task_id=10, origin=HOME)
    second = task(MissionKind.BOT, task_id=11, origin=SECOND)
    mixed = SchedulerFacts(
        now_utc=NOW,
        per_task={
            main.task_id: TaskFacts(free_lines=1, targets_remaining=0),
            second.task_id: TaskFacts(free_lines=1, targets_remaining=4),
        },
    )

    assert not has_work(main, mixed)
    assert has_work(second, mixed)


def test_a_full_line_pool_does_not_stop_a_bot_task_that_owes_a_report() -> None:
    """BOT 链路和海盗一样：收报告不占航线，否则任务会卡住永远退不出去。"""
    assert has_work(BOT, facts(every={"free_lines": 0}, per={BOT: {"reports_due": True}}))


def test_a_due_report_on_one_bot_task_does_not_wake_the_other() -> None:
    """战报也按出发星球分：主星那些没到的战报不该让 2 号星那个任务进信箱扑空。"""
    main = task(MissionKind.BOT, task_id=10, origin=HOME)
    second = task(MissionKind.BOT, task_id=11, origin=SECOND)
    mixed = SchedulerFacts(
        now_utc=NOW,
        per_task={
            main.task_id: TaskFacts(free_lines=0, targets_remaining=3, reports_due=True),
            second.task_id: TaskFacts(free_lines=0, targets_remaining=3, reports_due=False),
        },
    )

    assert has_work(main, mixed)
    assert not has_work(second, mixed)


# -- decide --------------------------------------------------------------------


def test_the_highest_priority_task_with_work_starts() -> None:
    """勾了 1-2-3：海盗优先。"""
    decision = decide([PIRATE, BOT, SCAN], facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, PIRATE)


def test_priority_order_is_honored_not_just_input_order() -> None:
    """输入顺序和优先级顺序故意反过来，防住排序被删掉。"""
    snapshot = (
        task(MissionKind.BOT, task_id=BOT.task_id, priority=5),
        task(MissionKind.PIRATE, task_id=PIRATE.task_id, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, snapshot[1])


def test_scan_never_wins_even_with_the_smallest_priority_number() -> None:
    """规格变更：扫描恒排最后，不可拖。即使数据库里出现一条坏行，把 SCAN
    的 priority 设成全场最小，攻击任务仍然优先——领域层结构性兜底。
    """
    snapshot = (
        task(MissionKind.SCAN, task_id=SCAN.task_id, priority=0),
        task(MissionKind.PIRATE, task_id=PIRATE.task_id, priority=99),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, snapshot[1])


def test_scanning_fills_the_gap_when_the_attack_tasks_are_blocked() -> None:
    """勾了 1-3：海盗配额用尽后，扫描顶上。"""
    decision = decide(
        [PIRATE, SCAN], facts(pirate_dispatches_today=32), running=None, min_dwell=DWELL
    )

    assert decision == Decision(Action.START, SCAN)


def test_two_attack_tasks_yield_to_the_one_with_a_due_report_when_lines_are_full() -> None:
    """规格第九节场景：两个攻击任务在航线占满时的让位——PIRATE 有到期战报，
    BOT 没有，该起 PIRATE（收报告不占航线）。
    """
    decision = decide(
        [PIRATE, BOT, SCAN],
        facts(every={"free_lines": 0}, per={PIRATE: {"reports_due": True}}),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == Decision(Action.START, PIRATE)


def test_two_attack_tasks_both_yield_to_scan_when_lines_are_full_and_nothing_is_due() -> None:
    """同一场景的另一半：航线占满且谁都没有到期战报——两个攻击任务都没活干，
    扫描顶上填空隙。
    """
    decision = decide(
        [PIRATE, BOT, SCAN], facts(every={"free_lines": 0}), running=None, min_dwell=DWELL
    )

    assert decision == Decision(Action.START, SCAN)


def test_a_disabled_task_never_starts() -> None:
    snapshot = (
        task(MissionKind.PIRATE, task_id=PIRATE.task_id, enabled=False, priority=0),
        SCAN,
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, SCAN)


def test_an_auto_disabled_task_never_starts() -> None:
    """连续失败被自动停用的任务不该把调度循环拖成满速空转。"""
    snapshot = (
        task(
            MissionKind.PIRATE,
            task_id=PIRATE.task_id,
            priority=0,
            disabled_reason="连续 3 次异常退出",
        ),
        SCAN,
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, SCAN)


def test_scanning_is_preempted_once_an_attack_task_has_work() -> None:
    running = RunningProcess(
        task_id=SCAN.task_id, kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(seconds=90)
    )

    decision = decide([PIRATE, SCAN], facts(), running=running, min_dwell=DWELL)

    assert decision == Decision(Action.PREEMPT, PIRATE)


def test_the_ranking_scan_is_preempted_once_an_attack_task_has_work() -> None:
    """⚠️⚠️ **今晚这套跑起来最要命的一条。**

    军力榜采集和扫描一样是**填空隙**的（`GAP_FILLERS`），攻击到点了必须抢得回
    鼠标。抢不回来的后果不是「慢一点」，是**整夜的攻击都被采集压着**——而采集
    一趟要十几分钟，战报窗口只有几分钟。

    变异测试当场抓到过：把抢占判断写回 `running.kind is MissionKind.SCAN`
    （加军力榜之前那版）时，所有用例照样绿。
    """
    running = RunningProcess(
        task_id=RANKING.task_id,
        kind=MissionKind.RANKING,
        started_at_utc=NOW - timedelta(seconds=90),
    )

    decision = decide([PIRATE, RANKING], facts(), running=running, min_dwell=DWELL)

    assert decision == Decision(Action.PREEMPT, PIRATE)


def test_the_ranking_scan_sorts_behind_every_attack_chain() -> None:
    """填空隙的排最后，否则它会插到攻击前面去——而它一跑就是十几分钟。"""
    decision = decide([RANKING, SCAN, BOT, PIRATE], facts(), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.START, PIRATE)


def test_scanning_is_not_preempted_before_the_minimum_dwell() -> None:
    """航线一空一占会引起秒级反复切换，而每次切换都要校几何 + 认屏。"""
    running = RunningProcess(
        task_id=SCAN.task_id, kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(seconds=10)
    )

    decision = decide([PIRATE, SCAN], facts(), running=running, min_dwell=DWELL)

    assert decision == Decision(Action.IDLE, None)


def test_an_attack_round_is_never_preempted() -> None:
    """中途杀掉可能正停在派遣面板上。攻击轮一旦启动就跑完。"""
    running = RunningProcess(
        task_id=BOT.task_id, kind=MissionKind.BOT, started_at_utc=NOW - timedelta(minutes=30)
    )

    decision = decide([PIRATE, BOT], facts(), running=running, min_dwell=DWELL)

    assert decision == Decision(Action.IDLE, None)


def test_nothing_to_do_is_idle_not_an_error() -> None:
    decision = decide([PIRATE], facts(pirate_dispatches_today=32), running=None, min_dwell=DWELL)

    assert decision == Decision(Action.IDLE, None)


# -- 重启冷却 ------------------------------------------------------------------


def test_a_chain_that_just_ran_is_held_back_by_the_restart_cooldown() -> None:
    """堵的是「立即收取」的空转。

    `expected_report_at_utc` 为 NULL 时战报判据恒为「该去收」，而战报可能只是
    还没到：runner 进信箱、扑空、退出、下一 tick 判据仍为真、再起一次。不是
    死循环，但每轮几十秒的导航全白费，还一直占着鼠标不让扫描进来。
    """
    just_ran = facts(
        every={"free_lines": 0},
        per={
            PIRATE: {
                "free_lines": 0,
                "reports_due": True,
                "last_started_at_utc": NOW - timedelta(minutes=1),
            }
        },
    )

    assert not has_work(PIRATE, just_ran, restart_cooldown=RESTART_COOLDOWN)


def test_the_cooldown_expires_and_the_chain_comes_back() -> None:
    """冷却是节流不是停用——过了就该照常起。"""
    cooled = facts(
        per={
            PIRATE: {
                "free_lines": 0,
                "reports_due": True,
                "last_started_at_utc": NOW - RESTART_COOLDOWN - timedelta(seconds=1),
            }
        }
    )

    assert has_work(PIRATE, cooled, restart_cooldown=RESTART_COOLDOWN)


def test_the_cooldown_only_holds_back_the_task_that_just_ran() -> None:
    """冷却**按任务**分。海盗刚跑完，不该连累 bot。"""
    mixed = facts(per={PIRATE: {"last_started_at_utc": NOW - timedelta(minutes=1)}})

    assert not has_work(PIRATE, mixed, restart_cooldown=RESTART_COOLDOWN)
    assert has_work(BOT, mixed, restart_cooldown=RESTART_COOLDOWN)


def test_one_bot_task_cooling_down_does_not_hold_back_the_other() -> None:
    """**同一链路的两个任务也各冷却各的。**

    按链路记的话，主星那个刚跑完，2 号星那个要干等五分钟——而它俩占的根本不是
    同一份航线，没有任何理由互相节流。
    """
    main = task(MissionKind.BOT, task_id=10, origin=HOME)
    second = task(MissionKind.BOT, task_id=11, origin=SECOND)
    mixed = SchedulerFacts(
        now_utc=NOW,
        per_task={
            main.task_id: TaskFacts(
                free_lines=1, targets_remaining=3, last_started_at_utc=NOW - timedelta(minutes=1)
            ),
            second.task_id: TaskFacts(free_lines=1, targets_remaining=3),
        },
    )

    assert not has_work(main, mixed, restart_cooldown=RESTART_COOLDOWN)
    assert has_work(second, mixed, restart_cooldown=RESTART_COOLDOWN)


def test_a_cooling_chain_yields_its_turn_to_the_next_one() -> None:
    """冷却期内该任务视为「没活干」，顺位让给下一个——这正是让扫描挤进来的口子。"""
    decision = decide(
        [PIRATE, SCAN],
        facts(per={PIRATE: {"last_started_at_utc": NOW - timedelta(minutes=1)}}),
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    )

    assert decision == Decision(Action.START, SCAN)


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
        every={"free_lines": 0},
        per={SCAN: {"free_lines": 0, "last_started_at_utc": NOW - timedelta(seconds=10)}},
    )

    assert has_work(SCAN, just_preempted, restart_cooldown=RESTART_COOLDOWN)
    assert decide(
        [PIRATE, BOT, SCAN],
        just_preempted,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    ) == Decision(Action.START, SCAN)


def test_scan_does_cool_down_after_it_crashed() -> None:
    """**崩掉的那一档不一样。**

    实机 2026-08-11 08:40:30 / 08:40:45 / 08:40:59：同一个「游戏窗口抢不到前台」
    把扫描连崩三次，每次 14 秒——不冷却的话，`MAX_CONSECUTIVE_FAILURES` 只需要
    **43 秒**就把这条链路自动停用，而另外两条有冷却的链路要撞满 10 分钟才落到
    同一个下场。于是最该一直有活干的那条，最容易被一阵前台争抢误判成坏掉。
    """
    just_crashed = facts(
        every={"free_lines": 0},
        per={
            SCAN: {
                "free_lines": 0,
                "last_started_at_utc": NOW - timedelta(seconds=15),
                "last_failure_at_utc": NOW - timedelta(seconds=14),
            }
        },
    )

    assert not has_work(SCAN, just_crashed, restart_cooldown=RESTART_COOLDOWN)
    assert decide(
        [PIRATE, BOT, SCAN],
        just_crashed,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    ) == Decision(Action.IDLE, None)


def test_scan_comes_back_once_the_crash_cooldown_expires() -> None:
    """**不许做成崩一次就再也不起。** 冷却是节流，不是墓碑。"""
    cooled = facts(
        every={"free_lines": 0},
        per={
            SCAN: {
                "free_lines": 0,
                "last_failure_at_utc": NOW - RESTART_COOLDOWN - timedelta(seconds=1),
            }
        },
    )

    assert has_work(SCAN, cooled, restart_cooldown=RESTART_COOLDOWN)


def test_a_scan_in_its_crash_cooldown_is_never_called_waiting_for_a_line() -> None:
    """扫描压根不派遣，航线满不满与它无关。

    `came_back_empty` 对它恒为真（它永远不会有 `last_dispatch_at_utc`），
    不挡一道的话，一条只是在崩溃冷却里的扫描会被 `status_of` 说成「等航线」
    ——一句用户照着去调航线数、调完也不会有任何变化的假话。
    """
    stuck = facts(
        per={
            SCAN: {
                "free_lines": 0,
                "last_started_at_utc": NOW - timedelta(seconds=15),
                "next_line_free_at_utc": NOW + timedelta(minutes=20),
            }
        }
    )

    assert not waiting_for_a_line(SCAN, stuck)


def test_a_cooling_chain_does_not_preempt_the_running_scan() -> None:
    """冷却中的海盗不算「有活干」，因此不足以打断扫描。

    少了这一条，抢占那一路就绕过了冷却：扫描被打断、海盗因冷却起不来，
    结果是谁都没在跑。
    """
    running = RunningProcess(
        task_id=SCAN.task_id, kind=MissionKind.SCAN, started_at_utc=NOW - timedelta(minutes=5)
    )

    decision = decide(
        [PIRATE, SCAN],
        facts(per={PIRATE: {"last_started_at_utc": NOW - timedelta(minutes=1)}}),
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
    "last_started_at_utc": NOW - RESTART_COOLDOWN - timedelta(seconds=1),
    "last_dispatch_at_utc": NOW - timedelta(hours=2),
}


def test_a_round_that_dispatched_nothing_is_recognised_as_empty() -> None:
    """判据就是两个时刻比大小：上一次启动之后再没有过一条被接受的派遣记录。"""
    assert came_back_empty(PIRATE, facts(per={PIRATE: dict(_EMPTY_ROUND)}))


def test_a_round_that_actually_dispatched_is_not_empty() -> None:
    """派出去了就不算空手而归——这一刻没有任何理由怀疑航线估算。"""
    productive = facts(
        per={
            PIRATE: {
                "last_started_at_utc": NOW - timedelta(minutes=10),
                "last_dispatch_at_utc": NOW - timedelta(minutes=9),
            }
        }
    )

    assert not came_back_empty(PIRATE, productive)


def test_a_chain_that_never_ran_is_not_treated_as_empty() -> None:
    """没跑过就没有「上一轮」。开机第一轮不该被自己的空白历史压住。"""
    assert not came_back_empty(PIRATE, facts())


def test_an_empty_round_stops_the_chain_while_a_fleet_is_still_out() -> None:
    """**这就是用户说的「航路上限到达后，不应继续海盗任务」。**

    估算说还有一条空闲航线，可上一轮从头跑到尾一发都没派出去，而且还有舰队在
    外面没回来——照着同一个估算再起一轮，只会把上一轮原样重演一遍。
    """
    blocked = facts(
        per={
            PIRATE: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW + timedelta(minutes=3),
                **_EMPTY_ROUND,
            }
        }
    )

    assert waiting_for_a_line(PIRATE, blocked)
    assert not has_work(PIRATE, blocked, restart_cooldown=RESTART_COOLDOWN)


def test_the_chain_comes_back_once_a_line_actually_frees_up() -> None:
    """**不许做成永久不起。** 压到的那个时刻是库里查出来的，到点自动解除。"""
    freed = facts(
        per={
            PIRATE: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW - timedelta(seconds=1),
                **_EMPTY_ROUND,
            }
        }
    )

    assert not waiting_for_a_line(PIRATE, freed)
    assert has_work(PIRATE, freed, restart_cooldown=RESTART_COOLDOWN)


def test_an_empty_round_with_nothing_in_flight_is_not_blocked() -> None:
    """一支在飞的都没有时，这一层对「航线满不满」没有任何证据，那就不猜。

    空手而归还有别的成因（这一圈没有海盗、目标都在保护期里）。单凭它就压着
    链路，等于把一条与航线无关的规则塞进航线判据，而且没有任何时刻可以解除。
    这一档照旧交给 `RESTART_COOLDOWN` 节流。
    """
    no_anchor = facts(
        per={PIRATE: {"free_lines": 3, "next_line_free_at_utc": None, **_EMPTY_ROUND}}
    )

    assert not waiting_for_a_line(PIRATE, no_anchor)
    assert has_work(PIRATE, no_anchor, restart_cooldown=RESTART_COOLDOWN)


def test_waiting_for_a_line_never_holds_back_report_collection() -> None:
    """只挡「去派」那半边判据。收报告不占航线，压着它只会让战报烂在信箱里。"""
    blocked = facts(
        per={
            PIRATE: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW + timedelta(minutes=3),
                "reports_due": True,
                **_EMPTY_ROUND,
            }
        }
    )

    assert waiting_for_a_line(PIRATE, blocked)
    assert has_work(PIRATE, blocked, restart_cooldown=RESTART_COOLDOWN)


def test_an_empty_pirate_round_does_not_hold_back_the_bot_chain() -> None:
    """空手而归按任务分。海盗那轮什么都没派出去，不该连累 bot。"""
    mixed = facts(
        per={
            PIRATE: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW + timedelta(minutes=3),
                **_EMPTY_ROUND,
            }
        }
    )

    assert waiting_for_a_line(PIRATE, mixed)
    assert not waiting_for_a_line(BOT, mixed)


def test_an_empty_round_on_one_planet_does_not_hold_back_the_other_planet() -> None:
    """**不同出发星球互不影响**，在「等航线」这半边同样成立。

    主星那个任务撞满了航线、正等着一条空出来；2 号星那个任务上一轮也空手而归，
    但那颗星球上一支在飞的都没有（`next_line_free_at_utc` 为 None），
    没有任何证据说它没位子——不许拿主星的返航时刻把它一起压住。
    """
    main = task(MissionKind.BOT, task_id=10, origin=HOME, fleet_lines=5)
    second = task(MissionKind.BOT, task_id=11, origin=SECOND, fleet_lines=2)
    mixed = SchedulerFacts(
        now_utc=NOW,
        per_task={
            main.task_id: TaskFacts(
                free_lines=1,
                targets_remaining=3,
                next_line_free_at_utc=NOW + timedelta(minutes=3),
                **_EMPTY_ROUND,  # type: ignore[arg-type]
            ),
            second.task_id: TaskFacts(
                free_lines=2,
                targets_remaining=3,
                next_line_free_at_utc=None,
                **_EMPTY_ROUND,  # type: ignore[arg-type]
            ),
        },
    )

    assert waiting_for_a_line(main, mixed)
    assert not waiting_for_a_line(second, mixed)
    assert has_work(second, mixed, restart_cooldown=RESTART_COOLDOWN)


def test_scanning_fills_the_gap_while_the_attack_chains_wait_for_a_line() -> None:
    """两条攻击链路都在等航线时，扫描顶上——那正是它存在的理由。

    这一条盯的是整轮里最贵的那件事：实机上那九轮不只是白跑，它们一直占着鼠标，
    扫描一次都挤不进来。
    """
    stuck = facts(
        per={
            PIRATE: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW + timedelta(minutes=3),
                **_EMPTY_ROUND,
            },
            BOT: {
                "free_lines": 3,
                "next_line_free_at_utc": NOW + timedelta(minutes=3),
                **_EMPTY_ROUND,
            },
        }
    )

    decision = decide(
        [PIRATE, BOT, SCAN],
        stuck,
        running=None,
        min_dwell=DWELL,
        restart_cooldown=RESTART_COOLDOWN,
    )

    assert decision == Decision(Action.START, SCAN)


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


# -- 环境故障：多个任务在同一时间窗里一起倒 ------------------------------------
#
# **实机 2026-08-12。** 01:55「BOT 已停用（连续 3 次异常退出，退出码 1）」，
# 04:37 三条**全部**已停用。BOT 从 01:55 停到 04:37，近三个小时一发没派。
# 三条链路共用一个游戏窗口、一个鼠标、一份连接和一台机器，同时坏掉几乎必然是
# 那些共用的东西坏了，而不是三处互不相干的代码在同一晚一起长出 bug。


def test_a_task_failing_by_itself_is_its_own_problem() -> None:
    """**豁免不能退化成「所有失败都不算失败」。**

    只有它一个在倒的时候，那就是它自己的毛病。这一条为假，自动停用整个失效，
    调度循环会在一个坏掉的任务上一轮轮空转。
    """
    assert not looks_like_an_environment_fault(PIRATE.task_id, NOW, {})


def test_two_tasks_failing_minutes_apart_read_as_one_environment_fault() -> None:
    """环境坏掉时几个任务是**接连**倒下的：起来就崩、崩完等一个冷却、再来。"""
    recent = {SCAN.task_id: NOW - timedelta(seconds=30)}

    assert looks_like_an_environment_fault(PIRATE.task_id, NOW, recent)


def test_failures_far_apart_are_two_separate_faults() -> None:
    """**这是「怎么区分」那道题的另一半。**

    隔了大半个钟头才轮到第二个，那不是同一阵故障——环境坏掉时每条链路
    五分钟就撞一次，不会等那么久。时间窗一放开，这条豁免就会开始吃掉真正的故障。

    ⚠️ 这里的 40 分钟**写死**，不许写成 `ENVIRONMENT_FAULT_WINDOW + 1 分钟`：
    那样的话时间窗改多大，这个时刻就跟着挪多远，用例永远绿——变异验证时正是
    这么发现的，把窗口放大到一整天它照样通过。
    """
    recent = {SCAN.task_id: NOW - timedelta(minutes=40)}

    assert not looks_like_an_environment_fault(PIRATE.task_id, NOW, recent)


def test_the_window_covers_a_burst_but_not_a_night() -> None:
    """窗口得比一次重启冷却宽、比一整夜窄，两头都会坏事。

    - **窄于 `RESTART_COOLDOWN`**：环境坏掉时第二条链路要等前一条的冷却过去才
      轮得到再崩一次，「接连倒下」根本落不进同一个窗口，豁免形同虚设。
    - **宽到按小时算**：一整夜里两处互不相干的真故障必然会挤进同一个窗口，
      于是自动停用被这条豁免整个吃掉。
    """
    assert RESTART_COOLDOWN < ENVIRONMENT_FAULT_WINDOW < timedelta(hours=1)


def test_a_task_repeating_its_own_crash_never_corroborates_itself() -> None:
    """同一个任务崩两次不构成「多个一起倒」。

    自己给自己作证的话，任何一个高频复发的真故障都会自动豁免掉，
    `MAX_CONSECUTIVE_FAILURES` 从此永远数不到。
    """
    recent = {PIRATE.task_id: NOW - timedelta(seconds=30)}

    assert not looks_like_an_environment_fault(PIRATE.task_id, NOW, recent)


def test_the_tasks_in_one_fault_include_the_one_that_just_failed() -> None:
    """调用方拿这一组去清计数：刚倒下的那个也记错了账，不能漏掉自己。"""
    recent = {SCAN.task_id: NOW - timedelta(seconds=30), BOT.task_id: NOW}

    together = tasks_failing_together(PIRATE.task_id, NOW, recent)

    assert together == {PIRATE.task_id, SCAN.task_id, BOT.task_id}


def test_a_record_from_the_future_is_ignored_rather_than_trusted() -> None:
    """出现比「现在」还晚的记录只能是时钟被调过。

    那时宁可少认一次环境故障，也不要凭一个说不清的差值去豁免一次真正的崩溃。

    ⚠️ 这里的 5 分钟**必须落在时间窗以内**：取一个窗口以外的未来时刻，判据写成
    `abs(at - moment) <= window` 也照样能通过——那样这条用例就只是在重测窗口宽度，
    根本没碰「未来」这件事。
    """
    recent = {SCAN.task_id: NOW + timedelta(minutes=5)}

    assert not looks_like_an_environment_fault(PIRATE.task_id, NOW, recent)
