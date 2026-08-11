"""派遣链路撞上单按钮弹窗时的处理。

三个弹窗**分两类，处理方式相反**：

    没有可执行的任务    目标在 8 小时保护期 → **跳过这个目标**，继续下一个
    未选择任何战舰      预设的战舰全在外面   → 停下整轮，等舰队返航
    同时派遣的舰队数量已达上限  航线占满     → 停下整轮，等舰队返航

两条不变量，坏哪一条都是静默的：

1. **资源耗尽不是失败**，整轮要正常收尾（退出码 0）。当成失败的话，航线占满
   这种必然会发生的事连撞三次，调度器就把整条链路自动停用了——而它只是需要
   等舰队飞回来。
2. **点完「出发！」不等于派出去了**。航线满时游戏在那一步弹窗，这一发根本没飞；
   照记不误的话，库里多出一条根本不存在的派遣，调度器据此以为一条航线被占着，
   等一份永远不会来的战报，要到 6 小时后才被判缺失清掉。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.game import pirate_ui
from evo_helper.tools.pirate_loop import Outcome, PirateLoop, RoundExhausted

TARGET = Coordinate(2, 137, 4)


class _Driver:
    """只记点了哪些标签。"""

    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, _seconds: float) -> None:
        pass


def _loop(dialog_text: str) -> tuple[Any, _Driver]:
    """一个只会读出 `dialog_text` 的循环。"""
    loop = PirateLoop.__new__(PirateLoop)
    driver = _Driver()
    loop._driver = driver  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._read = lambda *_a, **_k: dialog_text  # type: ignore[attr-defined, assignment]
    return loop, driver


# -- 认出来之后怎么办 ---------------------------------------------------------


def test_no_dialog_lets_the_flow_continue() -> None:
    loop, driver = _loop("")
    assert loop._handle_dialog(TARGET) is True
    assert driver.clicks == [], "没有弹窗就不该点任何东西"


def test_a_protected_target_is_skipped_not_fatal() -> None:
    """保护期只影响这一个目标，后面的还能打。"""
    loop, driver = _loop(pirate_ui.DIALOG_NO_MISSION)
    assert loop._handle_dialog(TARGET) is False
    assert driver.clicks == ["关闭弹窗"]
    assert loop._outcome.refused == [(TARGET, pirate_ui.DIALOG_NO_MISSION)]


@pytest.mark.parametrize("message", [pirate_ui.DIALOG_NO_SHIPS, pirate_ui.DIALOG_LINES_FULL])
def test_resource_exhaustion_stops_the_round(message: str) -> None:
    """资源耗尽跳到下一个目标也一样派不出去，所以停整轮。"""
    loop, driver = _loop(message)
    with pytest.raises(RoundExhausted):
        loop._handle_dialog(TARGET)
    assert driver.clicks == ["关闭弹窗"], "停轮之前也要先把弹窗关掉"


def test_an_unknown_dialog_is_not_treated_as_clear() -> None:
    """没见过的弹窗贴不回词表，`_dialog()` 返回 None。

    此时 `_handle_dialog` 会放行——这是有意的：它只管这三个已知弹窗，
    「认不出的画面」由既有的那几道闸门（简报任务类型、面板标题）去挡。
    这条钉住的是**它不会把陌生弹窗硬贴成已知的那三个之一**：贴错的代价是
    「跳过目标」和「停整轮」做反。
    """
    loop, _driver = _loop("服务器维护中，请稍后再试")
    assert loop._dialog() is None


def test_a_one_character_misread_is_still_recognised() -> None:
    """实机把「派遣」读成过「派遗」。差一个字就认不出的话，这套接线等于没接。"""
    loop, _driver = _loop("同时派遗的舰队数量已达上限。")
    with pytest.raises(RoundExhausted):
        loop._handle_dialog(TARGET)


# -- 不许记下没发生的派遣 -----------------------------------------------------


def test_a_dialog_after_launch_means_the_fleet_never_left() -> None:
    """点完「出发！」弹出航线满 → `_launch` 必须返回假，调用方就不会记派遣。

    这一条错了，库里会多一条根本不存在的派遣：调度器以为一条航线被占着，
    等一份永远不会来的战报，直到 `MAX_REPORT_AGE` 才清掉。
    """
    loop = PirateLoop.__new__(PirateLoop)
    driver = _Driver()
    loop._driver = driver  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._briefing_mission = lambda: "侦察"  # type: ignore[attr-defined, assignment]
    loop._read = lambda *_a, **_k: pirate_ui.DIALOG_LINES_FULL  # type: ignore[attr-defined, assignment]

    with pytest.raises(RoundExhausted):
        loop._launch(TARGET, "侦察")

    assert "出发" in driver.clicks, "简报核对通过时该点出发"


# -- 整轮的收尾 ---------------------------------------------------------------


def test_the_round_ends_cleanly_when_resources_run_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """`run()` 吞掉 `RoundExhausted` 并正常返回——退出码 0，不计入连续失败。"""
    from evo_helper.tools import pirate_loop as module

    loop = PirateLoop.__new__(PirateLoop)
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._navigator = type("N", (), {"ensure_system_view": lambda _s, _f: True})()  # type: ignore[attr-defined]
    loop._nav_labels = lambda: ""  # type: ignore[attr-defined, assignment]
    loop._reset_to_known_screen = lambda: None  # type: ignore[attr-defined, assignment]
    # 会话巡检要真截屏，这条测试只关心 `RoundExhausted` 的收尾，桩掉即可。
    loop._ensure_session = lambda **_k: False  # type: ignore[attr-defined, assignment]
    # 开工对账要开库、翻信箱，同样不在这条测试的范围内。
    loop.reconcile_today = lambda: None  # type: ignore[attr-defined, assignment, method-assign]
    loop._sweep = lambda: (_ for _ in ()).throw(  # type: ignore[attr-defined, assignment]
        RoundExhausted(pirate_ui.DIALOG_LINES_FULL)
    )
    monkeypatch.setattr("evo_helper.game.game_window.ensure_game_window", lambda *a, **k: None)
    monkeypatch.setattr(module, "say", lambda _m: None)

    outcome = loop.run()  # 不抛就算过

    assert isinstance(outcome, Outcome)
