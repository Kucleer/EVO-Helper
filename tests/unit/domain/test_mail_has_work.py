"""信箱回读什么时候算「有活干」，以及谁能被抢占。

方案 `docs/邮件读取优化/方案.md` §3.3 / §3.4 / §3.5。

⚠️ 这里守的三条都是**评审里被推翻过一次**的：

1. **首跑放行** —— 初版写「没有记录时不成立」，而新任务永远产生不了第一条记录，
   于是它永远起不来；同时回收路由可能已经切到空闲趟，**两边都不读回收报告**。
2. **按结束算不按开始算** —— 按开始算时，一趟 ≥ 冷却时长的回读结束那一刻就又到期，
   它永远排最前，军力榜到期了也永远轮不上。回收预算开着时一趟约 6 分钟，不是边角。
3. **空档够长才起** —— 「此刻派不出去」不等于「接下来几分钟都派不出去」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    GAP_FILLERS,
    MAIL_MIN_WINDOW,
    MissionKind,
    SchedulerFacts,
    TaskFacts,
    TaskSnapshot,
    mail_has_work,
    preemptible,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _task(*, cooldown_minutes: int | None = 5) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=1,
        kind=MissionKind.MAIL,
        name="信箱回读",
        enabled=True,
        priority=1,
        origin=Coordinate(4, 277, 15),
        fleet_lines=None,
        mail_idle_cooldown=(
            None if cooldown_minutes is None else timedelta(minutes=cooldown_minutes)
        ),
    )


def _facts(*, last_ended: datetime | None, free_at: datetime | None = None) -> SchedulerFacts:
    return SchedulerFacts(
        now_utc=NOW,
        earliest_line_free_at_utc=free_at,
        per_task={1: TaskFacts(last_ended_at_utc=last_ended)},
    )


class TestTheFirstRun:
    def test_no_record_at_all_means_go(self) -> None:
        """⚠️ **首跑放行。** 没有结束记录 = 从没跑过，不是「刚跑完」。

        写成「没有记录就不成立」的话，新任务永远产生不了第一条记录 ——
        它就再也起不来了，而回收路由这时可能已经切到空闲趟，两边都不读。
        """
        assert mail_has_work(_task(), _facts(last_ended=None)) is True


class TestTheCooldownRunsFromTheEnd:
    @pytest.mark.parametrize(
        ("minutes_ago", "expected"),
        [(4, False), (5, True), (30, True)],
    )
    def test_it_measures_from_the_last_end(self, minutes_ago: int, expected: bool) -> None:
        """边界取「到点就放行」（≥），与别处的冷却同向。"""
        ended = NOW - timedelta(minutes=minutes_ago)

        assert mail_has_work(_task(cooldown_minutes=5), _facts(last_ended=ended)) is expected

    def test_zero_cooldown_means_every_time(self) -> None:
        """⚠️ 冷却填 `0` 是**合法且有用**的：每次轮到就翻。

        它和「关掉这条链路」不是一回事 —— 关掉用任务上的复选框。
        """
        just_now = NOW - timedelta(seconds=1)

        assert mail_has_work(_task(cooldown_minutes=0), _facts(last_ended=just_now)) is True


class TestTheWindowMustBeLongEnough:
    def test_a_line_freeing_up_soon_blocks_the_trip(self) -> None:
        """⚠️ **这一条防的是回归，不是优化。**

        某条线 1 分钟后就空时起一趟，进箱 31 s 后就会被截止或被抢占，
        而抢占要等 `min_dwell` 60 s —— 攻击白等最多 60 s，
        **而今天这种时刻鼠标是空的、攻击零等待**。
        """
        soon = NOW + timedelta(minutes=1)

        assert mail_has_work(_task(), _facts(last_ended=None, free_at=soon)) is False

    def test_a_long_enough_window_goes(self) -> None:
        assert (
            mail_has_work(_task(), _facts(last_ended=None, free_at=NOW + MAIL_MIN_WINDOW)) is True
        )

    def test_an_unknown_free_time_leans_towards_going(self) -> None:
        """⚠️ 算不出最早放线时刻就**放行**。

        `None` 的含义是「算不出」（全场只剩读不到飞行时长的派遣），不是
        「没有线会空」—— 那时压着不读没有依据，而运行器里的截止判据仍然兜着。
        """
        assert mail_has_work(_task(), _facts(last_ended=None, free_at=None)) is True


class TestWhoCanBePreempted:
    def test_mail_and_scan_can_be_stopped(self) -> None:
        """两者的共同点是**随起随停没有代价**：扫描游标持久化，回读每封独立入库。"""
        assert preemptible(MissionKind.SCAN)
        assert preemptible(MissionKind.MAIL)

    def test_ranking_cannot(self) -> None:
        """⚠️ 军力榜也填空隙，**但它一趟 6 分钟不可续** —— 断了整趟白跑。

        所以「可被抢占」不等于「填空隙」，两个集合必须分开。
        """
        assert MissionKind.RANKING in GAP_FILLERS
        assert not preemptible(MissionKind.RANKING)

    def test_the_attack_chains_cannot(self) -> None:
        """攻击轮按设计不许被打断（可能正停在派遣面板上）。"""
        assert not preemptible(MissionKind.BOT)
        assert not preemptible(MissionKind.PIRATE)
