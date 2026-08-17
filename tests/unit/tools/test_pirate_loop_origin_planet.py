"""开工阶段切出发星球：一轮只切一次，切不成就一发都不派。

两条链路共用这一段（`BotLoop` 继承 `PirateLoop.run`），所以这里两边都钉一遍。

为什么「切不成就不派」不能松：舰队会从**游戏此刻选中的那颗**星球飞出去，而
`attack_intents.origin_*` 上写的是这一轮配的那颗。两者不一致时战报认领
（出发坐标 + 目标坐标 + 时间就近）永远配不上，飞行时间与航线钟也全按错的距离算。
这正是 #49 那道临时闸门当初要防的局面——闸门删掉之后，防线就只剩这一道回读。

⚠️ 全程不碰游戏：切换器是假的，窗口校验被桩掉。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY
from evo_helper.game.planet_list import SwitchResult
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop, exit_code_for

SECOND = Coordinate(9, 250, 8)
TARGETS = (Coordinate(2, 137, 4), Coordinate(2, 137, 9), Coordinate(2, 138, 4))


class _FakeNavigator:
    def __init__(self) -> None:
        self.invalidated = 0
        self.current: Coordinate | None = None
        #: `adopt_readback` 收到过的 `(坐标, 三个读数)`，顺序原样记下来。
        self.readbacks: list[tuple[Coordinate, tuple[str, ...]]] = []

    def ensure_system_view(self, _read_labels: Any) -> bool:
        return True

    def confirm(self, coordinate: Coordinate) -> None:
        self.current = coordinate

    def adopt_readback(self, coordinate: Coordinate, values: Any) -> bool:
        """照着真货的规矩来：读数逐字对得上才记，否则清缓存。"""
        self.readbacks.append((coordinate, tuple(values)))
        expected = (str(coordinate.galaxy), str(coordinate.system), str(coordinate.position))
        if tuple(values) == expected:
            self.confirm(coordinate)
            return True
        self.invalidate()
        return False

    def invalidate(self) -> None:
        self.invalidated += 1
        self.current = None


class _FakeSwitcher:
    """记下被要求切到哪几颗星球，按剧本给结局。"""

    def __init__(self, result: SwitchResult) -> None:
        self._result = result
        self.asked: list[Coordinate] = []

    def switch_to(self, target: Coordinate) -> SwitchResult:
        self.asked.append(target)
        return self._result


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: SwitchResult = SwitchResult.SWITCHED,
    origin: Coordinate | None = SECOND,
    last_reconciled_minutes_ago: float | None = 1.0,
) -> tuple[Any, _FakeSwitcher, list[str]]:
    """`last_reconciled_minutes_ago` 决定这一轮翻不翻信箱（`domain.reconcile_cooldown`）。

    默认 1 分钟前刚对过账 = 冷却中、本轮不翻。切星球那条链路的用例只关心切换，
    不该被那一趟信箱搅进来；要看信箱就把它设成 `None`（从没对过账，必翻）。
    """
    from evo_helper.game import game_window

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)
    monkeypatch.setattr(module, "say", lambda _message: None)

    swept: list[str] = []
    switcher = _FakeSwitcher(result)
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=False, attack=True, origin=origin)
    loop._outcome = Outcome()
    loop._current_planet = None
    loop._navigator = _FakeNavigator()
    loop._nav_labels = lambda: ""
    loop._reset_to_known_screen = lambda: None
    loop._ensure_session = lambda **_k: False
    loop._require_system_view = lambda _what: None
    loop._goto_planet_surface = lambda: True
    # 切完星球要回读导航栏三个值框（见 `_adopt_navigation_bar`）。默认读到出发星
    # 本身，也就是「回读通过」那一支；要看读不通就在用例里改掉它。
    loop._navigation_bar_values = lambda: (
        (str(origin.galaxy), str(origin.system), str(origin.position))
        if origin is not None
        else ("", "", "")
    )
    loop.planet_switcher = lambda **_k: switcher
    loop._reconcile_decision = None
    loop._last_reconciled_at = lambda: (
        None
        if last_reconciled_minutes_ago is None
        else datetime.now(UTC) - timedelta(minutes=last_reconciled_minutes_ago)
    )
    loop.reconcile_today = lambda: swept.append("开工那一趟信箱")
    loop._sweep = lambda: swept.append("扫目标")
    return loop, switcher, swept


class TestSwitchingOncePerRound:
    def test_the_round_switches_to_the_configured_planet_before_dispatching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, switcher, swept = _loop(monkeypatch)

        loop.run()

        assert switcher.asked == [SECOND]
        assert swept == ["扫目标"]

    def test_the_mailbox_is_read_before_the_switch_is_attempted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """顺序是承重的：**读信箱在切星球之前**。

        切星球是开工阶段最容易失手的一步（认坐标、拖列表、回读），而读信箱
        跟站在哪颗星球上无关。排在后面的话，切换一失手这一轮就一份战报都不入库。

        这里钉的是**交错顺序**，光看两个清单各自的内容看不出来。
        """
        loop, switcher, order = _loop(monkeypatch, last_reconciled_minutes_ago=None)
        loop.reconcile_today = lambda: order.append("信箱")
        loop._sweep = lambda: order.append("扫目标")
        switcher.switch_to = lambda target: (  # type: ignore[method-assign]
            order.append("切星球"),
            switcher.asked.append(target),
            SwitchResult.SWITCHED,
        )[-1]

        loop.run()

        assert order == ["信箱", "切星球", "扫目标"]

    def test_switching_first_returns_to_planet_surface_for_the_planet_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """恒星系底栏同一像素不是「行星」；必须先回地表。"""
        loop, switcher, order = _loop(monkeypatch, last_reconciled_minutes_ago=None)
        loop._goto_planet_surface = lambda: (order.append("回地表"), True)[1]
        switcher.switch_to = lambda target: (  # type: ignore[method-assign]
            order.append("切星球"),
            switcher.asked.append(target),
            SwitchResult.SWITCHED,
        )[-1]

        assert loop.ensure_origin_planet() is True
        assert order == ["回地表", "切星球"]

    def test_refuses_to_open_the_planet_list_when_the_surface_cannot_be_reached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, switcher, _order = _loop(monkeypatch)
        loop._goto_planet_surface = lambda: False

        assert loop.ensure_origin_planet() is False
        assert switcher.asked == []
        assert loop._outcome.busy == "切出发星球前回不到星球地表"

    def test_several_targets_still_cost_exactly_one_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**一轮只切一次。** 挂在每个目标前面的话，每颗星球都要多付一次
        「开浮层 + 可能几次拖动 + 回读」——十几秒，而出发星球在一轮之内不会变。
        """
        loop, switcher, _swept = _loop(monkeypatch)
        loop._sweep = lambda: [loop.ensure_origin_planet() for _target in TARGETS]

        loop.run()

        assert switcher.asked == [SECOND], "每个目标各切一次的话这里会是三颗"

    def test_the_memory_is_only_written_after_the_readback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """回读没过就不记「已经在这颗星球上了」，否则下一次问它会说不用切了。

        与 `SystemNavigator.current` 同一条规矩：打过的字不算数，读回来的才算。
        """
        loop, _switcher, _swept = _loop(monkeypatch, result=SwitchResult.UNCONFIRMED)

        loop.ensure_origin_planet()

        assert loop._current_planet is None

    def test_a_round_with_no_origin_configured_falls_back_to_the_global_home(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """手工跑命令行时不给 `--origin`，回落到 `EVO_HELPER_ORIGIN`。

        回落到「不切」是不行的：那样游戏停在上一轮那颗星球上，而记账写的是主星。
        """
        loop, switcher, _swept = _loop(monkeypatch, origin=None)

        loop.run()

        assert switcher.asked == [module.origin()]


class TestRefusingToDispatchWhenTheSwitchFailed:
    @pytest.mark.parametrize("result", [SwitchResult.NOT_FOUND, SwitchResult.UNCONFIRMED])
    def test_the_reports_are_still_read_but_nothing_is_dispatched(
        self, monkeypatch: pytest.MonkeyPatch, result: SwitchResult
    ) -> None:
        """**绝不「点了就当切成了」——但也绝不因此把读战报一起停掉。**

        这条原先断言的是 `swept == []`（信箱也没读）。那是落地时的真实行为，
        而它是个 bug：信箱是账号级的，读它跟站在哪颗星球上毫无关系，
        切换失手只该挡住派遣。照原样一轮不读，「战报缺失、没入库」
        （用户 2026-08-13 报的毛病）就会由这个功能自己再生产一遍。

        ⚠️ 断言写成 `swept == []` 时它是绿的——**方向写反的断言照样全绿**。
        所以这里钉的是清单本身，不是「有没有东西」。
        """
        loop, _switcher, swept = _loop(monkeypatch, result=result)

        outcome = loop.run()

        assert swept == [], "冷却中的那一轮不翻信箱，切星球失败也不该改变这一点"
        assert outcome.attacked == []
        assert outcome.busy is not None
        assert "9:250:8" in outcome.busy

    def test_a_readback_that_did_not_confirm_exits_as_busy_rather_than_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """按 1 收场的话，切换偶尔不成连撞三次就把整条链路自动停用了
        （`domain.scheduler` 的连续失败判据），而它只是需要下一轮再试一次。
        """
        loop, _switcher, _swept = _loop(monkeypatch, result=SwitchResult.UNCONFIRMED)

        assert exit_code_for(loop.run()) == EXIT_ENVIRONMENT_BUSY

    def test_a_planet_that_is_not_in_the_list_at_all_exits_as_a_real_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """**这一档不许豁免。** 列表里翻遍了都没这颗星球 = 配错了坐标，
        而接口那侧无从校验（自己有哪几颗星球只写在游戏画面上，库里没有）。

        它不会自己好。当成 `EXIT_ENVIRONMENT_BUSY` 的后果是一个静默死循环：
        每轮几十秒就退，不计故障、不报警，停顿看门狗也接不住（那东西抓的是
        「跑着却没进展」）。任务整夜显示「在跑」，实际一发不派。
        """
        loop, _switcher, _swept = _loop(monkeypatch, result=SwitchResult.NOT_FOUND)

        outcome = loop.run()

        assert outcome.busy_is_permanent is True
        assert exit_code_for(outcome) == 1
        assert exit_code_for(outcome) != EXIT_ENVIRONMENT_BUSY

    def test_a_round_that_switched_fine_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _switcher, _swept = _loop(monkeypatch)

        assert exit_code_for(loop.run()) == 0


class _FakeSession:
    def __init__(self, *, reconnected: bool) -> None:
        from evo_helper.game.session_keeper import ScreenState

        self.state = ScreenState.IN_GAME
        self.ready = True
        self.reconnected = reconnected
        self.detail = ""


class _FakeKeeper:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def ensure_connected(self, *, force: bool = False) -> _FakeSession:
        del force
        return self._session


class TestForgettingTheCurrentPlanetAfterAReconnect:
    """重连之后那份「本轮已经切到哪」的记忆必须作废。

    ⚠️ 这是**轮内**的事，不是开工时的事：`_ensure_session` 会在目标核对失败后的
    复位重试里被调用（`_goto_checked`），而它的恢复阶梯最后一级是关窗重开
    Chrome——游戏重走一遍入口序列，之后当前星球是哪一颗本仓无从得知。

    不清的后果恰恰是切换星球这个功能存在的理由：`switch_needed` 看到「已经切到
    9:250:8」于是不再切，而画面可能已经回到主星，本轮余下每一发都从主星飞出去，
    而 `attack_intents.origin_*` 上写着 9:250:8。战报永远配不上，**且一声不响**。

    与「重连之后清导航缓存」是同一条规矩，只是那条写在先、这份记忆来得晚。
    """

    def _loop_with(self, reconnected: bool) -> Any:
        loop = PirateLoop.__new__(PirateLoop)
        loop._navigator = _FakeNavigator()
        loop._current_planet = SECOND
        loop._keeper = lambda: _FakeKeeper(_FakeSession(reconnected=reconnected))
        return loop

    def test_a_reconnect_clears_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(module, "say", lambda _message: None)
        loop = self._loop_with(reconnected=True)

        assert loop._ensure_session(force=True) is True
        assert loop._current_planet is None, "重连之后不许再说「本轮已经切过了」"

    def test_it_is_cleared_together_with_the_navigator_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """两份记忆同生同死。只清一份的话，下一个人加第三份时照着抄也只会清一份。"""
        monkeypatch.setattr(module, "say", lambda _message: None)
        loop = self._loop_with(reconnected=True)

        loop._ensure_session(force=True)

        assert loop._navigator.invalidated == 1
        assert loop._current_planet is None

    def test_a_healthy_session_keeps_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """没重连就别清——白清一次的代价是多切一次星球，十几秒。"""
        monkeypatch.setattr(module, "say", lambda _message: None)
        loop = self._loop_with(reconnected=False)

        assert loop._ensure_session(force=True) is False
        assert loop._current_planet == SECOND


def test_the_bot_chain_goes_through_the_very_same_gate() -> None:
    """`BotLoop` 不许自己覆盖这一段。

    先例摆在那里：它曾经覆盖 `run()`、把开工前置抄了一遍，结果漏了
    `except RoundExhausted` 和后来加的断线重连（见
    `tests/unit/tools/test_bot_loop.py`）。同一个坑不该在出发星球上再踩一次。
    """
    from evo_helper.tools.bot_loop import BotLoop

    assert BotLoop.run is PirateLoop.run
    assert BotLoop.ensure_origin_planet is PirateLoop.ensure_origin_planet
