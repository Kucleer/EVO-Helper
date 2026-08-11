"""导航栏：一次只设一个字段，不变的字段不重设。"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game.system_navigator import (
    GALAXY_FIELD,
    OK_BUTTON,
    POSITION_FIELD,
    SYSTEM_FIELD,
    SYSTEM_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    SystemNavigator,
    crop_reader,
    on_system_view,
)


class FakeDriver:
    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.actions.append(("click", (x, y)))

    def type_number(self, value: int) -> None:
        self.actions.append(("type", value))

    def capture(self) -> Any:
        return object()

    def wait(self, seconds: float) -> None:
        return None


def fields_touched(driver: FakeDriver) -> list[tuple[int, int]]:
    return [payload for kind, payload in driver.actions if kind == "click" and payload != OK_BUTTON]


def test_first_hop_sets_all_three_fields() -> None:
    driver = FakeDriver()
    SystemNavigator(driver).goto(Coordinate(2, 121, 5))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_each_field_is_committed_before_the_next_one_is_opened() -> None:
    # 点开字段会弹出覆盖整条导航栏的浮层；一口气点两个字段，第二个数字会进第一个。
    driver = FakeDriver()
    SystemNavigator(driver).goto(Coordinate(2, 121, 5))
    assert driver.actions == [
        ("click", GALAXY_FIELD),
        ("type", 2),
        ("click", OK_BUTTON),
        ("click", SYSTEM_FIELD),
        ("type", 121),
        ("click", OK_BUTTON),
        ("click", POSITION_FIELD),
        ("type", 5),
        ("click", OK_BUTTON),
    ]


def test_next_position_in_the_same_system_only_sets_the_position() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    navigator.confirm(Coordinate(2, 121, 5))
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [POSITION_FIELD]


def test_crossing_a_system_keeps_the_galaxy() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 20))
    navigator.confirm(Coordinate(2, 121, 20))
    driver.actions.clear()
    navigator.goto(Coordinate(2, 122, 5))
    assert fields_touched(driver) == [SYSTEM_FIELD, POSITION_FIELD]


def test_typing_alone_is_not_evidence_of_where_the_nav_bar_is() -> None:
    """**本文件的重点。** 打完字不算数：省字段只能靠回读确认过的那份记忆。

    实机 2026-08-11：一次「设恒星系」的点击落到了银河系框上，游戏把 136 截断成
    最大值 9。按「我刚才打了什么」记，缓存当场就是错的，而判「一样」用的就是
    那份错记忆——银河系字段再没被重设，连续 44 个目标坐标核对全不过。
    按「面板回读到什么」记，错的记不进来：拿不准就把三个字段全重设一遍。
    """
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    assert navigator.current is None
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_invalidate_forces_all_three_fields_again() -> None:
    # 重连或弹窗之后导航栏里是什么已经不可知，不能再靠记忆省字段。
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    navigator.confirm(Coordinate(2, 121, 5))
    navigator.invalidate()
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_on_system_view_needs_two_labels() -> None:
    # 「行星」在底部导航条上也有，单个标签命中不足以证明在哪一屏。
    assert on_system_view("银河系  恒星系  行星")
    assert on_system_view("银河系 恒星系")
    assert not on_system_view("行星 舰队 太空舱 商店 联盟")
    assert not on_system_view("")


def test_ensure_system_view_does_nothing_when_already_there() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 1, 5))
    navigator.confirm(Coordinate(2, 1, 5))
    driver.actions.clear()
    assert navigator.ensure_system_view(lambda: "银河系 恒星系 行星")
    assert driver.actions == []
    # 没换视图，回读确认过的那份记忆还作数。
    assert navigator.current == Coordinate(2, 1, 5)


def test_ensure_system_view_switches_back_from_the_planet_view() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.current = Coordinate(2, 1, 5)
    reads = iter(["行星 舰队 太空舱 商店 联盟", "银河系 恒星系 行星"])

    assert navigator.ensure_system_view(lambda: next(reads))
    assert fields_touched(driver) == [VIEW_MENU_BUTTON, SYSTEM_VIEW_BUTTON]
    # 换过视图之后导航栏里是什么已经不可知了。
    assert navigator.current is None


def test_ensure_system_view_gives_up_instead_of_clicking_blind() -> None:
    # 切不回去还往 (795, 71) 点，就是在认不出的画面上乱点。
    driver = FakeDriver()
    assert not SystemNavigator(driver).ensure_system_view(lambda: "什么都没有", attempts=2)
    assert fields_touched(driver) == [
        VIEW_MENU_BUTTON,
        SYSTEM_VIEW_BUTTON,
        VIEW_MENU_BUTTON,
        SYSTEM_VIEW_BUTTON,
    ]


class FakeImage:
    def __init__(self) -> None:
        self.cropped: list[tuple[int, int, int, int]] = []

    def crop(self, box: tuple[int, int, int, int]) -> str:
        self.cropped.append(box)
        return f"crop{box}"


def test_crop_reader_passes_the_recipe_through() -> None:
    image = FakeImage()
    seen: list[tuple[str, bool, int]] = []

    def ocr(crop: str, *, digits: bool, upscale: int, resample: str = "lanczos") -> str:
        seen.append((crop, digits, upscale))
        return "读数"

    read = crop_reader(image, ocr)
    assert read((1, 2, 3, 4), digits=True, upscale=5) == "读数"
    assert image.cropped == [(1, 2, 3, 4)]
    assert seen == [("crop(1, 2, 3, 4)", True, 5)]
