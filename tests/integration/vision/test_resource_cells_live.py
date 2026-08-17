"""「获得资源」12 格，跑在真截图上。

⚠️ **这一条不能用假 OCR 代替。** 假取字面只证明接线对；而这一块真正难的地方是
**字高只有 9 像素**：一个 `0` 的墨迹是 7×9，直接喂给 tesseract 的话 12 格里读不出
几格，还会把 `0` 读成 `5`——一个看起来完全正常、入库之后再也分辨不出来的数。
「裁到墨迹 + 放大 12× + 补黑边 + 两套配方谈拢」这一整套配方就是为它调的，
而配方好不好只有真图能回答。

样本 `var/logs/vp-detail.png`：2026-08-08 那份 FAIL 战报的未滚动详情页
（标定视口 1920×879）。12 格全是 0。

⚠️ **全 0 的样本证得了几何与读数，证不了「多位数读得对」。** 仓库里眼下没有
一份收获非零的实拍。那一半必须实机验证：真打开一份有非零收获的战报，
对着画面逐格核 `928K` / `501.1K` / `233` 这些值读没读对。
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
