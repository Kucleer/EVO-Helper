from __future__ import annotations

from evo_helper.game.session_keeper import (
    HEALTH_CHECK_INTERVAL_S,
    MAX_WINDOW_RESTARTS,
    RESTART_BUDGET_WINDOW_S,
    ScreenState,
    SessionKeeper,
    classify_screen,
)


class TestScreenClassification:
    def test_the_game_is_recognised_by_its_nav_bar(self) -> None:
        assert classify_screen("行星 舰队 太空舱 商店 联盟") is ScreenState.IN_GAME

    def test_the_system_view_counts_as_in_game(self) -> None:
        assert classify_screen("银河系 恒星系 行星 OK") is ScreenState.IN_GAME

    def test_the_start_screen_is_recognised(self) -> None:
        assert classify_screen("EV-T ORION 1 / Kucleer  START") is ScreenState.START

    def test_start_wins_over_the_faded_title_behind_it(self) -> None:
        """START 页背景里也印着淡淡的 ETERNAL VOID，顺序判错会一直点错按钮。"""
        assert classify_screen("ETERNAL VOID ... START") is ScreenState.START

    def test_the_entry_screen_is_recognised(self) -> None:
        assert classify_screen("ETERNAL VOID 简体中文 进入 更新") is ScreenState.ENTRY

    def test_the_click_anywhere_hint_also_means_entry(self) -> None:
        assert classify_screen("点击任意位置继续") is ScreenState.ENTRY

    def test_an_unrecognised_screen_is_not_guessed(self) -> None:
        assert classify_screen("服务器维护中，请稍后再试") is ScreenState.UNKNOWN
        assert classify_screen("") is ScreenState.UNKNOWN


class TestTwoKindsOfDisconnect:
    """「连接已断开」和「连接已断开，**无法重新连接**」善后完全不同。

    前者点掉弹窗还能接回去；后者页面已经死了，点掉照样回不去，只能关窗重开。
    """

    def test_a_recoverable_disconnect_is_recoverable(self) -> None:
        assert classify_screen("连接已断开") is ScreenState.DISCONNECTED

    def test_the_real_dead_session_text_is_recognised(self) -> None:
        """实机原文（2026-08-11，`var/logs/now-check.png`）。"""
        assert classify_screen("连接已断开，无法重新连接。") is ScreenState.DEAD_SESSION

    def test_the_dead_session_wins_over_its_own_prefix(self) -> None:
        """**判据顺序的要害。**

        可恢复那条的文案是不可恢复那条的**前缀**：「连接已断开」整个包含在
        「连接已断开，无法重新连接。」里面。先判前缀就永远走不到重开那一支——
        现象是一遍遍点掉弹窗、一遍遍等不到入口页，最后报「会话不可用」，
        而它其实只需要重开一次窗口。
        """
        for text in (
            "连接已断开，无法重新连接。",
            "连接已断开, 无法重新连接",
            "  连接已断开，无法重新连接。  ",
        ):
            assert classify_screen(text) is ScreenState.DEAD_SESSION, text

    def test_the_dialog_is_still_judged_before_the_nav_bar_behind_it(self) -> None:
        """弹窗是浮层，底下的导航条还画在画面上。

        后判就会读出「商店/联盟」并给出 IN_GAME——在一个死会话上一路点下去，
        全程不报错。这条对两种弹窗都必须成立。
        """
        assert classify_screen("连接已断开，无法重新连接。 商店 联盟") is ScreenState.DEAD_SESSION
        assert classify_screen("连接已断开 商店 联盟") is ScreenState.DISCONNECTED


