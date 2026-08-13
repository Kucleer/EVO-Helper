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
    restart_if_still_unusable,
)


class _Driver:
    def __init__(self) -> None:
        self.clicks: list[tuple[int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def wait(self, _seconds: float) -> None:
        pass


class _Keeper:
    def __init__(
        self,
        outcomes: list[ReconnectOutcome],
        *,
        after_restart: ReconnectOutcome | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._after_restart = after_restart
        self.calls = 0
        self.restarts: list[str] = []

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome:
        self.calls += 1
        return self._outcomes.pop(0) if self._outcomes else _outcome(ScreenState.UNKNOWN)

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        """默认「重开也没救回来」——配额耗尽时真实现返回的正是这个形状。"""
        self.restarts.append(reason)
        return self._after_restart or _outcome(ScreenState.UNKNOWN)


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


# -- 阶梯最后一级：关窗重开 -----------------------------------------------------
#
# **上一轮没能正常收尾是常态不是意外**：进程被强杀、断电、强制重启、用户点了任务
# 管理器，都会留下这种状态。实测 `taskkill /F /T` 杀 runner 时把 Chrome 一起收走了
# （它是 `start-console.bat` 的子进程），所以下一轮面对的可能是「窗口压根不存在」
# ——那一档由 `LiveDriver.window()` → `ensure_game_window()` 兜住；这里兜的是
# 「窗口在、画面救不回来」。


def test_a_usable_session_is_never_restarted() -> None:
    """**每一级只在上一级失败之后才走。**

    会话好好的时候关窗重开是纯粹的破坏：窗口凭空关掉又开一次，还白吃掉一次
    3 次/小时的配额。
    """
    keeper = _Keeper([])

    result = restart_if_still_unusable(_outcome(ScreenState.IN_GAME), keeper)

    assert result.state is ScreenState.IN_GAME
    assert keeper.restarts == []


def test_a_screen_that_survives_the_overlay_rung_gets_the_window_restarted() -> None:
    """关浮层都救不回来，才轮到关窗重开。

    这一级**不在认不出的画面上点任何东西**：只送一个 `WM_CLOSE` 给游戏窗口那个
    句柄（等同用户点右上角 ×），再由 `ensure_game_window` 拉一个新的，之后仍旧走
    判据驱动的入口序列。
    """
    keeper = _Keeper([], after_restart=_outcome(ScreenState.IN_GAME))

    result = restart_if_still_unusable(_outcome(ScreenState.UNKNOWN), keeper)

    assert result.state is ScreenState.IN_GAME
    assert len(keeper.restarts) == 1


def test_an_exhausted_restart_budget_stops_instead_of_restarting_again() -> None:
    """**预算耗尽就停，不是接着重启。**

    服务端维护时每次巡检都会撞到这一屏；没有上限就成了「每 10 分钟关一次
    Chrome 再开一次」，一直折腾到有人来看。配额耗尽时 `restart_and_reenter`
    返回一个 `ready` 为假的结局，调用方据此安全停止。
    """
    keeper = _Keeper([])

    result = restart_if_still_unusable(_outcome(ScreenState.UNKNOWN), keeper)

    assert not result.ready
    assert len(keeper.restarts) == 1, "重开只许试一次，不许成环"


def test_a_throttled_check_is_not_a_reason_to_restart() -> None:
    """节流返回 None 是「这次不用查」，不是「查不到」。"""
    keeper = _Keeper([])

    assert restart_if_still_unusable(None, keeper) is None
    assert keeper.restarts == []
