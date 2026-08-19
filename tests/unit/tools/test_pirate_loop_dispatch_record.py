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
        self.flight_sources: list[Any] = []
        self.flight_speeds: list[str | None] = []
        #: 学出来的系数。默认 None = 这颗星球还没学出来，公式那一路弃权。
        self.coefficient: Any = None
        self.coefficient_calls: list[tuple[Any, str, str | None]] = []

    def save_attack_intent(self, intent: Any) -> None:
        self.saved_intent = intent

    def save_dispatch(self, dispatch: Any) -> None:
        self.saved_dispatch = dispatch

    def flight_coefficient(self, *, origin: Any, mission_kind: str, fleet_speed: str | None) -> Any:
        self.coefficient_calls.append((origin, mission_kind, fleet_speed))
        return self.coefficient

    def record_flight_time(
        self,
        dispatch_id: UUID,
        flight: timedelta | None,
        dispatched_at_utc: datetime,
        *,
        source: Any = None,
        fleet_speed: str | None = None,
    ) -> None:
        self.flight_calls.append((dispatch_id, flight, dispatched_at_utc))
        self.flight_sources.append(source)
        self.flight_speeds.append(fleet_speed)


def _measured(flight: timedelta | None) -> Any:
    """把一个时长包成「从简报页读出来的」结论，给只关心记账那几条用。"""
    from evo_helper.domain.flight_estimate import FlightEstimate, FlightSource

    return FlightEstimate(
        flight=flight,
        source=None if flight is None else FlightSource.BRIEFING_ARRIVAL,
        reason="用例",
    )


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

    loop._record_dispatch(uuid4(), _measured(timedelta(minutes=7)))  # type: ignore[attr-defined]

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

    loop._record_dispatch(uuid4(), _measured(None))  # type: ignore[attr-defined]

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


#: 这几条用例读的目标。`_read_flight_time` 现在要拿它去算距离公式那一路。
TARGET = Coordinate(2, 137, 14)


def _loop_reading(text: str, *, arrival: str = "", speed: str = "") -> Any:
    """造一个「简报上各块 ROI 分别读作什么」的 loop。

    默认只有飞行时间那一行有内容，到达时间与速度都读空——也就是**改动之前
    那个单来源的世界**。这几条老用例守的判据（上界、天、存图、闹钟不是闸门）
    与来源无关，所以让它们继续在那个世界里跑，多出来的两个来源各自另有专文。
    """
    from evo_helper.tools.pirate_loop import LoopOptions, PirateLoop

    date_text, _, time_text = arrival.partition(" ")

    def _read(roi: tuple[int, int, int, int], **_kwargs: Any) -> str:
        if roi == pirate_ui.BRIEFING_ARRIVAL_DATE_ROI:
            return date_text
        if roi == pirate_ui.BRIEFING_ARRIVAL_TIME_ROI:
            return time_text
        if roi == pirate_ui.BRIEFING_SPEED_ROI:
            return speed
        if roi == pirate_ui.BRIEFING_SPEED_PERCENT_ROI:
            return "100%" if speed else ""
        return text

    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _FakeDriver()  # type: ignore[attr-defined]
    loop._options = LoopOptions(systems=(), scout=False, attack=True, origin=ORIGIN)
    loop._read = _read  # type: ignore[attr-defined]
    loop._dump_frame = lambda *_args, **_kwargs: None  # type: ignore[attr-defined]
    # 没有账本 = 学不出系数 = 公式那一路弃权。这几条守的是**读屏**那一段，
    # 而公式弃权正是它们要的场面（三个来源只剩两个 OCR）。
    loop._repository = None  # type: ignore[attr-defined]
    return loop


def test_a_credible_flight_time_becomes_the_return_alarm() -> None:
    loop = _loop_reading("8分3秒")

    assert loop._read_flight_time(TARGET).flight == timedelta(minutes=8, seconds=3)


def test_an_implausibly_long_flight_time_is_treated_as_a_misread() -> None:
    """`8分3秒` 被读成 `8时3分` 是 60 倍——而且不会报错，只会看起来像「在等」。

    这里只有时长这一个来源，没有绝对到达时间可以交叉验证
    （`DispatchBriefing.duration_agrees` 那道校验用不上），所以量级错只能靠上界拦。
    拦下来写 NULL，等待调度器据此改为「立即尝试收取」：白跑一趟，
    而不是让这条链路安静地停摆八小时。
    """
    loop = _loop_reading("8时3分")

    assert loop._read_flight_time(TARGET).flight is None


def test_a_flight_time_in_days_is_never_believed() -> None:
    """`parse_game_duration` 认得 `X天…`，而这条链路打的是同系目标。"""
    loop = _loop_reading("42天17时34分58秒")

    assert loop._read_flight_time(TARGET).flight is None


