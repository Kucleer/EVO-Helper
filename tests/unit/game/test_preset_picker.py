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
from evo_helper.game.preset_picker import (
    PRESET_DRAG_WAIT_S,
    PRESET_NAME_ROI,
    PresetNotFound,
    PresetPicker,
    _clickable_hit,
    merged_names,
)

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


# -- 先看打开时这一屏 ----------------------------------------------------------


def _legacy_pick(picker: PresetPicker, name: str) -> int:
    """**改动之前那一版 `pick()` 的原样搬运**，一行都没改。

    留一份参照实现，是为了让「首屏没命中就走老路」这件事有一个不会跟着改动一起
    漂移的对照：下面那条等价性用例比的是两版在**驱动上留下的动作序列**，
    而不是我手抄一串期望值。手抄的期望值改一次代码就得改一次，改着改着就成了
    「按新行为重新誊写」，那时它已经不证明任何东西了。
    """
    from evo_helper.game.pirate_ui import PRESET_MAX_DRAGS, PRESET_NAME_ROW_Y

    picker.expand()
    entries = list(picker.scroll_to_left_end())
    screens: list[list[str]] = []
    previous: list[str] | None = None
    for _attempt in range(PRESET_MAX_DRAGS + 1):
        runs = merged_names(entries)
        screens.append([text for _x, text in runs])
        target = _clickable_hit(runs, name)
        if target is not None:
            picker.driver.click(target, PRESET_NAME_ROW_Y, label=f"预设 {name}")
            picker.driver.wait(PRESET_DRAG_WAIT_S)
            return target
        words = [text for _x, text in entries]
        if previous is not None and words == previous:
            break
        previous = words
        entries = list(picker.scroll_right_once())
    picker.scroll_to_left_end()
    raise PresetNotFound(f"预设条上找不到 {name!r}；从左到右逐屏读到的是 {screens}")


def test_a_preset_on_the_screen_the_strip_opened_at_is_clicked_without_any_drag() -> None:
    """(a) 打开时就在目标那一屏 → **一次都不拖**，直接点。

    「拖到左端夹住」的判据是「往左拖一次之后名字没变」，所以条本来就开在左端时
    也必然白拖一次、白读一屏。实测（生产 `system_log`，2026-08-17 一天 177 次派遣）
    「翻预设条」137 次落在 9–10 秒的最短路径上、只有 40 次是 13–14 秒——
    也就是说这一次白拖是常态，不是例外。省下的是约 12 分钟／天。
    """
    picker, driver, strip = _picker([_left_end_screen(), [(830, "探路"), (990, "BBB")]])

    assert picker.pick("AAA") == 748

    assert driver.drags == [], "打开时就看得见，不该拖任何一次"
    assert _preset_clicks(driver) == [(748, PRESET_NAME_ROW_Y, "预设 AAA")]
    assert strip.at == 0, "条不该被挪动过"


def test_the_shortcut_also_works_when_the_strip_did_not_open_at_the_left_end() -> None:
    """捷径认的是「这一屏有没有」，不是「是不是左端」。

    用户口径（2026-08-18）：「目前首屏预设就是 AAA 和 BBB」——首屏是哪一屏由游戏
    决定，本仓不该假设它一定是左端那一屏。
    """
    screens = [_left_end_screen(), [(830, "探路"), (990, "BBB")]]
    picker, driver, _strip = _picker(screens, at=1)

    assert picker.pick("BBB") == 990

    assert driver.drags == []


def test_missing_on_the_opening_screen_replays_the_old_route_move_for_move() -> None:
    """(b) 首屏没有 → 老路，**驱动上的动作序列与改动前逐字一致**。

    捷径多读的那一屏不是白读：它被当作 `scroll_to_left_end` 的起点传下去，
    所以连 OCR 的次数都没变。这里两边各跑一条同样的假预设条，比的是
    点击与拖动的完整流水。
    """
    screens = [
        _left_end_screen(),
        [(830, "探路"), (990, "中转")],
        [(770, "中转"), (985, "CCC")],
    ]

    new_picker, new_driver, new_strip = _picker([list(screen) for screen in screens], at=1)
    old_picker, old_driver, old_strip = _picker([list(screen) for screen in screens], at=1)

    assert new_picker.pick("CCC") == _legacy_pick(old_picker, "CCC")

    assert new_driver.clicks == old_driver.clicks
    assert new_driver.drags == old_driver.drags
    assert new_strip.at == old_strip.at


