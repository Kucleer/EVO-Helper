"""扫描开工时读到 `UNKNOWN`，先关浮层再判死。

事故（2026-08-11 02:38，调度器跑着）：上一条链路把游戏停在一个面板上，扫描开工
时 `classify_screen` 读不到底部导航条，给出 UNKNOWN，于是 1.5 秒就

    会话不可用：unrecognised screen；安全停止

并返回 1。连着三次，调度器把扫描整条**自动停用**。日志里只有三行同样的字，
而会话好好的——那一晚海盗和 bot 都在正常派遣。

关键事实：**真掉线落不到 UNKNOWN。** `classify_screen` 把登录序列判成 ENTRY /
START / DISCONNECTED，各有各的分支。所以 UNKNOWN 基本只剩「浮层压着导航条」
这一种解释。
"""

from __future__ import annotations

from evo_helper.game.session_keeper import ReconnectOutcome, ScreenState
from evo_helper.tools.scan_coordinates import (
    OVERLAY_CLOSE_ATTEMPTS,
    OVERLAY_CLOSE_BUTTON,
    dismiss_overlays_if_unrecognised,
)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def wait(self, _seconds: float) -> None:
        pass


class _Keeper:
    def __init__(self, outcomes: list[ReconnectOutcome]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome:
        self.calls += 1
        return self._outcomes.pop(0) if self._outcomes else _outcome(ScreenState.UNKNOWN)


def _outcome(state: ScreenState) -> ReconnectOutcome:
    return ReconnectOutcome(state, reconnected=False, detail=state.value)


def test_a_recognised_screen_is_left_alone() -> None:
    """会话正常时一下都不许点——稳态是绝大多数情况。"""
    driver, keeper = _Driver(), _Keeper([])

    result = dismiss_overlays_if_unrecognised(_outcome(ScreenState.IN_GAME), driver, keeper)

    assert result.state is ScreenState.IN_GAME
    assert driver.clicks == []
    assert keeper.calls == 0


def test_an_overlay_is_closed_and_the_session_rechecked() -> None:
    """本文件的重点：UNKNOWN 先按「有浮层」处理。"""
    driver = _Driver()
    keeper = _Keeper([_outcome(ScreenState.IN_GAME)])

    result = dismiss_overlays_if_unrecognised(_outcome(ScreenState.UNKNOWN), driver, keeper)

    assert result.state is ScreenState.IN_GAME
    assert driver.clicks == [(*OVERLAY_CLOSE_BUTTON, "关闭面板")]


def test_a_stacked_overlay_takes_more_than_one_click() -> None:
    """列表 → 详情这种套了两层的，第一下只退回列表。"""
    driver = _Driver()
    keeper = _Keeper([_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.IN_GAME)])

    result = dismiss_overlays_if_unrecognised(_outcome(ScreenState.UNKNOWN), driver, keeper)

    assert result.state is ScreenState.IN_GAME
    assert len(driver.clicks) == 2


def test_it_gives_up_instead_of_clicking_forever() -> None:
    """关不掉就交还 UNKNOWN，由调用方停止——可能是维护公告或界面改版。"""
    driver = _Driver()
    keeper = _Keeper([])

    result = dismiss_overlays_if_unrecognised(_outcome(ScreenState.UNKNOWN), driver, keeper)

    assert result.state is ScreenState.UNKNOWN
    assert len(driver.clicks) == OVERLAY_CLOSE_ATTEMPTS


def test_a_login_screen_is_handed_straight_back() -> None:
    """真掉线不归这里管：ENTRY / START / DISCONNECTED 走守护自己的入口序列，
    在这里乱点关闭键只会把那条路搅乱。"""
    for state in (ScreenState.ENTRY, ScreenState.START, ScreenState.DISCONNECTED):
        driver, keeper = _Driver(), _Keeper([])

        result = dismiss_overlays_if_unrecognised(_outcome(state), driver, keeper)

        assert result.state is state
        assert driver.clicks == []


def test_a_throttled_check_passes_through() -> None:
    """守护按时间节流时返回 None，那不是「认不出」。"""
    driver, keeper = _Driver(), _Keeper([])

    assert dismiss_overlays_if_unrecognised(None, driver, keeper) is None
    assert driver.clicks == []
