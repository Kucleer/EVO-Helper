"""开工阶段切出发星球：一轮只切一次，切不成就一发都不派。

两条链路共用这一段（`BotLoop` 继承 `PirateLoop.run`），所以这里两边都钉一遍。

为什么「切不成就不派」不能松：舰队会从**游戏此刻选中的那颗**星球飞出去，而
`attack_intents.origin_*` 上写的是这一轮配的那颗。两者不一致时战报认领
（出发坐标 + 目标坐标 + 时间就近）永远配不上，飞行时间与航线钟也全按错的距离算。
这正是 #49 那道临时闸门当初要防的局面——闸门删掉之后，防线就只剩这一道回读。

⚠️ 全程不碰游戏：切换器是假的，窗口校验被桩掉。
"""

from __future__ import annotations

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

    def ensure_system_view(self, _read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        self.invalidated += 1


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
) -> tuple[Any, _FakeSwitcher, list[str]]:
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
    loop.planet_switcher = lambda **_k: switcher
    loop.reconcile_today = lambda: swept.append("开工那一趟信箱")
    loop._sweep = lambda: swept.append("扫目标")
    return loop, switcher, swept


class TestSwitchingOncePerRound:
    def test_the_round_switches_to_the_configured_planet_before_anything_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, switcher, swept = _loop(monkeypatch)

        loop.run()

        assert switcher.asked == [SECOND]
        assert swept == ["开工那一趟信箱", "扫目标"]

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
    def test_nothing_is_swept_when_the_switch_did_not_confirm(
        self, monkeypatch: pytest.MonkeyPatch, result: SwitchResult
    ) -> None:
        """**绝不「点了就当切成了」。** 两种失败都要在扫目标之前把这一轮停住。"""
        loop, _switcher, swept = _loop(monkeypatch, result=result)

        outcome = loop.run()

        assert swept == [], "回读没过就不许进目标循环"
        assert outcome.attacked == []
        assert outcome.busy is not None
        assert "9:250:8" in outcome.busy

    def test_the_failed_round_exits_as_busy_rather_than_as_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """按 1 收场的话，切换偶尔不成连撞三次就把整条链路自动停用了
        （`domain.scheduler` 的连续失败判据），而它只是需要下一轮再试一次。
        """
        loop, _switcher, _swept = _loop(monkeypatch, result=SwitchResult.UNCONFIRMED)

        assert exit_code_for(loop.run()) == EXIT_ENVIRONMENT_BUSY

    def test_a_round_that_switched_fine_still_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _switcher, _swept = _loop(monkeypatch)

        assert exit_code_for(loop.run()) == 0


def test_the_bot_chain_goes_through_the_very_same_gate() -> None:
    """`BotLoop` 不许自己覆盖这一段。

    先例摆在那里：它曾经覆盖 `run()`、把开工前置抄了一遍，结果漏了
    `except RoundExhausted` 和后来加的断线重连（见
    `tests/unit/tools/test_bot_loop.py`）。同一个坑不该在出发星球上再踩一次。
    """
    from evo_helper.tools.bot_loop import BotLoop

    assert BotLoop.run is PirateLoop.run
    assert BotLoop.ensure_origin_planet is PirateLoop.ensure_origin_planet
