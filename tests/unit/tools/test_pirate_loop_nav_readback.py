"""切完出发星球之后回读导航栏，让下一个目标少设几个字段。

## 这是什么

`ensure_origin_planet` 原先切完星球无条件 `invalidate()`，于是**下一个目标的
`goto` 三个字段全设**——哪怕出发星是 `4:277:15`、目标是 `4:273:12`，那个银河系
`4` 一个字都没变。实测（生产 `system_log`，2026-08-17 一天 177 次派遣）37 次
三字段导航里 33 次紧跟在「出发星球：切到 …」之后，而一个字段是 6.6 秒。

## 这里守的是什么

**省字段永远只能建立在回读之上。** 2026-08-11 那次事故（一次「设恒星系」落到
银河系框上，136 被截成 9，缓存与导航栏分岔，连续 44 个目标核对全不过、13 分钟
一发没派）换来的规矩是「缓存里只放回读确认过的坐标」，这条改动不许把它松开：
读通了才认，读不通照旧三个字段全设。**方向永远是「拿不准就多设」。**

⚠️ 全程不碰游戏：驱动是假的，切换器是假的，导航栏读数由用例给。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.game.planet_list import SwitchResult
from evo_helper.game.system_navigator import (
    GALAXY_FIELD,
    NAV_VALUE_RECIPES,
    NAV_VALUE_ROIS,
    OK_BUTTON,
    POSITION_FIELD,
    SYSTEM_FIELD,
    SYSTEM_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    SystemNavigator,
)
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import LoopOptions, NavBarReading, Outcome, PirateLoop

ORIGIN = Coordinate(4, 277, 15)
#: 同银河、不同恒星系：银河系那一格一个字都没变。
SAME_GALAXY = Coordinate(4, 273, 12)
#: 同恒星系：银河系和恒星系两格都没变。
SAME_SYSTEM = Coordinate(4, 277, 3)
#: 跨银河：三格都得重设。
OTHER_GALAXY = Coordinate(2, 137, 4)

#: 出发星球读回来的样子。`_read_navigation_bar` 汇出去的就是这三个字符串。
ORIGIN_READS = ("4", "277", "15")


def _reading(values: tuple[str, str, str]) -> NavBarReading:
    """把三个汇总值包成 `NavBarReading`；原始读数这里不重要，填成一致的一列。"""
    return NavBarReading(values=values, reads=tuple((value,) for value in values))


class _FakeScanDriver:
    """`SystemNavigator` 要的最小操作面。记下点了哪儿、打了什么。"""

    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.actions.append(("click", (x, y)))

    def type_number(self, value: int) -> None:
        self.actions.append(("type", value))

    def capture(self) -> Any:
        return object()

    def wait(self, seconds: float) -> None:
        del seconds


class _FakeSwitcher:
    def __init__(self, result: SwitchResult = SwitchResult.SWITCHED) -> None:
        self._result = result
        self.asked: list[Coordinate] = []

    def switch_to(self, target: Coordinate) -> SwitchResult:
        self.asked.append(target)
        return self._result


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reads: tuple[str, str, str] = ORIGIN_READS,
    result: SwitchResult = SwitchResult.SWITCHED,
) -> tuple[Any, _FakeScanDriver, list[str]]:
    """接一条真的 `SystemNavigator`，其余全是假的。

    ⚠️ **导航器必须是真货。** 这里要数的是「下一个目标点了几个字段」，而那个判断
    整个住在 `SystemNavigator.goto` 里；换成假导航器就只能断言「调没调 confirm」，
    那是在检查实现而不是检查收益。

    ⚠️ **`_require_system_view` 也走真的那条。** 它在切完星球之后必然要切视图，
    而 `ensure_system_view` 内部自己会 `invalidate()`——回读放在它前面就会被抹掉。
    用例要能看见这个顺序，桩掉它就看不见了。
    """
    monkeypatch.setattr(module, "say", lambda _message: None)

    driver = _FakeScanDriver()
    said: list[str] = []
    monkeypatch.setattr(module, "say", said.append)

    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=False, attack=True, origin=ORIGIN)
    loop._outcome = Outcome()
    loop._current_planet = None
    loop._driver = driver
    # 两个证据预算各自独立：整帧缩略图（排障用）与值框裁片（标定用）。
    loop._nav_readback_dumps = 0
    loop._nav_value_crop_shapes = set()
    loop._navigator = SystemNavigator(driver)
    # 切完星球停在新星球地表，所以第一次读标签读不到导航栏；切过视图才读得到。
    labels = iter(["行星 舰队 太空舱 商店 联盟"] + ["银河系 恒星系 行星"] * 8)
    loop._nav_labels = lambda: next(labels)
    loop._goto_planet_surface = lambda: True
    loop.planet_switcher = lambda **_k: _FakeSwitcher(result)
    loop._read_navigation_bar = lambda: _reading(reads)
    return loop, driver, said


def _fields_touched(driver: _FakeScanDriver) -> list[tuple[int, int]]:
    """点过的字段框（去掉 OK 与视图菜单那两下）。"""
    menu = {OK_BUTTON, VIEW_MENU_BUTTON, SYSTEM_VIEW_BUTTON}
    return [
        payload
        for kind, payload in driver.actions
        if kind == "click" and payload not in menu  # type: ignore[comparison-overlap]
    ]


def _goto_after_switch(
    monkeypatch: pytest.MonkeyPatch,
    target: Coordinate,
    *,
    reads: tuple[str, str, str] = ORIGIN_READS,
) -> list[tuple[int, int]]:
    loop, driver, _said = _loop(monkeypatch, reads=reads)

    assert loop.ensure_origin_planet() is True

    driver.actions.clear()
    loop._navigator.goto(target)
    return _fields_touched(driver)


class TestFieldsSavedAfterASuccessfulReadback:
    def test_a_target_in_the_origin_galaxy_only_sets_two_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(a) 回读通过 → 同银河的下一个目标不再重设银河系。

        `4:277:15` → `4:273:12`：那个 `4` 一个字都没变，重设它是白花 6.6 秒。
        """
        assert _goto_after_switch(monkeypatch, SAME_GALAXY) == [SYSTEM_FIELD, POSITION_FIELD]

    def test_a_target_in_the_origin_system_only_sets_one_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(b) 顺带白捡的一档：目标和出发星同恒星系，连恒星系那一格也省了。"""
        assert _goto_after_switch(monkeypatch, SAME_SYSTEM) == [POSITION_FIELD]

    def test_a_target_in_another_galaxy_still_sets_all_three(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(d) 跨银河仍旧三个字段全设——省的是**没变的**那些格，不是随便省。"""
        assert _goto_after_switch(monkeypatch, OTHER_GALAXY) == [
            GALAXY_FIELD,
            SYSTEM_FIELD,
            POSITION_FIELD,
        ]


