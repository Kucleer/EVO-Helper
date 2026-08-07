from __future__ import annotations

from evo_helper.game.session_keeper import (
    HEALTH_CHECK_INTERVAL_S,
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
