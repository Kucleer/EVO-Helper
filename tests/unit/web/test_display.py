"""控制台的显示表：只管好看，不管判据。"""

from __future__ import annotations

from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scheduler import MissionKind, TaskStatus
from evo_helper.game.pirate_ui import PIRATE_TRIGGER_SHIPS
from evo_helper.web.display import (
    BATTLE_RESULT_LABELS,
    DISPATCH_STATE_LABELS,
    LIST_SHIP_COLUMNS,
    MISSION_LABELS,
    STATUS_GLYPHS,
    STATUS_TONES,
    TARGET_KIND_GLYPHS,
    TARGET_KIND_TONES,
    missing_intel_labels,
    missing_status_tones,
)


def test_every_status_has_its_own_slot() -> None:
    """八档一个都不能少。

    页面按状态上色，色调表里少一格就意味着有两档被当成了同一件事——而恰恰是
    「未启用 / 待命」与「冷却中 / 等航线」这两对最不能混：没勾的任务显示
    「待命」是谎话，冷却中显示「等航线」会让用户去调航线数、调完还是不动。
    """
    assert missing_status_tones() == []
    assert len(STATUS_TONES) == len(TaskStatus)
    assert len(STATUS_GLYPHS) == len(TaskStatus)


def test_no_two_statuses_share_a_glyph() -> None:
    """色永远配一个字形（`console.css` 顶部那条）。

    两档共用一个字形，在灰度下、对色盲用户就等于合并了——这一层存在的
    全部理由就是不让它们合并。
    """
    assert len(set(STATUS_GLYPHS.values())) == len(TaskStatus)


def test_every_mission_kind_has_a_label() -> None:
    """标签由服务端下发，页面和桌面悬浮窗都不自己拼。"""
    assert set(MISSION_LABELS) == {kind.value for kind in MissionKind}


def test_the_list_columns_are_the_pirate_trigger_ships() -> None:
    """情报中心那四列存在的理由就是「侦察判定看的是这四个」。

    抄一份字面量的话，判定表增删一种舰船而这边没跟上，页面会安静地少显示一列——
    而少的那一列恰恰是决定打不打的那个数。
    """
    assert LIST_SHIP_COLUMNS == PIRATE_TRIGGER_SHIPS


def test_every_intel_state_has_its_own_slot() -> None:
    """派遣结果、战果、目标类型三张表一档都不能缺。

    缺一档，页面上就会冒出一个没人翻译过的英文常量；更糟的是两档被当成同一件事，
    而「未派出（被闸门拦下）」与「被拒（游戏没接受）」正是最不能混的一对——
    它们对应两种完全不同的排查方向。
    """
    assert missing_intel_labels() == []


def test_bot_and_pirate_do_not_share_a_colour_or_a_glyph() -> None:
    """列表里 bot 与海盗要一眼分得开，而色不是唯一的信号。

    共用同一个 tone 就等于没分；共用同一个字形，在灰度下、对色盲用户同样等于没分。
    """
    assert len(set(TARGET_KIND_TONES.values())) == len(TARGET_KIND_LABELS)
    assert len(set(TARGET_KIND_GLYPHS.values())) == len(TARGET_KIND_LABELS)


def test_no_two_dispatch_states_share_a_label() -> None:
    """四档各是一句不同的话。两档写成同一个词，筛选结果就解释不通。"""
    assert len(set(DISPATCH_STATE_LABELS.values())) == len(DISPATCH_STATE_LABELS)


def test_no_two_battle_results_share_a_label() -> None:
    assert len(set(BATTLE_RESULT_LABELS.values())) == len(BATTLE_RESULT_LABELS)
