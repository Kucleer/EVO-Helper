"""控制台的显示表：只管好看，不管判据。"""

from __future__ import annotations

from evo_helper.domain.scheduler import MissionKind, TaskStatus
from evo_helper.web.display import (
    MISSION_LABELS,
    STATUS_GLYPHS,
    STATUS_TONES,
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
