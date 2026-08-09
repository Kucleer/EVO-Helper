"""扫描器的会话巡检：认出哪一屏，以及**只在读到按钮时才点**。"""

from __future__ import annotations

from typing import Any

from evo_helper.game.session_keeper import ScreenState
from evo_helper.tools.scan_coordinates import (
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


def keeper_for(texts: dict[tuple[int, int, int, int], str]):
    """假时钟每问一次就跳 10 秒，等待循环立刻超时——测试不该真的睡两分钟。"""
    driver = FakeDriver(texts)
    ticks = iter(range(0, 100_000, 10))
    keeper = make_session_keeper(
        driver,  # type: ignore[arg-type]
        fake_ocr,
        clock=lambda: float(next(ticks)),
        sleep=lambda _s: None,
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


def test_the_health_check_only_runs_once_per_interval() -> None:
    driver, keeper = keeper_for(IN_GAME)
    assert keeper.ensure_connected() is not None  # 首次必查
    assert keeper.ensure_connected() is None  # 未到点就不查
