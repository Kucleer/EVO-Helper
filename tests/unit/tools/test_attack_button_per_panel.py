"""无主面板和有主面板的「攻击」按钮不在同一个位置。

实机（2026-08-11）：bot 链路每一发都倒在「找不到预设 探路」，预设条读到的是
`['4', 'i]']`——一堆噪声。原因不在 OCR：`attack()` 用的是 `pirate_ui.ATTACK_BUTTON`
`(1032, 540)`，那是**敌对海盗**面板上的位置。bot 星球是有主面板，中间那一排是
「攻击 / 侦察 / 扫描 / 回收」四个小图标（y≈398），(1032, 540) 落在图标排和舰船格
之间的空白处。点了等于没点，派遣面板根本没开，接着去读预设条自然读到噪声。

也就是说 bot 链路**从来没有真正派出过一发**，而失败信息一直指向预设。

所以这里钉的是「两种面板各用各的按钮」这条结构约束，而不是某个具体像素值：
坐标以后可能因为界面改版而变，但「BotLoop 不能沿用海盗那个坐标」不会变。
"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game import pirate_ui
from evo_helper.game.preset_picker import PresetNotFound
from evo_helper.tools.bot_loop import BotLoop
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop

TARGET = Coordinate(2, 137, 14)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def wait(self, _seconds: float) -> None:
        pass

    def capture(self) -> Any:
        return _DumpImage()


class _DumpImage:
    width = 1
    height = 1

    def save(self, _path: Any) -> None:
        pass


class _Navigator:
    def invalidate(self) -> None:
        pass


def _loop(cls: type, monkeypatch: Any) -> tuple[Any, _Driver]:
    """一个点开派遣面板就立刻在预设那一步收场的循环——只为看第一下点在哪。"""
    driver = _Driver()
    loop = cls.__new__(cls)
    loop._driver = driver
    loop._navigator = _Navigator()
    loop._outcome = Outcome()
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._preset_names = lambda: []

    class _Picker:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def pick(self, wanted: str) -> None:
            raise PresetNotFound(f"预设条上找不到 {wanted!r}")

    monkeypatch.setattr("evo_helper.tools.pirate_loop.PresetPicker", _Picker)
    return loop, driver


def test_the_two_panels_do_not_share_a_button() -> None:
    """这条是本文件的重点，也是唯一一条不依赖具体像素的。"""
    assert PirateLoop.ATTACK_BUTTON != BotLoop.ATTACK_BUTTON


def test_pirate_loop_clicks_the_unowned_panel_button(monkeypatch: Any) -> None:
    loop, driver = _loop(PirateLoop, monkeypatch)

    loop.attack(TARGET, preset="AAA")

    assert driver.clicks[0] == (*pirate_ui.ATTACK_BUTTON, "攻击")


def test_bot_loop_clicks_the_owned_panel_button(monkeypatch: Any) -> None:
    loop, driver = _loop(BotLoop, monkeypatch)

    loop.attack(TARGET, preset="探路")

    assert driver.clicks[0] == (*pirate_ui.BOT_ATTACK_BUTTON, "攻击")
