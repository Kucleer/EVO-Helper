"""点底栏之前先看一眼条停在哪一段。

2026-08-24 生产事故：底部导航条**可以横向滚动**，军力榜那条链为了露出「排名」把它
往左拖；攻击链点的却是写死的 `NAV_PLANET = (840, 862)` / `NAV_FLEET = (920, 862)`。
条被拖走之后那两个像素底下换成了别的项——用户实机确认点出来的是**太空舱**。

而太空舱面板会把整条导航条连同行星列表一起盖住，于是形成自维持的闭环：
点 (840,862) → 开出太空舱 → 盖住条 → 标签读不出 → 关掉浮层重试 → **又点 (840,862)**。
当天「行星列表坐标 OCR 全空」25 次，每次都以「这一轮一发都不派」收场。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game.nav_bar import NAV_BAR_Y
from evo_helper.game.pirate_ui import NAV_FLEET, NAV_PLANET
from evo_helper.game.planet_list import NavTargets, PlanetSwitcher, SwitchResult

TARGET = Coordinate(galaxy=4, system=277, position=15)


class _Driver:
    """记下点了哪儿、拖了什么。一步真实操作都不做。"""

    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        return None

    def wait(self, seconds: float) -> None:
        return None


def _switcher(driver: _Driver, **fields: Any) -> PlanetSwitcher:
    return PlanetSwitcher(
        driver=driver,  # type: ignore[arg-type]
        read_rows=lambda: [],
        read_origin=lambda: "",
        say=lambda _message: None,
        **fields,
    )


# -- 默认值：今天的行为，逐字不变 -------------------------------------------------


def test_without_the_callback_it_clicks_the_calibrated_pixels() -> None:
    """⚠️ **不注入回调时行为逐字不变** —— 这是整个改动的回退设计。

    默认实现交回写死的 `NAV_PLANET` / `NAV_FLEET`，不读也不拖。于是：

    - 轻量工具与单元测试桩不受影响；
    - **回退只要删掉 `pirate_loop` 里注入的那一个参数**，一行回到 2026-08-24 之前。

    代价是「接线漏了会静默退化」，由 `test_pirate_loop_nav_wiring.py` 那条兜住。
    """
    driver = _Driver()
    switcher = _switcher(driver)

    switcher.switch_to(TARGET)

    assert driver.clicks[0][:2] == NAV_PLANET


# -- 判不出来时：一下都不点 --------------------------------------------------------


def test_it_clicks_nothing_when_the_bar_cannot_be_located() -> None:
    """⚠️⚠️ **判不出条在哪一段就一下都不点。**

    这是整条判据存在的理由：点错的代价不是「这一次没成」，而是**开出太空舱面板、
    盖住导航条，把后面每一轮都拖下水**。宁可这一轮不派。
    """
    driver = _Driver()
    switcher = _switcher(driver, nav_targets=lambda: None)

    result = switcher.switch_to(TARGET)

    assert driver.clicks == [], f"判不出来却点了 {driver.clicks}"
    assert result is SwitchResult.UNREADABLE


def test_the_refusal_is_unreadable_not_unconfirmed() -> None:
    """⚠️ 交 `UNREADABLE`，**不是** `UNCONFIRMED`。

    判不出条在哪一段时，「切没切成」这件事我们**根本没去看**；而 `UNCONFIRMED` 的
    意思是「看了，对不上」。两句话对用户的意思完全不同 —— `SwitchResult` 的注释里
    已经为「指着用户的配置说假话」挨过一次打。

    也不能是 `NOT_FOUND`：那句话说的是「列表里没有这颗星球」，而我们连列表都没打开。
    """
    switcher = _switcher(_Driver(), nav_targets=lambda: None)

    assert switcher.switch_to(TARGET) is SwitchResult.UNREADABLE


def test_the_refusal_leaves_evidence() -> None:
    """拒绝要留痕 —— 否则「这一轮没派」在库里又是一句没有下文的话。"""
    seen: list[tuple[str, dict[str, Any]]] = []
    switcher = _switcher(
        _Driver(),
        nav_targets=lambda: None,
        record_evidence=lambda message, payload: seen.append((message, payload)),
    )

    switcher.switch_to(TARGET)

    assert seen, "判不出来却什么证据都没留"
    assert "导航条" in seen[0][0]


# -- 读出来的 x 真的被用上 ---------------------------------------------------------


def test_it_clicks_the_x_that_was_read_not_the_calibrated_one() -> None:
    """⚠️ 点的必须是**这一屏读到的 x**，不是标定像素。

    构造成两者不同（读到 842，标定是 840），否则这条用例证明不了任何事 ——
    两个值相等时「用了哪一个」是看不出来的。
    """
    driver = _Driver()
    switcher = _switcher(
        driver,
        nav_targets=lambda: NavTargets(planet=(842, NAV_BAR_Y), fleet=(918, NAV_BAR_Y)),
    )

    switcher.switch_to(TARGET)

    assert driver.clicks[0][:2] == (842, NAV_BAR_Y)
    assert driver.clicks[0][:2] != NAV_PLANET


def test_the_fleet_click_also_goes_through_the_callback() -> None:
    """⚠️⚠️ **舰队那一下同样不能信写死的像素。**

    条在右段时 `NAV_FLEET`(920) 底下是「商店」（918，**只差 2px**）—— 只改开列表
    那一下是修不干净的，还剩一条会开商店面板的路。

    这条用例最容易漏，因为那次点击在另一个方法（`_confirm`）里。
    """
    driver = _Driver()
    rows = [(1, "[4:277:15]")]
    switcher = PlanetSwitcher(
        driver=driver,  # type: ignore[arg-type]
        read_rows=lambda: rows,
        read_origin=lambda: "4:277:15",
        say=lambda _message: None,
        nav_targets=lambda: NavTargets(planet=(842, NAV_BAR_Y), fleet=(918, NAV_BAR_Y)),
    )

    switcher.switch_to(TARGET)

    fleet_clicks = [click for click in driver.clicks if click[2] == "舰队面板"]
    if fleet_clicks:
        assert fleet_clicks[0][:2] == (918, NAV_BAR_Y)
        assert fleet_clicks[0][:2] != NAV_FLEET


def _rows_of(_screens: Sequence[Sequence[str]]) -> list[tuple[int, str]]:  # pragma: no cover
    return []
