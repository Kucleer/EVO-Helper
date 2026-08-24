"""窗口内不够时，攻击让位给军力榜去补货。

用户口径（2026-08-24）：「轮到该星系 bot 攻击时，如果不足就去采集（现在的采集
效率很高）采集够了就开始攻击，而不是轮空星系」。

判据本体在 `domain.scheduler.yields_to_a_scan`，「补得进来吗」那半边的记账在
`application.mission_scheduler._scan_can_still_help`（那里才有相邻两趟的历史）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    MilitaryWindowPool,
    MissionKind,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    has_work,
    yields_to_a_scan,
)

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)


def _task(task_id: int = 1, kind: MissionKind = MissionKind.BOT) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id,
        kind=kind,
        name=f"任务{task_id}",
        enabled=True,
        priority=0,
        origin=Coordinate(galaxy=4, system=277, position=15),
        fleet_lines=2,
    )


def _facts(
    task: TaskSnapshot,
    *,
    in_window: int | None,
    floor: int = 100,
    can_help: bool = True,
    free_lines: int = 2,
    remaining: int = 50,
) -> SchedulerFacts:
    window = None if in_window is None else MilitaryWindowPool(in_window=in_window, floor=floor)
    return SchedulerFacts(
        now_utc=NOW,
        per_task={
            task.task_id: TaskFacts(
                free_lines=free_lines,
                targets_remaining=remaining,
                military_window=window,
                scan_can_still_help=can_help,
            )
        },
    )


# -- 判据本体 -------------------------------------------------------------------


def test_a_task_short_of_fresh_targets_yields_the_slice() -> None:
    """窗口内不够、而且补得进来 —— 这一跳让给军力榜。

    守的是这条口径要治的那件事：原先窗口不够时选靶只是「放弃窗口、改用旧读数」，
    BOT 照旧有活干、照旧占着前台，而军力榜是填空隙任务、结构性排在它后面，
    于是补货那一趟永远轮不到。生产实测（2026-08-24 08:26 → 08:32，连着四趟攻击）：
    本周期读数 **56 → 54 → 52 → 51 单调下降**，中间一趟扫描都没插进来。
    """
    task = _task()

    assert yields_to_a_scan(task, _facts(task, in_window=51, floor=100)) is True


def test_a_task_with_enough_in_window_does_not_yield() -> None:
    """够门限就照常打——让位只在缺货时发生，不是每一跳都让。"""
    task = _task()

    assert yields_to_a_scan(task, _facts(task, in_window=100, floor=100)) is False
    assert yields_to_a_scan(task, _facts(task, in_window=101, floor=100)) is False


def test_it_stops_yielding_once_the_scan_stops_helping() -> None:
    """⚠️⚠️ **补不进来了就不再让位** —— 这是唯一的防死锁闸。

    门限配得比榜上能采到的还高时（生产 2026-08-24 差点这样：门限 200，
    而本周期总共才采到 227 个），扫描每趟都跑、池子每趟都不涨。少了这一档，
    BOT 会**永远**让位、一发不打，而页面显示的是「没活干」——一句听起来正常、
    实际相反的话。这个仓反复在防的就是这种静默停摆。
    """
    task = _task()

    assert yields_to_a_scan(task, _facts(task, in_window=51, can_help=False)) is False


def test_a_task_without_a_military_pool_never_yields() -> None:
    """不是军力优先那条链路的任务没有这个池子，判据一律不成立。

    ⚠️ 交 `None` 而不是当成「不够用」：把「没有这个概念」判成「缺货」会让
    区域攻击那条链路也跟着让位，而它压根不看军力读数。
    """
    task = _task()

    assert yields_to_a_scan(task, _facts(task, in_window=None)) is False


def test_the_pool_is_read_per_task_not_account_wide() -> None:
    """⚠️ **按任务问，不是按账号问。**

    每个军力任务有自己的出发点，能打到的目标不一样。拿账号级那个「最饿的」去判，
    一个星系缺货会让**所有**星系一起让位——那些本来还有货的星系就白白轮空了，
    而「不许轮空星系」正是这条口径的原话。

    这一条构造的正是那个形状：账号级窗口报 0（另一个星系饿着），
    而这个任务自己窗口内有 120 个、够门限 100。
    """
    task = _task()
    facts = SchedulerFacts(
        now_utc=NOW,
        # 账号级：最饿的那个报 0 —— 扫描安全阀该据此放行，但让位判据不许读它。
        military_window=MilitaryWindowPool(in_window=0, floor=100),
        per_task={
            task.task_id: TaskFacts(
                free_lines=2,
                targets_remaining=50,
                military_window=MilitaryWindowPool(in_window=120, floor=100),
                scan_can_still_help=True,
            )
        },
    )

    assert yields_to_a_scan(task, facts) is False


# -- 接进 has_work ---------------------------------------------------------------


def test_has_work_says_no_while_the_task_is_yielding() -> None:
    """让位要真的表现成「没活干」，否则军力榜拿不到时间片。

    军力榜排在攻击之后靠的就是 `has_work`：`decide()` 取第一个「有活干」的任务，
    而填空隙的那几种结构性排在最后。所以让位唯一的兑现方式就是这里答 False。
    """
    task = _task()

    assert has_work(task, _facts(task, in_window=51, floor=100)) is False


def test_has_work_still_says_yes_when_the_scan_cannot_help() -> None:
    """补不进来时照旧打——「绝不停摆」这条底线由 `has_work` 这一路兜住。"""
    task = _task()

    assert has_work(task, _facts(task, in_window=51, can_help=False)) is True


def test_yielding_does_not_touch_the_gap_fillers() -> None:
    """军力榜自己不受这道判据影响，否则它会把自己让掉、谁都不跑。

    ⚠️ `has_work` 里填空隙那一支在 BOT 之前就 `return True` 了，所以这一条钉的是
    那个次序：哪天有人把让位判据挪到那一支之前，这条会红。
    """
    ranking = _task(task_id=2, kind=MissionKind.RANKING)
    facts = SchedulerFacts(
        now_utc=NOW,
        per_task={
            ranking.task_id: TaskFacts(
                military_window=MilitaryWindowPool(in_window=0, floor=100),
                scan_can_still_help=True,
            )
        },
    )

    assert has_work(ranking, facts) is True


def test_a_finished_round_still_wins_over_yielding() -> None:
    """本轮已经走完的任务答「没活干」，与让位无关——两条路殊途同归，但成因不同。

    钉的是次序：`bot_round_complete` 在让位判据之前。反过来的话，一个已经走完的
    任务会打出一句「让位补货」的日志，而它压根没有活干——日志说假话比不说更糟。
    """
    task = _task()
    facts = _facts(task, in_window=51, remaining=0)

    assert has_work(task, facts) is False