class TestAReadbackThatDidNotConfirm:
    @pytest.mark.parametrize(
        ("reads", "why"),
        [
            (("", "", ""), "三个框都读不出"),
            (("4", "277", ""), "行星那一格读空"),
            (("4", "27", "15"), "恒星系读成了别的数"),
            (("9", "277", "15"), "银河系读成了别的数"),
        ],
    )
    def test_every_field_is_set_again(
        self, monkeypatch: pytest.MonkeyPatch, reads: tuple[str, str, str], why: str
    ) -> None:
        """(c) **回读没通过 → 三个字段全设**，也就是这条改动之前的行为。

        读不出与读出别的坐标一律走同一支。这是整份文件的承重墙：省字段只许建立在
        证据上，`{why}` 都不是证据。放松成「读出一部分就认」的那一刻，
        2026-08-11 那份错记忆就又有地方长出来了。
        """
        del why
        assert _goto_after_switch(monkeypatch, SAME_GALAXY, reads=reads) == [
            GALAXY_FIELD,
            SYSTEM_FIELD,
            POSITION_FIELD,
        ]

    def test_the_cache_is_left_empty_rather_than_half_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _driver, _said = _loop(monkeypatch, reads=("4", "277", ""))

        loop.ensure_origin_planet()

        assert loop._navigator.current is None

    def test_a_switch_that_never_succeeded_does_not_get_a_readback_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """切都没切成就别谈回读——那时游戏停在哪颗星球上根本不知道。"""
        loop, _driver, _said = _loop(monkeypatch, result=SwitchResult.UNCONFIRMED)
        loop._read_navigation_bar = lambda: pytest.fail("切换失败还去回读导航栏")

        assert loop.ensure_origin_planet() is False
        assert loop._navigator.current is None


