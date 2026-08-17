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
    """用户 2026-08-17 逐格确认的对照表。

    ⚠️ **逐个断言，不是只数个数。** 这一块最容易犯、也最难发现的错就是「12 个
    名字都在、只是顺序错了」——数字全对，只是安在了别的资源名下，页面上一点
    异样都没有。只断言长度的话，这类错误一条都拦不住。
    """

    @pytest.mark.parametrize(
        ("slot", "name"),
        [
            (0, "金属"),
            (1, "晶体"),
            (2, "气体"),
            (3, "暗能量"),
            (4, "银河素"),
            (5, "合金碎片"),
            (6, "晶体矿石"),
            (7, "能量凝胶"),
            (8, "泰坦立方"),
            (9, "收割者碎片"),
            (10, "银河石碎片"),
            (11, "银河石能量"),
        ],
    )
    def test_each_slot_has_its_confirmed_name(self, slot: int, name: str) -> None:
        assert SLOT_LABELS[slot] == name
        assert slot_label(slot) == name

    def test_the_grid_order_is_not_the_inventory_page_order(self) -> None:
        """⚠️ **slot 4 与 slot 5 相对「太空舱」页是对调的。**

        太空舱是 `暗能量 / 合金碎片 / 银河素`，战报网格是
        `暗能量 / 银河素 / 合金碎片`。谁照太空舱的顺序去「修正」对照表，
        就会把这两种资源整体对错。这一条钉的就是「别去修正它」。
        """
        assert MATERIAL_NAMES.index("合金碎片") < MATERIAL_NAMES.index("银河素")
        assert SLOT_LABELS.index("银河素") < SLOT_LABELS.index("合金碎片")

    def test_only_that_one_pair_is_out_of_order(self) -> None:
        """⚠️ **别把「对调」推广成「处处不同」。**

        名字本身是同一套词：`晶体矿石` 就是太空舱页上那个词（用户 2026-08-17
        更正，先前口述的「晶体碎片」是笔误）。把两边共有的那几项按各自顺序列出来，
        只有银河素 / 合金碎片这一对是反的。
        """
        shared_by_grid = [name for name in SLOT_LABELS if name in MATERIAL_NAMES]
        shared_by_page = [name for name in MATERIAL_NAMES if name in SLOT_LABELS]

        assert "晶体矿石" in shared_by_grid
        swapped = [
            (left, right)
            for left, right in zip(shared_by_grid, shared_by_page, strict=True)
            if left != right
        ]
        assert swapped == [("银河素", "合金碎片"), ("合金碎片", "银河素")]

    def test_the_twelve_names_cannot_be_derived_from_the_inventory_page(self) -> None:
        """「太空舱前 9 项 + 3 个常规 = 12」这个算式是**巧合**，不是推导。

        常规三种（金属/晶体/气体）不在太空舱页上；`银河石碎片` / `银河石能量`
        同样不在。拿那个算式去「验证」对照表，只会得出错误结论。
        """
        assert {"金属", "晶体", "气体"}.isdisjoint(MATERIAL_NAMES)
        assert {"银河石碎片", "银河石能量"}.isdisjoint(MATERIAL_NAMES)

    def test_the_database_still_stores_positions_not_names(self) -> None:
        """⚠️ **名字只活在这张常量表里，库里存的仍是 0..11。**

        这次能一行常量改完、历史数据自动对上，正是那个设计的价值。写进库就再也
        没有第二次了——所以解析出来的条目上不该有任何名字字段。
        """
        entries = parse_resource_grid(FAIL_CELLS[:6] + ("233",) + FAIL_CELLS[7:])
        assert entries is not None
        (entry,) = entries
        assert entry.slot == 6
        assert not any("name" in field or "label" in field for field in vars(entry))

    def test_positions_beyond_the_table_fall_back_to_a_number(self) -> None:
        """回落留给「将来网格变大、多出没命名的格子」那种情形。

        那时候按位置说话，比顺手编一个名字安全得多。
        """
        from evo_helper.domain import battle_resources

        monkeypatched = battle_resources.SLOT_LABELS[:-1]
        original = battle_resources.SLOT_LABELS
        battle_resources.SLOT_LABELS = monkeypatched  # type: ignore[misc]
        try:
            assert battle_resources.slot_label(11) == "第 12 格"
        finally:
            battle_resources.SLOT_LABELS = original  # type: ignore[misc]

    def test_out_of_range_slots_raise(self) -> None:
        with pytest.raises(IndexError):
            slot_label(GAINED_SLOT_COUNT)
