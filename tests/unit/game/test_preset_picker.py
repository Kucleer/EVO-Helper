"""选预设：一屏一屏找、只点当屏读到的位置、点不到保存按钮、找不到就不点。

派遣面板保留上一次的选择（实机上是「轻型战斗机 1000」），所以「没选中预设」
不是「少了个优化」，而是「把一千架轻型战斗机送出去」。这里守的就是那条底线，
外加一条只往一个方向失效的底线：**预设条最右端那个「+ 保存当前舰队」永远点不到**。
"""

from __future__ import annotations

import pytest

from evo_helper.game.pirate_ui import (
    PRESET_DRAG_FROM_X,
    PRESET_DRAG_RIGHT_FROM_X,
    PRESET_DRAG_RIGHT_TO_X,
    PRESET_DRAG_TO_X,
    PRESET_NAME_ROW_Y,
    PRESET_SAFE_CLICK_MAX_X,
    PRESET_SAVE_BUTTON_MARGIN_PX,
    PRESET_STRIP_ROI,
)
from evo_helper.game.preset_picker import PRESET_NAME_ROI, PresetNotFound, PresetPicker

Screen = list[tuple[int, str]]

#: 「+ 保存当前舰队」挂在预设条内容的最右端，条拖到右端时它就贴着条的右界。
#: 诱饵一律摆在这个**绝对**坐标上，不写成 `PRESET_SAFE_CLICK_MAX_X + n`。
#:
#: ⚠️ 这不是风格问题。写成相对值时，边距常量被调宽（120 → 0）诱饵会跟着往右挪，
#: 于是「点不到保存按钮」那几条**全是绿的**——变异实测过，那一轮真的绿了。
#: 闸门要守的是真实像素上的那块地方，断言就得钉在真实像素上。
SAVE_BUTTON_X = PRESET_STRIP_ROI[2] - 20

#: 保存按钮与可点区之间至少要留这么宽。实机标定值是 120（`PRESET_SAVE_BUTTON_MARGIN_PX`）；
#: 这里只卡下限——调**宽**是往安全的方向走，调窄才是把闸门拆了。
MIN_SAFE_MARGIN_PX = 120


class _Strip:
    """一条会滚动的假预设条。

    `screens` 从左端到右端排开，`at` 是眼下停在第几屏；两端夹住。
    读一屏只交出**那一屏**的词框——测试里唯一的位置来源，实现要是从别处
    变出坐标来，下面的断言就接不上。
    """

    def __init__(self, screens: list[Screen], *, at: int = 0) -> None:
        self.screens = screens
        self.at = at

    def read(self) -> Screen:
        return list(self.screens[self.at])

    def scroll(self, delta: int) -> None:
        self.at = max(0, min(len(self.screens) - 1, self.at + delta))


class _Driver:
    def __init__(self, strip: _Strip) -> None:
        self._strip = strip
        self.clicks: list[tuple[int, int, str]] = []
        self.drags: list[tuple[int, int, int, int, str]] = []
        #: 每次点击发生时预设条停在第几屏。用来钉「点的 x 来自当屏」。
        self.click_at: list[int] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))
        self.click_at.append(self._strip.at)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        self.drags.append((from_x, from_y, to_x, to_y, label))
        # 手指往右划 → 内容右移 → 露出左边；反之露出右边。
        self._strip.scroll(-1 if to_x > from_x else 1)

    def wait(self, seconds: float) -> None:
        del seconds


def _picker(screens: list[Screen], *, at: int = 0) -> tuple[PresetPicker, _Driver, _Strip]:
    strip = _Strip(screens, at=at)
    driver = _Driver(strip)
    return PresetPicker(driver=driver, read_names=strip.read), driver, strip


def _preset_clicks(driver: _Driver) -> list[tuple[int, int, str]]:
    return [click for click in driver.clicks if click[2].startswith("预设 ")]


def _left_end_screen() -> Screen:
    return [(748, "AAA"), (985, "探路")]