def test_every_recipe_is_tried_on_every_settle_round() -> None:
    """读不出来时要把**每一套配方 × 每一轮等待**都试满，而不是一次读不出就认输。

    ⚠️ 这一条守的是「重试确实发生了」，**不是**「重试有用」。2026-08-18 的复标
    表明飞行时间那一行的失败是**确定性**的（同一块像素每次读出同样的乱码，
    49 张实拍上现行配方 0/47），重试救不回来——真正的修法是换来源，见
    `pirate_ui.ARRIVAL_RECIPES`。重试仍然留着，是因为它挡的是另一件事：
    面板**还在滑进来**（`_settle` 的注释记着那次「等 2.4 秒判一次判不到，
    而失败时存下的那一帧读得清清楚楚」）。
    """
    from evo_helper.tools.pirate_loop import FLIGHT_RECIPES, FLIGHT_SETTLE_TRIES

    loop = _loop_reading("读不出来的一行")
    seen: list[tuple[int, int | None]] = []
    plain = loop._read

    def _read(roi: tuple[int, int, int, int], **kwargs: Any) -> str:
        if roi == pirate_ui.BRIEFING_FLIGHT_ROI:
            seen.append((kwargs.get("upscale"), kwargs.get("threshold")))
        return str(plain(roi, **kwargs))

    loop._read = _read
    loop._dump_frame = lambda name, roi=None: None  # type: ignore[attr-defined]

    assert loop._read_flight_time(TARGET).flight is None
    assert seen == list(FLIGHT_RECIPES) * FLIGHT_SETTLE_TRIES


def test_a_totally_unreadable_flight_line_leaves_a_frame_behind() -> None:
    """四套配方全败就存一帧现场。

    ⚠️ 没有这张图，下次查「为什么这一发的飞行时间是 NULL」只能靠猜——判据收紧
    之后 NULL 变多了，而 NULL 与「ROI 框歪了」「面板还没铺开」在日志上长得一模一样。
    """
    loop = _loop_reading("读不出来的一行")
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[attr-defined]

    assert loop._read_flight_time(TARGET).flight is None
    assert dumped == ["briefing-flight-unreadable"]


def test_a_readable_flight_line_leaves_no_frame_behind() -> None:
    """反过来：读得出来就不许存图。一轮几十发，存图会把 `var/logs/` 淹掉。"""
    loop = _loop_reading("8分3秒")
    dumped: list[str] = []
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[attr-defined]

    assert loop._read_flight_time(TARGET).flight == timedelta(minutes=8, seconds=3)
    assert dumped == []


def test_an_arrival_time_in_the_past_is_thrown_away_not_turned_into_a_negative_flight() -> None:
    """⚠️ **读出来的到达时刻落在过去时必须丢掉，不许当成一个负的时长。**

    这条路是 `到达时刻 - 现在`，所以一位数字读错就可能得到负数。同一张 OCR
    网格里 `3×/None` 就把 `09:26:27` 读成过 `03:26:27`——差六小时，足够把一趟
    30 分钟的飞行算成 −5.5 小时。

    负数不会触发 `MAX_CREDIBLE_FLIGHT` 那道上界（它只管大的），会一路写进库：
    `expected_report_at_utc` 落在过去 → 战报一被判「到点了」就赖在到期单子上；
    `line_free_at_utc` 落在过去 → 调度器以为航线**已经空了**，接着派，
    撞上游戏那句「同时派遣的舰队数量已达上限。」。

    丢掉之后这一路当作读不出来，换下一套配方；全都不行就交给别的来源。
    """
    loop = _loop_reading("", arrival="13/08/2026 17:02:56")

    assert loop._read_flight_time(TARGET).flight is None


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
        if roi == pirate_ui.BRIEFING_SPEED_ROI:
            return "14.520"
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


def test_the_fleet_speed_on_the_briefing_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **屏幕上那个速度必须落库。**

    它是 `domain.flight_estimate.fit_seconds_per_root_unit` 的**作废信号**：
    系数按出发星球从历史实测里学，而编组一换那些样本立刻不算数——能认出这次
    变化的只有屏幕上这个数（`preset_name` 与 `preset_signature` 都认不出来，
    2026-08-17 那天 13 发慢了 26% 就是这么错过去的）。

    不落库就没有「上一发是什么速度」可比，那道作废永远不会触发。
    """
    repository = _RecordingRepository()
    loop = _attackable_loop(monkeypatch, [], repository)

    assert loop.attack(Coordinate(2, 137, 14), preset="BBB") is True

    assert repository.flight_speeds == ["14.520"]


def test_the_coefficient_is_asked_for_this_planet_and_this_kind_of_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问库要系数时，问的是**这一轮的出发星球**和**这一发的类型**。

    问错星球就回到「全局共用一个 k」那个错（9:250:8 与 4:277:15 差 0.7%）；
    问错类型就会拿侦察去标定攻击，而侦察艇快约 40 倍。
    """
    repository = _RecordingRepository()
    loop = _attackable_loop(monkeypatch, [], repository)

    assert loop.attack(Coordinate(2, 137, 14), preset="BBB") is True

    assert repository.coefficient_calls == [(ORIGIN, MISSION_KIND_ATTACK, "14.520")]


def test_a_repository_that_cannot_answer_never_holds_back_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **学不出系数只是少一个来源，不许把这一发拦下来。**

    方向与「读不出飞行时间也照派」一致：飞行时间是闹钟不是闸门。这条链路已经
    因为「ROI 与放大倍数不配」白白拦下过四发完全正常的攻击。
    """
    repository = _RecordingRepository()

    def _boom(**_kwargs: Any) -> Any:
        raise RuntimeError("库连不上")

    repository.flight_coefficient = _boom  # type: ignore[method-assign]
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
