"""攻击侦查 → 分档 → 攻击：判定这一层的规则。

真正驱动鼠标的部分在 `pirate_loop` 里已经实机跑通，这里只守判定：
**分档用的是游戏里真实存在的预设标题**，以及「2K 以下不派」。
"""

from __future__ import annotations

from evo_helper.domain.fleet_preset import DEFAULT_PRESET
from evo_helper.domain.fleet_tier import FleetTier, tier_for
from evo_helper.tools.bot_loop import PROBE_PRESET, parse_target


def test_the_probe_uses_the_in_game_scout_preset() -> None:
    """攻击侦查用「探路」——这是游戏里真实存在的标题，选预设是按标题找的。"""
    assert PROBE_PRESET == DEFAULT_PRESET.name == "探路"


def test_each_tier_maps_to_a_real_in_game_preset_title() -> None:
    """用户确认（2026-08-09）：甲=AAA、乙=BBB、丙=CCC。

    原先写的是「攻击组合甲/乙/丙」——游戏里没有这些预设，`PresetPicker`
    照它去找一定找不到，于是每一发都在「找不到预设」上整发放弃。
    """
    assert tier_for(3000).preset == "AAA"
    assert tier_for(6000).preset == "BBB"
    assert tier_for(9000).preset == "CCC"


def test_a_negligible_fleet_is_not_attacked() -> None:
    """2K 以下不派：用户明确说过那个量级不值得为它挑组合。"""
    tier = tier_for(1500)

    assert tier is FleetTier.NEGLIGIBLE
    assert tier.preset is None


def test_targets_are_parsed_as_full_coordinates() -> None:
    target = parse_target("2:137:14")

    assert (target.galaxy, target.system, target.position) == (2, 137, 14)


def test_bot_attacks_are_labelled_bot_not_pirate() -> None:
    """BotLoop 继承 PirateLoop 的写库路径，标签必须跟着子类走。

    标错的代价不是「日志难看」：海盗每天 32 次是游戏硬限制，bot 的发数
    混进去会让助手以为配额还没用完，多打的那一发会被强制返回。
    """
    from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
    from evo_helper.tools.bot_loop import BotLoop
    from evo_helper.tools.pirate_loop import PirateLoop

    assert PirateLoop.TARGET_KIND == TARGET_KIND_PIRATE
    assert BotLoop.TARGET_KIND == TARGET_KIND_BOT
