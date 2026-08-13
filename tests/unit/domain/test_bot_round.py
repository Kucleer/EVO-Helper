"""bot 目标在一轮里走的三态，以及「平局就再打」那条口径的边界。

态从库里推导而不是新增列：派了几发、每发的战报回来了没有、打成了什么，
`attack_dispatches` + `battle_reports` 已经全知道了。多一列就多一处可能和
事实对不上的地方。
"""

from __future__ import annotations

from evo_helper.domain.battle_outcome import OUTCOME_DRAW, OUTCOME_FAIL, OUTCOME_VICTORY
from evo_helper.domain.bot_round import (
    BOT_ATTACK_PRESET,
    MAX_ATTACKS_PER_TARGET,
    BotPhase,
    DispatchFact,
    phase_of,
)


def test_a_target_with_no_dispatch_this_round_needs_an_attack() -> None:
    assert phase_of(()) is BotPhase.NEEDS_ATTACK


def test_an_attack_in_flight_means_wait_for_its_report() -> None:
    assert phase_of((DispatchFact(has_report=False),)) is BotPhase.AWAITING_ATTACK_REPORT


def test_a_win_completes_the_target() -> None:
    facts = (DispatchFact(has_report=True, outcome=OUTCOME_VICTORY),)

    assert phase_of(facts) is BotPhase.DONE


def test_a_loss_completes_the_target_too() -> None:
    """打输了也走完。**只有平局才重打**——用户口径只提了平局。

    把战败也算成「再来一发」等于拿同一套预设去撞同一支守军，
    第二发的结局只会和第一发一样，白烧一条航线。
    """
    facts = (DispatchFact(has_report=True, outcome=OUTCOME_FAIL),)

    assert phase_of(facts) is BotPhase.DONE


def test_a_draw_sends_another_attack_at_the_same_coordinate() -> None:
    """「如果同一坐标攻击结果为平局，则继续进行攻击」（用户口径 2026-08-13）。"""
    facts = (DispatchFact(has_report=True, outcome=OUTCOME_DRAW),)

    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_the_cap_is_a_small_finite_number() -> None:
    """**上限的取值本身要钉住，不能只钉「有个上限」。**

    下面那条用例拿 `MAX_ATTACKS_PER_TARGET` 当尺子量 `phase_of`，所以把常量改成
    100 它照样绿——那种「自己量自己」的断言挡不住「上限名存实亡」这种改动。
    这一条补上缺的那一半：3 发（初打一发 + 平局最多补两发）是个决定，与仓里
    另外两条自愈配额同一档（断线重开 3 次/滚动 1 小时、认不出只自愈一次）。
    """
    assert MAX_ATTACKS_PER_TARGET == 3


def test_the_retries_are_capped_so_one_target_cannot_eat_the_round() -> None:
    """连着平局也不能无限打下去。

    没有上限的话，两边势均力敌的一个坐标会把整轮的航线全吃掉，别的目标一发都
    轮不到，而日志上只是一句接一句「又打了一发」。上限打满之后这个目标算走完，
    留给下一轮。
    """
    draws = tuple(
        DispatchFact(has_report=True, outcome=OUTCOME_DRAW) for _ in range(MAX_ATTACKS_PER_TARGET)
    )

    assert phase_of(draws[:-1]) is BotPhase.NEEDS_ATTACK
    assert phase_of(draws) is BotPhase.DONE


def test_only_the_last_shot_decides_whether_to_go_again() -> None:
    """口径说的是「这一发的结果」，不是「历史上有没有平过」。

    按 any 判的话，先平后胜的目标会一直被判成还要再打，直到撞上发数上限——
    多出来的那两发全是白打的。
    """
    facts = (
        DispatchFact(has_report=True, outcome=OUTCOME_DRAW),
        DispatchFact(has_report=True, outcome=OUTCOME_VICTORY),
    )

    assert phase_of(facts) is BotPhase.DONE


def test_an_unreadable_outcome_does_not_trigger_a_retry() -> None:
    """战果算不出来（四个数缺一个）**不算平局**，不重打。

    重打的唯一依据是**确认**是平局。拿「算不出」去重打，等于凭一次 OCR 失手
    再送一支舰队出去——而「损失单位」那一行要把详情页拖到底才读得到，
    读不到是常见情形（`domain.battle_outcome.survivors`）。
    """
    facts = (DispatchFact(has_report=True, outcome=None),)

    assert phase_of(facts) is BotPhase.DONE


def test_waiting_for_a_report_beats_going_again() -> None:
    """上一发平局、这一发还在飞：等，不再补第三发。

    不等的话，同一个坐标上会在几趟之内摞起四五支舰队——而是不是平局本来就要
    等战报回来才知道。
    """
    facts = (
        DispatchFact(has_report=True, outcome=OUTCOME_DRAW),
        DispatchFact(has_report=False),
    )

    assert phase_of(facts) is BotPhase.AWAITING_ATTACK_REPORT


def test_the_attack_preset_is_a_real_in_game_preset_title() -> None:
    """守的是**这个字符串必须是游戏里真实存在的预设标题**。

    派遣链路按标题在预设条上 OCR 找（`game.preset_picker`），找不到就抛
    `PresetNotFound`，整发放弃。实机日志里出现过 `预设条上找不到 'CCC'；
    这一屏读到的是 ['AAA', '探路']`——成因是选择器只往左拖、够不到右边的预设
    （PR #100 已修）。**BBB 正是要往右拖才看得到的那一档**，所以这条守卫在
    分档删掉之后不但仍然成立，还比原先更要紧：现在每一发用的都是它，
    标题一旦对不上，这条链路一发都派不出去。

    这条断言原先叫 `test_each_tier_maps_to_a_real_in_game_preset_title`，
    守的是 AAA/BBB/CCC 三个标题；分档没了，守的对象收敛成 BBB 一个。
    """
    assert BOT_ATTACK_PRESET == "BBB"


def test_the_runner_dispatches_with_exactly_that_preset() -> None:
    """判据里的预设名必须和派遣链路真正选的那个是同一个字。

    两边各写一份字面量，改了一边就会静默失配：`bot_loop` 拿一个标题去选，
    而这一层按另一个标题判「这一发是不是本轮打的」。
    """
    from evo_helper.tools import bot_loop

    assert bot_loop.BOT_ATTACK_PRESET is BOT_ATTACK_PRESET
