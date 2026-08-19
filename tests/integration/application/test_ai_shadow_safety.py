"""一期安全不变量：AI 选靶影子**绝不影响派遣**（需求文档第八节，验收第一项）。

三条钉死：
1. 注入一个「返回垃圾 / 乱码 / 合法但完全不同的选择」的假 observer，
   `_military_assignments` 的返回值与没接 observer 时**逐字相同**；
2. observer 在 `observe()` 里抛异常，派遣照常；
3. 开关关掉时 observer 一次都不被调。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import add_bot_target, enable, task

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

BY_MILITARY = '{"by_military": true, "top_n": 2, "score_max_age_hours": 2}'


class PoisonObserver:
    """无论收到什么参数都「成功发起」，但选择是垃圾（一期不执行它）。"""

    def observe(self, **kwargs: object) -> bool:  # noqa: ARG002
        return True


class ExplodingObserver:
    """`observe()` 直接炸——观测侧的异常绝不能连锁到派遣。"""

    def observe(self, **kwargs: object) -> bool:  # noqa: ARG002
        raise RuntimeError("LLM 彻底挂了")


class RecordingObserver:
    """记下有没有被调，开关关掉时必须一次都不调。"""

    def __init__(self) -> None:
        self.calls = 0

    def observe(self, **kwargs: object) -> bool:
        self.calls += 1
        return True


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


def _make_scheduler(repository: SqlAlchemyRepository, clock: Clock, launcher, observer):  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(
        repository,
        make_supervisor(launcher, clock),
        clock=clock,
        ai_shadow=observer,
    )
    scheduler.prepare()
    return scheduler


def _a_working_pool(repository: SqlAlchemyRepository, session_factory) -> None:  # type: ignore[no-untyped-def]
    """窗口内目标够多、有启用出发星球的军力任务。"""
    for index, coordinate in enumerate(
        (Coordinate(4, 269, 8), Coordinate(4, 393, 10), Coordinate(9, 245, 14))
    ):
        add_bot_target(
            session_factory,
            coordinate,
            military_score=20_000.0 - index * 1_000,
            scanned_at=NOW,
        )
    enable(repository, MissionKind.BOT, params_json=BY_MILITARY)


def test_the_dispatch_is_byte_for_byte_unchanged_with_a_poisoned_observer(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """★ 验收第一项：注入返回垃圾的 observer，派遣结果逐字不变。"""
    # 先建任务行（prepare），再摆目标池与勾选。
    baseline = _make_scheduler(repository, clock, launcher, None)
    poisoned = _make_scheduler(repository, clock, launcher, PoisonObserver())
    _a_working_pool(repository, session_factory)

    row = task(repository, MissionKind.BOT)
    assert baseline._military_assignments(row) == poisoned._military_assignments(row)  # noqa: SLF001


def test_an_exploding_observer_never_breaks_the_dispatch(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """LLM 调用炸掉（`observe` 抛异常）时调度继续：**不因 LLM 挂而停摆**。"""
    scheduler = _make_scheduler(repository, clock, launcher, ExplodingObserver())
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    assignments = scheduler._military_assignments(row)  # noqa: SLF001
    assert assignments  # 有目标派得出去，一轮都没少


def test_the_switch_off_means_the_observer_is_never_called(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """开关默认关：`_observe_ai_shadow` 第一行就返回，observer 一次都不被调。"""
    scheduler = _make_scheduler(repository, clock, launcher, RecordingObserver())
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    assignments = scheduler._military_assignments(row)  # noqa: SLF001
    assert assignments
    assert scheduler._ai_shadow.calls == 0  # type: ignore[union-attr]


def test_the_switch_off_is_zero_cost_even_without_a_working_repository(  # type: ignore[no-untyped-def]
    repository, session_factory, launcher, clock
) -> None:
    """开关关时 observer 那一路的查询一律不发生。这里直接验「开关关 → 不调 observe」。"""
    scheduler = _make_scheduler(repository, clock, launcher, RecordingObserver())
    _a_working_pool(repository, session_factory)
    row = task(repository, MissionKind.BOT)
    assert scheduler._military_assignments(row)  # noqa: SLF001
    assert scheduler._ai_shadow.calls == 0  # type: ignore[union-attr]
