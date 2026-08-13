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
    def __init__(
        self,
        outcomes: list[ReconnectOutcome | None],
        *,
        after_restart: ReconnectOutcome | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._after_restart = after_restart
        self.calls = 0
        self.restarts: list[str] = []

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome | None:
        self.calls += 1
        return self._outcomes.pop(0)

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        """关窗重开。默认「重开也没救回来」，好让不写这一档的用例照旧停在原地。

        真实现里配额耗尽时返回的也正是这个形状：`ready` 为假的一个结局。
        """
        self.restarts.append(reason)
        return self._after_restart or ReconnectOutcome(
            ScreenState.UNKNOWN, reconnected=False, detail="restart budget exhausted"
        )


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[ReconnectOutcome | None],
    *,
    after_restart: ReconnectOutcome | None = None,
) -> tuple[Any, _Keeper, list[str]]:
    events: list[str] = []
    keeper = _Keeper(outcomes, after_restart=after_restart)
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


def test_a_live_session_never_reaches_the_restart_rung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**阶梯每一级只在上一级失败之后才走。**

    会话好好的时候关窗重开是纯粹的破坏：用户会看到窗口凭空关掉又开一次，
    还白吃掉一次 3 次/小时的配额。
    """
    loop, keeper, _events = _loop(monkeypatch, [_outcome(ScreenState.IN_GAME)])

    assert loop._ensure_session(force=True) is False
    assert keeper.restarts == []


def test_closing_an_overlay_is_enough_and_nothing_gets_restarted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第二级救回来了，就不许再走第三级。"""
    loop, keeper, events = _loop(
        monkeypatch, [_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.IN_GAME)]
    )

    assert loop._ensure_session(force=True) is False
    assert events == ["关浮层"]
    assert keeper.restarts == []


def test_a_screen_that_stays_unknown_escalates_to_a_window_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**用户口径（2026-08-12）：「你需要设计一个兜底机制」。**

    起因是强杀 runner 之后画面卡住、无法交互。关浮层救不回来时原先就地抛异常、
    整轮退出码 1，而画面上一个「掉线」字样都没有——`SessionKeeper.reconnect`
    那条重连路根本不会被触发。现在再加一级：关窗重开。

    这一级**不在认不出的画面上点任何东西**：只送一个 `WM_CLOSE` 给游戏窗口，
    重开之后仍旧走判据驱动的入口序列。
    """
    loop, keeper, events = _loop(
        monkeypatch,
        [_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.UNKNOWN)],
        after_restart=_outcome(ScreenState.IN_GAME, reconnected=True),
    )

    assert loop._ensure_session(force=True) is True
    assert len(keeper.restarts) == 1
    # 重开之后画面整个换过一遍，导航器那份记忆记的是重开前的坐标。
    assert events == ["关浮层", "清缓存"]


def test_it_still_stops_when_even_a_restart_does_not_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**预算耗尽就停，不是接着重启。**

    `SessionKeeper` 的滚动配额是 3 次 / 1 小时，用尽后 `restart_and_reenter`
    返回一个 `ready` 为假的结局。服务端维护时每次开工都会撞到这一屏，接着重启
    就成了「每几分钟关一次 Chrome 再开一次」，一直折腾到有人来看——那比停下来
    更糟。这里的假守护默认返回的正是「配额耗尽」那个结局。
    """
    loop, keeper, _events = _loop(
        monkeypatch, [_outcome(ScreenState.UNKNOWN), _outcome(ScreenState.UNKNOWN)]
    )

    with pytest.raises(RuntimeError, match="重开也没能回到游戏内"):
        loop._ensure_session(force=True)
    assert len(keeper.restarts) == 1, "重开只许试一次，不许成环"


def test_a_throttled_check_is_not_treated_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """守护按时间节流，没到点返回 None——那是「这次不用查」，不是「查不到」。"""
    loop, _keeper, events = _loop(monkeypatch, [None])

    assert loop._ensure_session() is False
    assert events == []
