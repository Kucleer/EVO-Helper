"""视图恢复不了时的兜底：关窗重开一次，再试一次切视图。

用户口径（2026-08-11）：「**切不回就重启，这是兜底策略。**」

实机上整轮倒在这里：

    File ".../tools/pirate_loop.py", line 1307, in _close_mail
        raise RuntimeError("读完邮件切不回恒星系视图；安全停止")

——就地停摆、退出码 1。而画面上**一个「掉线」字样都没有**，所以
`SessionKeeper.reconnect()` 那条重连路根本不会被触发：它的判据是读到
「连接已断开」/「无法重新连接」。三处同一形状的 `raise`（开工、派出之后、
读完邮件之后）现在统一走 `_require_system_view`。

这里钉住的是**兜底的边界**，不是「重开能救回来」这件事本身：

- 只重开一次，不许变成循环；
- 重开完仍然要走正常判据，不许因为「刚重开过」就假定自己在游戏内；
- 配额用完时 `restart_and_reenter` 返回拒绝结局，这时就该老实抛。

⚠️ 全程不碰真窗口：重开动作在 `SessionKeeper` 里是注入进来的，这里连
`SessionKeeper` 都是假的。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.session_keeper import ReconnectOutcome, ScreenState
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import PirateLoop


class _Navigator:
    """按剧本回答「切回恒星系视图了吗」。剧本用完就一直回答最后那个答案。"""

    def __init__(self, answers: list[bool]) -> None:
        self._answers = answers
        self.calls = 0
        self.invalidated = 0

    def ensure_system_view(self, _read_nav_labels: Any) -> bool:
        self.calls += 1
        return self._answers[min(self.calls - 1, len(self._answers) - 1)]

    def invalidate(self) -> None:
        self.invalidated += 1


class _Keeper:
    def __init__(self, outcome: ReconnectOutcome) -> None:
        self._outcome = outcome
        self.reasons: list[str] = []

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        self.reasons.append(reason)
        return self._outcome


def _in_game() -> ReconnectOutcome:
    return ReconnectOutcome(ScreenState.IN_GAME, reconnected=True, detail="restarted")


def _refused(
    detail: str = "restart budget exhausted: 3/3 restarts within 3600s",
) -> ReconnectOutcome:
    return ReconnectOutcome(ScreenState.UNKNOWN, reconnected=False, detail=detail)


def _loop(
    monkeypatch: pytest.MonkeyPatch, answers: list[bool], outcome: ReconnectOutcome
) -> tuple[Any, _Navigator, _Keeper]:
    navigator = _Navigator(answers)
    keeper = _Keeper(outcome)
    loop = PirateLoop.__new__(PirateLoop)
    loop._navigator = navigator  # type: ignore[attr-defined]
    loop._keeper = lambda: keeper  # type: ignore[attr-defined, assignment, method-assign]
    loop._nav_labels = lambda: ""  # type: ignore[attr-defined, assignment, method-assign]
    monkeypatch.setattr(module, "say", lambda _m: None)
    return loop, navigator, keeper


def test_a_view_that_switches_back_never_restarts_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重开会让用户看着窗口消失又出现。切得回来的时候绝不该重开。"""
    loop, navigator, keeper = _loop(monkeypatch, [True], _in_game())

    loop._require_system_view("读完邮件切不回恒星系视图")

    assert keeper.reasons == []
    assert (navigator.calls, navigator.invalidated) == (1, 0)


def test_a_view_that_will_not_come_back_triggers_one_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文件的重点：原来这里是就地 `raise`，整轮退出码 1。"""
    loop, navigator, keeper = _loop(monkeypatch, [False, True], _in_game())

    loop._require_system_view("读完邮件切不回恒星系视图")

    assert keeper.reasons == ["读完邮件切不回恒星系视图"]
    assert navigator.calls == 2, "重开之后必须重新试一次切视图"


def test_the_nav_cache_is_dropped_after_a_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    """重开之后画面整个换过一遍，导航器那份记忆记的是重开前的坐标。"""
    loop, navigator, _keeper = _loop(monkeypatch, [False, True], _in_game())

    loop._require_system_view("派出之后切不回恒星系视图")

    assert navigator.invalidated == 1


def test_it_still_raises_when_the_view_is_gone_after_the_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _navigator, _keeper = _loop(monkeypatch, [False, False], _in_game())

    with pytest.raises(RuntimeError, match="重开之后仍然切不回来"):
        loop._require_system_view("读完邮件切不回恒星系视图")


def test_the_retry_never_becomes_a_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """**只重试一次。** 无限重开比不重开更糟：维护期间会一直折腾用户的桌面。"""
    loop, navigator, keeper = _loop(monkeypatch, [False], _in_game())

    with pytest.raises(RuntimeError):
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert len(keeper.reasons) == 1, "重开只许一次"
    assert navigator.calls == 2, "切视图只许再试一次"


def test_a_refused_restart_is_raised_rather_than_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配额用完（多半是服务端在维护）时 `restart_and_reenter` 返回拒绝结局。

    这时就该老实抛出去，而不是接着往下走——画面还是那个画面。
    """
    loop, navigator, keeper = _loop(monkeypatch, [False], _refused())

    with pytest.raises(RuntimeError, match="budget exhausted"):
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert len(keeper.reasons) == 1
    assert navigator.calls == 1, "重开都被拒了，不该再去切一次视图"


def test_a_restart_that_lands_outside_the_game_is_not_clicked_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 重开**之后**仍然要走正常判据，不许因为「刚重开过」就假定在游戏内。"""
    loop, navigator, _keeper = _loop(
        monkeypatch,
        [False],
        ReconnectOutcome(ScreenState.START, reconnected=False, detail="did not reach the game"),
    )

    with pytest.raises(RuntimeError, match="重开也没能回到游戏内"):
        loop._require_system_view("开工时切不到恒星系视图")

    assert navigator.calls == 1


class _Driver:
    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        pass

    def wait(self, _seconds: float) -> None:
        pass


def test_closing_the_mail_goes_through_the_same_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实机就是倒在这一处。它必须真的接在兜底上，而不是另写一份 `raise`。"""
    loop, _navigator, keeper = _loop(monkeypatch, [False, True], _in_game())
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: False  # type: ignore[attr-defined, assignment, method-assign]

    loop._close_mail()

    assert keeper.reasons == ["读完邮件切不回恒星系视图"]


def test_leaving_the_dispatch_list_goes_through_the_same_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _navigator, keeper = _loop(monkeypatch, [False, True], _in_game())
    loop._driver = _Driver()  # type: ignore[attr-defined]

    loop._leave_dispatch_list()

    assert keeper.reasons == ["派出之后切不回恒星系视图"]
