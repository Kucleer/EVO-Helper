"""登录/加载中间态：等它变成 START，而不是当场判「画面认不出」。

事故（2026-08-17，runner CY-202305011401）：游戏的登录流程更新之后，库里开始出现

    17:xx  画面认不出：尺寸 (1920, 917)；导航条读到 '> =. _'；入口标题读到 ''
    18:04  画面认不出：尺寸 (1920, 917)；导航条读到 ''；入口标题读到 'TAL pisE'

两条是同一件事的两个瞬间：一次导航条读到噪声而标题空，一次反过来。它们都不是坏
画面，而是**登录页正在往 START 翻**的那几秒。用户口径：「这里应该是等待变更为
start」。

这一组用例钉三件事，缺任何一件这个修复都可能是假的：

1. **中间态要等，不要报错。**
2. **等到 START 就正常往下走**（该点的还是要点）。
3. **等不到就仍然按原来那条「认不出」收场。** 这一条最要紧：少了它，一个
   「永远等下去」的实现会把前两条全测绿，而实机上换来的是整夜静默空转。
"""

from __future__ import annotations

import pytest

from evo_helper.game.session_keeper import (
    HEALTH_CHECK_INTERVAL_S,
    LOGIN_SETTLE_TIMEOUT_S,
    SETTLED_SCREENS,
    START_POLL_S,
    ReconnectOutcome,
    ScreenState,
    SessionKeeper,
    classify_screen,
)