def test_a_refusal_reports_exactly_what_it_used_to_report() -> None:
    """没找到那一支同样一字不改：措辞与「逐屏读到的是」那份清单都照旧。

    报错文字是实机排障唯一能看到的东西，捷径不该顺手把它改了。
    """
    screens = [_left_end_screen(), [(830, "探路"), (990, "BBB")]]

    new_picker, _nd, _ns = _picker([list(screen) for screen in screens])
    old_picker, _od, _os = _picker([list(screen) for screen in screens])

    with pytest.raises(PresetNotFound) as new_error:
        new_picker.pick("CCC")
    with pytest.raises(PresetNotFound) as old_error:
        _legacy_pick(old_picker, "CCC")

    assert str(new_error.value) == str(old_error.value)


def test_an_opening_screen_that_reads_blank_is_not_taken_as_absence() -> None:
    """(c) **首屏读空绝不等于「这儿没有」**，也不能当成老路的起点。

    这正是 2026-08-13 通宵事故的形态：一屏读空被当成「这一段没有预设」，于是往右
    拖过头，白跑 145 次约两小时。捷径把「读一屏」提到了最前面，等于给这个坑新开了
    一个入口——读空必须落回老路，而且**不许把那份空清单当作 `scroll_to_left_end`
    的起点**（拿空清单当起点，「拖了一次还是空」会被误判成「已经夹住了」）。

    这里让打开时那一屏（第 1 屏）连读多次都是空，而目标 `BBB` 就在第 0 屏。
    """
    strip = _FlakyStrip(
        [_left_end_screen(), [(830, "探路"), (990, "CCC")]],
        blank_screen=1,
        blank_times=999,
    )
    strip.at = 1
    driver = _Driver(strip)
    picker = PresetPicker(driver=driver, read_names=strip.read)

    assert picker.pick("AAA") == 748

    assert _preset_clicks(driver) == [(748, PRESET_NAME_ROW_Y, "预设 AAA")]
    assert driver.drags, "读空之后必须真的去拖，而不是当场判「没有」"


def test_the_shortcut_never_clicks_into_the_save_button_margin() -> None:
    """(d) 首屏命中但中心 x 落在安全区右边 → 不点，落老路。

    捷径走的必须是同一个 `_clickable_hit`。绕过它的实现会在这里点下去——
    而那个位置上是「+ 保存当前舰队」，点一下就覆盖用户自己维护的预设。
    """
    picker, driver, _strip = _picker([[(760, "AAA"), (SAVE_BUTTON_X, "CCC")]])

    with pytest.raises(PresetNotFound, match="CCC"):
        picker.pick("CCC")

    assert _preset_clicks(driver) == []
    assert driver.drags, "被边距挡下之后要接着走老路，不是就地放弃"


def test_the_shortcut_never_clicks_a_name_that_reads_like_the_save_button() -> None:
    """(e) 首屏读到含「保存」的名字 → 不当候选。

    独立于坐标的第二道闸，捷径同样要过。
    """
    picker, driver, _strip = _picker([[(760, "AAA"), (900, "+保存当前舰队")]])

    with pytest.raises(PresetNotFound, match="保存当前舰队"):
        picker.pick("保存当前舰队")

    assert _preset_clicks(driver) == []