# -- 左端 ----------------------------------------------------------------------


def test_it_drags_left_until_the_strip_stops_moving() -> None:
    """打开时停在哪不一定，所以先往左夹住；判据是「读到的不再变化」。"""
    picker, driver, _strip = _picker(
        [_left_end_screen(), [(830, "探路"), (990, "BBB")]],
        at=1,
    )

    x = picker.pick("AAA")

    assert x == 748
    assert (748, PRESET_NAME_ROW_Y, "预设 AAA") in driver.clicks
    # 两次拖动：第一次到了左端，第二次读到一样的就停。
    assert len(driver.drags) == 2


def test_the_search_starts_leftward_before_it_ever_goes_right() -> None:
    """先往左夹住再往右扫，顺序不能反。

    往左夹住是为了拿到一个确定的起点；先往右扫的话，打开时停在中间的那次
    就会漏掉左边的预设——`AAA` 正是最左那个。
    """
    picker, driver, _strip = _picker([_left_end_screen(), [(830, "探路"), (990, "BBB")]], at=1)

    picker.pick("BBB")

    directions = [to_x > from_x for from_x, _fy, to_x, _ty, _label in driver.drags]
    assert directions, "一次都没拖"
    # True = 往左（露出左侧）。第一段必须全是 True，右扫开始之后不再回头往左。
    assert directions == sorted(directions, reverse=True)


# -- 往右找得到，而且点的是当屏读到的位置 --------------------------------------


def test_a_preset_only_reachable_by_dragging_right_is_found() -> None:
    """用户口径（2026-08-11）：「BBB 和 CCC 需要拖动才可以找到，实际是有的」。

    左端那一屏是 `AAA / 探路`，BBB、CCC 在更右边。只在左端那一屏找的话它俩
    永远进不了候选，而 bot 现在**每一发**用的都是 BBB（`domain.bot_round`），
    而报出来的是「预设条上找不到 'CCC'」，看着像游戏里根本没有这个预设。
    """
    screens = [
        _left_end_screen(),
        [(760, "探路"), (985, "BBB")],
        [(770, "BBB"), (990, "CCC")],
    ]

    picker, driver, _strip = _picker(screens)
    assert picker.pick("CCC") == 990
    assert (990, PRESET_NAME_ROW_Y, "预设 CCC") in driver.clicks

    picker, driver, _strip = _picker(screens)
    assert picker.pick("BBB") == 985


def test_the_clicked_x_is_the_one_read_on_the_screen_it_was_clicked_from() -> None:
    """用户口径（2026-08-11）：「需要识别文本进行定位，而不是直接定位」。

    预设条是连续滚动的，拖动步距从来没标定过、还带惯性。所以点下去的 x 只能是
    **命中那一屏刚 OCR 出来的**中心 x，不许拿别的屏的位置换算。

    这里让同一个名字在三屏上都出现、但每屏的 x 都不同：任何「用上一屏/下一屏/
    第一屏的位置去点」的实现，落点都对不上命中屏读到的那一组 x。
    """
    screens = [
        [(748, "AAA"), (985, "探路")],
        [(700, "探路"), (900, "CCC")],  # ← 命中屏：CCC 在 900
        [(640, "CCC"), (880, "DDD")],  # ← 再往右 CCC 挪到了 640
    ]
    picker, driver, strip = _picker(screens)

    x = picker.pick("CCC")

    clicks = _preset_clicks(driver)
    assert len(clicks) == 1
    (clicked_x, _y, _label) = clicks[0]
    assert x == clicked_x
    # 点击发生时停在第几屏，落点就必须是那一屏读出来的某个词框中心。
    at = driver.click_at[driver.clicks.index(clicks[0])]
    assert clicked_x in {word_x for word_x, _text in screens[at]}
    assert (at, clicked_x) == (1, 900)
    assert strip.at == 1


