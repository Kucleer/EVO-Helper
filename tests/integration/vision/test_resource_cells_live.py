"""「获得资源」12 格，跑在**整帧**实拍上（`var/logs/vp-detail.png`）。

守的是 `crop_to_viewport` + `layout_for_viewport` 这条几何链路：整窗截图先裁成
1920×879 的视口，再按视口取网格 ROI。样本是 2026-08-08 那份 FAIL 战报的未滚动
详情页，12 格全是 0。

⚠️ **识别本身不在这里守。** 全 0 的样本证得了几何，证不了「多位数读得对」——
上一版配方正是在这张图上「12 格全对」，换到有非零收获的真图上 34 份里只有 5 份
逐格正确。识别的判据分两处：字形层面在 `tests/unit/vision/test_resource_digits.py`
（合成字条，CI 里照跑），真实像素层面在 `test_resource_grid_corpus_live.py`
（34 份实拍面板，语料不进仓库、CI 里跳过）。

这张图不在仓库里（`var/logs/` 是实机产物），所以这条用例平时是跳过的。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT, parse_resource_grid
from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

LOGS = Path("var/logs")
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
SAMPLE = "vp-detail"

pytestmark = pytest.mark.skipif(
    not (Path(TESSERACT).is_file() and (LOGS / f"{SAMPLE}.png").is_file()),
    reason="缺战报详情页实拍（var/logs/vp-detail.png）或 Tesseract",
)


def _screens():  # type: ignore[no-untyped-def]
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    image = crop_to_viewport(Image.open(LOGS / f"{SAMPLE}.png"))
    layout = layout_for_viewport(image.width, image.height)
    return ImageReportScreens(image, layout, tesseract_cmd=TESSERACT)  # type: ignore[arg-type]


def test_all_twelve_cells_read_as_zero() -> None:
    """12 格逐格读出 `0`。

    ⚠️ **一格都不许是空的。** 空的意思是「这一格没读出来」，而整块会因此作废
    （`parse_resource_grid` 返回 None）——也就是这份战报的收获永远不会入库。
    这条用例正是那套配方唯一的凭据：不裁到墨迹时实测只读出零星几格，还有两格
    读成了 `5`。
    """
    cells = _screens().resource_cells()

    assert len(cells) == GAINED_SLOT_COUNT
    assert cells == ("0",) * GAINED_SLOT_COUNT


def test_the_grid_parses_into_an_empty_but_valid_haul() -> None:
    """全 0 是一次**成功的读数**，不是失败：空元组，不是 None。

    这两者在库里的区别就是「这一发没捞着东西」和「这一发的收获没记下来」。
    """
    assert parse_resource_grid(_screens().resource_cells()) == ()
