"""bot 目标在一轮里走的三态。

态从库里推导而不是新增列：`preset_name` 已经把两种派遣分开了——
探路发用「探路」，攻击发用分档预设（AAA/BBB/CCC）。多一列就多一处
可能和事实对不上的地方。
"""

from __future__ import annotations

from evo_helper.domain.bot_round import BotPhase, DispatchFact, phase_of


def test_a_target_with_no_dispatch_this_round_needs_a_probe() -> None:
    assert phase_of(()) is BotPhase.NEEDS_PROBE


def test_a_probe_still_in_flight_means_wait_for_its_report() -> None:
    facts = (DispatchFact(preset_name="探路", has_report=False),)

    assert phase_of(facts) is BotPhase.AWAITING_PROBE_REPORT


def test_a_returned_probe_report_means_tier_and_attack() -> None:
    facts = (DispatchFact(preset_name="探路", has_report=True),)

    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_an_attack_in_flight_means_wait_for_its_report() -> None:
    facts = (
        DispatchFact(preset_name="探路", has_report=True),
        DispatchFact(preset_name="BBB", has_report=False),
    )

    assert phase_of(facts) is BotPhase.AWAITING_ATTACK_REPORT


def test_a_returned_attack_report_completes_the_target() -> None:
    facts = (
        DispatchFact(preset_name="探路", has_report=True),
        DispatchFact(preset_name="BBB", has_report=True),
    )

    assert phase_of(facts) is BotPhase.DONE


def test_a_target_judged_not_worth_attacking_is_done_not_stuck() -> None:
    """分档判定「2K 以下不派」的目标没有攻击发，但它已经走完流程。

    把它算成未完成，任务 2 就永远结束不了。
    """
    facts = (DispatchFact(preset_name="探路", has_report=True, skipped=True),)

    assert phase_of(facts) is BotPhase.DONE
