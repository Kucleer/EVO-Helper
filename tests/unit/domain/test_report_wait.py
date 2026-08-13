from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.records import MISSION_KIND_ATTACK, MISSION_KIND_SCOUT
from evo_helper.domain.report_wait import (
    BATCH_WINDOW,
    MAX_SESSION_BACKOFF,
    MIN_CREDIBLE_ATTACK_FLIGHT,
    PendingReport,
    ReportWaitPlanner,
    SessionBackoff,
    WaitAction,
    parse_game_duration,
    vet_flight_time,
)

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

#: 改为 5 秒之后的默认唤醒余量。预计时间是本地记的发出时刻加简报读到的飞行时长，
#: 精度足够，不需要原先那 1 分钟——那是没有可靠预计时间的年代留下的。
MARGIN = timedelta(seconds=5)


class TestGameDuration:
    """游戏内倒计时格式：`X天Y时Z分W秒`，缺省段会被省略。"""

    def test_full_form(self) -> None:
        assert parse_game_duration("42天17时34分58秒") == timedelta(
            days=42, hours=17, minutes=34, seconds=58
        )

    def test_without_days(self) -> None:
        assert parse_game_duration("8时3分20秒") == timedelta(hours=8, minutes=3, seconds=20)

    def test_minutes_and_seconds_only(self) -> None:
        assert parse_game_duration("3分20秒") == timedelta(minutes=3, seconds=20)

    def test_seconds_only(self) -> None:
        assert parse_game_duration("45秒") == timedelta(seconds=45)

    def test_colon_form(self) -> None:
        """顶部栏用的是 `01:53:19` 这种冒号格式。"""
        assert parse_game_duration("01:53:19") == timedelta(hours=1, minutes=53, seconds=19)

    def test_surrounding_text_is_tolerated(self) -> None:
        assert parse_game_duration("剩余 8时3分20秒 抵达") == timedelta(
            hours=8, minutes=3, seconds=20
        )

    def test_unreadable_returns_none(self) -> None:
        assert parse_game_duration("即将抵达") is None
        assert parse_game_duration("") is None

    def test_zero_duration_is_rejected(self) -> None:
        """读成 0 说明没读到数字，不能当成「已抵达」。"""
        assert parse_game_duration("0秒") is None


#: OCR 把某一段认错之后的实测输入，以及**真值**。
#:
#: 每一条老实现都返回一个看起来完全合理的错值、不报错、不打日志：
#:
#:     3夭19旪36分7秒  ->  0:36:07        （少了 3 天 19 小时）
#:     3天19时36外7秒  ->  3 days, 19:00  （少了 36 分 7 秒）
#:     З天19时36分7秒  ->  19:36:07       （少了 3 天）
#:     3天19:36分7秒   ->  3 days, 0:00   （少了 19:36:07）
#:     2旪15分7秒      ->  0:15:07        （少了 2 小时）
#:
#: 断言的一律是**返回 None**，不是「返回某个截断值」。
GARBLED_DURATIONS = [
    pytest.param("3夭19旪36分7秒", id="天与时都被认错"),
    pytest.param("3天19时36外7秒", id="分被认错"),
    pytest.param("З天19时36分7秒", id="首位数字被认成西里尔字母"),
    pytest.param("3天19:36分7秒", id="时被认成冒号"),
    pytest.param("2旪15分7秒", id="时被认错-只剩分秒"),
    pytest.param("3天19时36分Ⅶ秒", id="末段数字被认错-单位还在"),
]


class TestGarbledDurationIsRejected:
    """OCR 认错一段之后，**整条都不许读出来**。

    老实现用 `finditer` 逐个找、取第一个含数字的匹配，于是「前面糊了就丢掉
    前面」：返回的碎片自己是一条合法的时长，小两三个数量级却一声不响。
    生产库 `attack_dispatches` 里 197 发有飞行时长的攻击，66 发落在 0–60 秒、
    最大值正好 **59 秒**（一个「秒」字段能装下的最大数），而 60–300 秒一发都
    没有——那 66 发就是这条路径的指纹。

    后果分两头：`expected_report_at_utc` 读成 7 秒 → 战报一产生就被判到点，
    每趟信箱都白烧开封预算；`line_free_at_utc` 读成 7 秒 → 调度器以为航线
    十几秒后就空，接着派、撞上游戏的「同时派遣的舰队数量已达上限」。
    """

    @pytest.mark.parametrize("text", GARBLED_DURATIONS)
    def test_partial_match_returns_none(self, text: str) -> None:
        assert parse_game_duration(text) is None

    def test_the_intact_form_still_parses(self) -> None:
        """对照组：同一串没被认错时必须照旧读得出来。

        没有这一条，「一律返回 None」也能让上面那组全绿。
        """
        assert parse_game_duration("3天19时36分7秒") == timedelta(
            days=3, hours=19, minutes=36, seconds=7
        )

    def test_a_leftover_digit_anywhere_rejects(self) -> None:
        """匹配之外还剩数字 = 有一段没成链。"""
        assert parse_game_duration("19x36分7秒") is None

    def test_a_stranded_unit_on_the_left_rejects(self) -> None:
        """数字被吃掉、单位还杵在紧邻左边：外面一个数字都不剩，照样不许读。"""
        assert parse_game_duration("时36分7秒") is None

    def test_noise_between_segments_rejects(self) -> None:
        """段间只允许空白。插进噪声就等于把数字配到隔了一段的单位上。"""
        assert parse_game_duration("3天.19时36分7秒") is None


