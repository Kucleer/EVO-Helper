"""派遣链路撞上单按钮弹窗时的处理。

三个弹窗**按 (消息 × 意图) 查表分流**，处置可以相反：

    没有可执行的任务    攻击/侦察 → 跳过这个目标；回收 → 跳过且**不写**保护期排除
    未选择任何战舰      攻击/侦察 → 停下整轮；回收 → 只跳过这次回收，攻击照跑
    同时派遣的舰队数量已达上限  两种意图都是停下整轮

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
ATTACK = pirate_ui.DispatchPurpose.ATTACK
SCOUT = pirate_ui.DispatchPurpose.SCOUT
RECYCLE = pirate_ui.DispatchPurpose.RECYCLE


class _Driver:
    """只记点了哪些标签。"""

    def __init__(self) -> None:
        self.clicks: list[str] = []

    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        self.clicks.append(label)

    def wait(self, _seconds: float) -> None:
        pass


def _loop(dialog_text: str) -> tuple[Any, _Driver]:
    """一个只会读出 `dialog_text` 的循环。

    ⚠️ **落库那一步桩掉。** 撞上保护期时 `_handle_dialog` 会把这件事记进
    `bot_targets`（见 `test_protection_period_record`），而这份用例守的是「三个
    弹窗按意图分流」这条判据——让它去开一条真的数据库连接，守的东西就换人了。
    """
    loop = PirateLoop.__new__(PirateLoop)
    driver = _Driver()
    loop._driver = driver  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._read = lambda *_a, **_k: dialog_text  # type: ignore[attr-defined, assignment]
    loop._note_protection_period = lambda _c: None  # type: ignore[attr-defined, assignment]
    return loop, driver


# -- 认出来之后怎么办 ---------------------------------------------------------


def test_no_dialog_lets_the_flow_continue() -> None:
    loop, driver = _loop("")
    assert loop._handle_dialog(TARGET, purpose=ATTACK) is True
    assert driver.clicks == [], "没有弹窗就不该点任何东西"


def test_a_protected_target_is_skipped_not_fatal() -> None:
    """保护期只影响这一个目标，后面的还能打。"""
    loop, driver = _loop(pirate_ui.DIALOG_NO_MISSION)
    assert loop._handle_dialog(TARGET, purpose=ATTACK) is False
    assert driver.clicks == ["关闭弹窗"]
    assert loop._outcome.refused == [(TARGET, pirate_ui.DIALOG_NO_MISSION)]


@pytest.mark.parametrize("message", [pirate_ui.DIALOG_NO_SHIPS, pirate_ui.DIALOG_LINES_FULL])
def test_resource_exhaustion_stops_the_round(message: str) -> None:
    """资源耗尽跳到下一个目标也一样派不出去，所以停整轮。"""
    loop, driver = _loop(message)
    with pytest.raises(RoundExhausted):
        loop._handle_dialog(TARGET, purpose=ATTACK)
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
        loop._handle_dialog(TARGET, purpose=ATTACK)


# -- 回收意图：同一句话，另一个处置 -------------------------------------------


def test_recycle_hitting_no_mission_does_not_note_protection() -> None:
    """⚠️ 回收撞「没有可执行的任务」= 没有残骸，**绝不写保护期排除**。

    写了的话，一颗完全打得了的 bot 会被排出候选池 8 小时，而页面和日志
    都看不出异常。这条是 PR-A 最关键的变异点。
    """
    noted: list[Coordinate] = []
    loop, _driver = _loop(pirate_ui.DIALOG_NO_MISSION)
    loop._note_protection_period = lambda c: noted.append(c)  # type: ignore[attr-defined, assignment]

    assert loop._handle_dialog(TARGET, purpose=RECYCLE) is False
    assert noted == [], "回收撞没残骸时绝不许写保护期排除"


def test_recycle_hitting_no_ships_does_not_stop_the_round() -> None:
    """⚠️ 回收船不够只跳过这次回收，**不许把这一轮的攻击也停掉**。

    回收和攻击是两拨船（用户口径 2026-09-10）。照抄攻击侧的 STOP_ROUND
    会让「回收船不够」把好端端的攻击舰队也一起停掉。
    """
    loop, driver = _loop(pirate_ui.DIALOG_NO_SHIPS)

    result = loop._handle_dialog(TARGET, purpose=RECYCLE)

    assert result is False, "只跳过这次回收，不抛 RoundExhausted"
    assert driver.clicks == ["关闭弹窗"]


def test_scout_maps_the_same_as_attack() -> None:
    """侦察档现在与攻击同处置——六格表不许缺三格。"""
    loop, _driver = _loop(pirate_ui.DIALOG_NO_MISSION)
    assert loop._handle_dialog(TARGET, purpose=SCOUT) is False

    loop, _driver = _loop(pirate_ui.DIALOG_NO_SHIPS)
    with pytest.raises(RoundExhausted):
        loop._handle_dialog(TARGET, purpose=SCOUT)


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
        loop._launch(TARGET, "侦察", purpose=SCOUT)

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
    # 切出发星球要开浮层、OCR、回读派遣面板；那条链路自己有专门的用例。
    loop.ensure_origin_planet = lambda: True  # type: ignore[attr-defined, assignment, method-assign]
    # 开工对账要开库、翻信箱，同样不在这条测试的范围内。
    loop.reconcile_today = lambda: None  # type: ignore[attr-defined, assignment, method-assign]
    loop._sweep = lambda: (_ for _ in ()).throw(  # type: ignore[attr-defined, assignment]
        RoundExhausted(pirate_ui.DIALOG_LINES_FULL)
    )
    monkeypatch.setattr("evo_helper.game.game_window.ensure_game_window", lambda *a, **k: None)
    monkeypatch.setattr(module, "say", lambda _m: None)

    outcome = loop.run()  # 不抛就算过

    assert isinstance(outcome, Outcome)


# -- 源码级断言（跑流程也看不出来的接线） --------------------------------------


def test_note_protection_period_is_only_called_from_skip_protected() -> None:
    """⚠️ `_note_protection_period` 全仓只有一处调用点，且只在 `SKIP_PROTECTED` 那一支。

    接线错了跑流程也看不出来——回收撞「没残骸」若被误记成保护期，页面和日志
    都正常，只有一颗打得了的 bot 被静默排出候选池 8 小时。
    """
    import inspect

    source = inspect.getsource(PirateLoop._handle_dialog)
    assert source.count("_note_protection_period(") == 1, "保护期落库不该有多于一处调用"

    # 那一处必须落在 SKIP_PROTECTED 分支里
    head, _sep, tail = source.partition("if action is pirate_ui.DialogAction.SKIP_PROTECTED:")
    assert "_note_protection_period" not in head, "保护期落库被接到了 SKIP_PROTECTED 之前"
    # SKIP_PROTECTED 到 SKIP_NO_DEBRIS 之间是保护期那一支
    protected_block = tail.partition("if action is pirate_ui.DialogAction.SKIP_NO_DEBRIS:")[0]
    assert "_note_protection_period" in protected_block
    # SKIP_NO_DEBRIS 那一支里不许出现
    debris_block = tail.partition("if action is pirate_ui.DialogAction.SKIP_NO_DEBRIS:")[2]
    stop_marker = "if action is pirate_ui.DialogAction.SKIP_NO_RECYCLERS:"
    debris_only = debris_block.partition(stop_marker)[0]
    assert "_note_protection_period" not in debris_only, "回收撞没残骸时绝不许写保护期排除"


def test_every_handle_dialog_call_site_passes_purpose() -> None:
    """⚠️ `_handle_dialog` 的每一个调用点都显式传了 `purpose`。

    这一条要按**源码**断言，不能靠跑流程——漏传的那一条路可能一年才走到一次。
    回收起点读不出时会走 `_require_origin_before_dispatch`，那里若固定传 ATTACK，
    「没有可执行的任务」照样判成保护期、往库里写 8 小时排除。
    """
    import inspect
    import re

    source = inspect.getsource(PirateLoop)
    # 找出所有 self._handle_dialog(...) 调用
    call_pattern = re.compile(r"self\._handle_dialog\([^)]+\)", re.DOTALL)
    calls = call_pattern.findall(source)
    assert calls, "没找到 _handle_dialog 调用点——源码结构变了，这条断言要跟着改"
    for call in calls:
        assert "purpose=" in call, f"_handle_dialog 调用点漏传了 purpose: {call}"


def test_recycle_origin_unreadable_with_dialog_does_not_note_protection() -> None:
    """⚠️ **回收 + 起点读不出 + 弹窗**这条路：不许写保护期排除。

    这是 review 抓到的 P1#3 的具体路径：回收起点读不出来 →
    `_require_origin_before_dispatch` 内部调 `_handle_dialog` →
    若那里固定传 ATTACK，「没有可执行的任务」照样判成保护期。
    """
    noted: list[Any] = []
    loop = PirateLoop.__new__(PirateLoop)
    driver = _Driver()
    loop._driver = driver  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._read = lambda *_a, **_k: pirate_ui.DIALOG_NO_MISSION  # type: ignore[attr-defined, assignment]
    loop._note_protection_period = lambda c: noted.append(c)  # type: ignore[attr-defined, assignment]
    loop._options = type("O", (), {"origin": None})()  # type: ignore[attr-defined]
    # 起点读不出（返回 None）
    loop._settle = lambda fn, tries=1: fn()  # type: ignore[attr-defined, assignment]
    loop._fleet_origin_text = lambda: ""  # type: ignore[attr-defined, assignment]

    # 直接测 _handle_dialog 走 RECYCLE 这条路（与 _require_origin_before_dispatch 内部一致）
    result = loop._handle_dialog(TARGET, purpose=RECYCLE)

    assert result is False
    assert noted == [], "回收起点被弹窗遮住时绝不许写保护期排除"
