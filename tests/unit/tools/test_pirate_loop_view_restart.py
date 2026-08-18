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

from evo_helper.domain.models import Coordinate
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


def _in_game(restarts_left: int = 0) -> ReconnectOutcome:
    return ReconnectOutcome(
        ScreenState.IN_GAME, reconnected=True, detail="restarted", restarts_left=restarts_left
    )


def _refused(
    detail: str = "restart budget exhausted: 3/3 restarts within 3600s",
    *,
    restarts_left: int = 0,
) -> ReconnectOutcome:
    return ReconnectOutcome(
        ScreenState.UNKNOWN, reconnected=False, detail=detail, restarts_left=restarts_left
    )


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


def test_the_origin_planet_memory_is_dropped_after_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**2026-08-18 那次错账的触发点就是这一支。** 生产 `system_log` 一句不差：

        18:52:07  起点回读 '9:250:8'，确认当前星球是 9:250:8
        18:53:32  已发动攻击 → 9:231:7（预设 AAA）      ← 确实从 9:250:8（18.5 分）
        18:54:59  派出之后切不回恒星系视图；关窗重开一次再试（兜底策略）
        18:55:34  重开之后已经重新进到游戏内
        18:56:22  已发动攻击 → 9:205:14（预设 BBB）     ← 已经是主星（125.0 分）

    重开的是整个 Chrome 窗口，游戏重新走一遍入口序列，**落点是主星**。而这里原先
    只清了导航器缓存，出发星球那份记忆一个字没动——`switch_needed` 于是说
    「本轮已经切到 9:250:8，不用切」，余下每一发都从主星飞出去，
    `attack_intents.origin_*` 上却写着 9:250:8。一发白占 3.4 小时航线，账还是错的。

    另外两处关窗重开（`_ensure_session` 的重连支、`_mailbox_restart`）**都清了**。
    三处共用一件事而只改了两处，代价就是这个。
    """
    loop, _navigator, _keeper = _loop(monkeypatch, [False, True], _in_game())
    loop._current_planet = Coordinate(9, 250, 8)

    loop._require_system_view("派出之后切不回恒星系视图")

    assert loop._current_planet is None


def test_a_view_that_came_back_on_its_own_keeps_the_origin_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反过来：没重开就不许清。

    清一次的代价是下一轮多切一次星球（约 20 秒）。切视图本身不改当前星球——
    真改了的话 `ensure_origin_planet` 里那次「切完再切回恒星系视图」就自相矛盾了。
    """
    loop, _navigator, keeper = _loop(monkeypatch, [True], _in_game())
    loop._current_planet = Coordinate(9, 250, 8)

    loop._require_system_view("派出之后切不回恒星系视图")

    assert keeper.reasons == []
    assert loop._current_planet == Coordinate(9, 250, 8)


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


# -- 抛出去之后按哪个退出码收场 -------------------------------------------------
#
# 抛本身只是把这一轮打住；**真正决定后果的是退出码**。`EXIT_ENVIRONMENT_BUSY`
# 不计入连续失败，1 计入。判据是关窗重开配额——那份配额在滚动窗口内是有限的，
# 所以「还有配额就报 75」必然有尽头：同一小时里最多三轮能这么收场，之后
# `restart_and_reenter` 直接被拒、配额恒为 0、退回 1，豁免照常攒。
#
# 反过来若无条件报 75，调度器会每隔一个冷却再起一轮、再吃一次配额、再什么都不
# 推进，而**豁免计数不再增长，再没有任何东西会最终把它停下来**——2026-08-17 那种
# 故障就会从「26 分钟后被 6/6 拦住」变成整夜静默空转。


def test_a_restart_with_budget_left_is_worth_another_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop, _navigator, _keeper = _loop(monkeypatch, [False], _refused(restarts_left=2))

    with pytest.raises(module.SessionUnavailable) as caught:
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert caught.value.recoverable is True


def test_an_exhausted_restart_budget_is_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **安全底线。** 配额耗尽还是回不去，说明重开这条路已经证明救不了。"""
    loop, _navigator, _keeper = _loop(monkeypatch, [False], _refused(restarts_left=0))

    with pytest.raises(module.SessionUnavailable) as caught:
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert caught.value.recoverable is False


def test_a_view_still_gone_after_a_successful_restart_follows_the_same_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重开成功、视图还是切不回来：判据仍旧是「重开之后还剩几次配额」。"""
    loop, _navigator, _keeper = _loop(monkeypatch, [False, False], _in_game(restarts_left=2))

    with pytest.raises(module.SessionUnavailable, match="重开之后仍然切不回来") as caught:
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert caught.value.recoverable is True


def test_a_view_still_gone_with_the_budget_spent_is_a_real_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一处的另一半：重开是成功了，但那是最后一次配额。"""
    loop, _navigator, _keeper = _loop(monkeypatch, [False, False], _in_game(restarts_left=0))

    with pytest.raises(module.SessionUnavailable, match="重开之后仍然切不回来") as caught:
        loop._require_system_view("读完邮件切不回恒星系视图")

    assert caught.value.recoverable is False


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