class TestTheOrderTheReadbackHappensIn:
    """⚠️ **回读必须排在 `_require_system_view` 之后。**

    `ensure_system_view` 一旦需要切视图，它自己就 `invalidate()` 了。回读排在前面
    的话，刚记下的那份确认会被当场抹掉——**功能一次都不会生效，而且一声不响**：
    日志照样打「导航栏回读 … 确认停在 …」，下一个目标却仍旧三个字段全设。
    """

    def test_the_view_is_switched_before_the_nav_bar_is_read(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        loop, _driver, _said = _loop(monkeypatch)
        order: list[str] = []
        labels = iter(["行星 舰队 太空舱 商店 联盟", "银河系 恒星系 行星"])

        def read_labels() -> str:
            order.append("读导航栏标签")
            return next(labels)

        loop._nav_labels = read_labels
        loop._read_navigation_bar = lambda: (order.append("回读"), _reading(ORIGIN_READS))[1]

        assert loop.ensure_origin_planet() is True

        assert order[-1] == "回读", f"回读必须最后发生，实际顺序 {order}"
        assert "读导航栏标签" in order

    def test_the_confirmation_survives_the_view_switch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """把回读挪到 `_require_system_view` 前面，这条就会变红。

        断言的是**结果**（缓存里最后留下了什么），不是调用顺序——顺序断言可以靠
        「顺序对但被抹掉了」蒙混过去，这条不行。
        """
        loop, driver, _said = _loop(monkeypatch)

        loop.ensure_origin_planet()

        assert loop._navigator.current == ORIGIN
        # 视图真的切过：否则「顺序无所谓」这件事就没被验到。
        assert (VIEW_MENU_BUTTON, SYSTEM_VIEW_BUTTON) == tuple(
            payload
            for kind, payload in driver.actions
            if kind == "click" and payload in {VIEW_MENU_BUTTON, SYSTEM_VIEW_BUTTON}
        )


class TestTheLogSaysWhichWayItWent:
    """两条支路在库里必须分得开：出事时只有 `system_log` 拿得到。"""

    def test_a_confirmed_readback_says_what_it_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop, _driver, said = _loop(monkeypatch)

        loop.ensure_origin_planet()

        assert any("确认停在 4:277:15" in line for line in said), said

    def test_a_failed_readback_says_what_it_read_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读不出也要把读到的原样说出去。

        ⚠️ 只说「回读失败」是不够的：这个仓库出过「日志说假话比不说更糟」的事故，
        而「三个框读成什么」正是下次校准 ROI / 配方的唯一线索。
        """
        loop, _driver, said = _loop(monkeypatch, reads=("4", "27", "15"))

        loop.ensure_origin_planet()

        assert any("'27'" in line and "对不上" in line for line in said), said

    def test_a_mismatch_that_looks_like_a_dropped_digit_says_so(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ 「漏了位」和「导航栏真在别处」在库里必须分得开。

        生产 2026-08-18 那 28 次全是漏位——用户当场核对过「实际页面是切回去了」，
        可日志只说「对不上」，于是两天都没人往「是读错了」这个方向看。
        """
        loop, _driver, said = _loop(monkeypatch, reads=("4", "77", "15"))

        loop.ensure_origin_planet()

        assert any("疑似把 '277' 漏了位" in line for line in said), said

    def test_a_real_mismatch_is_not_called_a_dropped_digit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """反过来也要成立：`166` 不是 `277` 漏了位，不许这么写。

        判据说假话比不说更糟——把真的「导航栏在别处」说成「读错了」，会把排障
        引到完全相反的方向去。
        """
        loop, _driver, said = _loop(monkeypatch, reads=("4", "166", "15"))

        loop.ensure_origin_planet()

        assert any("读作 '166'，期望 '277'" in line for line in said), said
        assert not any("漏了位" in line for line in said), said


class TestTheEvidenceThatGoesIntoTheDatabase:
    """⚠️ **这一整类是补上来的：缺了它，这个缺陷藏了 28 轮没人发现。**

    上线以来 28 次回读全部对不上，而 `payload_json` 是 `{}`、一帧都没留——
    日志里只有汇总后的 `('4','77','15')`，说不出「哪一套配方给的、别的配方读出了
    什么」。CLAUDE.md 的判据是「出事时能不能只靠库里的日志定位」，当时不能。
    """

    def _records(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
        records: list[tuple[str, str, dict[str, Any]]] = []
        monkeypatch.setattr(
            module,
            "record_system_log",
            lambda level, source, message, payload=None: records.append(
                (level, message, dict(payload or {}))
            ),
        )
        monkeypatch.setattr(module, "thumbnail_base64", lambda _image: "PNG")
        return records

    def test_a_mismatch_records_every_recipe_read_for_every_box(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """三个框 × 每套配方的原始读数，一个都不许省。"""
        records = self._records(monkeypatch)
        loop, _driver, _said = _loop(monkeypatch)
        loop._read_navigation_bar = lambda: NavBarReading(
            values=("4", "77", "15"), reads=(("4", "4"), ("77", "277"), ("15", "15"))
        )

        loop.ensure_origin_planet()

        assert len(records) == 1, records
        level, message, payload = records[0]
        assert level == "WARNING"
        assert "导航栏回读对不上" in message
        assert payload["expected"] == "4:277:15"
        assert payload["adopted"] == ["4", "77", "15"]
        assert payload["reads"] == {
            "galaxy": ["4", "4"],
            "system": ["77", "277"],
            "position": ["15", "15"],
        }
        assert "漏了位" in payload["verdict"]

    def test_a_confirmed_readback_writes_no_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读通了就不该往库里塞证据——每轮一条 WARNING 会把真故障淹掉。"""
        records = self._records(monkeypatch)
        loop, _driver, _said = _loop(monkeypatch)

        loop.ensure_origin_planet()

        assert records == []

    def test_the_frame_is_capped_but_the_text_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ **文字每次都记，图封顶。**

        文字不限流：这一支一轮最多触发一次，而每出现一次就等于白付两次字段输入。
        图封顶：几张几乎一样的截图对定位没有增量，理由同 `MAX_COORD_DUMPS`。
        """
        records = self._records(monkeypatch)
        loop, _driver, _said = _loop(monkeypatch, reads=("4", "77", "15"))

        rounds = loop.MAX_NAV_READBACK_FRAMES + 2
        for _ in range(rounds):
            loop._current_planet = None
            loop._adopt_navigation_bar(ORIGIN)

        assert len(records) == rounds, "文字不许限流"
        framed = [payload for _l, _m, payload in records if "thumbnail_png_base64" in payload]
        assert len(framed) == loop.MAX_NAV_READBACK_FRAMES


def _synthetic_frame(digits: tuple[int, int, int]) -> Any:
    """一张 1920×917 的假整帧：每个值框里画 N 个白方块，当作 N 位数字。

    ⚠️ 合成而不是用实拍：这个文件跑在 CI 里，而**实拍一张都不许进 Git**
    （`.gitignore` 第二段：公开仓库，值框上就是坐标）。方块之间留 3 列空白，
    与实测的字间距 1–3 列同量级。

    真像素上的标定另有其处：`tests/integration/vision/test_nav_bar_values_live.py`。
    """
    from PIL import Image, ImageDraw

    frame = Image.new("RGB", (1920, 917), (0, 0, 0))
    pen = ImageDraw.Draw(frame)
    for count, roi in zip(digits, NAV_VALUE_ROIS, strict=True):
        for index in range(count):
            left = roi[0] + 20 + index * 11
            pen.rectangle([left, roi[1] + 8, left + 7, roi[3] - 8], fill=(255, 255, 255))
    return frame


class TestReadingTheNavigationBar:
    """`_read_navigation_bar` 自己：一帧上跑完全部配方，再交给 `agreed_value` 汇总。"""

    def _loop_reading(self, answers: dict[tuple[Any, int, int, bool], str]) -> Any:
        """接一个假的取字函数，记下每一次读屏用的 (ROI, 配方)。"""
        loop = PirateLoop.__new__(PirateLoop)
        loop._reads = []
        loop._captures = 0

        def read(
            roi: Any,
            *,
            digits: bool,
            upscale: int,
            threshold: int | None = None,
            tight: bool = False,
        ) -> str:
            assert digits, "值框是纯数字，必须走数字白名单那一档"
            loop._reads.append((roi, upscale, threshold, tight))
            return answers.get((roi, upscale, threshold or 0, tight), "")

        def frame_reader() -> Any:
            loop._captures += 1
            return _synthetic_frame(loop._digits), read

        loop._frame_reader = frame_reader
        #: 这一帧上每个框画几位数字。⚠️ **位数判据是真跑的，不是桩掉的**——
        #: 用例给的是像素，`digits_on_screen` 自己去数。桩掉它就等于把这一步
        #: 从「接进去了没有」的检查里摘出去，而这个仓刚为同一类漏子付过账。
        loop._digits = (1, 3, 2)
        return loop

    def _all_recipes(self, values: tuple[str, str, str]) -> dict[tuple[Any, int, int, bool], str]:
        """每个框在**每一套**配方下都读出同一个值。"""
        return {
            (roi, upscale, threshold, tight): value
            for roi, value in zip(NAV_VALUE_ROIS, values, strict=True)
            for upscale, threshold, tight in NAV_VALUE_RECIPES
        }

    def test_every_recipe_runs_even_after_one_of_them_reads_something(self) -> None:
        """⚠️ **这条就是缺陷本体。**

        老实现是「第一套读出非空就采纳，后面几套不跑」。生产上 `(3,170)` 把 `277`
        读成 `77`——非空、一票通过、后两套根本没机会说话，于是 28 次回读 28 次
        对不上。现在每个框都必须把全部配方跑完，汇总权交给 `agreed_value`。
        """
        loop = self._loop_reading(self._all_recipes(ORIGIN_READS))

        assert loop._read_navigation_bar().values == ORIGIN_READS
        assert len(loop._reads) == len(NAV_VALUE_ROIS) * len(NAV_VALUE_RECIPES)

    def test_all_fifteen_reads_come_off_one_single_frame(self) -> None:
        """三个框要当成一个坐标一起判定，就不能来自三个不同时刻的画面。

        顺带也是性能判据：`_read` 每调一次重截一张图，15 次读屏就是 15 张。
        """
        loop = self._loop_reading(self._all_recipes(ORIGIN_READS))

        loop._read_navigation_bar()

        assert loop._captures == 1

    def test_the_recipes_are_tallied_per_box_not_across_boxes(self) -> None:
        """**每个框各自汇总**，不是三个框绑在一起。

        实拍上一张图里三个框的难度并不一样（`137` 每套配方都读得对，`12` 只有
        两套读得出）。绑在一起就会因为一个框读不出而把整份判成读不出。
        """
        answers = self._all_recipes(ORIGIN_READS)
        # 行星那个框只有前两套读得出，其余读空——够票，照样该汇出 "15"。
        for upscale, threshold, tight in NAV_VALUE_RECIPES[2:]:
            answers.pop((NAV_VALUE_ROIS[2], upscale, threshold, tight))
        loop = self._loop_reading(answers)

        assert loop._read_navigation_bar().values == ORIGIN_READS

    def test_one_recipe_backed_by_the_digit_count_is_enough(self) -> None:
        """⚠️⚠️ **一票 + 位数对得上 = 采纳。这是 2026-08-25 位数判据带来的改变。**

        从前这里断言「一票一律不算数」。那条规矩挡的是老实现「第一套读出什么就是
        什么」，方向没错，但它把生产上 **134 个格子**一起挡掉了：

            真值 261 ← ['261', '26', '26', '6', '61']

        `261` 只有一票，够票的是截断的 `26`——于是每次都交空串。而屏上明明白白是
        3 位数字。

        位数是**独立于 OCR 的第二个证人**：`26` 和 `6` 因为位数不符当场出局，
        剩下唯一的 `261` 就不再是「孤证」了。两份互不依赖的证据，够。

        ⚠️ **孤证还得有旁证。** 这里照生产那一格的形状造：一套读出完整的 `277`，
        其余读成 `77`、`7` —— 它们**都能解释成 `277` 漏了字**，说的是同一件事，
        只是各自少看见几位。其余读数一个字都没有时不算旁证，由下一条钉。
        """
        answers = self._all_recipes(ORIGIN_READS)
        truncated = ["77", "7", "77", "7"]
        for (upscale, threshold, tight), text in zip(NAV_VALUE_RECIPES[1:], truncated):
            answers[(NAV_VALUE_ROIS[1], upscale, threshold, tight)] = text
        loop = self._loop_reading(answers)

        assert loop._read_navigation_bar().values == ORIGIN_READS

    def test_a_lone_read_with_nothing_backing_it_is_still_refused(self) -> None:
        """⚠️⚠️ **孤证 + 没有旁证 = 交空串**，哪怕位数对得上。

        实拍上撞见的那一格：真值 `9`，五套里只有一套吐出 `3`，其余全空。位数数出
        1 位、`3` 也是 1 位 —— 只看位数就会采纳它，而**没有任何东西支持那个 `3`**。

        这一条是「一票就够」那条放宽的另一半闸。第一版没有它，当场在实拍语料上
        多出一个读错。
        """
        answers = self._all_recipes(ORIGIN_READS)
        for upscale, threshold, tight in NAV_VALUE_RECIPES:
            answers.pop((NAV_VALUE_ROIS[0], upscale, threshold, tight), None)
        first = NAV_VALUE_RECIPES[0]
        answers[(NAV_VALUE_ROIS[0], first[0], first[1], first[2])] = "3"
        loop = self._loop_reading(answers)

        assert loop._read_navigation_bar().values == ("", "277", "15")

    def test_a_lone_read_whose_length_contradicts_the_screen_is_refused(self) -> None:
        """⚠️ 反过来：孤证 + **位数对不上** —— 交空串。

        这一条和上一条是一对，缺了它「一票就够」会退化成无条件的一票通过，
        也就是 2026-08-18 那个缺陷本体。

        构造：屏上恒星系框是 3 位（`loop._digits` 的中间那个），而唯一读出来的
        是两位的 `27`。位数这个证人明说「你只看见了一部分」。
        """
        answers = self._all_recipes(ORIGIN_READS)
        for upscale, threshold, tight in NAV_VALUE_RECIPES[1:]:
            answers.pop((NAV_VALUE_ROIS[1], upscale, threshold, tight))
        first = NAV_VALUE_RECIPES[0]
        answers[(NAV_VALUE_ROIS[1], first[0], first[1], first[2])] = "27"
        loop = self._loop_reading(answers)

        assert loop._read_navigation_bar().values == ("4", "", "15")

    def test_the_digit_count_kills_a_unanimous_but_truncated_read(self) -> None:
        """⚠️⚠️ **五套一致地少读一位，位数判据照样否决。**

        生产 98 次的那一格：真值 `117`，四套读成 `7`、一套读空。票数上它是
        压倒性的一致，裁决规则里没有任何东西看得出屏上还有两位——**老规则和窄化
        之后的规则都交出 `7`**，一个错的坐标。

        今天它没闯祸只因为调用方拿它和出发星球比、`7 ≠ 117` 就作废了。可万一哪天
        出发星球真是 `8:7:6` 而导航栏停在 `8:117:6`，这一格会假确认，接着三格全对
        就被采纳——正是 `SystemNavigator` 类注释里 136→9 那次事故的形状。

        位数判据是唯一堵得住它的东西：屏上 3 位，`7` 是 1 位，出局。
        """
        answers = self._all_recipes(ORIGIN_READS)
        for upscale, threshold, tight in NAV_VALUE_RECIPES:
            answers[(NAV_VALUE_ROIS[1], upscale, threshold, tight)] = "7"
        loop = self._loop_reading(answers)

        assert loop._read_navigation_bar().values == ("4", "", "15")

    def test_a_box_no_recipe_can_read_comes_back_empty(self) -> None:
        """读不出就交空串，绝不猜——空串走的是「不确认」那一支。"""
        loop = self._loop_reading({})

        assert loop._read_navigation_bar().values == ("", "", "")

    def test_the_raw_reads_are_kept_alongside_the_verdict(self) -> None:
        """原始读数必须原样带出来——它是排障时唯一能分开「画面不对」和「读错」的东西。"""
        loop = self._loop_reading(self._all_recipes(ORIGIN_READS))

        reading = loop._read_navigation_bar()

        assert len(reading.reads) == len(NAV_VALUE_ROIS)
        assert all(len(row) == len(NAV_VALUE_RECIPES) for row in reading.reads)
        assert reading.reads[1] == ("277",) * len(NAV_VALUE_RECIPES)


def test_the_bot_chain_shares_the_very_same_readback() -> None:
    """`BotLoop` 不许自己抄一份。"""
    from evo_helper.tools.bot_loop import BotLoop

    assert BotLoop._adopt_navigation_bar is PirateLoop._adopt_navigation_bar
    assert BotLoop._read_navigation_bar is PirateLoop._read_navigation_bar