def test_it_stops_dragging_as_soon_as_the_target_shows_up() -> None:
    """找到就停，不必拖到最右——每多拖一格都是一次真实的拖动加一秒多的等待。"""
    screens = [
        _left_end_screen(),
        [(760, "探路"), (985, "BBB")],
        [(770, "BBB"), (990, "CCC")],
        [(780, "CCC"), (995, "DDD")],
    ]
    picker, driver, strip = _picker(screens)

    picker.pick("BBB")

    assert strip.at == 1
    rightward = [drag for drag in driver.drags if drag[2] < drag[0]]
    assert len(rightward) == 1


# -- 「+ 保存当前舰队」这一条 --------------------------------------------------


def test_the_right_margin_is_never_clicked_even_if_the_name_is_read_there() -> None:
    """本文件里最要紧的一条：命中落在右边距里，宁可当作没找到。

    右边距那一段留给「+ 保存当前舰队」——点到它会覆盖用户自己维护的预设，
    是整条链路上唯一会改坏用户配置的控件。这里把目标摆在保存按钮那个位置上且
    **只摆在那里**（拖到右端也还在那里），所以正确的结果是一个预设都不点、
    抛 `PresetNotFound`。
    """
    picker, driver, _strip = _picker([[(760, "AAA"), (SAVE_BUTTON_X, "CCC")]])

    with pytest.raises(PresetNotFound, match="CCC"):
        picker.pick("CCC")

    assert _preset_clicks(driver) == []


def test_no_click_inside_the_strip_ever_lands_in_the_right_margin() -> None:
    """把闸门写成「落在预设条里的所有点击」，而不是「叫『预设 X』的那些点击」。

    每一屏都在边距里塞一个正好叫得上名字的诱饵，从左到右一路诱惑；实现只要有
    一处拿边距里的 x 去点条上的东西，这条就红——哪怕它没用「预设 」当标签。

    （`PRESET_TOGGLE` 在 x=1176、也就是边距那一段里，但它在 y=646、条的**上方**，
    不在条上。边距是条内部的事，所以按 y 圈定范围而不是无差别地卡所有点击。）
    """
    screens: list[Screen] = [
        [(748, "AAA"), (SAVE_BUTTON_X, "CCC")],
        [(760, "探路"), (SAVE_BUTTON_X - 8, "CCC")],
        [(770, "BBB"), (SAVE_BUTTON_X - 16, "CCC")],
    ]
    picker, driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound):
        picker.pick("CCC")

    on_strip = [
        (x, y, label)
        for x, y, label in driver.clicks
        if PRESET_STRIP_ROI[1] <= y <= PRESET_STRIP_ROI[3]
    ]
    # 绝对边界，不是 `PRESET_SAFE_CLICK_MAX_X`：见 `SAVE_BUTTON_X` 那段。
    for x, _y, label in on_strip:
        assert x < PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX, f"{label} 点进了保存按钮那条边距"


def test_dragging_right_never_presses_down_inside_the_right_margin() -> None:
    """往右拖必须**按**在边距左边。

    按下才可能触发按钮；往左拖是按在 800、松手才到 1150，松手落在按钮上不触发它，
    所以左拖的老坐标不用改，右拖的起点却必须自己守住。
    """
    screens = [_left_end_screen(), [(760, "探路"), (985, "BBB")]]
    picker, driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound):
        picker.pick("查无此预设")

    rightward = [drag for drag in driver.drags if drag[2] < drag[0]]
    assert rightward, "一次都没往右拖，这条就白守了"
    for from_x, _fy, _to_x, _ty, _label in rightward:
        assert from_x < PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX


