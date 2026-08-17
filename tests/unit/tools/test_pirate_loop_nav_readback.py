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
    OK_BUTTON,
    POSITION_FIELD,
    SYSTEM_FIELD,
    SYSTEM_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    SystemNavigator,
)
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop

ORIGIN = Coordinate(4, 277, 15)
#: 同银河、不同恒星系：银河系那一格一个字都没变。
SAME_GALAXY = Coordinate(4, 273, 12)
#: 同恒星系：银河系和恒星系两格都没变。
SAME_SYSTEM = Coordinate(4, 277, 3)
#: 跨银河：三格都得重设。
OTHER_GALAXY = Coordinate(2, 137, 4)

#: 出发星球读回来的样子。`_navigation_bar_values` 交出去的就是这三个字符串。
ORIGIN_READS = ("4", "277", "15")


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
    loop._navigator = SystemNavigator(driver)
    # 切完星球停在新星球地表，所以第一次读标签读不到导航栏；切过视图才读得到。
    labels = iter(["行星 舰队 太空舱 商店 联盟"] + ["银河系 恒星系 行星"] * 8)
    loop._nav_labels = lambda: next(labels)
    loop._goto_planet_surface = lambda: True
    loop.planet_switcher = lambda **_k: _FakeSwitcher(result)
    loop._navigation_bar_values = lambda: reads
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
        loop._navigation_bar_values = lambda: pytest.fail("切换失败还去回读导航栏")

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
        loop._navigation_bar_values = lambda: (order.append("回读"), ORIGIN_READS)[1]

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


class TestReadingTheNavigationBar:
    """`_navigation_bar_values` 自己：每个框各自逐套配方试到读出为止。"""

    def _loop_reading(self, answers: dict[tuple[Any, int, int], str]) -> Any:
        loop = PirateLoop.__new__(PirateLoop)
        loop._reads = []

        def read(roi: Any, *, digits: bool, upscale: int, threshold: int | None = None) -> str:
            assert digits, "值框是纯数字，必须走数字白名单那一档"
            loop._reads.append((roi, upscale, threshold))
            return answers.get((roi, upscale, threshold or 0), "")

        loop._read = read
        return loop

    def test_the_first_recipe_that_reads_wins(self) -> None:
        from evo_helper.game.system_navigator import NAV_VALUE_RECIPES, NAV_VALUE_ROIS

        first = NAV_VALUE_RECIPES[0]
        answers = {
            (roi, first[0], first[1]): value
            for roi, value in zip(NAV_VALUE_ROIS, ("4", "277", "15"), strict=True)
        }
        loop = self._loop_reading(answers)

        assert loop._navigation_bar_values() == ("4", "277", "15")
        assert len(loop._reads) == 3, "第一套就读出来了，不该再试后面几套"

    def test_each_box_falls_through_to_the_next_recipe_on_its_own(self) -> None:
        """**每个框各自换配方**，不是三个框绑在一起。

        实拍上出现过「同一张图，三倍读得出行星、两倍才读得出银河系」——绑在一起
        换的话那一张就整份读空，白白退回三字段。
        """
        from evo_helper.game.system_navigator import NAV_VALUE_RECIPES, NAV_VALUE_ROIS

        first, second = NAV_VALUE_RECIPES[0], NAV_VALUE_RECIPES[1]
        answers = {
            (NAV_VALUE_ROIS[0], second[0], second[1]): "4",
            (NAV_VALUE_ROIS[1], first[0], first[1]): "277",
            (NAV_VALUE_ROIS[2], first[0], first[1]): "15",
        }
        loop = self._loop_reading(answers)

        assert loop._navigation_bar_values() == ("4", "277", "15")

    def test_a_box_no_recipe_can_read_comes_back_empty(self) -> None:
        """读不出就交空串，绝不猜——空串走的是「不确认」那一支。"""
        loop = self._loop_reading({})

        assert loop._navigation_bar_values() == ("", "", "")


def test_the_bot_chain_shares_the_very_same_readback() -> None:
    """`BotLoop` 不许自己抄一份。"""
    from evo_helper.tools.bot_loop import BotLoop

    assert BotLoop._adopt_navigation_bar is PirateLoop._adopt_navigation_bar
    assert BotLoop._navigation_bar_values is PirateLoop._navigation_bar_values