class TestFlightTimeVetting:
    """第二道防线：攻击读出小于 3 分钟的，当没读出来。"""

    def test_short_attack_flight_is_rejected(self) -> None:
        """生产库里攻击的两簇之间（60–300 秒）一发都没有，59 秒不是物理量。"""
        assert vet_flight_time(timedelta(seconds=59), mission_kind=MISSION_KIND_ATTACK) is None
        assert vet_flight_time(timedelta(seconds=7), mission_kind=MISSION_KIND_ATTACK) is None

    def test_the_boundary_is_three_minutes(self) -> None:
        """3 分钟整放行：下限是「低于就丢」，不是「不高于就丢」。

        卡在 3 分钟而不是当前科技的真实下限 5 分钟——科技会升级、舰队会变快。
        """
        assert MIN_CREDIBLE_ATTACK_FLIGHT == timedelta(minutes=3)
        exactly = MIN_CREDIBLE_ATTACK_FLIGHT
        assert vet_flight_time(exactly, mission_kind=MISSION_KIND_ATTACK) == exactly

    def test_normal_attack_flight_passes(self) -> None:
        """生产库里攻击那一簇的最小值 300 秒，以及最长的 1877 秒。"""
        for seconds in (300, 907, 1877):
            flight = timedelta(seconds=seconds)
            assert vet_flight_time(flight, mission_kind=MISSION_KIND_ATTACK) == flight

    def test_scout_flights_are_untouched(self) -> None:
        """侦察不进这道闸门——**不是**因为它那批历史值可信。

        生产库 371 发侦察落在 14–135 秒，但那批数字本身就疑似截断产物：
        最久的几发全是 135 秒（= 2 分 15 秒）、次一批全是 121 秒（= 2 分 1 秒），
        都是「分+秒」两段的形状，而飞得最久的那几发打的偏偏是主星系内最近的
        目标。所以侦察量不出下限来，这道闸门就不该管它；它靠读全校验那一道防。

        这条钉的是「别顺手把攻击的下限推广到所有发次」。
        """
        for seconds in (14, 121):
            flight = timedelta(seconds=seconds)
            assert vet_flight_time(flight, mission_kind=MISSION_KIND_SCOUT) == flight

    def test_unknown_mission_kinds_are_untouched(self) -> None:
        """还没被量过的发次类型没有理由套用攻击的经验值。"""
        flight = timedelta(seconds=7)
        assert vet_flight_time(flight, mission_kind="EXPEDITION") == flight

    def test_none_stays_none(self) -> None:
        assert vet_flight_time(None, mission_kind=MISSION_KIND_ATTACK) is None


def pending(minutes: int, closed: bool = False) -> PendingReport:
    return PendingReport(
        dispatch_id=f"d{minutes}",
        expected_report_at_utc=NOW + timedelta(minutes=minutes),
        closed=closed,
    )


def pending_s(seconds: int, closed: bool = False) -> PendingReport:
    """秒级的到期时间——批量分组的窗口是 60 秒，用分钟表达不出来。"""
    return PendingReport(
        dispatch_id=f"s{seconds}",
        expected_report_at_utc=NOW + timedelta(seconds=seconds),
        closed=closed,
    )


