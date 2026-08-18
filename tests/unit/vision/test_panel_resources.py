"""从**存档面板**上读 12 格：ROI 换算，以及「这张图不该读」的那几条。

面板是合成的（`support.panels`，拿字模自己画的，不含任何游戏数据）——理由和
`test_resource_digits.py` 的一样：本仓是公开仓库，实拍面板一张都不许进 git。

⚠️ **这里证不了真实像素上的准确率**，那一半在
`tests/integration/vision/test_resource_grid_corpus_live.py`（34 份实拍语料，
放在 `var/` 下、缺图就跳过）。这里证的是几何：把 12 格 ROI 从视口坐标平移到
面板坐标这一步有没有对上，以及对不上时会不会当场停下来。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT
from evo_helper.vision.optional.panel_resources import (
    panel_cell_regions,
    panel_size,
    read_panel_resource_cells,
)
from evo_helper.vision.report_layout import LIVE_LAYOUT

pytest.importorskip("PIL.Image", reason="要 Pillow 才画得出面板")

from support.panels import panel_bytes, render_panel  # noqa: E402 - importorskip 必须在前

#: 随手编的 12 格，不取自任何一份真实战报。带上了后缀、小数点、孤零零的 0。
SAMPLE: tuple[str, ...] = (
    "486.2K",
    "12.1K",
    "272K",
    "0",
    "17",
    "4",
    "233",
    "0",
    "66",
    "8",
    "0",
    "0",
)


class TestTheGridLandsInsideThePanel:
    def test_every_cell_is_the_viewport_roi_moved_by_the_panel_origin(self) -> None:
        """平移就是全部换算。多一套面板坐标系的常量，就会多一处会各改各的地方。"""
        panel = LIVE_LAYOUT.report_panel
        grid = LIVE_LAYOUT.resource_grid

        regions = panel_cell_regions()

        assert len(regions) == GAINED_SLOT_COUNT
        assert [region.as_box() for region in regions] == [
            (
                grid.cell(slot).left - panel.left,
                grid.cell(slot).top - panel.top,
                grid.cell(slot).right - panel.left,
                grid.cell(slot).bottom - panel.top,
            )
            for slot in range(GAINED_SLOT_COUNT)
        ]

    def test_no_cell_hangs_off_the_panel(self) -> None:
        """裁到面板外面去的格子会被静静截断，读出来是个少一位的合法数字。"""
        width, height = panel_size()

        for region in panel_cell_regions():
            assert 0 <= region.left < region.right <= width
            assert 0 <= region.top < region.bottom <= height

    def test_the_panel_size_is_the_archive_roi(self) -> None:
        """存档面板的尺寸就是 `report_panel` 那块 ROI，520×695。"""
        assert panel_size() == (520, 695)


class TestReadingASynthesisedPanel:
    def test_all_twelve_cells_come_back(self) -> None:
        """把 12 格画在它们真正的位置上，就该逐格读回来。"""
        assert read_panel_resource_cells(panel_bytes(SAMPLE)) == SAMPLE

    def test_a_blank_cell_reads_as_the_empty_string_not_zero(self) -> None:
        """⚠️ 空串是「没读出来」，**不是 0**。补 0 是在编数据。"""
        cells = list(SAMPLE)
        cells[6] = ""

        read = read_panel_resource_cells(panel_bytes(cells))

        assert read[6] == ""
        assert read[5] == SAMPLE[5]


class TestRefusingToReadTheWrongImage:
    def test_a_panel_of_another_size_is_refused(self) -> None:
        """⚠️ 尺寸不符不缩放、不猜。按常量硬裁会读出一屏像模像样的错数。"""
        from io import BytesIO

        buffer = BytesIO()
        render_panel(SAMPLE).crop((0, 0, 520, 694)).convert("RGB").save(
            buffer, format="WEBP", lossless=True
        )

        with pytest.raises(ValueError, match="520x694"):
            read_panel_resource_cells(buffer.getvalue())

    def test_bytes_that_are_not_an_image_are_refused(self) -> None:
        """半截字节 / 不是图片：这是采集的故障，不是「这一格没读出来」。"""
        with pytest.raises(ValueError, match="解不开"):
            read_panel_resource_cells(b"not an image at all")
