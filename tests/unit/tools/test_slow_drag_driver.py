"""把 `LiveDriver` 接到 `game.ranking_nav` 上的那层适配器。

`RankingNavigator` 要的不是一个 `drag`，而是 `press` / `move_to` / `release`
三个原语——分步慢拖必须发生在 `game` 层，否则那一层就得反过来 import `tools`
（理由写在 `ranking_nav` 模块头）。而 `LiveDriver` 只有一步式的 `drag`。

这一层薄得几乎没有逻辑，但它握着两样别处握不到的东西：**窗口原点**和
**「手指现在按着」这个状态**。下面每一条钉的都是这两样东西弄错时的后果，
而那些后果没有一个会当场报错——全都是安静地拖歪、或者把鼠标按着不放。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.ranking_nav import RankingNavigator
from evo_helper.tools.scan_coordinates import SlowDragDriver


class _Gui:
    """假的 pyautogui：只记账，不碰真鼠标。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail_on_move = False

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> None:  # noqa: N802 - 照 pyautogui
        if self.fail_on_move:
            raise RuntimeError("急停")
        self.calls.append(("move", (x, y)))

    def mouseDown(self) -> None:  # noqa: N802 - 照 pyautogui
        self.calls.append(("down", None))

    def mouseUp(self) -> None:  # noqa: N802 - 照 pyautogui
        self.calls.append(("up", None))


class _Live:
    """假的 `LiveDriver`。`origin()` 每次返回下一个值，好看出它被读了几次。"""

    def __init__(self, origins: list[tuple[int, int]] | None = None) -> None:
        self._gui = _Gui()
        self._origins = origins or [(7, 52)]
        self.origin_reads = 0
        self.focus_calls = 0
        self.clicks: list[tuple[int, int, str]] = []
        self.waits: list[float] = []

    def focus(self) -> None:
        self.focus_calls += 1

    def origin(self) -> tuple[int, int]:
        value = self._origins[min(self.origin_reads, len(self._origins) - 1)]
        self.origin_reads += 1
        return value

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


def _moves(gui: _Gui) -> list[tuple[int, int]]:
    return [payload for kind, payload in gui.calls if kind == "move"]


def _kinds(gui: _Gui) -> list[str]:
    return [kind for kind, _payload in gui.calls]


# -- 窗口原点 ------------------------------------------------------------------


def test_the_press_lands_in_the_window_not_on_the_screen() -> None:
    """面板坐标要加上窗口原点才是屏幕坐标。

    `ranking_ui` 里的数全是**客户区**坐标（窗口左上角为 0,0），而 pyautogui 动的是
    **屏幕**。少加这一步，导航条那一拖就按在屏幕 (1122, 862) 上——窗口没最大化时
    那可能是任何东西。
    """
    live = _Live([(7, 52)])

    SlowDragDriver(live).press(1122, 862, label="导航条左移")

    assert _moves(live._gui) == [(1129, 914)]


def test_the_drag_stays_in_the_frame_it_pressed_in() -> None:
    """⚠️ **原点在按下时取一次，整趟拖动都用它。**

    `origin()` 走 `client_box(self.window())`，而 `window()` 会在窗口不见时把游戏
    重新拉起来——也就是说这个调用**不便宜、而且可能有副作用**。真正要命的是：
    每一步重取一次，窗口只要在拖动途中动了一下（用户碰了一下、系统弹了个框），
    后半程的落点就换了一套参照系，于是这一拖从中间开始拐弯。

    这里让 `origin()` 每次返回不同的值。移动全部按第一次那个原点算，就说明
    中途没有再读过。
    """
    live = _Live([(7, 52), (500, 500), (900, 900)])
    driver = SlowDragDriver(live)

    driver.press(960, 700)
    driver.move_to(960, 500)
    driver.move_to(960, 300)

    assert live.origin_reads == 1
    assert [y for _x, y in _moves(live._gui)] == [752, 552, 352]


def test_the_next_drag_reads_the_origin_again() -> None:
    """但**下一趟**要重取：两趟之间隔着点击、等待，窗口完全可能已经动了。

    也就是说原点是「一趟拖动」的作用域，不是「一个 driver」的。缓存下来的话，
    第二趟会按第一趟的参照系走——同样是安静地拖歪。
    """
    live = _Live([(7, 52), (10, 60)])
    driver = SlowDragDriver(live)

    driver.press(0, 0)
    driver.release()
    driver.press(0, 0)

    assert live.origin_reads == 2
    assert _moves(live._gui) == [(7, 52), (10, 60)]


def test_a_finished_drag_leaves_no_finger_down() -> None:
    """松完手就得回到「没按着」，否则上面那两条护栏在第一趟之后就失效了。

    忘了清状态的话：多余的 `release()` 会真的发一次 mouseUp——那时候手指已经松了，
    这一下松的是**用户自己正按着的拖动**；而拖完之后走岔了的 `move_to` 会拿着
    上一趟的原点安静地把鼠标移走，而不是像第一趟之前那样被拦下来。
    """
    driver = SlowDragDriver(_Live())
    driver.press(960, 700)
    driver.move_to(960, 500)
    driver.release()
    before = len(driver._driver._gui.calls)

    driver.release()

    assert len(driver._driver._gui.calls) == before  # 第二次松手什么都没发
    with pytest.raises(RuntimeError, match="没有按下"):
        driver.move_to(960, 300)


