"""接线：`pirate_loop` 真的把「看一眼导航条」接进了 `PlanetSwitcher`。

⚠️ **这条用例是冲着一个具体事故形态写的**：代码写了但从没接上，单元测试全绿、
变异验证全红，而实机上那个 ROI 从落地起就没读出过东西（`FLIGHT_RECIPES` 那段账）。

`PlanetSwitcher.nav_targets` 的默认值**是今天的行为**（交回写死的
`NAV_PLANET` / `NAV_FLEET`）——那是有意的，回退只要删掉注入的那一个参数。
代价就是「接线漏了会静默退化成旧行为」，而这里就是唯一发现它的地方。
"""

from __future__ import annotations

from evo_helper.game.planet_list import PlanetSwitcher
from evo_helper.tools.pirate_loop import PirateLoop


def test_the_switcher_is_built_with_the_nav_bar_guard() -> None:
    """⚠️ `planet_switcher()` 造出来的对象必须带着真实的「看一眼导航条」回调。

    判据是「不是默认值」而不是「等于某个具体函数」：默认值是那个 lambda，
    接上了就该换成 `PirateLoop._bottom_nav_targets` 那个绑定方法。
    """
    switcher = PirateLoop.planet_switcher(_loop())

    assert switcher.nav_targets != PlanetSwitcher.nav_targets, (
        "`nav_targets` 还是默认值——接线漏了，行为静默退回 2026-08-24 之前"
    )
    assert getattr(switcher.nav_targets, "__name__", "") == "_bottom_nav_targets"


def test_the_guard_refuses_when_the_screen_cannot_be_captured() -> None:
    """截不到图就交 `None`（= 一下都不点），而不是照标定像素硬点。

    ⚠️ 「看不到画面」与「看到了但条在右段」的正确处置是同一个：不点。
    读不出来时**条在哪一段是未知的**，而这次事故的全部代价就来自「未知时照旧点」。
    """
    loop = _loop()

    assert loop._bottom_nav_targets() is None  # noqa: SLF001 - 钉的就是这一层


class _NoCapture:
    """轻量驱动：没有 `capture`，照 `_close_button_visible` 那条的先例。"""

    def click(self, x: int, y: int, *, label: str = "") -> None: ...

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None: ...

    def wait(self, seconds: float) -> None: ...


def _loop() -> PirateLoop:
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _NoCapture()  # type: ignore[attr-defined]  # noqa: SLF001
    return loop
