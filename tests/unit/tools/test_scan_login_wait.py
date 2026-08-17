"""恢复阶梯上「等登录走完」这一级：它排在关浮层之后、关窗重开之前。

排在关浮层之后：浮层是 `UNKNOWN` 里更常见的那一种，几秒就能证否，把 90 秒的等待
挪到它前面等于每次都白等一分半。

排在关窗重开之前：**这一级要挡住的正是那一下**。2026-08-17 登录流程更新之后，
翻页那几秒读出来的画面跟真认不出长得一样，而助手当时的动作是关掉 Chrome 重开——
既救不了，又把本来马上就好的会话亲手弄坏，还吃掉一次 3 次 / 1 小时的重开配额。
"""

from __future__ import annotations

from evo_helper.game.session_keeper import ReconnectOutcome, ScreenState
from evo_helper.tools.scan_coordinates import (
    restart_if_still_unusable,
    wait_for_login_if_unrecognised,
)


def _outcome(state: ScreenState, *, ready_detail: str = "") -> ReconnectOutcome:
    return ReconnectOutcome(state, reconnected=False, detail=ready_detail or state.value)


class _Keeper:
    """只认这一级会用到的两个动作，别的都不给——多给就测不出「没走那条路」。"""

    def __init__(self, settled: ReconnectOutcome | None = None) -> None:
        self._settled = settled or _outcome(ScreenState.IN_GAME)
        self.waits = 0
        self.restarts: list[str] = []

    def wait_for_known_screen(self) -> ReconnectOutcome:
        self.waits += 1
        return self._settled

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        self.restarts.append(reason)
        return _outcome(ScreenState.UNKNOWN)


class TestTheRungOnlyFiresOnUnrecognisedScreens:
    def test_a_healthy_session_is_passed_through_untouched(self) -> None:
        keeper = _Keeper()
        session = _outcome(ScreenState.IN_GAME)

        assert wait_for_login_if_unrecognised(session, keeper) is session
        assert keeper.waits == 0, "会话好好的还去等，等于凭空给每一轮加上一分半"

    def test_no_session_at_all_is_passed_through(self) -> None:
        """巡检没到点时返回 None，这一级不许把它变成别的东西。"""
        keeper = _Keeper()

        assert wait_for_login_if_unrecognised(None, keeper) is None
        assert keeper.waits == 0

    def test_a_disconnect_dialog_is_not_waited_out(self) -> None:
        """掉线弹窗认得出，善后是点掉它——等在那儿只会白等满上限。"""
        keeper = _Keeper()
        session = _outcome(ScreenState.DISCONNECTED)

        assert wait_for_login_if_unrecognised(session, keeper) is session
        assert keeper.waits == 0

    def test_an_unrecognised_screen_is_waited_out(self) -> None:
        keeper = _Keeper()

        outcome = wait_for_login_if_unrecognised(_outcome(ScreenState.UNKNOWN), keeper)

        assert keeper.waits == 1
        assert outcome.ready


class TestTheRungSitsBeforeTheWindowRestart:
    def test_a_login_that_settles_never_reaches_the_restart(self) -> None:
        """(b) 等到了 → 正常继续，**不关窗重开**。"""
        keeper = _Keeper(settled=_outcome(ScreenState.IN_GAME))

        outcome = restart_if_still_unusable(
            wait_for_login_if_unrecognised(_outcome(ScreenState.UNKNOWN), keeper), keeper
        )

        assert outcome.ready
        assert keeper.restarts == [], "登录才到一半就关掉 Chrome，救不了还会把会话弄坏"

    def test_a_screen_that_never_settles_still_reaches_the_restart(self) -> None:
        """(c) 等不到 → 阶梯照旧往下走，收场跟这个功能出现之前一模一样。"""
        keeper = _Keeper(settled=_outcome(ScreenState.UNKNOWN))

        outcome = restart_if_still_unusable(
            wait_for_login_if_unrecognised(_outcome(ScreenState.UNKNOWN), keeper), keeper
        )

        assert keeper.waits == 1
        assert len(keeper.restarts) == 1, "等待必须有尽头，超时之后该重开还是要重开"
        assert not outcome.ready
