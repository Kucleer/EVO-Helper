"""舰种名绝不能以拉丁乱码的形式流出去，也绝不能被错位替换。

这两条都是 2026-08-08 那份战报上真实发生过的事故：
名称列读出 10 行、数量列读出 9 行，行数一对不上，整列退回英文那遍，
`轻型战斗机` 入库成了 `SRLS HL`——数量全对，所以没有任何报错。
"""

from __future__ import annotations

from evo_helper.tools.repair_ship_names import in_catalogue_order, needs_repair


class TestVocabularyNames:
    """`_vocabulary_names` 负责把中文那遍的装饰性噪声行摘掉。"""

    def test_junk_rows_are_dropped_but_near_misses_are_kept(self) -> None:
        from evo_helper.vision.optional.report_screens import _vocabulary_names

        # `无晨舰` 差一个字，是真数据；`”` 和 `1 17` 是噪声。
        kept = _vocabulary_names(["轻型战斗机", "无晨舰", "”", "1 17", "钛能守卫者"])

        assert kept == ["轻型战斗机", "无晨舰", "钛能守卫者"]


class TestCatalogueOrder:
    """对位替换唯一的结构性旁证：名单必须是游戏目录的子序列。"""

    def test_a_catalogue_ordered_list_with_a_gap_is_accepted(self) -> None:
        # 中间缺行不破坏顺序——入库那次本来就会丢尾行。
        assert in_catalogue_order(["轻型战斗机", "巡洋舰", "钛能守卫者", "火箭发射器"])

    def test_ships_must_come_before_defences(self) -> None:
        assert not in_catalogue_order(["火箭发射器", "轻型战斗机"])

    def test_a_shuffled_list_is_refused(self) -> None:
        """顺序乱了说明行位置读错了，这时按位置替换会把 A 舰种改成 B 舰种。"""
        assert not in_catalogue_order(["巡洋舰", "轻型战斗机"])

    def test_a_name_outside_the_catalogue_is_refused(self) -> None:
        assert not in_catalogue_order(["轻型战斗机", "SRLS HL"])


class TestNeedsRepair:
    def test_latin_noise_needs_repair(self) -> None:
        for garbled in ("SRLS HL", "BHR", "SRKEML", "RET LE"):
            assert needs_repair(garbled), garbled

    def test_a_real_ship_name_is_left_alone(self) -> None:
        for good in ("轻型战斗机", "钛能守卫者", "火箭发射器"):
            assert not needs_repair(good), good
