"""「获得资源」那 12 格的网格判据。

用户口径（2026-08-17）：只统计这 12 个值；残骸与两个百分比不做。

⚠️ 这里守的两件事，坏掉之后都**不会报错**：

- **位置映射**：格子编号一旦错位，数字全对、只是安在了别的槽位上。
- **全有或全无**：读不全还入库，缺的那几格会被后来的人当成 0。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.battle_resources import (
    GAINED_SLOT_COUNT,
    MATERIAL_NAMES,
    SLOT_LABELS,
    parse_resource_grid,
    slot_label,
)
from evo_helper.domain.records import BattleResourceEntry

#: 用户 2026-08-17 给的那份 VICTORY 战报，逐格原样（行优先）。
VICTORY_CELLS = (
    "928K",
    "501.1K",
    "342.9K",
    "7.7K",
    "0",
    "1.2K",
    "233",
    "0",
    "66",
    "4",
    "0",
    "0",
)

#: 同一天那份 FAIL 战报：12 格全 0。位置与上面那份**逐格对齐**，
#: 值为 0 的格子照样占位显示 `0`，不会被压缩掉——「网格固定」就是这么验的。
FAIL_CELLS = ("0",) * GAINED_SLOT_COUNT


class TestSlotMapping:
    def test_the_grid_is_four_by_three(self) -> None:
        assert GAINED_SLOT_COUNT == 12

    def test_slots_are_numbered_row_major(self) -> None:
        """第一行左起 0/1/2/3。**这个顺序就是库里存的那个 `slot`。**

        改了它，历史数据会连同解释一起错位，而且一声不吭。
        """
        entries = parse_resource_grid(VICTORY_CELLS)
        assert entries is not None
        assert entries == (
            BattleResourceEntry(slot=0, amount=928_000, approximate=True, uncertainty=500),
            BattleResourceEntry(slot=1, amount=501_100, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=2, amount=342_900, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=3, amount=7_700, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=5, amount=1_200, approximate=True, uncertainty=50),
            BattleResourceEntry(slot=6, amount=233, approximate=False, uncertainty=0),
            BattleResourceEntry(slot=8, amount=66, approximate=False, uncertainty=0),
            BattleResourceEntry(slot=9, amount=4, approximate=False, uncertainty=0),
        )

    def test_zero_cells_leave_no_row(self) -> None:
        """槽位 4/7/10/11 是 0，不占行。「没有行 = 0」的另一半在库那一侧。"""
        entries = parse_resource_grid(VICTORY_CELLS)
        assert entries is not None
        assert {entry.slot for entry in entries} == {0, 1, 2, 3, 5, 6, 8, 9}


class TestAllZeroReport:
    def test_a_blank_haul_still_parses(self) -> None:
        """⚠️ **全 0 要能正常入库，而且和「没读到」分得开。**

        空元组 = 12 格都读到了、都是 0；`None` = 没读全。
        """
        assert parse_resource_grid(FAIL_CELLS) == ()


class TestAllOrNothing:
    def test_one_unreadable_cell_voids_the_whole_grid(self) -> None:
        """⚠️ 读不全就一格都不给。

        只存读到的那几格，剩下的会在库里长得和「那几格是 0」一模一样——
        一次读不全就此变成几个凭空捏造的零，而且不留痕迹。
        """
        cells = list(VICTORY_CELLS)
        cells[6] = ""
        assert parse_resource_grid(cells) is None

    def test_a_half_read_glyph_voids_it_too(self) -> None:
        """`'.'`、`'K'` 这种半个结果非空却毫无意义，不能当成读数。"""
        cells = list(FAIL_CELLS)
        cells[0] = "K"
        assert parse_resource_grid(cells) is None

    def test_a_non_integer_reading_voids_it(self) -> None:
        """`1.5` 语法上合法，但资源数量全是整数——读出它只可能是读串了。"""
        cells = list(FAIL_CELLS)
        cells[3] = "1.5"
        assert parse_resource_grid(cells) is None

    def test_a_short_grid_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="12 格"):
            parse_resource_grid(("0", "0"))


class TestSlotLabels:
    def test_uncalibrated_slots_are_shown_by_position(self) -> None:
        """⚠️ **没核对过就不编名字。**

        对照表填错的症状是「数字都对、只是安在了别的资源名下」——页面上每一格
        都有一个像模像样的数，没有任何人会发现。
        """
        assert all(label is None for label in SLOT_LABELS)
        assert slot_label(0) == "第 1 格"
        assert slot_label(11) == "第 12 格"

    def test_the_material_names_are_kept_but_not_wired_in(self) -> None:
        """材料页那十个名字只是记在手边，**不是槽位顺序**。

        十个名字配十二个格子——按序号抄进去连数量都对不上，更别说对应关系。
        """
        assert len(MATERIAL_NAMES) != GAINED_SLOT_COUNT
        assert set(SLOT_LABELS) == {None}

    def test_out_of_range_slots_raise(self) -> None:
        with pytest.raises(IndexError):
            slot_label(GAINED_SLOT_COUNT)
