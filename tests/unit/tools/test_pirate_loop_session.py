"""开工前先确认会话，以及「认不出的画面」不等于「掉线」。

两件事在实机上各坑过一次：

1. **顺序**（2026-08-11 02:10）：会话掉了，而 `run()` 先切视图。导航栏标签在登录页
   上读不到，`ensure_system_view` 朝视图菜单坐标盲点三次然后放弃——永远走不到能
   重连的 `SessionKeeper`。接着 bot 对着 START 页把 80 个目标一个个试，每个 ~35 秒。
   `run_scan` 里针对这个顺序写过一整段注释，这两条链路当时漏抄了。

2. **UNKNOWN 不是掉线**（2026-08-11 02:24，修第 1 条时引入的回归）：把会话巡检提到
   最前面之后，只要上一轮把游戏停在某个浮层上（信箱、飞行中列表、派遣面板），
   导航条就被盖住，`classify_screen` 给出 UNKNOWN，于是当场「安全停止」——而会话
   好好的。登录页会被判成 ENTRY/START，**落不到 UNKNOWN**，所以 UNKNOWN 该先关
   浮层再问一次。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.session_keeper import ReconnectOutcome, ScreenState
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import PirateLoop


class _Keeper:
    def __init__(self, outcomes: list[ReconnectOutcome | None]) -> None:
        self._outcomes = outcomes
        self.calls = 0

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome | None:
        self.calls += 1
        return self._outcomes.pop(0)


def _loop(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[ReconnectOutcome | None]
) -> tuple[Any, _Keeper, list[str]]:
    events: list[str] = []
    keeper = _Keeper(outcomes)
    loop = PirateLoop.__new__(PirateLoop)
    loop._keeper = lambda: keeper  # type: ignore[attr-defined, assignment, method-assign]
    loop._reset_to_known_screen = lambda: events.append("关浮层")  # type: ignore[assignment, method-assign]
    loop._navigator = type("N", (), {"invalidate": lambda _s: events.append("清缓存")})()  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "say", lambda _m: None)
    return loop, keeper, events


def _outcome(state: ScreenState, *, reconnected: bool = False) -> ReconnectOutcome:
    return ReconnectOutcome(state, reconnected=reconnected, detail=state.value)


def test_a_live_session_costs_one_check_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, keeper, events = _loop(monkeypatch, [_outcome(ScreenState.IN_GAME)])

    assert loop._ensure_session(force=True) is False
    assert keeper.calls == 1
    assert events == []


def test_a_reconnect_clears_the_nav_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """重连之后导航器那份记忆记的是掉线前的坐标，必须作废。"""
    loop, _keeper, events = _loop(monkeypatch, [_outcome(ScreenState.IN_GAME, reconnected=True)])

    assert loop._ensure_session(force=True) is True
    assert events == ["清缓存"]


def test_an_unknown_screen_closes_overlays_and_asks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本文件的重点：UNKNOWN 多半是浮层压着导航条，不是掉线。"""
    loop, keeper, events = _loop(
        monkeypatch, [_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.IN_GAME)]
    )

    assert loop._ensure_session(force=True) is False
    assert keeper.calls == 2
    assert events == ["关浮层"]


def test_it_still_stops_when_the_screen_stays_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """关掉浮层还是认不出，就真的停——可能是维护公告或改版，不许乱点。"""
    loop, _keeper, _events = _loop(
        monkeypatch, [_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.UNKNOWN)]
    )

    with pytest.raises(RuntimeError, match="会话不可用"):
        loop._ensure_session(force=True)


def test_a_throttled_check_is_not_treated_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守护按时间节流，没到点返回 None——那是「这次不用查」，不是「查不到」。"""
    loop, _keeper, events = _loop(monkeypatch, [None])

    assert loop._ensure_session() is False
    assert events == []
