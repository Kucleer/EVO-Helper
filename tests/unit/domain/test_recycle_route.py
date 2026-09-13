"""回收报告归谁读：空闲回读那一趟，还是攻击轮开工兜底趟。

方案 `docs/邮件读取优化/方案.md` §3.6。

⚠️⚠️ 这条路由是整个方案的**直接动机**：把读回收报告挪出派遣前面。
而它在评审里被推翻过两次，两次都是「有一种状态下两边都不读」：

1. 初版写「空闲回读停用时开工趟才读」，漏了**首跑**：任务刚启用、还没翻完过，
   路由已经切到空闲趟，而空闲趟这时还没跑起来 —— 回收报告没人读。
2. 第二版漏了**时效**：攻击持续有活的时段空闲回读一直拿不到窗口，
   路由却一直指着它 —— 回收报告几小时没人读。

所以这里的四态必须**逐个钉住**。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import (
    RECYCLE_IDLE_STALE,
    RECYCLE_ROUTE_IDLE,
    RECYCLE_ROUTE_ROUND,
    MissionKind,
    TaskSnapshot,
    recycle_route,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _mail(
    *,
    enabled: bool = True,
    disabled_reason: str | None = None,
    window: tuple[datetime, datetime] | None = None,
) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=1,
        kind=MissionKind.MAIL,
        name="信箱回读",
        enabled=enabled,
        priority=1,
        origin=Coordinate(4, 277, 15),
        fleet_lines=None,
        disabled_reason=disabled_reason,
        enabled_from_utc=None if window is None else window[0],
        enabled_until_utc=None if window is None else window[1],
    )


def _route(tasks: list[TaskSnapshot], *, completed_at: datetime | None) -> str:
    return recycle_route(tasks, now_utc=NOW, last_mail_completed_at=completed_at)


class TestTheFourStates:
    def test_a_live_task_that_swept_recently_takes_it(self) -> None:
        """唯一交给空闲趟的那一态：任务活着 **且** 最近真翻完过。"""
        assert _route([_mail()], completed_at=NOW - timedelta(minutes=5)) == RECYCLE_ROUTE_IDLE

    def test_a_disabled_task_leaves_it_with_the_round(self) -> None:
        """⚠️ 用户已经把回收旋钮开着、而信箱回读还停用时，**必须回到今天的行为**。

        这一条是「停用时行为与今天相同」那句承诺的落点。
        """
        assert _route([_mail(enabled=False)], completed_at=NOW) == RECYCLE_ROUTE_ROUND

    def test_an_auto_disabled_task_leaves_it_with_the_round(self) -> None:
        """被自动停用（连续失败）也算不可运行 —— 它不会自己回来。"""
        assert (
            _route([_mail(disabled_reason="连续失败 3 次")], completed_at=NOW)
            == RECYCLE_ROUTE_ROUND
        )

    def test_a_task_that_never_swept_leaves_it_with_the_round(self) -> None:
        """⚠️ **首跑那一态。** 刚启用、还没翻完过 ⇒ 兜底趟继续读。

        写成「任务活着就交给它」的话，从启用那一刻到它第一次翻完之间，
        **两边都不读回收报告**。
        """
        assert _route([_mail()], completed_at=None) == RECYCLE_ROUTE_ROUND

    def test_a_stale_sweep_leaves_it_with_the_round(self) -> None:
        """⚠️ **时效那一态。** 攻击持续有活时空闲回读拿不到窗口，超时就交还兜底。

        没有它，满载时段回收报告会几小时没人读 —— 而承诺是「最坏晚约一小时」。
        """
        stale = NOW - RECYCLE_IDLE_STALE - timedelta(seconds=1)

        assert _route([_mail()], completed_at=stale) == RECYCLE_ROUTE_ROUND

    def test_the_boundary_is_inclusive(self) -> None:
        """正好卡在时效上仍算活着（与别处的冷却同向，区间取闭）。"""
        edge = NOW - RECYCLE_IDLE_STALE

        assert _route([_mail()], completed_at=edge) == RECYCLE_ROUTE_IDLE


class TestTheWindowIsOneOfThePrerequisites:
    def test_outside_its_schedule_window_it_does_not_take_it(self) -> None:
        past = (NOW - timedelta(hours=3), NOW - timedelta(hours=1))

        assert _route([_mail(window=past)], completed_at=NOW) == RECYCLE_ROUTE_ROUND


class TestNoTaskAtAll:
    def test_an_empty_table_leaves_it_with_the_round(self) -> None:
        """库里根本没有这一行（老库、还没种出来）⇒ 今天的行为。"""
        assert _route([], completed_at=None) == RECYCLE_ROUTE_ROUND


class TestWhatItDeliberatelyDoesNotLookAt:
    @pytest.mark.parametrize("attr", ["mail_idle_cooldown"])
    def test_the_cooldown_is_not_part_of_the_route(self, attr: str) -> None:
        """⚠️⚠️ **路由不看冷却，也不看空档长度。**

        那两条问的是「这一刻该不该起一趟」，而路由问的是「读回收这件事**归谁**」。
        把冷却算进来的后果：信箱回读每次处于冷却里，回收读信就被塞回派遣前面一次
        —— 而把它挪出派遣前面正是这个方案的全部目的。

        这里用源码断言而不是行为断言，因为「没看」本身没有可观察的行为差异。
        """
        import inspect

        source = inspect.getsource(recycle_route)

        assert attr not in source, (
            f"路由里出现了 {attr}：它是「该不该起一趟」的判据，不是「归谁读」的判据。"
        )
        assert "earliest_line_free_at" not in source
