"""派遣记录必须带上「战报什么时候到」，以及记的是哪一类目标。

回程闹钟是助手松手之后唯一的依据（见 `domain.report_wait` 的模块头）。
不写它，等待调度器会把每一发都当成「立刻去收」，于是助手在战报还没产生时
反复登录——既白跑，又要和用户抢会话。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
from evo_helper.game import pirate_ui


class _RecordingRepository:
    """只记下写库这一层收到了什么。"""

    def __init__(self) -> None:
        self.saved_intent: Any | None = None
        self.saved_dispatch: object | None = None
        self.flight_calls: list[tuple[UUID, timedelta | None, datetime]] = []

    def save_attack_intent(self, intent: Any) -> None:
        self.saved_intent = intent

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


# -- 飞行时间写进库 ---------------------------------------------------------


def test_the_flight_time_from_the_briefing_reaches_the_database() -> None:
    repository = _RecordingRepository()
    loop = _loop_with(repository)

    loop._record_dispatch(uuid4(), timedelta(minutes=7))  # type: ignore[attr-defined]

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


# -- 记的是哪一类目标 -------------------------------------------------------


def _intent_target_kind(loop_class: Any, repository: _RecordingRepository) -> str:
    from evo_helper.tools.pirate_loop import LoopOptions

    loop = loop_class.__new__(loop_class)
    loop._repository = repository
    loop._run_id = uuid4()
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._record_intent(Coordinate(2, 137, 14), preset="BBB")
    assert repository.saved_intent is not None
    return str(repository.saved_intent.target_kind)


def test_a_bot_attack_is_written_to_the_database_as_a_bot_not_a_pirate() -> None:
    """光断言两个类属性的取值挡不住这个回归——要看真正写进 `AttackIntent` 的是哪个。

    `BotLoop` 走的是继承来的 `_record_intent`。那里一旦写回硬编码的
    `TARGET_KIND_PIRATE`，bot 的发数就吃掉海盗当日 32 次的配额（游戏硬限制），
    助手于是以为还有余额，多打的那一发被游戏强制返回。
    """
    from evo_helper.tools.bot_loop import BotLoop
    from evo_helper.tools.pirate_loop import PirateLoop

    assert _intent_target_kind(BotLoop, _RecordingRepository()) == TARGET_KIND_BOT
    assert _intent_target_kind(PirateLoop, _RecordingRepository()) == TARGET_KIND_PIRATE


# -- 飞行时间怎么读的 -------------------------------------------------------


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

    assert loop._read_flight_time() == timedelta(minutes=8, seconds=3)  # type: ignore[attr-defined]


def test_an_implausibly_long_flight_time_is_treated_as_a_misread() -> None:
    """`8分3秒` 被读成 `8时3分` 是 60 倍——而且不会报错，只会看起来像「在等」。

    这里只有时长这一个来源，没有绝对到达时间可以交叉验证
    （`DispatchBriefing.duration_agrees` 那道校验用不上），所以量级错只能靠上界拦。
    拦下来写 NULL，等待调度器据此改为「立即尝试收取」：白跑一趟，
    而不是让这条链路安静地停摆八小时。
    """
    loop = _loop_reading("8时3分")

    assert loop._read_flight_time() is None  # type: ignore[attr-defined]


def test_a_flight_time_in_days_is_never_believed() -> None:
    """`parse_game_duration` 认得 `X天…`，而这条链路打的是同系目标。"""
    loop = _loop_reading("42天17时34分58秒")

    assert loop._read_flight_time() is None  # type: ignore[attr-defined]


def test_the_ceiling_leaves_room_for_the_longest_briefing_ever_observed() -> None:
    """上界不能收得太紧，否则误杀合法的长途飞行。

    仓库里最长的实测简报是 `28分 21秒`（`tests/unit/vision/test_dispatch_briefing.py`），
    而那是一趟深空探索，比这条链路任何一发都远。上界要留足余量。
    """
    from evo_helper.tools.pirate_loop import MAX_CREDIBLE_FLIGHT

    assert MAX_CREDIBLE_FLIGHT >= timedelta(minutes=28, seconds=21) * 10


# -- 读的时机 ---------------------------------------------------------------


class _RecordingDriver:
    """按顺序记下每一次点击。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self._events.append(f"click:{label}")

    def wait(self, seconds: float) -> None:
        return None


class _FakeNavigator:
    def ensure_system_view(self, read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        return None


def _attackable_loop(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    repository: _RecordingRepository,
) -> Any:
    """一个能跑完 `attack()` 的 loop：驱动、导航、选预设全是假的。

    选预设那一步换掉是必须的——`PresetPicker` 会去 OCR 真的截图。
    这里要守的是派遣记录，不是预设条。
    """
    from evo_helper.tools import pirate_loop as module

    class _Picker:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def pick(self, name: str) -> None:
            return None

    monkeypatch.setattr(module, "PresetPicker", _Picker)

    def _read(roi: tuple[int, int, int, int], **_kwargs: Any) -> str:
        if roi == pirate_ui.BRIEFING_FLIGHT_ROI:
            events.append("read:飞行时间")
            return "8分3秒"
        if roi == pirate_ui.BRIEFING_MISSION_ROI:
            return "攻击"
        return ""

    loop = module.PirateLoop.__new__(module.PirateLoop)
    loop._driver = _RecordingDriver(events)
    loop._navigator = _FakeNavigator()
    loop._options = module.LoopOptions(systems=(), scout=False, attack=True)
    loop._outcome = module.Outcome()
    loop._repository = repository
    loop._run_id = uuid4()
    loop._read = _read
    return loop


def test_the_flight_time_is_read_before_the_launch_click(monkeypatch: pytest.MonkeyPatch) -> None:
    """简报页只在点「出发！」之前存在。

    把这一读挪到 `_launch` 之后，实机上四次重试全会落空（还白等三秒），
    飞行时间**永久恒为 NULL**——完全退回修复之前的状态，而且一声不响：
    调度器只会显示「在等」。所以这里守的是顺序，不是「读到了没有」。
    """
    events: list[str] = []
    loop = _attackable_loop(monkeypatch, events, _RecordingRepository())

    assert loop.attack(Coordinate(2, 137, 14), preset="BBB") is True

    assert "read:飞行时间" in events, "根本没读飞行时间"
    assert "click:出发" in events, "根本没点出发"
    assert events.index("read:飞行时间") < events.index("click:出发")


def test_the_flight_time_actually_reaches_the_repository_on_a_real_attack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """顺序对了还不够——读到的那个值要真的落到库里。"""
    repository = _RecordingRepository()
    loop = _attackable_loop(monkeypatch, [], repository)

    assert loop.attack(Coordinate(2, 137, 14), preset="BBB") is True

    assert [flight for _id, flight, _at in repository.flight_calls] == [
        timedelta(minutes=8, seconds=3)
    ]
