"""「出发！」必须被动作闸门挡住。

实机标定时踩到过：`LiveDriver` 构造 `HumanInput` 时没开闸门（也就是一个
自称只读的驱动），却用 `label="出发"` 成功点掉了简报页上的按钮，
真的派出了一支舰队——因为黑名单里只有「派遣」，没有「出发」。

这个按钮是整条链路里唯一真正把舰队送出去的一下，偏偏长得最不像动作：
绿色 ✓ 看着像终点，其实只是进简报页；`出发！` 才是终点。
"""

from __future__ import annotations

import pytest

from evo_helper.game.human_input import HumanInput, NavigationOnlyError


class _Backend:
    """记下收到的调用，好断言「压根没点出去」。"""

    FAILSAFE = True

    def __init__(self) -> None:
        self.clicks = 0

    def moveTo(self, x: int, y: int, duration: float) -> None:  # noqa: N802 - 对齐 pyautogui
        pass

    def click(self) -> None:
        self.clicks += 1

    def dragTo(self, x: int, y: int, duration: float) -> None:  # noqa: N802 - 对齐 pyautogui
        pass


def test_a_navigation_only_driver_cannot_press_launch() -> None:
    backend = _Backend()
    human = HumanInput(backend, sleep=lambda _seconds: None)

    with pytest.raises(NavigationOnlyError):
        human.click(1078, 815, label="出发")

    assert backend.clicks == 0, "被拒绝的点击不能已经打出去了"


def test_the_attack_chain_may_still_press_launch() -> None:
    """闸门只有一处开关。攻击链路显式打开它，所以照样点得动。"""
    backend = _Backend()
    human = HumanInput(backend, sleep=lambda _seconds: None, allow_actions=True)

    human.click(1078, 815, label="出发")

    assert backend.clicks == 1