class Recorder:
    def __init__(self, states: list[ScreenState]) -> None:
        self.states = states
        self.entry_clicks = 0
        self.start_clicks = 0
        self.now = 0.0

    def observe(self) -> ScreenState:
        return self.states.pop(0) if self.states else ScreenState.IN_GAME

    def entry(self) -> None:
        self.entry_clicks += 1

    def start(self) -> None:
        self.start_clicks += 1

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def keeper(recorder: Recorder, **kwargs: object) -> SessionKeeper:
    return SessionKeeper(
        observe=recorder.observe,
        click_entry=recorder.entry,
        click_start=recorder.start,
        clock=recorder.clock,
        sleep=recorder.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


class TestHealthCheckInterval:
    def test_the_interval_is_ten_minutes(self) -> None:
        assert HEALTH_CHECK_INTERVAL_S == 600.0

    def test_the_first_check_is_always_due(self) -> None:
        assert keeper(Recorder([ScreenState.IN_GAME])).due()

    def test_a_check_is_not_due_again_immediately(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])
        k = keeper(recorder)
        k.ensure_connected()
        assert not k.due()

    def test_a_check_becomes_due_after_the_interval(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME, ScreenState.IN_GAME])
        k = keeper(recorder)
        k.ensure_connected()
        recorder.now += HEALTH_CHECK_INTERVAL_S
        assert k.due()

    def test_an_undue_check_does_nothing(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])
        k = keeper(recorder)
        k.ensure_connected()
        assert k.ensure_connected() is None

    def test_force_overrides_the_interval(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME, ScreenState.IN_GAME])
        k = keeper(recorder)
        k.ensure_connected()
        assert k.ensure_connected(force=True) is not None


class TestReconnect:
    def test_a_live_session_is_left_alone(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])
        outcome = keeper(recorder).reconnect()
        assert outcome.ready
        assert not outcome.reconnected
        assert (recorder.entry_clicks, recorder.start_clicks) == (0, 0)

    def test_the_entry_sequence_walks_entry_then_start(self) -> None:
        recorder = Recorder(
            [ScreenState.ENTRY, ScreenState.START, ScreenState.LOADING, ScreenState.IN_GAME]
        )
        outcome = keeper(recorder).reconnect()
        assert outcome.ready and outcome.reconnected
        assert (recorder.entry_clicks, recorder.start_clicks) == (1, 1)

    def test_a_start_screen_skips_the_entry_click(self) -> None:
        recorder = Recorder([ScreenState.START, ScreenState.IN_GAME])
        outcome = keeper(recorder).reconnect()
        assert outcome.ready
        assert recorder.entry_clicks == 0
        assert recorder.start_clicks == 1

    def test_an_unknown_screen_is_never_clicked(self) -> None:
        """乱点可能误触派遣、删信或领奖。"""
        recorder = Recorder([ScreenState.UNKNOWN])
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert (recorder.entry_clicks, recorder.start_clicks) == (0, 0)
        assert "unrecognised" in outcome.detail

    def test_start_is_only_clicked_when_start_is_actually_observed(self) -> None:
        """中途读不清时可以继续等，但绝不能凭猜去点 START。"""
        recorder = Recorder([ScreenState.ENTRY] + [ScreenState.UNKNOWN] * 400)
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert recorder.start_clicks == 0

    def test_a_load_that_never_finishes_reports_failure(self) -> None:
        recorder = Recorder([ScreenState.START] + [ScreenState.LOADING] * 200)
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert "did not reach the game" in outcome.detail


class TestSlowTransitions:
    """切屏时间不稳定：固定等待会让整条序列在第一步断掉。"""

    def test_a_slow_entry_transition_is_polled_not_assumed(self) -> None:
        recorder = Recorder(
            [
                ScreenState.ENTRY,  # 初次观察
                ScreenState.ENTRY,  # 点完还没切过去
                ScreenState.ENTRY,
                ScreenState.START,  # 终于切到了
                ScreenState.LOADING,
                ScreenState.IN_GAME,
            ]
        )
        outcome = keeper(recorder).reconnect()
        assert outcome.ready and outcome.reconnected
        assert recorder.entry_clicks == 1

    def test_a_slow_game_load_is_polled(self) -> None:
        recorder = Recorder(
            [ScreenState.START, ScreenState.LOADING, ScreenState.LOADING, ScreenState.IN_GAME]
        )
        assert keeper(recorder).reconnect().ready

    def test_an_entry_that_never_advances_gives_up(self) -> None:
        recorder = Recorder([ScreenState.ENTRY] * 200)
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert recorder.start_clicks == 0


