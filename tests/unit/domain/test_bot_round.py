"""bot 目标在一轮里走的三态。

态从库里推导而不是新增列：派了几发、每发的战报回来了没有，
`attack_dispatches` + `battle_reports` 已经全知道了。多一列就多一处可能和
事实对不上的地方。

**战果不在这份判据里。** 平局重打已按用户口径（2026-08-17）移除，所以本模块
一个 `outcome` 都不该出现。下面那几条守的正是这个新口径：平局、胜、负一律
走完。战果本身仍然是观测事实，它的守卫在 `test_battle_outcome` 与展示那一侧。
"""

from __future__ import annotations

from evo_helper.domain.bot_round import (
    BOT_ATTACK_PRESET,
    BotPhase,
    DispatchFact,
    phase_of,
)


def test_a_target_with_no_dispatch_this_round_needs_an_attack() -> None:
    assert phase_of(()) is BotPhase.NEEDS_ATTACK


def test_an_attack_in_flight_means_wait_for_its_report() -> None:
    assert phase_of((DispatchFact(has_report=False),)) is BotPhase.AWAITING_ATTACK_REPORT


def test_a_shot_whose_report_came_back_completes_the_target() -> None:
    """战报回来了就走完。**判据里没有战果这一项**。

    这一条原先分成三条（胜 → 走完、负 → 走完、平 → 再打一发），最后那条是
    2026-08-13 的口径；2026-08-17 用户要求「bot 攻击移除平局再打一次机制」，
    于是三条合成一条：回来了就完了。
    """
    assert phase_of((DispatchFact(has_report=True),)) is BotPhase.DONE


def test_the_phase_decision_cannot_see_the_outcome_at_all() -> None:
    """**平局重打移除之后，战果连进都进不来这一层。**

    这条守的是移除本身，不是某一个分支的取值：只要 `DispatchFact` 上重新长出
    一个战果字段，「平局就再打」就有地方接回去，而那正是用户 2026-08-17
    要求去掉的东西。把它钉在结构上，比逐个战果各写一条断言更难绕过去。

    ⚠️ 这不是说战果没人要了。`battle_reports.outcome` 照旧写、照旧显示，
    守卫在 `tests/unit/domain/test_battle_outcome.py` 与日志页那几条 e2e 上。
    这里说的只是：**判「还要不要再打」的时候不看它。**
    """
    from dataclasses import fields

    assert tuple(item.name for item in fields(DispatchFact)) == ("has_report",)


def test_more_shots_this_round_still_just_means_done() -> None:
    """本轮同一坐标上有好几发、全都回了战报：一律走完，不再补刀。

    多发是会发生的——战报过期被剔掉之后允许重来一发
    （`repository.bot_dispatch_facts`）。原先这里还有一条
    `MAX_ATTACKS_PER_TARGET = 3` 的上限，专门用来兜住「平局无限重打」；
    重打没了，上限也就没有要兜的东西，跟着一起删掉而不是留成死常量。

    所以这条同时守两件事：多发不会被判成「还要再打」，也不存在一个「打满三发
    才算完」的门槛——第二发回来就该是 `DONE`，不是等到第三发。
    """
    for count in (1, 2, 3, 4):
        facts = tuple(DispatchFact(has_report=True) for _ in range(count))

        assert phase_of(facts) is BotPhase.DONE, f"{count} 发全部回报之后应当走完"


def test_waiting_for_a_report_beats_everything_else() -> None:
    """有一发还在飞就等，哪怕前面那一发已经回来了。

    不等的话，同一个坐标上会在几趟之内摞起四五支舰队。
    """
    facts = (
        DispatchFact(has_report=True),
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