class TestWaitPlanner:
    def planner(self) -> ReportWaitPlanner:
        return ReportWaitPlanner()

    def test_no_dispatch_means_complete(self) -> None:
        plan = self.planner().plan((), now_utc=NOW)
        assert plan.action is WaitAction.COMPLETE

    def test_all_closed_means_complete(self) -> None:
        plan = self.planner().plan((pending(-30, closed=True),), now_utc=NOW)
        assert plan.action is WaitAction.COMPLETE

    def test_a_due_report_is_collected_now(self) -> None:
        plan = self.planner().plan((pending(-1),), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_a_future_report_makes_the_run_wait(self) -> None:
        plan = self.planner().plan((pending(90),), now_utc=NOW)
        assert plan.action is WaitAction.WAIT
        # 默认余量 5 秒。
        assert plan.resume_at_utc == NOW + timedelta(minutes=90) + MARGIN

    def test_wait_targets_the_earliest_pending_report(self) -> None:
        """三份相隔都远超批量窗口，所以只等最早那一份，不并组。"""
        plan = self.planner().plan((pending(200), pending(45), pending(120)), now_utc=NOW)
        assert plan.resume_at_utc == NOW + timedelta(minutes=45) + MARGIN

    def test_the_default_margin_is_conservative_but_small(self) -> None:
        """余量存在是为了少抢一次会话，但不能大到错过战报有效期。"""
        plan = self.planner().plan((pending(60),), now_utc=NOW)
        assert (
            timedelta(0)
            < plan.resume_at_utc - (NOW + timedelta(minutes=60))
            <= timedelta(minutes=5)
        )

    def test_closed_dispatches_do_not_hold_the_run_open(self) -> None:
        plan = self.planner().plan(
            (pending(10, closed=True), pending(-5)),
            now_utc=NOW,
        )
        assert plan.action is WaitAction.COLLECT

    def test_a_due_report_wins_over_a_later_pending_one(self) -> None:
        """先收能收的，剩下的继续等——不要为了等最后一个而不收已到的。"""
        plan = self.planner().plan((pending(-5), pending(300)), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_unknown_expected_time_is_collected_rather_than_waited_forever(self) -> None:
        """飞行时间没读到时不能无限等，改为立即尝试收取。"""
        unknown = PendingReport(dispatch_id="d?", expected_report_at_utc=None, closed=False)
        plan = self.planner().plan((unknown,), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_margin_delays_the_wake_up(self) -> None:
        """提前登录只是白跑一趟，但每次白跑都要抢一次会话，所以留余量。"""
        planner = ReportWaitPlanner(margin=timedelta(minutes=2))
        plan = planner.plan((pending(30),), now_utc=NOW)
        assert plan.resume_at_utc == NOW + timedelta(minutes=32)

    def test_margin_does_not_delay_an_already_due_report(self) -> None:
        planner = ReportWaitPlanner(margin=timedelta(minutes=2))
        assert planner.plan((pending(-10),), now_utc=NOW).action is WaitAction.COLLECT

    def test_the_default_margin_is_five_seconds(self) -> None:
        plan = self.planner().plan((pending_s(600),), now_utc=NOW)
        assert plan.resume_at_utc == NOW + timedelta(seconds=600) + MARGIN


class TestReportBatching:
    """同一个读取窗口里到期的战报，一趟收完。

    每一趟收取都要 `ensure_game_window()` + 认屏 + 进信箱，中间还夹一次任务切换。
    10:00:00 和 10:00:30 各一份就跑两趟，是纯粹的浪费。
    """

    def planner(self) -> ReportWaitPlanner:
        return ReportWaitPlanner()

    def test_a_due_report_waits_for_a_near_neighbour(self) -> None:
        """**这条是批量分组的看门狗。**

        一份 10 秒前就到期了、另一份 30 秒后到期。按「有任一份到期就去收」会
        立刻起一趟，然后 30 秒后再起第二趟；批量分组要求把这一下并成一趟。
        去掉分组这条会立刻变红。
        """
        plan = self.planner().plan((pending_s(-10), pending_s(30)), now_utc=NOW)
        assert plan.action is WaitAction.WAIT
        assert plan.resume_at_utc == NOW + timedelta(seconds=30) + MARGIN

    def test_the_group_is_collected_once_its_latest_member_is_due(self) -> None:
        """等到组里**最晚**的那份到期（再加余量）才去收，那时一趟能读全。"""
        at = NOW + timedelta(seconds=30) + MARGIN
        plan = self.planner().plan((pending_s(-10), pending_s(30)), now_utc=at)
        assert plan.action is WaitAction.COLLECT

    def test_a_distant_report_does_not_join_the_group(self) -> None:
        """窗口外的那份不并进来——为它多等下去就不是省一趟，是白白压着已到的战报。"""
        plan = self.planner().plan((pending_s(-10), pending_s(300)), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_the_group_is_measured_from_the_earliest_not_transitively(self) -> None:
        """分组按「距最早那份多远」算，不是一份挨一份地续下去。

        续着算的话，每 59 秒来一份就能把收取无限期推后——本该有界的等待
        变成永远不收。这里 100 秒那份必须落在组外，等待封顶在
        `BATCH_WINDOW + margin`。
        """
        plan = self.planner().plan((pending_s(0), pending_s(50), pending_s(100)), now_utc=NOW)
        assert plan.resume_at_utc == NOW + timedelta(seconds=50) + MARGIN
        assert plan.resume_at_utc is not None
        assert plan.resume_at_utc - NOW <= BATCH_WINDOW + MARGIN

    def test_an_unknown_expected_time_is_never_batched(self) -> None:
        """NULL 没有可比的到期时间，不参与分组，也不该被组里那份拖住。

        「读不到飞行时间就立即收取」是既定的降级语义（宁可白跑，也不能无限等）。
        让它去等一个 60 秒后的邻居，等于把这条降级悄悄改成了延迟。
        """
        unknown = PendingReport(dispatch_id="d?", expected_report_at_utc=None, closed=False)
        plan = self.planner().plan((unknown, pending_s(30)), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_closed_reports_do_not_drag_the_group_forward(self) -> None:
        """已闭合的不在待收之列，更不能把收取时刻往后拖。"""
        plan = self.planner().plan((pending_s(-10), pending_s(30, closed=True)), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT

    def test_the_batch_window_is_configurable(self) -> None:
        planner = ReportWaitPlanner(margin=timedelta(0), batch_window=timedelta(seconds=10))
        # 30 秒那份落在 10 秒的窗口外，于是已到期的那份立刻收。
        plan = planner.plan((pending_s(-5), pending_s(30)), now_utc=NOW)
        assert plan.action is WaitAction.COLLECT


class TestSessionBackoff:
    """助手不能和用户抢登录：退避重试，用户有优先权。"""

    def test_first_retry_is_short(self) -> None:
        assert SessionBackoff().delay_for(attempt=1) == timedelta(seconds=30)

    def test_delay_doubles(self) -> None:
        backoff = SessionBackoff()
        assert backoff.delay_for(attempt=2) == timedelta(minutes=1)
        assert backoff.delay_for(attempt=3) == timedelta(minutes=2)

    def test_delay_is_capped(self) -> None:
        assert SessionBackoff().delay_for(attempt=99) == MAX_SESSION_BACKOFF

    def test_cap_is_not_absurdly_long(self) -> None:
        """封顶要足够短，否则战报可能在助手醒来前就过期了。"""
        assert MAX_SESSION_BACKOFF <= timedelta(minutes=30)

    def test_attempt_must_be_positive(self) -> None:
        with pytest.raises(ValueError, match="attempt"):
            SessionBackoff().delay_for(attempt=0)

    def test_gives_up_after_the_configured_attempts(self) -> None:
        backoff = SessionBackoff(max_attempts=3)
        assert not backoff.exhausted(attempt=3)
        assert backoff.exhausted(attempt=4)

    def test_giving_up_is_a_safe_pause_not_a_failure(self) -> None:
        """拿不到会话不是系统故障，是用户在玩——安全暂停，等人工恢复。"""
        assert SessionBackoff().pause_reason(attempt=99).startswith("session unavailable")


class TestWaitStatesAreControllable:
    """舰队在飞的几个小时里，用户必须仍能干预。"""

    def test_waiting_states_can_be_paused_and_stopped(self) -> None:
        from evo_helper.domain.models import RunState
        from evo_helper.domain.state_machine import can_transition

        for waiting in (RunState.AWAITING_REPORT, RunState.WAITING_SESSION):
            for target in (RunState.PAUSED, RunState.EMERGENCY_STOPPED, RunState.FAILED):
                assert can_transition(waiting, target), (waiting, target)

    def test_draining_can_release_the_session_and_come_back(self) -> None:
        from evo_helper.domain.models import RunState
        from evo_helper.domain.state_machine import can_transition

        assert can_transition(RunState.DRAINING, RunState.AWAITING_REPORT)
        assert can_transition(RunState.AWAITING_REPORT, RunState.DRAINING)

    def test_a_lost_session_retries_rather_than_failing(self) -> None:
        from evo_helper.domain.models import RunState
        from evo_helper.domain.state_machine import can_transition

        assert can_transition(RunState.AWAITING_REPORT, RunState.WAITING_SESSION)
        assert can_transition(RunState.WAITING_SESSION, RunState.AWAITING_REPORT)
        assert can_transition(RunState.WAITING_SESSION, RunState.DRAINING)

    def test_resuming_a_paused_wait_keeps_waiting(self) -> None:
        """在等战报时被暂停，恢复后应该接着等，而不是从头重新扫描。"""
        from evo_helper.domain.models import RunState
        from evo_helper.domain.state_machine import can_transition

        assert can_transition(RunState.PAUSED, RunState.AWAITING_REPORT)

    def test_a_wait_cannot_jump_straight_to_completed(self) -> None:
        """只有收完战报（经由 DRAINING）才算结束。"""
        from evo_helper.domain.models import RunState
        from evo_helper.domain.state_machine import can_transition

        assert not can_transition(RunState.AWAITING_REPORT, RunState.COMPLETED)