class TestTransientUnknownDuringTransitions:
    """过渡屏 OCR 读出来是花的，那是「此刻读不清」，不是「这一屏认不出」。"""

    def test_a_garbled_transition_is_waited_through(self) -> None:
        recorder = Recorder(
            [
                ScreenState.ENTRY,
                ScreenState.UNKNOWN,  # 过渡中，读不清
                ScreenState.UNKNOWN,
                ScreenState.START,
                ScreenState.IN_GAME,
            ]
        )
        outcome = keeper(recorder).reconnect()
        assert outcome.ready and outcome.reconnected

    def test_a_garbled_load_is_waited_through(self) -> None:
        recorder = Recorder(
            [ScreenState.START, ScreenState.UNKNOWN, ScreenState.UNKNOWN, ScreenState.IN_GAME]
        )
        assert keeper(recorder).reconnect().ready

    def test_an_unknown_screen_before_any_click_still_stops(self) -> None:
        """点击前遇到认不出的画面（维护公告等）仍必须立刻停，不能乱点。"""
        recorder = Recorder([ScreenState.UNKNOWN])
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert (recorder.entry_clicks, recorder.start_clicks) == (0, 0)

    def test_persistent_unknown_eventually_gives_up(self) -> None:
        recorder = Recorder([ScreenState.ENTRY] + [ScreenState.UNKNOWN] * 400)
        outcome = keeper(recorder).reconnect()
        assert not outcome.ready
        assert recorder.start_clicks == 0


class _Restarter:
    """假的「关窗重开」。

    ⚠️ 测试里**绝不许**真的开关窗口或改窗口尺寸——真实现在
    `game_window.restart_game_window` 里，它是注入进来的，所以这里换成计数器。
    """

    def __init__(self, *, fails: bool = False) -> None:
        self.calls = 0
        self._fails = fails

    def __call__(self) -> None:
        self.calls += 1
        if self._fails:
            raise RuntimeError("Chrome 拉不起来")


