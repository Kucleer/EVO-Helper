from __future__ import annotations

import pytest

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
        """认不出就 UNKNOWN，然后停止——不猜。

        ⚠️ 这里原先拿「服务器维护中，请稍后再试」当例子。2026-08-15 03:30 那一晚
        它真的出现了，而且**把整晚堵死了**（详见下面维护那条）。所以它现在是
        认得出的一档，例子换成了别的。这条本身仍然成立：没教过的画面不许猜。
        """
        assert classify_screen("加载时发生了意外错误，请重试。") is ScreenState.UNKNOWN
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


class TestBudgetOnTheOutcome:
    """**剩余配额要跟着结局一起交出去。**

    调用方拿它回答一个问题：巡检没能回到游戏内时，这一轮该按
    `EXIT_ENVIRONMENT_BUSY`（不计入连续失败）收场，还是按硬失败收场。
    判据整段在 `domain.scheduler.exit_code_for_environment_fault`——一句话：
    配额是滚动窗口内**有限**的，所以「还有配额就豁免」这条判据必然有尽头；
    换成一条没有尽头的判据，坏掉的环境就会整夜静默空转。
    """

    def test_a_healthy_session_reports_the_full_budget(self) -> None:
        outcome = keeper(Recorder([ScreenState.IN_GAME]), restart_window=_Restarter()).reconnect()

        assert outcome.restarts_left == MAX_WINDOW_RESTARTS

    def test_each_restart_eats_one(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        guard = keeper(recorder, restart_window=_Restarter(), max_restarts=2)

        outcome = guard.reconnect()

        assert outcome.restarts_left == 1

    def test_a_spent_budget_reports_nothing_left(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        guard = keeper(recorder, restart_window=_Restarter(), max_restarts=1)
        guard.reconnect()

        recorder.states = [ScreenState.DEAD_SESSION]
        outcome = guard.reconnect()

        assert not outcome.ready
        assert outcome.restarts_left == 0, "配额耗尽 = 这不是暂时的，调用方要按硬失败收场"

    def test_an_unrecognised_screen_carries_the_budget_too(self) -> None:
        """`UNKNOWN` 那条分支提前返回，一次重开都没走——它同样要如实报配额。"""
        guard = keeper(Recorder([ScreenState.UNKNOWN]), restart_window=_Restarter())

        assert guard.reconnect().restarts_left == MAX_WINDOW_RESTARTS

    def test_no_restart_action_means_no_budget_at_all(self) -> None:
        """没注入重开动作时重开这条路压根不存在，说「还有配额」就是骗调用方。"""
        guard = keeper(Recorder([ScreenState.UNKNOWN]))

        assert guard.reconnect().restarts_left == 0

    def test_the_budget_rolls_forward_on_the_outcome_as_well(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        guard = keeper(recorder, restart_window=_Restarter(), max_restarts=1)
        guard.reconnect()

        recorder.states = [ScreenState.UNKNOWN]
        recorder.now += RESTART_BUDGET_WINDOW_S + 1

        assert guard.reconnect().restarts_left == 1

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


class TestRestartAndReenter:
    """画面恢复不了时的兜底入口：不是掉线，但就是回不去，那就关窗重开。

    用户口径（2026-08-11）：「切不回就重启，这是兜底策略。」实机上倒在
    「读完邮件切不回恒星系视图」——画面上一个「掉线」字样都没有，于是
    `reconnect` 那条重连路（判据是「连接已断开」/「无法重新连接」）压根不会
    被触发，整轮就地停摆、退出码 1。
    """

    @pytest.fixture(autouse=True)
    def _no_real_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ **本类的每一条测试都不许碰真窗口。**

        照 `test_game_window.TestRestartGameWindow._no_real_windows` 的做法办：
        那个 fixture 是整改验证时真把用户的游戏窗口从 1920x917 拽成 1894x556
        之后才加的。`SessionKeeper` 的重开动作是注入进来的（这里注入 `_Restarter`），
        真实现在 `game_window` 里；一旦有人把真调用接回来，这里立刻炸，而且什么都没动。
        """
        from evo_helper.game import game_window

        def explode(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("测试绝不许碰真窗口：重开动作必须是注入进来的假货")

        for name in ("ensure_game_window", "restart_game_window", "resize_to_viewport"):
            monkeypatch.setattr(game_window, name, explode)

    def test_it_restarts_and_walks_the_entry_sequence_again(self) -> None:
        """重开之后新窗口停在入口页，「进入」和 START 都得重点一遍。"""
        recorder = Recorder([ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME])
        restart = _Restarter()

        outcome = keeper(recorder, restart_window=restart).restart_and_reenter("切不回恒星系视图")

        assert restart.calls == 1
        assert outcome.ready and outcome.reconnected
        assert (recorder.entry_clicks, recorder.start_clicks) == (1, 1)

    def test_it_reuses_the_entry_sequence_rather_than_a_copy_of_it(self) -> None:
        """入口序列只有一份：慢过渡要轮询、不许固定等待——那几条来之不易。

        复制一份的话，这条会绿在 `reconnect` 上、红在这里。
        """
        recorder = Recorder(
            [
                ScreenState.ENTRY,
                ScreenState.ENTRY,  # 点完还没切过去
                ScreenState.UNKNOWN,  # 过渡中读不清
                ScreenState.START,
                ScreenState.LOADING,
                ScreenState.IN_GAME,
            ]
        )

        outcome = keeper(recorder, restart_window=_Restarter()).restart_and_reenter("切不回视图")

        assert outcome.ready
        assert recorder.entry_clicks == 1, "慢过渡要轮询，不能重点一次「进入」"

    def test_a_restart_that_lands_nowhere_is_reported_not_clicked_through(self) -> None:
        """⚠️ 重开之后**不许**因为「刚重开过」就假定自己在游戏内。"""
        recorder = Recorder([ScreenState.UNKNOWN] * 400)

        outcome = keeper(recorder, restart_window=_Restarter()).restart_and_reenter("切不回视图")

        assert not outcome.ready
        assert (recorder.entry_clicks, recorder.start_clicks) == (0, 0)

    def test_the_reason_is_what_gets_logged(self) -> None:
        """重开是有代价的动作：日志得说清是什么把它逼到这一步。"""
        recorder = Recorder([ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME])
        said: list[str] = []

        keeper(recorder, restart_window=_Restarter(), log=said.append).restart_and_reenter(
            "读完邮件切不回恒星系视图"
        )

        assert any("读完邮件切不回恒星系视图" in line for line in said)

    def test_it_still_needs_a_restart_action(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])

        outcome = keeper(recorder).restart_and_reenter("切不回视图")

        assert not outcome.ready
        assert "restart" in outcome.detail

    def test_a_refusal_is_not_dressed_up_as_a_dead_session(self) -> None:
        """这条路上画面并没有写着「无法重新连接」，结局不该那么说。"""
        recorder = Recorder([ScreenState.IN_GAME])

        outcome = keeper(recorder).restart_and_reenter("切不回视图")

        assert outcome.state is not ScreenState.DEAD_SESSION

    def test_a_restart_that_blows_up_is_reported_not_raised(self) -> None:
        recorder = Recorder([ScreenState.IN_GAME])

        outcome = keeper(recorder, restart_window=_Restarter(fails=True)).restart_and_reenter("x")

        assert not outcome.ready
        assert "Chrome 拉不起来" in outcome.detail


class TestTheRestartBudgetIsShared:
    """⚠️ **两条路必须共用同一份配额。**

    服务端维护时「无法重新连接」和「视图切不回来」都会撞上。各记各的账就等于
    把上限翻倍，`MAX_WINDOW_RESTARTS` 那道拦无限重启的闸门就形同虚设。
    """

    def test_a_view_failure_is_refused_once_dead_sessions_spent_the_budget(self) -> None:
        recorder = Recorder([ScreenState.DEAD_SESSION])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=1)

        guard.reconnect()  # 死会话用掉唯一的名额
        recorder.states = [ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        outcome = guard.restart_and_reenter("切不回恒星系视图")

        assert restart.calls == 1, "配额已被死会话那条用光，这一次不该再重开"
        assert not outcome.ready
        assert "budget" in outcome.detail

    def test_a_dead_session_is_refused_once_view_failures_spent_the_budget(self) -> None:
        """反方向同样成立，否则只是把翻倍挪了个方向。"""
        recorder = Recorder([ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=1)

        guard.restart_and_reenter("切不回恒星系视图")
        recorder.states = [ScreenState.DEAD_SESSION]
        outcome = guard.reconnect()

        assert restart.calls == 1
        assert "budget" in outcome.detail

    def test_the_two_paths_share_one_rolling_window(self) -> None:
        """配额是滚动窗口，不是两个各自计数的桶。"""
        recorder = Recorder([ScreenState.DEAD_SESSION])
        restart = _Restarter()
        guard = keeper(recorder, restart_window=restart, max_restarts=1)

        guard.reconnect()
        recorder.now += RESTART_BUDGET_WINDOW_S + 1
        recorder.states = [ScreenState.ENTRY, ScreenState.START, ScreenState.IN_GAME]
        outcome = guard.restart_and_reenter("切不回恒星系视图")

        assert restart.calls == 2, "老得看不见的那次重开不该再占配额"
        assert outcome.ready


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


def test_a_maintenance_notice_is_recognised_before_anything_underneath_it() -> None:
    """⚠️⚠️ **2026-08-15 03:30 实机：这一屏把整晚堵死了。**

    服务器停机维护，游戏弹出一张公告**盖在 START 页上**。助手完全不认识它，
    而 `START_ROI` 那个位置上坐着的是公告的「知道了」按钮——于是
    `start_button` 一遍遍读到「知道了」、一遍遍判「读不出 START」，
    bot 链路空转了二十分钟，一发都没派。

    判据必须排在最前：公告是浮层，底下的 START 与导航条照样读得出来，
    后判就会把一台**停机的服务器**认成「在 START 页上」，然后一路点下去。
    这和掉线弹窗那条是同一个道理，这是它的第二个实例。
    """
    assert classify_screen("服务器维护") is ScreenState.MAINTENANCE
    # 浮层底下透出来的字一起读到时，仍然要判成维护而不是 START / 在线。
    assert classify_screen("服务器维护 START") is ScreenState.MAINTENANCE
    assert classify_screen("服务器维护 商店 联盟") is ScreenState.MAINTENANCE


def test_a_plain_start_screen_is_not_mistaken_for_maintenance() -> None:
    """公告点掉之后回到的就是 START 页，误判会让助手反复去点一个不存在的按钮。"""
    assert classify_screen("START") is ScreenState.START
