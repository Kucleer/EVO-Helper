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


def test_skipped_says_nothing_about_an_attack_dispatch() -> None:
    """`skipped` 只表达探路之后「分档说不值得打」，不表达「这一发的战报丢了」。

    判「战报永远不会来」要知道派出时刻、飞行时间、战报有效期——那些事实在仓储
    那一侧。所以 `phase_of` 有一条前置条件：**调用方必须先把已判定收不到的派遣
    剔除掉**。这里把边界钉死，免得日后有人给 `skipped` 加上第二个含义：
    真加了，攻击发一旦被误标，目标就会跳过 `AWAITING_ATTACK_REPORT` 直接算走完，
    那一发的战报再也没人去收。
    """
    facts = (
        DispatchFact(preset_name="探路", has_report=True),
        DispatchFact(preset_name="BBB", has_report=False, skipped=True),
    )

    assert phase_of(facts) is BotPhase.AWAITING_ATTACK_REPORT


def test_the_probe_preset_name_tracks_the_single_source_of_truth() -> None:
    """判据里的「探路」必须和派遣链路选的那个预设是同一个字。

    两边各写一份字面量，改了一边就会静默失配：新派的探路发不再被认作探路发，
    于是每一趟都判成 `NEEDS_PROBE`，每个目标每 tick 白烧一条航线。
    """
    from evo_helper.domain.bot_round import PROBE_PRESET_NAME
    from evo_helper.domain.fleet_preset import DEFAULT_PRESET
    from evo_helper.tools.bot_loop import PROBE_PRESET

    assert PROBE_PRESET_NAME == DEFAULT_PRESET.name == PROBE_PRESET
