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
from evo_helper.domain.records import (
    MISSION_KIND_ATTACK,
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
)
from evo_helper.game import pirate_ui

#: 本轮配的出发星球。派出之前的起点闸门要拿它跟派遣面板的回读比。
ORIGIN = Coordinate(2, 137, 18)


class _RecordingRepository:
    """只记下写库这一层收到了什么。"""

    def __init__(self) -> None:
        self.saved_intent: Any | None = None
        self.saved_dispatch: Any | None = None
        self.flight_calls: list[tuple[UUID, timedelta | None, datetime]] = []

    def save_attack_intent(self, intent: Any) -> None:
        self.saved_intent = intent

    def save_dispatch(self, dispatch: Any) -> None:
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


def test_every_recipe_is_tried_on_every_settle_round() -> None:
    """读不出来时要把**每一套配方 × 每一轮等待**都试满，而不是一次读不出就认输。

    这一行现在是两个钟的唯一来源（战报到点时刻 + 航线空出时刻）。读不出来那一发
    按 `UNKNOWN_LINE_HOLD`（90 分钟）占航线，而真实往返是 10–62 分钟——白压吞吐。
    所以把 NULL 压回去的手段是**多试几次**（方向不许反过来去放松解析判据：
    读出一个小而合理的错值会同时污染两个钟，比 NULL 贵得多）。
    """
    from evo_helper.tools.pirate_loop import FLIGHT_RECIPES, FLIGHT_SETTLE_TRIES

    loop = _loop_reading("读不出来的一行")
    seen: list[tuple[int, int | None]] = []
    loop._read = lambda *_args, **kwargs: (  # type: ignore[attr-defined]
        seen.append((kwargs.get("upscale"), kwargs.get("threshold"))),
        "读不出来的一行",
    )[1]
    loop._dump_frame = lambda name, roi=None: None  # type: ignore[attr-defined]

    assert loop._read_flight_time() is None  # type: ignore[attr-defined]
    assert seen == list(FLIGHT_RECIPES) * FLIGHT_SETTLE_TRIES


def test_a_totally_unreadable_flight_line_leaves_a_frame_behind() -> None:
    """四套配方全败就存一帧现场。

    ⚠️ 没有这张图，下次查「为什么这一发的飞行时间是 NULL」只能靠猜——判据收紧
    之后 NULL 变多了，而 NULL 与「ROI 框歪了」「面板还没铺开」在日志上长得一模一样。
    """
    loop = _loop_reading("读不出来的一行")
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[attr-defined]

    assert loop._read_flight_time() is None  # type: ignore[attr-defined]
    assert dumped == ["briefing-flight-unreadable"]


def test_a_readable_flight_line_leaves_no_frame_behind() -> None:
    """反过来：读得出来就不许存图。一轮几十发，存图会把 `var/logs/` 淹掉。"""
    loop = _loop_reading("8分3秒")
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[attr-defined]

    assert loop._read_flight_time() == timedelta(minutes=8, seconds=3)  # type: ignore[attr-defined]
    assert dumped == []


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
    loop._options = module.LoopOptions(systems=(), scout=False, attack=True, origin=ORIGIN)
    loop._outcome = module.Outcome()
    loop._repository = repository
    loop._run_id = uuid4()
    loop._read = _read
    # 派出之前的起点闸门读派遣面板「起点」那一行。让它读到本轮配的那颗，
    # 这几条用例守的（记录、顺序）才跑得完；闸门自己另有专文。
    loop._read_coord_line = lambda _roi, _upscale, _resample: str(ORIGIN)
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


def test_an_attack_is_recorded_as_an_attack_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """攻击发的 `mission_kind` 是 `ATTACK`——日配额数的就是这一档。"""
    repository = _RecordingRepository()
    loop = _attackable_loop(monkeypatch, [], repository)

    assert loop.attack(Coordinate(2, 137, 14), preset="BBB") is True

    assert repository.saved_dispatch is not None
    assert repository.saved_dispatch.mission_kind == MISSION_KIND_ATTACK


# -- 侦察发也要记账 ---------------------------------------------------------


def _scoutable_loop(
    events: list[str],
    repository: _RecordingRepository,
    *,
    briefing: str = "侦察",
    flight: str = "8分3秒",
) -> Any:
    """一个能跑完 `scout()` 的 loop。侦察不选预设，所以不用换掉 `PresetPicker`。

    读屏也记进 `events`，因为侦察这边要守的不只是「读到了没有」，还有**读的时机**：
    简报页只在点「出发！」之前存在。
    """
    from evo_helper.tools import pirate_loop as module

    def _read(roi: tuple[int, int, int, int], **_kwargs: Any) -> str:
        if roi == pirate_ui.BRIEFING_FLIGHT_ROI:
            events.append("read:飞行时间")
            return flight
        return briefing if roi == pirate_ui.BRIEFING_MISSION_ROI else ""

    loop = module.PirateLoop.__new__(module.PirateLoop)
    loop._driver = _RecordingDriver(events)
    loop._navigator = _FakeNavigator()
    loop._options = module.LoopOptions(systems=(), scout=True, attack=False, origin=ORIGIN)
    loop._outcome = module.Outcome()
    loop._repository = repository
    loop._run_id = uuid4()
    loop._read = _read
    # 侦察一样要过起点闸门（它一样占航线、一样按出发坐标记账）。让它读到本轮配的那颗。
    loop._read_coord_line = lambda _roi, _upscale, _resample: str(ORIGIN)
    # 闸门拦下时 `_launch` 会把现场存到 `var/logs/`。那是实机复盘用的，
    # 在单元测试里只会往仓库里丢 PNG。
    loop._dump_frame = lambda *_args, **_kwargs: None
    return loop


