"""判定「现在是哪一屏」要多取几帧——单帧在会动的页面上是抛硬币。

事故（2026-08-11 07:16，调度器无人值守跑着）：游戏掉回入口页，而那一页在做明暗
动画。连读 6 帧的实测：

    第0帧 title='ETERNAL VOID' 进入='进入'   nav=''
    第1帧 title=''             进入=''      nav='>  =.  _'
    第2帧 title='ETERNAL VOID' 进入='进入'   nav=''
    第3帧 title=''             进入=''      nav='>  =.  _'

**一半的帧什么都读不出来。** `observe()` 只取一帧，落在空帧上就判 UNKNOWN，守护
报「认不出的画面」，runner 拒绝开工；连撞三次，bot 与扫描两条链路双双被自动停用。
而画面其实好好的——人手按同一套判据、多取几帧就走回游戏里了。

只对 UNKNOWN 重取，**没有放松「认不出的画面一律停止」**：每一帧都读不出才返回
UNKNOWN。这也顺带压住另一个歧义：入口页底下透着一层淡淡的 START，空帧上入口页
认不出时会退去读它，偶尔真能读成 START——那就会在入口页上去点 START。
"""

from __future__ import annotations

from typing import Any

from evo_helper.game.session_keeper import ScreenState
from evo_helper.tools.scan_coordinates import (
    ENTRY_TITLE_ROI,
    NAV_TEXT_ROI,
    OBSERVE_FRAMES,
    make_session_keeper,
)

#: 一帧「读得清的入口页」：标题读得出。
ENTRY_FRAME = {ENTRY_TITLE_ROI: "ETERNAL VOID"}
#: 一帧「读得清的游戏内」：导航条读得出。
IN_GAME_FRAME = {NAV_TEXT_ROI: "商店 联盟"}
#: 动画里那种什么都读不出的帧。导航条上是噪声，别的全空。
BLANK_FRAME: dict[tuple[int, int, int, int], str] = {NAV_TEXT_ROI: ">  =.  _"}


class _Crop:
    """假图的裁剪结果，只记住自己是第几帧、裁的哪一块。"""

    def __init__(self, frame: dict[Any, str], box: tuple[int, int, int, int]) -> None:
        self.frame = frame
        self.box = box


class _Image:
    def __init__(self, frame: dict[Any, str]) -> None:
        self._frame = frame

    def crop(self, box: tuple[int, int, int, int]) -> _Crop:
        return _Crop(self._frame, box)


class _Driver:
    """按给定顺序交出每一帧；用完之后一直重复最后一帧。"""

    def __init__(self, frames: list[dict[Any, str]]) -> None:
        self._frames = frames
        self.captures = 0

    def capture(self) -> _Image:
        frame = self._frames[min(self.captures, len(self._frames) - 1)]
        self.captures += 1
        return _Image(frame)

    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        raise AssertionError("这条测试只看判定，不该点任何东西")

    def wait(self, _seconds: float) -> None:
        pass


def _ocr(crop: Any, **_recipe: Any) -> str:
    return crop.frame.get(crop.box, "")


def _keeper(frames: list[dict[Any, str]]) -> tuple[Any, _Driver, list[float]]:
    slept: list[float] = []
    driver = _Driver(frames)
    keeper = make_session_keeper(driver, _ocr, sleep=slept.append)
    return keeper, driver, slept


def test_a_clean_first_frame_costs_one_capture() -> None:
    """读得清就别多取——稳态是绝大多数情况。"""
    keeper, driver, slept = _keeper([IN_GAME_FRAME])

    assert keeper._observe() is ScreenState.IN_GAME
    assert driver.captures == 1
    assert slept == []


def test_a_blank_frame_is_retried_not_believed() -> None:
    """本文件的重点：空帧不是「认不出的画面」，是没读到。"""
    keeper, driver, _slept = _keeper([BLANK_FRAME, ENTRY_FRAME])

    assert keeper._observe() is ScreenState.ENTRY
    assert driver.captures == 2


def test_alternating_frames_still_land_on_the_truth() -> None:
    """实机就是这个形状：空帧与好帧大致交替。"""
    keeper, _driver, _slept = _keeper([BLANK_FRAME, BLANK_FRAME, IN_GAME_FRAME])

    assert keeper._observe() is ScreenState.IN_GAME


def test_every_frame_blank_still_reports_unknown() -> None:
    """判据没有被放松：真的一帧都读不出，仍然是 UNKNOWN。"""
    keeper, driver, _slept = _keeper([BLANK_FRAME])

    assert keeper._observe() is ScreenState.UNKNOWN
    assert driver.captures == OBSERVE_FRAMES


def test_it_waits_between_frames() -> None:
    """不隔一下就重取等于把同一帧读四遍——动画根本没往前走。"""
    keeper, _driver, slept = _keeper([BLANK_FRAME])

    keeper._observe()

    assert len(slept) == OBSERVE_FRAMES - 1
    assert all(gap > 0 for gap in slept)
