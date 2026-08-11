"""扫描器的会话巡检：认出哪一屏，以及**只在读到按钮时才点**。"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.session_keeper import ScreenState
from evo_helper.tools.scan_coordinates import (
    DISCONNECT_BUTTON,
    DISCONNECT_TEXT_ROI,
    ENTRY_BUTTON_ROI,
    ENTRY_TITLE_ROI,
    NAV_TEXT_ROI,
    START_ROI,
    make_session_keeper,
)


class FakeDriver:
    def __init__(self, texts: dict[tuple[int, int, int, int], str]) -> None:
        self.texts = texts
        self.clicks: list[tuple[int, int, str]] = []

    def capture(self) -> Any:
        return _Frame(self.texts)

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))


class _Frame:
    def __init__(self, texts: dict[tuple[int, int, int, int], str]) -> None:
        self.texts = texts

    def crop(self, box: tuple[int, int, int, int]) -> tuple[dict[Any, str], tuple[int, ...]]:
        return (self.texts, box)  # type: ignore[return-value]


def fake_ocr(crop: Any, *, digits: bool, upscale: int, threshold: int | None = None) -> str:
    texts, box = crop
    return texts.get(box, "")


def keeper_for(
    texts: dict[tuple[int, int, int, int], str],
    *,
    restart_window: Any = None,
):
    """假时钟每问一次就跳 10 秒，等待循环立刻超时——测试不该真的睡两分钟。

    ``restart_window`` 一律传假的：**测试绝不许真的开关窗口。**
    """
    driver = FakeDriver(texts)
    ticks = iter(range(0, 100_000, 10))
    keeper = make_session_keeper(
        driver,  # type: ignore[arg-type]
        fake_ocr,
        clock=lambda: float(next(ticks)),
        sleep=lambda _s: None,
        restart_window=restart_window or (lambda: None),
    )
    return driver, keeper


IN_GAME = {NAV_TEXT_ROI: "行星 舰队 太空舱 商店 联盟"}
ENTRY = {ENTRY_TITLE_ROI: "ETERNAL VOID", ENTRY_BUTTON_ROI: "进入", START_ROI: "START"}
START = {START_ROI: "START"}


def test_a_live_session_is_left_alone() -> None:
    driver, keeper = keeper_for(IN_GAME)
    outcome = keeper.ensure_connected(force=True)
    assert outcome is not None and outcome.ready and not outcome.reconnected
    assert driver.clicks == []


def test_the_entry_page_wins_over_the_start_page_behind_it() -> None:
    """入口页浮在 START 页之上，底下那个 START 仍在画面里。

    先判 START 就会在入口页上去点 START——点的是被浮层盖住的地方，
    结果是既没进游戏、也说不清点到了什么。
    """
    driver, keeper = keeper_for(ENTRY)
    keeper.reconnect()
    assert driver.clicks, "入口页上应该点了「进入」"
    assert driver.clicks[0][2] == "进入"


def test_start_is_clicked_at_the_place_it_was_read() -> None:
    driver, keeper = keeper_for(START)
    keeper.reconnect()
    left, top, right, bottom = START_ROI
    assert driver.clicks[0][:2] == ((left + right) // 2, (top + bottom) // 2)


def test_an_unrecognised_screen_is_never_clicked() -> None:
    # 可能是维护公告或弹窗；乱点会误触派遣、删信或领奖。
    driver, keeper = keeper_for({NAV_TEXT_ROI: "谁知道这是什么"})
    outcome = keeper.reconnect()
    assert outcome.state is ScreenState.UNKNOWN
    assert not outcome.reconnected
    assert driver.clicks == []


#: 两种掉线弹窗共用同一块 ROI、同一行正文，只在文字上分。
RECOVERABLE_DISCONNECT = {DISCONNECT_TEXT_ROI: "连接已断开", NAV_TEXT_ROI: "商店 联盟"}
DEAD_SESSION = {DISCONNECT_TEXT_ROI: "连接已断开，无法重新连接。", NAV_TEXT_ROI: "商店 联盟"}


def test_the_popup_is_read_before_the_nav_bar_behind_it() -> None:
    """弹窗是浮层，底下的导航条还画在画面上，「商店/联盟」照样读得出来。

    先判导航条就会把死会话认成在线，之后每一步点击都石沉大海，全程不报错。
    """
    _driver, keeper = keeper_for(DEAD_SESSION)
    assert keeper._observe() is ScreenState.DEAD_SESSION


def test_a_recoverable_disconnect_is_dismissed_not_restarted() -> None:
    restarts: list[bool] = []
    driver, keeper = keeper_for(
        RECOVERABLE_DISCONNECT, restart_window=lambda: restarts.append(True)
    )

    keeper.reconnect()

    assert restarts == [], "点一下就能回去的时候不该关窗口"
    assert driver.clicks[0][:2] == DISCONNECT_BUTTON


def test_a_dead_session_restarts_the_window_and_never_clicks_the_dialog() -> None:
    """「无法重新连接」= 点掉弹窗也回不去。别在死页面上多留一次点击。"""
    restarts: list[bool] = []
    driver, keeper = keeper_for(DEAD_SESSION, restart_window=lambda: restarts.append(True))

    keeper.reconnect()

    assert restarts == [True]
    assert driver.clicks == []


def test_the_dismiss_action_itself_refuses_a_dead_session() -> None:
    """第二道防线：就算有人绕过守护直接调这个动作，也不许点在死页面上。

    守护本身已经把 DEAD_SESSION 引去关窗重开、根本走不到这里，所以这条判据
    只在「被绕过」时才起作用——而那正是它存在的理由。
    """
    driver, keeper = keeper_for(DEAD_SESSION)

    with pytest.raises(RuntimeError, match="读不到那行字"):
        keeper._dismiss_disconnect()
    assert driver.clicks == []


def test_the_health_check_only_runs_once_per_interval() -> None:
    driver, keeper = keeper_for(IN_GAME)
    assert keeper.ensure_connected() is not None  # 首次必查
    assert keeper.ensure_connected() is None  # 未到点就不查
