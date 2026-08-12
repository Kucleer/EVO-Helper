"""海盗目标在一轮里走的那几态。

用户要的三态是「不触发攻击 / 触发攻击 / 攻击完成」；这里落成四个终态，
多出来的 `SCOUT_UNREADABLE` 是刻意的——依据见 `domain.pirate_round` 的模块头。
所以这个文件里分量最重的断言是「没看清」不许塌成「不触发攻击」。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.pirate_round import (
    PHASE_LABELS,
    AttackFact,
    PiratePhase,
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