def test_a_scout_is_recorded_so_the_scheduler_can_see_the_line_it_holds() -> None:
    """**侦察占航线，所以它必须进库。**

    海盗一轮最多派 4 发侦察。一条记录都不写，这 4 条航线对调度器完全隐形：
    它以为航线空着就去派攻击，撞上「同时派遣的舰队数量已达上限。」。
    """
    repository = _RecordingRepository()
    loop = _scoutable_loop([], repository)

    assert loop.scout(Coordinate(2, 137, 14)) is True

    assert repository.saved_intent is not None
    assert repository.saved_dispatch is not None


def test_a_scout_is_recorded_as_a_scout_not_an_attack() -> None:
    """**这一条守的是当日 32 次配额。**

    配额查询只按 `target_kind` 过滤，而侦察也是打向海盗的。不区分发次的话，
    一轮 4 发侦察就吃掉 4 次攻击额度——额度以 4 倍速度消失，且完全静默。
    """
    repository = _RecordingRepository()
    loop = _scoutable_loop([], repository)

    assert loop.scout(Coordinate(2, 137, 14)) is True

    assert repository.saved_dispatch.mission_kind == MISSION_KIND_SCOUT


def test_a_scout_intent_is_written_even_when_the_briefing_gate_refuses() -> None:
    """闸门拦下的那发同样要出现在日志里——和 `attack()` 一个语义。

    意图在点「出发！」之前写，派遣在之后写；两者之差就是「想派但没派出去」。
    """
    repository = _RecordingRepository()
    loop = _scoutable_loop([], repository, briefing="攻击")

    assert loop.scout(Coordinate(2, 137, 14)) is False

    assert repository.saved_intent is not None
    assert repository.saved_dispatch is None


def test_recording_a_scout_does_not_move_any_click() -> None:
    """写库这件事不许改变点击顺序。

    `scout()` 里每一下点击、每一次等待都是实机事故换来的（派出之后停在
    「飞行中」列表上，不自己退出来，下一个目标的导航就会点到「取消任务」）。
    这条测试把顺序钉住：新增的只能是写库与**只读**的 OCR。
    """
    events: list[str] = []
    loop = _scoutable_loop(events, _RecordingRepository())

    assert loop.scout(Coordinate(2, 137, 14)) is True

    clicks = [event for event in events if event.startswith("click:")]
    assert clicks == ["click:侦察", "click:确认终点", "click:出发", "click:关闭面板"]


def test_a_scout_records_when_its_line_frees_up() -> None:
    """**记了账还得记对钟，否则等于没记。**

    `line_free_at_utc` 为 NULL 的派遣按既定语义**不计入在飞数**。所以侦察光写
    intent + dispatch 是不够的：不读飞行时长，那 4 条航线对调度器仍然完全隐形，
    「以为航线空着就去派攻击、撞上『同时派遣的舰队数量已达上限。』」这个
    原始症状原封不动。
    """
    repository = _RecordingRepository()
    loop = _scoutable_loop([], repository)

    assert loop.scout(Coordinate(2, 137, 14)) is True

    assert [flight for _id, flight, _at in repository.flight_calls] == [
        timedelta(minutes=8, seconds=3)
    ]


def test_the_scout_flight_time_is_read_before_the_launch_click() -> None:
    """和 `attack()` 那边同形：简报页只在点「出发！」之前存在。

    挪到 `_launch` 之后，四次重试全会落空（还白等三秒），`line_free_at_utc`
    永久恒为 NULL——一声不响地退回改动之前。这里守的是顺序，不是「读到了没有」。
    """
    events: list[str] = []
    loop = _scoutable_loop(events, _RecordingRepository())

    assert loop.scout(Coordinate(2, 137, 14)) is True

    assert "read:飞行时间" in events, "根本没读飞行时间"
    assert "click:出发" in events, "根本没点出发"
    assert events.index("read:飞行时间") < events.index("click:出发")


def test_an_unreadable_flight_time_never_holds_back_a_scout() -> None:
    """飞行时间是闹钟，不是闸门。

    读不到就照派、写 NULL——那等于退回改动之前的行为，不会更糟。加一道闸门则是
    让一次 OCR 抖动杀掉一发健康的派遣：这条链路已经因为「ROI 与放大倍数不配」
    白白拦下过四发攻击。
    """
    repository = _RecordingRepository()
    loop = _scoutable_loop([], repository, flight="")

    assert loop.scout(Coordinate(2, 137, 14)) is True

    assert repository.saved_dispatch is not None
    assert [flight for _id, flight, _at in repository.flight_calls] == [None]
