"""「获得资源」12 格数字 ROI 的几何。

版面是量出来的（2026-08-17，`var/logs/vp-detail.png`，标定视口 1920×879），
所以这里钉的是**量出来的那些数**：列距、行距、以及「不许吃到下一格的图标」。

⚠️ 几何漂了不会报错。ROI 往右挪一点就把下一格的图标裁了进来，tesseract 把
图标亮边当成字符——读出来的不是空，是一个混进了噪声的数。
"""

from __future__ import annotations

from evo_helper.vision.report_layout import LIVE_LAYOUT

GRID = LIVE_LAYOUT.resource_grid

#: 实测的图标左沿（`vp-detail.png` 上按亮度量的墨迹外接框）。数字必须裁在它们之前。
ICON_LEFTS = (736, 847, 967, 1080)

#: 实测的数字左沿与数字行顶。
NUMBER_LEFTS = (770, 883, 996, 1109)
NUMBER_ROW_TOPS = (513, 543, 573)


class TestGridShape:
    def test_it_is_four_columns_by_three_rows(self) -> None:
        assert (GRID.columns, GRID.rows, GRID.slots) == (4, 3, 12)

    def test_slots_are_numbered_row_major(self) -> None:
        """第一行左起 0/1/2/3。**这个顺序就是库里那个 `slot`。**"""
        first_row = [GRID.cell(slot) for slot in range(4)]
        assert [region.left for region in first_row] == sorted(region.left for region in first_row)
        assert len({region.top for region in first_row}) == 1
        assert GRID.cell(4).top > GRID.cell(0).top
        assert GRID.cell(4).left == GRID.cell(0).left


class TestMeasuredGeometry:
    def test_every_cell_contains_its_measured_number(self) -> None:
        """12 个 ROI 各自把实测的那一串数字**整个**包住，四周还有余量。"""
        for slot in range(GRID.slots):
            row, column = divmod(slot, GRID.columns)
            region = GRID.cell(slot)
            assert region.left < NUMBER_LEFTS[column]
            assert region.top < NUMBER_ROW_TOPS[row]
            # 实测字高 9 像素（`0` 的墨迹是 7×9）。
            assert region.bottom > NUMBER_ROW_TOPS[row] + 9

    def test_no_cell_reaches_the_next_icon(self) -> None:
        """⚠️ 前三列的 ROI 必须停在下一格图标之前。"""
        for slot in range(GRID.slots):
            _row, column = divmod(slot, GRID.columns)
            if column + 1 >= GRID.columns:
                continue
            assert GRID.cell(slot).right < ICON_LEFTS[column + 1]

    def test_rows_do_not_overlap(self) -> None:
        """行距 30、ROI 高 20，所以相邻两行之间必然留着缝。"""
        for slot in range(GRID.columns, GRID.slots):
            assert GRID.cell(slot).top > GRID.cell(slot - GRID.columns).bottom

    def test_the_grid_sits_inside_the_archived_panel(self) -> None:
        """这一块和存档截图是**同一屏**像素——接在同一趟里，不额外开导航。"""
        panel = LIVE_LAYOUT.report_panel
        for slot in range(GRID.slots):
            region = GRID.cell(slot)
            assert panel.left <= region.left and region.right <= panel.right
            assert panel.top <= region.top and region.bottom <= panel.bottom