def test_the_drag_endpoints_stay_clear_of_the_save_button() -> None:
    """坐标常量自身的不变量，改常量的人先撞这里。

    ⚠️ 全部拿**绝对像素**卡，不拿 `PRESET_SAFE_CLICK_MAX_X` 卡：那个数本身就是
    被守的对象，用它当尺子等于让被告自己举证。边距调宽是往安全方向走（放行），
    调窄等于拆闸门（拦下）。
    """
    assert PRESET_SAFE_CLICK_MAX_X == PRESET_STRIP_ROI[2] - PRESET_SAVE_BUTTON_MARGIN_PX
    assert PRESET_SAVE_BUTTON_MARGIN_PX >= MIN_SAFE_MARGIN_PX
    assert PRESET_SAFE_CLICK_MAX_X <= PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX
    # 右拖两端都在安全区内——**按下**的那一点尤其不能进边距。
    assert PRESET_DRAG_RIGHT_FROM_X < PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX
    assert PRESET_DRAG_RIGHT_TO_X < PRESET_DRAG_RIGHT_FROM_X
    assert PRESET_DRAG_RIGHT_TO_X >= PRESET_STRIP_ROI[0]
    # 左拖按下的那一点也在安全区内（松手那一点在边距里，但松手不触发按钮）。
    assert PRESET_DRAG_TO_X < PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX


def test_the_leftward_drag_keeps_the_coordinates_it_was_calibrated_with() -> None:
    """往左拖的两端一个字不改——那是实机走通的一对，别顺手跟着右拖一起「统一」了。"""
    picker, driver, _strip = _picker([_left_end_screen()])

    picker.pick("AAA")

    leftward = [drag for drag in driver.drags if drag[2] > drag[0]]
    assert leftward
    for from_x, from_y, to_x, to_y, _label in leftward:
        assert (from_x, to_x) == (PRESET_DRAG_TO_X, PRESET_DRAG_FROM_X)
        assert from_y == to_y


def test_the_name_row_ocr_window_cannot_even_reach_the_margin() -> None:
    """名字那行的 OCR 窗口整个待在可点区之内，所以真实词框天生够不着保存按钮。

    ⚠️ 右边就是第二个预设的数量列，看着很像「顺手放宽一点」的地方。放宽到边距里，
    保存按钮就进了候选池——那时挡着的只剩 `_clickable_hit` 里那道坐标闸。
    这条在放宽的**那一刻**就红，不用等实机。
    """
    assert PRESET_NAME_ROI[2] <= PRESET_STRIP_ROI[2] - MIN_SAFE_MARGIN_PX


def test_a_name_that_reads_like_the_save_button_is_never_a_candidate() -> None:
    """独立于坐标的第二道闸：读出来含「保存」的一律不当候选。

    坐标那道靠的是量出来的几何，几何会变；这道只认字。两道同时失效才点得到。
    """
    picker, driver, _strip = _picker([[(760, "AAA"), (900, "+保存当前舰队")]])

    with pytest.raises(PresetNotFound, match="保存当前舰队"):
        picker.pick("保存当前舰队")

    assert _preset_clicks(driver) == []


def test_it_returns_to_the_left_end_before_giving_up() -> None:
    """没找到就把条拖回左端再抛。

    抛错的那一刻条正停在右端——那正是「+ 保存当前舰队」露脸的位置，而下游坐标
    （`DISPATCH_CONFIRM` 在 (1156, 763)，落在 `PRESET_STRIP_ROI` 里）都是在条停
    在左端时标定的。把条还原成标定时的样子，下一步点下去才还是原来那个东西。
    """
    screens = [
        _left_end_screen(),
        [(760, "探路"), (985, "BBB")],
        [(770, "BBB"), (990, "CCC")],
    ]
    picker, _driver, strip = _picker(screens)

    with pytest.raises(PresetNotFound):
        picker.pick("查无此预设")

    assert strip.at == 0


# -- 找不到就不点 --------------------------------------------------------------


def test_a_missing_preset_is_refused_rather_than_approximated() -> None:
    """一路拖到右端都没有，就是没有。**不许凑合点一个**。"""
    screens = [_left_end_screen(), [(830, "探路"), (990, "BBB")]]
    picker, driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound, match="CCC"):
        picker.pick("CCC")

    # 只展开与拖动，**没有点任何预设**。
    assert [label for _x, _y, label in driver.clicks] == ["预设条"]