# -- 手指按着的时候不许做的事 --------------------------------------------------


def test_the_window_is_focused_once_at_the_press_and_never_mid_drag() -> None:
    """⚠️ **按着手指的时候绝不能去抢前台。**

    `LiveDriver.focus()` 抢不到就退避重试，最多 `0.3+0.6+0.9+1.2+1.5 = 4.5` 秒，
    抢不到还会**抛异常**。这两件事发生在按下之后各有各的坏处：

    - 睡 4.5 秒——游戏面板多半会把这一拖判成长按或者干脆超时丢掉。
    - 抛异常——异常从 `move_to` 里出去，`_slow_drag` 的 `finally` 兜得住，
      但代价是这一拖白做。

    所以只在 `press` 里抢一次。这也够了：一趟拖动是几百毫秒的事，
    前台不会在这中间换人。
    """
    live = _Live()
    driver = SlowDragDriver(live)

    driver.press(960, 700)
    driver.move_to(960, 500)
    driver.release()

    assert live.focus_calls == 1


def test_the_button_goes_down_only_after_the_mouse_is_in_place() -> None:
    """先移到位再按下。反过来的话，按下的那一瞬间鼠标还停在上一次的落点上——
    那一下就成了在**别的东西**上按下并拖走。
    """
    live = _Live()

    SlowDragDriver(live).press(1122, 862)

    assert _kinds(live._gui) == ["move", "down"]


# -- 松手：宁可多松一次，不能漏松一次 ------------------------------------------


def test_a_press_that_blew_up_still_gets_released() -> None:
    """⚠️ **这条是不对称风险的那一半。**

    `press` 里 `moveTo` 抛出来（pyautogui 的急停就是从这里抛的）时，按键状态是
    不确定的。多松一次是彻底无害的（没按着的时候 mouseUp 是个空操作），
    漏松一次则是**鼠标一直按着交还给用户**——那时整个桌面都在拖东西。

    所以「我按过了」这个状态要在碰鼠标**之前**就记上，宁可记早了。
    """
    live = _Live()
    live._gui.fail_on_move = True
    driver = SlowDragDriver(live)

    with pytest.raises(RuntimeError):
        driver.press(960, 700)
    live._gui.fail_on_move = False
    driver.release()

    assert _kinds(live._gui) == ["up"]


def test_releasing_without_a_press_does_nothing() -> None:
    """`_slow_drag` 在 `finally` 里松手，而 `press` 在 `try` 外面——`press` 还没碰
    鼠标就失败（抢不到前台）时，松手会在一次都没按下的情况下被调用。

    那时候发 mouseUp 虽然无害，但它会**把用户自己正按着的拖动给松开**。
    没按过就什么都不做。
    """
    live = _Live()

    SlowDragDriver(live).release()

    assert live._gui.calls == []


def test_moving_without_a_press_is_refused() -> None:
    """没有原点就没有参照系。这时候「尽力而为」地按面板坐标去移，等于把鼠标
    甩到屏幕上一个没人算过的地方——认不出的状态不动手，跟这一层别处一个规矩。
    """
    with pytest.raises(RuntimeError, match="没有按下"):
        SlowDragDriver(_Live()).move_to(960, 500)


# -- 接上去真的能用 ------------------------------------------------------------


def test_a_real_navigator_scroll_comes_out_as_one_proper_drag() -> None:
    """**这条才是这个适配器存在的理由**：拿真的 `RankingNavigator` 走一遍。

    实机上「一步到位的 dragTo 会被面板当成点击」，判据是面板收没收到连续的
    mousemove。所以要的形状是 `按下 → 一串移动 → 松开`，而且中间那串必须
    真的有很多下、并且**单调走到终点**。

    这里连 `SCROLL_FROM_Y=700 → SCROLL_TO_Y=300` 的方向一起钉住：把起止点接反了
    的话，榜单会往**上**滚，而每一屏都读得出行、也都和上一屏不同，
    于是滚动看上去一切正常，只是永远到不了第 639 名。
    """
    live = _Live([(7, 52)])
    driver = SlowDragDriver(live)
    screens = [[("a",)], [("b",)]]
    navigator: RankingNavigator[Any] = RankingNavigator(
        driver=driver,
        read_labels=list,
        read_rows=lambda: screens.pop(0) if screens else [("b",)],
        on_military_board=lambda _rows: True,
        say=lambda _message: None,
    )

    navigator.scroll_once()

    kinds = _kinds(live._gui)
    assert kinds[:2] == ["move", "down"]  # 先移到起点，再按下
    assert kinds[-1] == "up"
    assert kinds.count("down") == 1 and kinds.count("up") == 1
    moves = _moves(live._gui)
    assert len(moves) >= 8  # 一串，不是一下
    assert all(abs(x - 967) <= 1 for x, _y in moves)  # 960 + 原点 7，只有抖动那 1px
    ys = [y for _x, y in moves]
    assert ys == sorted(ys, reverse=True)  # 一路向上拖，也就是榜单向下滚
    assert ys[0] == 752 and ys[-1] == 352  # 700+52 → 300+52
