"""选预设：拖到左端、按名字点、找不到就不点。

派遣面板保留上一次的选择（实机上是「轻型战斗机 1000」），所以「没选中预设」
不是「少了个优化」，而是「把一千架轻型战斗机送出去」。这里守的就是那条底线。
"""

from __future__ import annotations

import pytest

from evo_helper.game.pirate_ui import PRESET_DRAG_FROM_X, PRESET_DRAG_TO_X, PRESET_NAME_ROW_Y
from evo_helper.game.preset_picker import PresetNotFound, PresetPicker


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []
        self.drags: list[tuple[int, int, int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        self.drags.append((from_x, from_y, to_x, to_y, label))

    def wait(self, seconds: float) -> None:
        del seconds


def _picker(screens: list[list[tuple[int, str]]]) -> tuple[PresetPicker, _Driver]:
    """`screens` 是逐次读到的屏；最后一屏会被反复读到（模拟夹住）。"""
    driver = _Driver()
    state = {"index": 0}

    def read() -> list[tuple[int, str]]:
        index = min(state["index"], len(screens) - 1)
        state["index"] += 1
        return screens[index]

    return PresetPicker(driver=driver, read_names=read), driver


def test_it_drags_left_until_the_strip_stops_moving() -> None:
    picker, driver = _picker(
        [
            [(830, "探路"), (990, "BBB")],
            [(748, "AAA"), (985, "探路")],
            [(748, "AAA"), (985, "探路")],
        ]
    )

    x = picker.pick("AAA")

    assert x == 748
    assert (748, PRESET_NAME_ROW_Y, "预设 AAA") in driver.clicks
    # 两次拖动：第一次内容变了所以继续，第二次读到一样的就停。
    assert len(driver.drags) == 2


def test_it_only_ever_drags_leftward() -> None:
    """预设条最右端是「+ 保存当前舰队」，点到它会改坏用户的预设。

    往左拖永远离那个按钮更远，所以这条链路一次也不许往右拖。
    """
    picker, driver = _picker([[(748, "AAA"), (985, "探路")]])

    picker.pick("AAA")

    for from_x, _from_y, to_x, _to_y, _label in driver.drags:
        assert from_x == PRESET_DRAG_TO_X
        assert to_x == PRESET_DRAG_FROM_X
        assert to_x > from_x


def test_a_missing_preset_is_refused_rather_than_approximated() -> None:
    picker, driver = _picker([[(830, "探路"), (990, "BBB")]])

    with pytest.raises(PresetNotFound, match="AAA"):
        picker.pick("AAA")

    # 只展开与拖动，**没有点任何预设**。
    assert [label for _x, _y, label in driver.clicks] == ["预设条"]


def test_a_name_split_by_ocr_is_merged_and_clicked_in_the_middle() -> None:
    """拆开的两块合成一个名字，点在整段中点——离相邻预设最远。"""
    picker, _driver = _picker([[(744, "AAA"), (760, "AAA"), (985, "探路")]])

    assert picker.pick("AAA") == 752


def test_a_chinese_name_split_per_character_is_still_found() -> None:
    """本文件里最贵的一条：tesseract 对中文是**按字**分词的。

    实机（2026-08-11）预设条拖到左端后读回来是 `['AAA', '探', '路']`，
    逐词做 `name in text` 于是永远匹配不上 `探路`——bot 链路每一发都倒在
    「找不到预设 探路」，而预设条上明明就有它。链路因此从未真正派出过一发。
    """
    picker, driver = _picker([[(747, "AAA"), (984, "探"), (994, "路")]])

    assert picker.pick("探路") == 989
    assert (989, PRESET_NAME_ROW_Y, "预设 探路") in driver.clicks


def test_two_distinct_presets_are_never_merged() -> None:
    """合并只能跨字，不能跨预设。

    合过头的代价是点错预设——预设决定送出去多少舰队。实测同名相邻两字差 10px，
    不同预设之间差 237px，所以这里用真实量级构造：`探`/`路` 该合，`AAA` 不该被
    卷进来，于是 `AAA探` 这种拼接出来的名字不存在，找它必须失败。
    """
    picker, _driver = _picker([[(747, "AAA"), (984, "探"), (994, "路")]])

    with pytest.raises(PresetNotFound, match="AAA探"):
        picker.pick("AAA探")