def test_the_refusal_reports_every_screen_it_read() -> None:
    """报错要能看出「拖过了、每屏都读到了什么」，否则实机上只能干瞪眼。

    上一次就是栽在这里：错误只报了一屏，读起来像「游戏里没有这个预设」，
    于是结论下反了——它有，只是没拖到。
    """
    screens = [_left_end_screen(), [(830, "探路"), (990, "BBB")]]
    picker, _driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound) as caught:
        picker.pick("CCC")

    message = str(caught.value)
    for name in ("AAA", "探路", "BBB"):
        assert name in message


def test_it_gives_up_instead_of_dragging_for_ever() -> None:
    """右端夹住了就停，不靠拖满次数。"""
    screens = [_left_end_screen(), [(830, "探路"), (990, "BBB")]]
    picker, driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound):
        picker.pick("CCC")

    rightward = [drag for drag in driver.drags if drag[2] < drag[0]]
    # 两屏：一格拖到右端，再一格读到一样的就收工。
    assert len(rightward) == 2


# -- 合并：跨字要合，跨预设、跨屏都不许合 --------------------------------------


def test_a_name_split_by_ocr_is_merged_and_clicked_in_the_middle() -> None:
    """拆开的两块合成一个名字，点在整段中点——离相邻预设最远。"""
    picker, _driver, _strip = _picker([[(744, "AAA"), (760, "AAA"), (985, "探路")]])

    assert picker.pick("AAA") == 752


def test_a_chinese_name_split_per_character_is_still_found() -> None:
    """本文件里最贵的一条：tesseract 对中文是**按字**分词的。

    实机（2026-08-11）预设条拖到左端后读回来是 `['AAA', '探', '路']`，
    逐词做 `name in text` 于是永远匹配不上 `探路`——bot 链路每一发都倒在
    「找不到预设 探路」，而预设条上明明就有它。链路因此从未真正派出过一发。
    """
    picker, driver, _strip = _picker([[(747, "AAA"), (984, "探"), (994, "路")]])

    assert picker.pick("探路") == 989
    assert (989, PRESET_NAME_ROW_Y, "预设 探路") in driver.clicks


def test_two_distinct_presets_are_never_merged() -> None:
    """合并只能跨字，不能跨预设。

    合过头的代价是点错预设——预设决定送出去多少舰队。实测同名相邻两字差 10px，
    不同预设之间差 237px，所以这里用真实量级构造：`探`/`路` 该合，`AAA` 不该被
    卷进来，于是 `AAA探` 这种拼接出来的名字不存在，找它必须失败。
    """
    picker, _driver, _strip = _picker([[(747, "AAA"), (984, "探"), (994, "路")]])

    with pytest.raises(PresetNotFound, match="AAA探"):
        picker.pick("AAA探")


def test_words_on_two_different_screens_are_never_merged_into_one_name() -> None:
    """跨屏不许合并。

    合并阈值 40px 是在**一屏之内**量出来的（同名相邻两字差 10px，不同预设差
    237px）。跨屏之后 x 的含义变了：下面这两屏里，前一屏最右的 `探`（990）和后
    一屏最左的 `路`（995）只差 5px，任何把两屏的词框倒进同一个池子再按 x 合并的
    写法都会拼出一个根本不存在的 `探路`，然后**点下去**——落点还是算出来的。

    正确结果是两屏都没有 `探路`，抛 `PresetNotFound`，一个预设都不点。
    """
    screens: list[Screen] = [
        [(760, "AAA"), (990, "探")],
        [(995, "路"), (1010, "线")],
    ]
    picker, driver, _strip = _picker(screens)

    with pytest.raises(PresetNotFound, match="探路"):
        picker.pick("探路")

    assert _preset_clicks(driver) == []