class _Screen:
    """按脚本一帧一帧地喂画面；脚本走到最后一帧就**一直**停在那一帧。

    ⚠️ 「脚本用完就给 IN_GAME」是不行的：超时那条用例要的正是「一直认不出」，
    自动变好会让它悄悄测成「等到了」——那就等于没测超时。
    """

    def __init__(self, states: list[ScreenState]) -> None:
        assert states, "至少要有一帧"
        self._states = list(states)
        self.observations = 0
        self.entry_clicks = 0
        self.start_clicks = 0
        #: 每一次点击发生时已经观察过几帧。用来证明「等待期间一下都没点」。
        self.clicks_at: list[int] = []
        self.now = 0.0

    def observe(self) -> ScreenState:
        self.observations += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]

    def entry(self) -> None:
        self.entry_clicks += 1
        self.clicks_at.append(self.observations)

    def start(self) -> None:
        self.start_clicks += 1
        self.clicks_at.append(self.observations)

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _keeper(screen: _Screen, **kwargs: object) -> SessionKeeper:
    return SessionKeeper(
        observe=screen.observe,
        click_entry=screen.entry,
        click_start=screen.start,
        clock=screen.clock,
        sleep=screen.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


class TestTheJudgementIsNotTheOcrScraps:
    """判据是「它会不会自己变」，**不是「它长什么样」**。

    OCR 每一帧读出来的碎字都不一样，把日志里那两串写死成判据，下一帧换个相位
    就失效。这两条用例守的就是「没人拿字面量偷懒」。
    """

    def test_the_logged_scraps_are_still_unrecognised(self) -> None:
        assert classify_screen("> =. _") is ScreenState.UNKNOWN
        assert classify_screen("TAL pisE") is ScreenState.UNKNOWN

    def test_waiting_is_not_a_new_screen_state(self) -> None:
        """`UNKNOWN` 不许算进「等到了」——不然一等到就说等到了。"""
        assert ScreenState.UNKNOWN not in SETTLED_SCREENS
        assert ScreenState.START in SETTLED_SCREENS


class TestTheWaitHasAnEnd:
    """上限比等待本身更要紧，理由整段在 `LOGIN_SETTLE_TIMEOUT_S`。"""

    def test_the_timeout_is_finite_and_shorter_than_a_health_check(self) -> None:
        assert 0 < LOGIN_SETTLE_TIMEOUT_S < HEALTH_CHECK_INTERVAL_S


class TestALoadingScreenIsWaitedOut:
    def test_a_transient_unrecognised_screen_is_waited_out_not_reported(self) -> None:
        """(a) 中间态 → 等，不是当场报「认不出」。"""
        screen = _Screen([ScreenState.UNKNOWN, ScreenState.UNKNOWN, ScreenState.IN_GAME])
        outcome = _keeper(screen).wait_for_known_screen()

        assert outcome.ready
        assert outcome.state is ScreenState.IN_GAME
        assert screen.now < LOGIN_SETTLE_TIMEOUT_S, "等到了就该停手，不该把上限耗满"

    def test_nothing_is_clicked_while_the_screen_is_still_unrecognised(self) -> None:
        """等待期间**一下都不点**——「认不出的画面绝不点击」在这一级一样成立。"""
        screen = _Screen([ScreenState.UNKNOWN, ScreenState.UNKNOWN, ScreenState.START])
        _keeper(screen).wait_for_known_screen()

        assert screen.clicks_at, "这一条只有在真点过 START 时才说明得了问题"
        assert min(screen.clicks_at) >= 3, "第 3 帧才读到 START，之前不许有任何点击"

    def test_reaching_start_carries_on_into_the_game(self) -> None:
        """(b) 等到 START → 照常点它、照常进游戏。"""
        screen = _Screen(
            [
                ScreenState.UNKNOWN,
                ScreenState.START,
                ScreenState.IN_GAME,
            ]
        )
        outcome = _keeper(screen).wait_for_known_screen()

        assert screen.start_clicks == 1
        assert outcome.ready
        assert outcome.reconnected

    def test_reaching_the_entry_page_walks_the_whole_sequence(self) -> None:
        """等到的不一定是 START——入口页也算等到了，之后照旧「进入」→ START。"""
        screen = _Screen(
            [
                ScreenState.UNKNOWN,
                ScreenState.ENTRY,
                ScreenState.START,
                ScreenState.IN_GAME,
            ]
        )
        outcome = _keeper(screen).wait_for_known_screen()

        assert (screen.entry_clicks, screen.start_clicks) == (1, 1)
        assert outcome.ready


class TestTheTimeoutFallsBackToTheOldEnding:
    """(c) **这一条最重要。** 少了它，一个「永远等下去」的实现会全绿。"""

    def test_a_screen_that_never_settles_ends_up_unrecognised(self) -> None:
        screen = _Screen([ScreenState.UNKNOWN])
        outcome = _keeper(screen).wait_for_known_screen()

        assert outcome.state is ScreenState.UNKNOWN
        assert not outcome.ready
        assert not outcome.reconnected
        assert "unrecognised screen" in outcome.detail

    def test_the_wait_actually_stops(self) -> None:
        """真的等满了上限，而且**真的停了下来**——两侧都要钉。

        只钉「等满了」挡不住无限等待；只钉「停下来了」挡不住「压根没等」。
        """
        screen = _Screen([ScreenState.UNKNOWN])
        _keeper(screen).wait_for_known_screen()

        assert screen.now == pytest.approx(LOGIN_SETTLE_TIMEOUT_S)
        assert screen.observations <= LOGIN_SETTLE_TIMEOUT_S / START_POLL_S + 2

    def test_a_screen_that_never_settles_is_never_clicked(self) -> None:
        screen = _Screen([ScreenState.UNKNOWN])
        _keeper(screen).wait_for_known_screen()

        assert (screen.entry_clicks, screen.start_clicks) == (0, 0)

    def test_the_timeout_says_how_long_it_waited(self) -> None:
        """三条日志（开始等 / 等到了 / 超时）都要带上等了多久，否则事后查不出来。"""
        screen = _Screen([ScreenState.UNKNOWN])
        said: list[str] = []
        _keeper(screen, log=said.append).wait_for_known_screen()

        assert any("登录还没走完" in line for line in said), said
        assert any(f"等了 {LOGIN_SETTLE_TIMEOUT_S:.0f} 秒" in line for line in said), said

    def test_settling_says_how_long_it_waited_too(self) -> None:
        screen = _Screen([ScreenState.UNKNOWN, ScreenState.IN_GAME])
        said: list[str] = []
        _keeper(screen, log=said.append).wait_for_known_screen()

        assert any("画面变成了" in line and "秒" in line for line in said), said


class TestADeadSessionIsNotWalkedThroughTheEntrySequence:
    """等着等着读到「无法重新连接」：那一屏的善后是关窗重开，不是入口序列。"""

    def test_a_dead_session_is_handed_back_to_the_restart_path(self) -> None:
        screen = _Screen([ScreenState.UNKNOWN, ScreenState.DEAD_SESSION])
        restarts: list[int] = []

        def restart() -> None:
            restarts.append(1)

        outcome: ReconnectOutcome = _keeper(screen, restart_window=restart).wait_for_known_screen()

        assert restarts, "读到死会话还去走入口序列，就是在一个死页面上白点"
        assert isinstance(outcome, ReconnectOutcome)
