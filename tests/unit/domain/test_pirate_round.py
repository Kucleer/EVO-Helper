"""海盗目标在一轮里走的那几态。

用户要的三态是「不触发攻击 / 触发攻击 / 攻击完成」；这里落成四个终态，
多出来的 `SCOUT_UNREADABLE` 是刻意的——依据见 `domain.pirate_round` 的模块头。
所以这个文件里分量最重的断言是「没看清」不许塌成「不触发攻击」。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.pirate_round import (
    MAX_SCOUTS_PER_DAY,
    PHASE_LABELS,
    AttackFact,
    PirateAction,
    PiratePhase,
    action_for,
    phase_for,
    phase_of,
)
from evo_helper.domain.scout_verdict import (
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
)


class TestBeforeTheScoutReport:
    def test_nothing_dispatched_at_all_still_needs_a_scout(self) -> None:
        assert phase_for(scouted=False, verdict=None, attacks=()) is PiratePhase.NEEDS_SCOUT

    def test_a_scout_in_flight_waits_for_its_report(self) -> None:
        assert phase_of(verdict=None, attacks=()) is PiratePhase.AWAITING_SCOUT_REPORT
        assert (
            phase_for(scouted=True, verdict=None, attacks=()) is PiratePhase.AWAITING_SCOUT_REPORT
        )


class TestAfterTheScoutReport:
    def test_skip_is_the_only_verdict_that_means_no_attack(self) -> None:
        assert phase_of(verdict=VERDICT_SKIP, attacks=()) is PiratePhase.NO_ATTACK

    def test_unreadable_is_not_no_attack(self) -> None:
        """「没看清」与「不触发攻击」必须是两个态。

        合成一个的后果不是显示不好看：库里 98 份侦察报告里 28 份非攻击判定
        **全部**是 `UNREADABLE`，合并之后界面会把这 28 个海盗一律标成
        「不值得打」，而真相是有一格从来没读出来过。
        """
        phase = phase_of(verdict=VERDICT_UNREADABLE, attacks=())

        assert phase is PiratePhase.SCOUT_UNREADABLE
        assert phase is not PiratePhase.NO_ATTACK
        assert PHASE_LABELS[phase] != PHASE_LABELS[PiratePhase.NO_ATTACK]

    def test_a_verdict_of_attack_with_nothing_dispatched_is_its_own_state(self) -> None:
        # 判定要打却一发没出去（航线满、面板认不出、找不到预设……）要有名字，
        # 不能和「不触发攻击」混成一片。
        assert phase_of(verdict=VERDICT_ATTACK, attacks=()) is PiratePhase.NEEDS_ATTACK

    def test_an_unknown_verdict_is_refused_rather_than_guessed(self) -> None:
        with pytest.raises(ValueError, match="未知的侦察判定"):
            phase_of(verdict="MAYBE", attacks=())


class TestAfterTheAttack:
    def test_a_dispatched_attack_waits_for_its_battle_report(self) -> None:
        attacks = (AttackFact(has_report=False),)

        assert phase_of(verdict=VERDICT_ATTACK, attacks=attacks) is (
            PiratePhase.AWAITING_ATTACK_REPORT
        )

    def test_the_battle_report_completes_the_target(self) -> None:
        attacks = (AttackFact(has_report=True),)

        assert phase_of(verdict=VERDICT_ATTACK, attacks=attacks) is PiratePhase.ATTACK_DONE

    def test_one_missing_report_out_of_two_still_waits(self) -> None:
        attacks = (AttackFact(has_report=True), AttackFact(has_report=False))

        assert phase_of(verdict=VERDICT_ATTACK, attacks=attacks) is (
            PiratePhase.AWAITING_ATTACK_REPORT
        )

    @pytest.mark.parametrize("verdict", [None, VERDICT_SKIP, VERDICT_UNREADABLE])
    def test_a_dispatched_attack_outranks_whatever_the_verdict_says_now(
        self, verdict: str | None
    ) -> None:
        """判定是会变的规则，攻击是已经发生的事实。

        规则改了、或者报告后来被重读成别的结论，都不该让一发已经飞出去的
        攻击在界面上退回「不触发攻击」。
        """
        assert phase_of(verdict=verdict, attacks=(AttackFact(has_report=True),)) is (
            PiratePhase.ATTACK_DONE
        )


def test_every_phase_has_its_own_chinese_label() -> None:
    # 措辞塌成同一句，四态就白分了。
    labels = [PHASE_LABELS[phase] for phase in PiratePhase]

    assert set(PHASE_LABELS) == set(PiratePhase)
    assert len(set(labels)) == len(labels)


class TestWhatToDoAboutIt:
    """态 → 动作。用户口径 2026-08-13：海盗刷新是当日内（游戏内 UTC+0）。

    实机账（2026-08-13 通宵 UTC 15:00–19:00）：侦察 111 发打在 54 个坐标上
    （2:137:1~4 各 5 发），攻击只有 12 发。判据 2026-08-11 就写好了，缺的是
    没有人问它——所以这一组钉的是**每个态各自该做什么**，一态一条。
    """

    def test_a_target_untouched_today_gets_a_scout(self) -> None:
        assert action_for(PiratePhase.NEEDS_SCOUT, scout_count=0) is PirateAction.SCOUT

    def test_a_scout_already_in_flight_is_not_scouted_again(self) -> None:
        """**这一条是 111 发那笔账的正主。**

        原先每一轮都当作今天没侦察过：认出是海盗就发一发，四个坐标一轮四发。
        """
        assert action_for(PiratePhase.AWAITING_SCOUT_REPORT, scout_count=1) is PirateAction.WAIT

    def test_a_verdict_of_attack_attacks_without_scouting_again(self) -> None:
        """报告已经判为「打」，再派一发侦察只是把配额烧掉再得出同一个结论。"""
        assert action_for(PiratePhase.NEEDS_ATTACK, scout_count=1) is PirateAction.ATTACK

    def test_an_attack_in_flight_is_left_alone(self) -> None:
        assert action_for(PiratePhase.AWAITING_ATTACK_REPORT, scout_count=1) is PirateAction.WAIT

    def test_a_finished_attack_ends_the_day_for_that_target(self) -> None:
        """今天已经攻击过 → 不侦查、不攻击。"""
        assert action_for(PiratePhase.ATTACK_DONE, scout_count=1) is PirateAction.DONE

    def test_a_fully_read_empty_pirate_is_not_scouted_again(self) -> None:
        """四格都读全了、都 ≤ 1：结论是确定的，再读一次还是它。"""
        assert action_for(PiratePhase.NO_ATTACK, scout_count=1) is PirateAction.DONE

    def test_an_unreadable_report_earns_exactly_one_make_up_scout(self) -> None:
        """**这一条是本组分量最重的：`UNREADABLE` 与 `SKIP` 的处置相反。**

        没看清 ≠ 这里是空的。补一次是为了给「下一次可能读全」一个机会；
        补完还是没看清就收手——ROI 落空是系统性的（库里 98 份报告里
        `收割者` 一格一份都没读出来），补第三次读到的还是同一个空格子。
        """
        assert action_for(PiratePhase.SCOUT_UNREADABLE, scout_count=1) is PirateAction.SCOUT
        assert action_for(PiratePhase.SCOUT_UNREADABLE, scout_count=2) is PirateAction.DONE

    def test_the_make_up_scout_is_capped_at_the_stated_maximum(self) -> None:
        """封顶就是 `MAX_SCOUTS_PER_DAY`，不是「再来一次」这种相对说法。"""
        assert MAX_SCOUTS_PER_DAY == 2
        for count in range(MAX_SCOUTS_PER_DAY):
            assert action_for(PiratePhase.SCOUT_UNREADABLE, scout_count=count) is PirateAction.SCOUT
        for count in (MAX_SCOUTS_PER_DAY, MAX_SCOUTS_PER_DAY + 3):
            assert action_for(PiratePhase.SCOUT_UNREADABLE, scout_count=count) is PirateAction.DONE

    def test_the_scout_count_only_matters_for_the_unreadable_phase(self) -> None:
        """别的态跟发数无关——发数一变结论就变的话，那是把两条规则搅在一起了。"""
        others = [phase for phase in PiratePhase if phase is not PiratePhase.SCOUT_UNREADABLE]
        for phase in others:
            assert action_for(phase, scout_count=0) is action_for(phase, scout_count=9)

    def test_a_negative_scout_count_is_refused(self) -> None:
        with pytest.raises(ValueError, match="侦察发数"):
            action_for(PiratePhase.SCOUT_UNREADABLE, scout_count=-1)

    def test_every_phase_maps_to_an_action(self) -> None:
        """新加一个态却忘了给它动作时，这条会当场炸而不是悄悄走进某个兜底分支。

        兜底分支是这里最危险的写法：漏掉的那个态会拿到 `SCOUT`（多派一发）或者
        `DONE`（整天不碰），两种都不响。所以 `action_for` 认不出的态直接抛，
        这条就是那个抛的凭据。
        """
        for phase in PiratePhase:
            assert isinstance(action_for(phase, scout_count=0), PirateAction)

        class _Bogus:
            pass

        with pytest.raises(ValueError, match="未知的海盗态"):
            action_for(_Bogus(), scout_count=0)  # type: ignore[arg-type]