def test_both_routes_are_told_apart_in_the_log() -> None:
    """两条路在结果上一模一样，只有日志能把它们分开。

    没有这一行，「捷径到底有没有生效」在实机上无从查证——而这条改动的**全部收益**
    就是它生效的那些次。
    """
    said: list[str] = []
    strip = _Strip([_left_end_screen(), [(830, "探路"), (990, "BBB")]])
    picker = PresetPicker(driver=_Driver(strip), read_names=strip.read, say=said.append)

    picker.pick("AAA")
    hit = list(said)
    said.clear()
    picker.pick("BBB")

    assert any("不用拖" in line for line in hit)
    assert any("拖到左端从头找" in line for line in said)


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

    ⚠️ 目标**不能**摆在打开时那一屏上：那样会走「先看这一屏」的捷径，一次都不拖，
    这条就变成空断言了。所以中间插一屏，让打开时那一屏读不到 `BBB`。
    """
    screens = [
        _left_end_screen(),
        [(830, "探路"), (990, "中转")],
        [(770, "中转"), (985, "BBB")],
    ]
    picker, driver, _strip = _picker(screens, at=1)

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
    """往左拖的两端一个字不改——那是实机走通的一对，别顺手跟着右拖一起「统一」了。

    ⚠️ 条必须**开在左端以外**：开在左端时「先看这一屏」的捷径会直接点中，一次左拖
    都不发生，这条就无从检查了。
    """
    picker, driver, _strip = _picker([_left_end_screen(), [(830, "探路"), (990, "BBB")]], at=1)

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


# -- 空屏不是证据 --------------------------------------------------------------


class _FlakyStrip(_Strip):
    """和 `_Strip` 一样，但**头几次读某一屏会返回空**——模拟拖动动画没停 / OCR 失手。"""

    def __init__(self, screens: list[Screen], *, blank_screen: int, blank_times: int) -> None:
        super().__init__(screens)
        self._blank_screen = blank_screen
        self._left = blank_times
        self.reads = 0

    def read(self) -> Screen:
        self.reads += 1
        if self.at == self._blank_screen and self._left > 0:
            self._left -= 1
            return []
        return super().read()


def test_a_screen_that_reads_blank_is_read_again_instead_of_believed() -> None:
    """**预设条不可能真的是空的**，所以空结果只能是这一帧没读出来。

    实机 2026-08-13 通宵：预设顺序是 AAA / 探路 / BBB / CCC（用户 2026-08-14 确认），
    而 `pick('BBB')` 逐屏读到的是

        [['AAA', '探路'], [], ['ccc'], ['ccc']]

    第 2 屏正是 BBB 那一屏，读成空、被当成「这儿没有」，于是拖过头，再看到两屏
    相同的 ccc 就判「到右端了」。**这样白跑了 145 次，每次约 50 秒，约两小时**
    ——「18 分钟才派出第一发」「四轮一发没派」全是它。
    """
    strip = _FlakyStrip(
        [[(748, "AAA"), (985, "探路")], [(760, "BBB")], [(760, "CCC")]],
        blank_screen=1,
        blank_times=2,
    )
    driver = _Driver(strip)
    picker = PresetPicker(driver=driver, read_names=strip.read)

    picker.pick("BBB")

    assert _preset_clicks(driver) == [(760, PRESET_NAME_ROW_Y, "预设 BBB")]


def test_a_strip_that_really_reads_blank_every_time_still_gives_up() -> None:
    """重读是有限次的。没有这条对照，「一直重读」也能让上面那条变绿——
    而那意味着一个真的读不出来的画面会把这一发卡死在原地。
    """
    strip = _FlakyStrip([[(748, "AAA")], [(760, "BBB")]], blank_screen=1, blank_times=999)
    picker = PresetPicker(driver=_Driver(strip), read_names=strip.read)

    with pytest.raises(PresetNotFound):
        picker.pick("BBB")


# -- 大小写 --------------------------------------------------------------------


def test_a_name_the_ocr_lowercased_is_still_matched() -> None:
    """实机 2026-08-13：`CCC` 有 118 次被读成 `ccc`，只有 1 次读对。

    大小写敏感的匹配意味着：哪天把某个任务配成 CCC，这条链路一发都派不出去，
    而报出来的是「预设条上找不到 'CCC'」——看上去像游戏里没有这个预设。
    """
    picker, driver, _strip = _picker([[(748, "AAA")], [(760, "ccc")]])

    picker.pick("CCC")

    assert _preset_clicks(driver) == [(760, PRESET_NAME_ROW_Y, "预设 CCC")]


def test_the_unambiguous_bbb_font_misread_is_recovered() -> None:
    """实机截图中 BBB 处于「探路 / CCC」之间，却被读成 BEB。

    首尾两个 B 都识别正确，只有中间的花体 B 被误读；这不是把任意近似名放宽，
    而是只接受三次重复代码的受限误读。精确读到 BBB 时仍必须优先点精确项。
    """
    picker, driver, _strip = _picker([[(748, "AAA"), (985, "探路")], [(760, "BEB"), (990, "CCC")]])

    assert picker.pick("BBB") == 760
    assert _preset_clicks(driver) == [(760, PRESET_NAME_ROW_Y, "预设 BBB")]


def test_an_exact_name_beats_the_bbb_font_misread() -> None:
    picker, driver, _strip = _picker([[(760, "BEB"), (900, "BBB")]])

    assert picker.pick("BBB") == 900
    assert _preset_clicks(driver) == [(900, PRESET_NAME_ROW_Y, "预设 BBB")]


def test_gold_names_survive_a_background_that_defeats_greyscale() -> None:
    """⚠️⚠️ **灰度化在预设名这一行上会瞎掉，而这条 2026-08-15 那一夜代价很大。**

    bot 链路整晚找不到 `BBB`、一发都没派，而 `BBB` 就明明白白印在屏幕上——
    实机量下来，预设条第 4 页上灰度读 `BBB` 是空串（3×/4×/6×/8× 全空）。

    成因是**亮度**：金字 (255,200,0) 灰度约 193，而它压着的蓝底在那一段约 150，
    两者太近。滚到暗一点的位置（第 1 页）灰度就好用——所以这不是「配方错了」，
    是「配方只在一部分滚动位置上成立」。

    金色掩膜按 `red - blue` 判，与背景明暗无关。
    """
    from PIL import Image

    from evo_helper.game.preset_picker import gold_mask

    # 金字压在亮蓝底上：灰度差只有 40 出头，掩膜差是黑白分明。
    gold, bright_blue = (255, 200, 0), (60, 150, 210)
    crop = Image.new("RGB", (4, 2), bright_blue)
    crop.putpixel((1, 1), gold)

    mask = list(gold_mask(crop).getdata())

    assert mask.count(0) == 1, "只有金色那一个像素该被抠成黑"
    assert set(mask) == {0, 255}, "掩膜必须是纯黑白，不留灰度"


def test_the_mask_ignores_the_white_quantity_columns_beside_the_names() -> None:
    """宽 ROI 之所以安全，全靠掩膜把白字数量列滤掉。

    窄 ROI（右界 1000）当初就是为了躲开那些数字，而掩膜下它们根本不在图里——
    所以掩膜那一档可以放心用整条预设条，而那正是它读得出 `BBB` 的原因：
    实机上窄 ROI 里 `BBB` 的像素在（148 个黑点），tesseract 却读成空串，
    因为一张大片空白里只有一个孤零零的词它认不出来。
    """
    from PIL import Image

    from evo_helper.game.preset_picker import gold_mask

    white_number, gold = (240, 240, 240), (255, 200, 0)
    crop = Image.new("RGB", (2, 1), white_number)
    crop.putpixel((1, 0), gold)

    assert list(gold_mask(crop).getdata()) == [255, 0]


class _TwoShotOcr:
    """第一次读空、第二次给字。用来验「灰度读空之后真的还有第二档」。"""

    def __init__(self) -> None:
        self.calls = 0
        self.Output = type("Output", (), {"DICT": "dict"})

    def image_to_data(self, _image: object, **_kwargs: object) -> dict[str, list[object]]:
        self.calls += 1
        if self.calls == 1:  # 灰度那一档：什么都没读到
            return {"text": [""], "left": [0], "width": [0]}
        return {"text": ["BBB"], "left": [540], "width": [69]}


class _ComplementaryOcr:
    """灰度只读到「探路」、金色掩膜才读到右侧 BBB 的同屏实机形状。"""

    Output = type("Output", (), {"DICT": "dict"})

    def __init__(self) -> None:
        self.calls = 0

    def image_to_data(self, _image: object, **_kwargs: object) -> dict[str, list[object]]:
        self.calls += 1
        if self.calls == 1:
            return {"text": ["探路"], "left": [540], "width": [90]}
        return {"text": ["BBB"], "left": [450], "width": [69]}


def test_an_empty_greyscale_reading_falls_through_to_the_mask() -> None:
    """⚠️ **这条钉的是「第二档真的存在」，不是掩膜本身好不好。**

    掩膜函数写对了、但 `name_words` 忘了调它——变异测试当场抓到过这个形状：
    把那一档从配方表里删掉，所有单测掩膜的用例照样全绿。

    而少了这一档的后果就是 2026-08-15 那一夜：bot 链路整晚找不到 `BBB`，
    一发都没派。
    """
    from PIL import Image

    from evo_helper.game.preset_picker import name_words

    ocr = _TwoShotOcr()

    words = name_words(Image.new("RGB", (1920, 917), (60, 150, 210)), ocr)

    assert ocr.calls == 2, "灰度读空之后必须再试一次掩膜"
    assert [text for _x, text in words] == ["BBB"]


def test_a_greyscale_hit_does_not_hide_bbb_in_the_gold_mask() -> None:
    """BBB 位于探路和 CCC 之间时，读到左侧「探路」也不能提前结束 OCR。"""
    from PIL import Image

    from evo_helper.game.preset_picker import name_words

    words = name_words(Image.new("RGB", (1920, 917), (60, 150, 210)), _ComplementaryOcr())

    assert [text for _x, text in words] == ["探路", "BBB"]


def test_a_successful_greyscale_reading_does_not_pay_for_the_mask() -> None:
    """灰度读到左侧名称，也仍要读掩膜中的右侧 BBB。"""
    from PIL import Image

    from evo_helper.game.preset_picker import name_words

    ocr = _TwoShotOcr()
    ocr.calls = 1  # 让第一次就返回有字的那一份

    name_words(Image.new("RGB", (1920, 917), (60, 150, 210)), ocr)

    assert ocr.calls == 3, "同一屏的两档 OCR 都必须跑，避免越过右侧 BBB"
