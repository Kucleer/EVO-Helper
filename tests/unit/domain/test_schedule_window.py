"""任务的定时开启 / 定时关闭：一道**与现有判据取交集**的时间闸门。

用户口径（2026-08-17）：每个任务可以设一个开启时刻和一个关闭时刻，到点自动生效。
绝对时刻、一次性，不是每天循环。

这一整个文件守的是四件事，每一件都对应一种「改坏了也全绿」的实现：

1. **不去写 `enabled`。** 定时器和复选框是「与」，谁都不覆盖谁。
2. **到点不抢停。** 这道门只挡「开新的一轮」。
3. **边界是左闭右开**：到点即可起，到点即不再起。
4. **两端都为空 = 行为与没有这项功能时完全一致。**
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    Action,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    after_schedule_window,
    before_schedule_window,
    decide,
    has_work,
    status_of,
    within_schedule_window,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DWELL = timedelta(seconds=60)
HOME = Coordinate(2, 137, 18)

#: 一个「什么都不挡」的任务事实：有一条空闲航线，本轮还剩目标。
#: 定时窗口之外的每一道闸门都是敞开的，所以任何一处不动都只能是窗口造成的。
_BASE = TaskFacts(free_lines=1, targets_remaining=5)


def task(kind: MissionKind, *, task_id: int = 1, **overrides: Any) -> TaskSnapshot:
    base = TaskSnapshot(
        task_id=task_id,
        kind=kind,
        name="",
        enabled=True,
        priority=0,
        origin=HOME,
        fleet_lines=6,
    )
    return replace(base, **overrides)


def facts(
    *tasks: TaskSnapshot, now: datetime = NOW, per: Mapping[int, TaskFacts] | None = None
) -> SchedulerFacts:
    per_task = {item.task_id: _BASE for item in tasks}
    per_task.update(per or {})
    return SchedulerFacts(now_utc=now, per_task=per_task)


# -- 两端都为空：行为一个字都不变 ----------------------------------------------


@pytest.mark.parametrize("kind", list(MissionKind))
def test_a_task_without_a_window_behaves_exactly_as_before(kind: MissionKind) -> None:
    """没配窗口的任务恒在窗口里。

    ⚠️ 这一条覆盖**每一种** `MissionKind`：新加一种链路而漏了它的话，那种任务会
    在没有任何配置的情况下突然停止调度，而页面上只会说「未到开启时间」——
    一句用户既没配过也无从修改的话。
    """
    plain = task(kind)

    assert within_schedule_window(plain, NOW) is True
    assert before_schedule_window(plain, NOW) is False
    assert after_schedule_window(plain, NOW) is False
    assert has_work(plain, facts(plain)) is True


# -- 边界：左闭右开 ------------------------------------------------------------
#
# 到点即可起（`now >= from`），到点即不再起（`now >= until` 就算已过）。
# 两侧合起来使相邻的两段窗口首尾相接，既不重叠也不留缝。


def test_the_moment_the_start_time_arrives_the_task_may_run() -> None:
    """`now == from` 算**在**窗口里。

    把开启侧写成 `now > from` 的话，正好落在开启时刻上的那一 tick 还起不来。
    单看是「晚一秒」，但调度器每秒一 tick、而用户填的是整分钟——真正的后果是
    「说好 22:00 开，22:00 那一下没动」，而页面还在说「未到开启时间」。
    """
    starts_now = task(MissionKind.BOT, enabled_from_utc=NOW)

    assert before_schedule_window(starts_now, NOW - timedelta(seconds=1)) is True
    assert before_schedule_window(starts_now, NOW) is False
    assert has_work(starts_now, facts(starts_now, now=NOW)) is True


def test_the_moment_the_stop_time_arrives_no_new_round_starts() -> None:
    """`now == until` 算**已过**，不再开新的一轮。

    ⚠️ 这里三个时刻一起断言，钉的正是那个差一位的边界：把判据从 `now >= until`
    放宽成 `now > until`，中间那一句会转红。放宽的实际后果是「14:00 关闭」在
    14:00 整那一 tick 还会再放一轮舰队出去——而用户填 14:00 的意思是那一刻起
    不再开新的，不是「14:00 那一秒再补一发」。
    """
    stops_now = task(MissionKind.BOT, enabled_until_utc=NOW)

    assert after_schedule_window(stops_now, NOW - timedelta(seconds=1)) is False
    assert after_schedule_window(stops_now, NOW) is True
    assert after_schedule_window(stops_now, NOW + timedelta(seconds=1)) is True


def test_a_task_inside_its_window_has_work_and_one_outside_does_not() -> None:
    """整段窗口走一遍：之前不动、之中动、之后不动。"""
    windowed = task(
        MissionKind.BOT,
        enabled_from_utc=NOW,
        enabled_until_utc=NOW + timedelta(hours=2),
    )

    assert has_work(windowed, facts(windowed, now=NOW - timedelta(minutes=1))) is False
    assert has_work(windowed, facts(windowed, now=NOW + timedelta(hours=1))) is True
    assert has_work(windowed, facts(windowed, now=NOW + timedelta(hours=2))) is False


# -- 与 `enabled` 取交集，不覆盖它 ---------------------------------------------


def test_an_open_window_does_not_turn_a_task_the_user_switched_off_back_on() -> None:
    """复选框勾掉的任务，窗口开着也不跑。

    这是「与」的一半。窗口若能把它拉起来，用户手动关掉的任务会在到点时自己
    复活——而用户关掉它的那一下就是「我不想让它跑」。
    """
    switched_off = task(
        MissionKind.BOT,
        enabled=False,
        enabled_from_utc=NOW - timedelta(hours=1),
        enabled_until_utc=NOW + timedelta(hours=1),
    )

    decision = decide([switched_off], facts(switched_off), running=None, min_dwell=DWELL)

    assert within_schedule_window(switched_off, NOW) is True
    assert decision.action is Action.IDLE
    assert decision.task is None


def test_the_window_never_rewrites_the_enabled_flag() -> None:
    """窗口关上之后，`enabled` 仍然是用户设的那个值。

    ⚠️ 这一条钉的是**列的取值**，不是行为。把实现改成「到点就把 `enabled` 写成
    False」的话，行为上看起来一模一样（任务同样不跑），但用户的意志被悄悄改掉了，
    而且事后翻库分不清那一下是谁关的——用户关的和定时器关的在列上长得一样。
    快照是 frozen 的，判据这一层结构上就写不动它；这里把那份保证写成一条会红的
    断言，好让「换个地方去写它」也逃不掉。
    """
    closed = task(
        MissionKind.BOT,
        enabled=True,
        enabled_until_utc=NOW - timedelta(minutes=1),
    )
    later = facts(closed, now=NOW)

    assert has_work(closed, later) is False
    assert closed.enabled is True
    assert status_of(closed, later, running=None).value == "已过关闭时间"


# -- 填空隙的那几种同样受管 ----------------------------------------------------


@pytest.mark.parametrize("kind", [MissionKind.SCAN, MissionKind.RANKING])
def test_gap_fillers_obey_the_window_too(kind: MissionKind) -> None:
    """扫描 / 军力榜跟着停（用户口径 2026-08-17）。

    这两种在 `has_work` 里有一条「恒有活干」的早退——窗口判定必须在它**之前**，
    否则填空隙的那几种会成为唯一不受定时管的任务，而它们恰恰是那种「一直有活干、
    因此一直占着鼠标」的链路。
    """
    filler = task(kind, task_id=3, enabled_until_utc=NOW - timedelta(minutes=1))

    assert has_work(filler, facts(filler)) is False


# -- 到点不抢停 ----------------------------------------------------------------


def test_a_running_task_is_not_cut_off_when_its_window_closes() -> None:
    """关闭时刻到了，正在跑的那一轮**不打断**（用户口径 2026-08-17）。

    中途抢停会留下半截状态（runner 可能正停在派遣面板上），而且已经派出去的
    舰队本来也停不了。

    ⚠️ 这里正在跑的是 `SCAN`——**它是唯一可被抢占的那一种**。拿一个本来就抢不动的
    任务来测，等于什么都没测：把实现改成「到点抢停」也照样绿。
    """
    scan = task(MissionKind.SCAN, task_id=3, enabled_until_utc=NOW - timedelta(minutes=1))
    running = RunningProcess(
        task_id=scan.task_id,
        kind=scan.kind,
        started_at_utc=NOW - timedelta(minutes=30),
    )

    decision = decide([scan], facts(scan), running=running, min_dwell=DWELL)

    assert decision.action is Action.IDLE
    assert decision.task is None


def test_a_closed_window_does_not_let_someone_else_preempt_the_running_scan() -> None:
    """窗口关掉不会凭空给别人制造出「值得抢占」的理由。

    这一条和上一条分开写：上一条只有一个任务，就算实现真的去抢停，也可能被
    「没有别人可换」掩盖过去。这里另放一个有活干的 bot 在场——`decide` 的抢占那一路
    完全够得着，唯一挡住它的是「扫描自己还在跑」这条不变量。
    """
    scan = task(MissionKind.SCAN, task_id=3, enabled_until_utc=NOW - timedelta(minutes=1))
    bot = task(MissionKind.BOT, task_id=2, enabled_until_utc=NOW - timedelta(minutes=1))
    running = RunningProcess(
        task_id=scan.task_id,
        kind=scan.kind,
        started_at_utc=NOW - timedelta(minutes=30),
    )

    decision = decide([bot, scan], facts(bot, scan), running=running, min_dwell=DWELL)

    assert decision.action is Action.IDLE


def test_a_running_task_still_reads_as_running_after_its_window_closed() -> None:
    """页面上它照样是「运行中」。

    不抢停就意味着它真的还在跑；这时显示「已过关闭时间」是句谎话，用户会以为
    已经停了，然后在真的还有 runner 在点鼠标的时候去动别的东西。
    """
    scan = task(MissionKind.SCAN, task_id=3, enabled_until_utc=NOW - timedelta(minutes=1))
    running = RunningProcess(
        task_id=scan.task_id, kind=scan.kind, started_at_utc=NOW - timedelta(minutes=30)
    )

    assert status_of(scan, facts(scan), running=running).value == "运行中"


# -- 状态要说出原因 ------------------------------------------------------------
#
# 2026-08-16 晚上刚发生过「任务不动而界面不说原因、查了一小时」的事。


def test_a_task_waiting_for_its_start_time_says_so_instead_of_ready() -> None:
    """未到开启时间**不能**显示成「待命」。

    「待命」的含义是「有活干、只是还没轮到它」。一个到点才会动的任务显示待命，
    用户会一直等下一轮——而下一轮永远不来。
    """
    pending = task(MissionKind.BOT, enabled_from_utc=NOW + timedelta(hours=1))

    assert status_of(pending, facts(pending), running=None).value == "未到开启时间"


def test_a_task_past_its_stop_time_says_so_instead_of_waiting_for_lines() -> None:
    """已过关闭时间不能显示成「等航线」之类会自己好起来的原因。

    「等航线」是一句用户照着去调航线数、调完也不会有任何变化的话。
    """
    expired = task(MissionKind.BOT, enabled_until_utc=NOW - timedelta(hours=1))

    assert status_of(expired, facts(expired), running=None).value == "已过关闭时间"


def test_the_window_reason_outranks_the_other_reasons_for_standing_still() -> None:
    """窗口之外，别的原因成不成立都不再重要。

    这里同时把配额也堵死。说「配额用尽」不算错，但它是一句会误导人的实话：
    用户会等到 UTC 00:00 配额重置，然后看着它照样不动。
    """
    pirate = task(MissionKind.PIRATE, enabled_from_utc=NOW + timedelta(hours=1))
    exhausted = SchedulerFacts(
        now_utc=NOW,
        pirate_dispatches_today=32,
        pirate_quota=32,
        per_task={pirate.task_id: _BASE},
    )

    assert status_of(pirate, exhausted, running=None).value == "未到开启时间"


def test_the_user_switching_a_task_off_still_outranks_the_window_reason() -> None:
    """复选框没勾时仍然显示「未启用」。

    次序上「没勾」在窗口之前：那是用户自己按下的那一下，比一个时刻更值得说。
    反过来的话，一个被手动关掉的任务会显示「未到开启时间」，读起来像是「到点
    它就会动」——而它不会。
    """
    off = task(MissionKind.BOT, enabled=False, enabled_from_utc=NOW + timedelta(hours=1))

    assert status_of(off, facts(off), running=None).value == "未启用"