class TestDeadSessionRestartsTheWindow:
    """「无法重新连接」= 页面已死，入口序列救不了它，只能关窗重开。"""

    def test_a_recoverable_disconnect_does_not_restart_anything(self) -> None:
        """重开是有代价的动作。点一下弹窗就能回去的时候绝不该重开。"""
        recorder = Recorder([ScreenState.DISCONNECTED, ScreenState.ENTRY, ScreenState.IN_GAME])
        dismissed: list[bool] = []
        restart = _Restarter()

        outcome = keeper(
            recorder, dismiss_disconnect=lambda: dismissed.append(True), restart_window=restart
        ).reconnect()

        assert restart.calls == 0, "可恢复的掉线不该关窗口"
        assert dismissed == [True]
        assert outcome.ready

    def test_a_dead_session_restarts_the_window_exactly_once(self) -> None:
        recorder = Recorder(
            [ScreenState.DEAD_SESSION, ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        )
        restart = _Restarter()

        outcome = keeper(recorder, restart_window=restart).reconnect()

        assert restart.calls == 1
        assert outcome.ready and outcome.reconnected

    def test_the_entry_sequence_is_walked_again_after_a_restart(self) -> None:
        """新窗口停在入口页，不是游戏内——「进入」和 START 都得重点一遍。"""
        recorder = Recorder(
            [ScreenState.DEAD_SESSION, ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        )

        keeper(recorder, restart_window=_Restarter()).reconnect()

        assert (recorder.entry_clicks, recorder.start_clicks) == (1, 1)

    def test_the_dead_dialog_is_never_clicked(self) -> None:
        """点掉这个弹窗回不去，已经实机验证过。别在死页面上多留一次点击。"""
        recorder = Recorder(
            [ScreenState.DEAD_SESSION, ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        )
        dismissed: list[bool] = []

        keeper(
            recorder,
            restart_window=_Restarter(),
            dismiss_disconnect=lambda: dismissed.append(True),
        ).reconnect()

        assert dismissed == []

    def test_without_a_restart_action_it_stops_instead_of_looping(self) -> None:
        """没给重开动作就停下并说清卡在哪，而不是退回去点弹窗——那条路没用。"""
        recorder = Recorder([ScreenState.DEAD_SESSION])
        dismissed: list[bool] = []

        outcome = keeper(recorder, dismiss_disconnect=lambda: dismissed.append(True)).reconnect()

        assert not outcome.ready
        assert outcome.state is ScreenState.DEAD_SESSION
        assert dismissed == []
        assert "restart" in outcome.detail

    def test_a_restart_that_blows_up_is_reported_not_raised(self) -> None:
        """重开失败不该把整条链路拖崩——调用方等的是一个结局，不是异常。"""
        recorder = Recorder([ScreenState.DEAD_SESSION])

        outcome = keeper(recorder, restart_window=_Restarter(fails=True)).reconnect()

        assert not outcome.ready
        assert "Chrome 拉不起来" in outcome.detail


class TestRestartBudget:
    """无限重开比不重开更糟：维护期间每次巡检都会撞到这一屏。"""

    def test_the_budget_is_three_per_hour(self) -> None:
        assert (MAX_WINDOW_RESTARTS, RESTART_BUDGET_WINDOW_S) == (3, 3600.0)

    def test_it_stops_restarting_once_the_budget_is_spent(self) -> None:
        recorder = Recorder([])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=2)

        for _ in range(4):
            recorder.states = [ScreenState.DEAD_SESSION]
            guard.reconnect()

        assert restart.calls == 2, "超过上限之后必须停止，而不是接着重开"

    def test_the_refusal_says_why(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        said: list[str] = []
        guard = keeper(recorder, restart_window=_Restarter(), max_restarts=1, log=said.append)
        guard.reconnect()

        recorder.states = [ScreenState.DEAD_SESSION]
        outcome = guard.reconnect()

        assert not outcome.ready
        assert "budget" in outcome.detail
        assert any("维护" in line for line in said)

    def test_the_budget_rolls_forward_instead_of_resetting_on_the_hour(self) -> None:
        """滚动窗口：老得看不见的那次重开不再占配额。

        整点清零会在小时交界处放出双倍配额（59 分连开 3 次，01 分又是 3 次）。
        """
        recorder = Recorder([ScreenState.DEAD_SESSION])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=1)
        guard.reconnect()

        recorder.states = [ScreenState.DEAD_SESSION]
        recorder.now += RESTART_BUDGET_WINDOW_S + 1
        guard.reconnect()

        assert restart.calls == 2

    def test_a_spent_budget_still_blocks_just_inside_the_window(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=1)
        guard.reconnect()

        recorder.states = [ScreenState.DEAD_SESSION]
        recorder.now += RESTART_BUDGET_WINDOW_S - 1
        guard.reconnect()

        assert restart.calls == 1

    def test_a_failed_restart_still_costs_a_slot(self) -> None:
        """否则一个必然失败的重开会被无限重试——正是这里要防的那种循环。"""
        recorder = Recorder([])
        restart = _Restarter(fails=True)
        guard = keeper(recorder, restart_window=restart, max_restarts=2)

        for _ in range(5):
            recorder.states = [ScreenState.DEAD_SESSION]
            guard.reconnect()

        assert restart.calls == 2


class TestRestartIsLoud:
    """重开会让用户看见窗口消失又出现。静默重启事后根本查不出来。"""

    def test_it_says_what_it_read_and_what_it_is_about_to_do(self) -> None:
        recorder = Recorder(
            [ScreenState.DEAD_SESSION, ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        )
        said: list[str] = []

        keeper(recorder, restart_window=_Restarter(), log=said.append).reconnect()

        joined = "\n".join(said)
        assert "无法重新连接" in joined, "得说清是读到什么才决定重开的"
        assert "重开" in joined
        assert "入口页" in joined, "得说清重开之后还要重走入口序列"

    def test_a_live_session_says_nothing(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])
        said: list[str] = []

        keeper(recorder, restart_window=_Restarter(), log=said.append).reconnect()

        assert said == []


class TestInGameMarkersSurviveOcr:
    """判据必须用 OCR 读得稳的词，否则在线的会话会被判成「认不出」。"""

    def test_the_real_ocr_output_of_the_nav_bar_is_recognised(self) -> None:
        # 实测读数：行星/舰队/太空舱 全被读错，只有 商店、联盟 是准的。
        assert classify_screen("和量 般队 太空能 商店 联盟") is ScreenState.IN_GAME

    def test_the_shop_marker_alone_is_enough(self) -> None:
        assert classify_screen("商店") is ScreenState.IN_GAME

    def test_the_alliance_marker_alone_is_enough(self) -> None:
        assert classify_screen("联盟") is ScreenState.IN_GAME

    def test_the_system_view_nav_is_still_recognised(self) -> None:
        assert classify_screen("银河系 恒星系 行星") is ScreenState.IN_GAME
