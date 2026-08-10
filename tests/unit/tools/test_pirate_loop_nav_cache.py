"""派遣面板开过之后必须清导航器缓存。

`SystemNavigator.goto` 只重设它认为变了的字段：

    if at is None or at.galaxy != coordinate.galaxy:
        self._set(GALAXY_FIELD, ...)      # 认为一样就整个跳过

这是个正确的优化——前提是它的记忆和导航栏实际值没分岔。一旦分岔，它**再也不会
自己纠回来**，因为「一样」的判断用的就是那份错记忆。

实机（2026-08-11 00:55–01:08，13 分钟、一发没派）：

1. 第一个目标 2:320:11 导航正常、坐标核对通过，走到派遣面板；
2. 预设条读成空，走 `PresetNotFound` 分支关掉面板 —— **这里原来没清缓存**；
3. 下一个目标 2:321:5：缓存说银河系已经是 2，跳过重设；那一下「设恒星系」落到了
   银河系框上，游戏把 136 截断成它的最大值 9；恒星系没被设，还停在 320；
4. 从此导航栏是 `[9:320:5]`，而缓存说 `2:320:11`。后面每个目标的银河系都是 2，
   于是**银河系字段再也不会被重设**，连续 44 个目标坐标核对全不过。

坐标核对每一次都拦对了——它拦的是「往银河系 9 打」。所以判据一个字都不动，
要补的是这一处缺失的 `invalidate()`。
"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game.preset_picker import PresetNotFound
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop

TARGET = Coordinate(2, 320, 11)


class _Driver:
    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        pass

    def wait(self, _seconds: float) -> None:
        pass


class _Navigator:
    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


def _loop(monkeypatch: Any, *, preset_found: bool) -> tuple[Any, _Navigator]:
    navigator = _Navigator()
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._navigator = navigator  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._options = LoopOptions(systems=(), scout=False, attack=True)  # type: ignore[attr-defined]
    loop._preset_names = lambda: []  # type: ignore[attr-defined, assignment]

    class _Picker:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def pick(self, wanted: str) -> None:
            if not preset_found:
                raise PresetNotFound(f"预设条上找不到 {wanted!r}；这一屏读到的是 []")

    monkeypatch.setattr("evo_helper.tools.pirate_loop.PresetPicker", _Picker)
    return loop, navigator


def test_a_missing_preset_invalidates_the_nav_cache(monkeypatch: Any) -> None:
    """本文件的重点。少了这一步，下一个目标就会把恒星系号写进银河系框。"""
    loop, navigator = _loop(monkeypatch, preset_found=False)

    assert loop.attack(TARGET, preset="探路") is False
    assert navigator.invalidated == 1, "关掉派遣面板之后导航栏状态已不可知，必须清缓存"


def test_the_refusal_is_still_recorded(monkeypatch: Any) -> None:
    """清缓存不能把原来的拦下记录挤掉——调度器靠它判定这一发没派出去。"""
    loop, _navigator = _loop(monkeypatch, preset_found=False)

    loop.attack(TARGET, preset="探路")

    assert loop._outcome.refused == [(TARGET, "找不到预设 探路")]
