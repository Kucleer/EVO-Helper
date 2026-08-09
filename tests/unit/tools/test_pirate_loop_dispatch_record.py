"""派遣记录必须带上「战报什么时候到」。

这一列是助手松手之后唯一的回程闹钟（见 `domain.report_wait` 的模块头）。
不写它，等待调度器会把每一发都当成「立刻去收」，于是助手在战报还没产生时
反复登录——既白跑，又要和用户抢会话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4


class _RecordingRepository:
    """只记下 `record_flight_time` 收到了什么。"""

    def __init__(self) -> None:
        self.saved_dispatch: object | None = None
        self.flight_calls: list[tuple[UUID, timedelta | None, datetime]] = []

    def save_dispatch(self, dispatch: object) -> None:
        self.saved_dispatch = dispatch

    def record_flight_time(
        self, dispatch_id: UUID, flight: timedelta | None, dispatched_at_utc: datetime
    ) -> None:
        self.flight_calls.append((dispatch_id, flight, dispatched_at_utc))


def _loop_with(repository: _RecordingRepository) -> object:
    from evo_helper.tools.pirate_loop import PirateLoop

    loop = PirateLoop.__new__(PirateLoop)  # 不跑 __init__：这里只测记录这一段
    loop._repository = repository  # type: ignore[attr-defined]
    loop._run_id = uuid4()  # type: ignore[attr-defined]
    return loop


def test_the_flight_time_from_the_briefing_reaches_the_database() -> None:
    from evo_helper.vision.parsers import DispatchBriefing, MissionType

    repository = _RecordingRepository()
    loop = _loop_with(repository)
    now = datetime.now(UTC)
    briefing = DispatchBriefing(
        mission_type=MissionType.ATTACK,
        flight=timedelta(minutes=7),
        arrival_at_utc=now + timedelta(minutes=7),
    )

    loop._record_dispatch(uuid4(), briefing)  # type: ignore[attr-defined]

    assert len(repository.flight_calls) == 1
    _dispatch_id, flight, _dispatched = repository.flight_calls[0]
    assert flight == timedelta(minutes=7)


def test_an_unreadable_briefing_still_records_the_dispatch_with_no_flight_time() -> None:
    """读不到简报不能吞掉派遣记录——那一发是真派出去了。

    飞行时间写 NULL，等待调度器据此改为「立即尝试收取」，
    而不是无限等一个不知道何时抵达的战报。
    """
    repository = _RecordingRepository()
    loop = _loop_with(repository)

    loop._record_dispatch(uuid4(), None)  # type: ignore[attr-defined]

    assert repository.saved_dispatch is not None
    assert len(repository.flight_calls) == 1
    _dispatch_id, flight, _dispatched = repository.flight_calls[0]
    assert flight is None


class _FakeDriver:
    """`_settle` 判据不成立时会 `wait` 一下。测试里不真的睡。"""

    def wait(self, seconds: float) -> None:
        return None


def _loop_reading(text: str) -> object:
    """造一个「简报上的飞行时间 ROI 读作 text」的 loop。"""
    from evo_helper.tools.pirate_loop import PirateLoop

    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _FakeDriver()  # type: ignore[attr-defined]
    loop._read = lambda *_args, **_kwargs: text  # type: ignore[attr-defined]
    return loop


def test_a_credible_flight_time_becomes_the_return_alarm() -> None:
    loop = _loop_reading("8分3秒")

    briefing = loop._read_briefing()  # type: ignore[attr-defined]

    assert briefing is not None
    assert briefing.flight == timedelta(minutes=8, seconds=3)


def test_an_implausibly_long_flight_time_is_treated_as_a_misread() -> None:
    """`8分3秒` 被读成 `8时3分` 是 60 倍——而且不会报错，只会看起来像「在等」。

    这里只有时长这一个来源，没有绝对到达时间可以交叉验证
    （`DispatchBriefing.duration_agrees` 那道校验用不上），所以量级错只能靠上界拦。
    拦下来写 NULL，等待调度器据此改为「立即尝试收取」：白跑一趟，
    而不是让这条链路安静地停摆八小时。
    """
    loop = _loop_reading("8时3分")

    assert loop._read_briefing() is None  # type: ignore[attr-defined]


def test_a_flight_time_in_days_is_never_believed() -> None:
    """`parse_game_duration` 认得 `X天…`，而这条链路打的是同系目标。"""
    loop = _loop_reading("42天17时34分58秒")

    assert loop._read_briefing() is None  # type: ignore[attr-defined]


def test_the_ceiling_leaves_room_for_the_longest_briefing_ever_observed() -> None:
    """上界不能收得太紧，否则误杀合法的长途飞行。

    仓库里最长的实测简报是 `28分 21秒`（`tests/unit/vision/test_dispatch_briefing.py`），
    而那是一趟深空探索，比这条链路任何一发都远。上界要留足余量。
    """
    from evo_helper.tools.pirate_loop import MAX_CREDIBLE_FLIGHT

    assert MAX_CREDIBLE_FLIGHT >= timedelta(minutes=28, seconds=21) * 10
