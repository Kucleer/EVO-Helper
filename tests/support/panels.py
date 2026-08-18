"""合成一张战报「存档面板」，用来测离线重跑那条路径。

## ⚠️ 为什么是画出来的，不是实拍

本仓是**公开仓库**，一份真实面板上写着账号 ID、出发星与目标坐标、逐格的资源
数量。这类图一张都不许进 git——2026-08-18 有一版把 34 份实拍面板当夹具提交了，
只能整个撤回（`.gitignore` 里现在有九条规则挡着）。

所以这里的面板是拿 `RESOURCE_GLYPHS`（字模表本身只是字体，不含任何账号数据）
现画的：一块 520×695 的纯背景，按生产版面常量把 12 格数字画在**它们真正的
位置**上。整个过程不读任何游戏产物，CI 里照跑。

字模层面的判据在 `tests/unit/vision/test_resource_digits.py`（那边画的是单独一格
的灰度网格）；这里多画一层「整块面板」，为的是连 ROI 换算一起验——把 12 格 ROI
从视口坐标平移到面板坐标这一步，只有整块图才验得了。

⚠️ **这批合成图证不了真实像素上的准确率。** 那一半在
`tests/integration/vision/test_resource_grid_corpus_live.py`（34 份实拍语料，
放在 `var/` 下、缺图就跳过）。
"""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Any

from evo_helper.vision.optional.panel_resources import panel_cell_regions, panel_size
from evo_helper.vision.resource_digits import INK_THRESHOLD, RESOURCE_GLYPHS

#: 背景亮度。远在 `INK_THRESHOLD` 之下，归一化之后是 0。
BACKGROUND_LUMINANCE = 38

#: 字条在一格里的落点。20 高的格子放 9 高的字条，上下都留得出余量。
BAND_TOP = 5
BAND_LEFT = 6

#: 字与字之间空几列背景。实拍的字距是浮动的，读法本来就不假设它是常量。
GLYPH_GAP = 1

_INK_SPAN = 255 - INK_THRESHOLD


def render_panel(cells: Sequence[str]) -> Any:
    """把 12 格文本画成一张面板图（`PIL.Image`，灰度）。

    某一格给空串就**留白**——那正是「这一格没读出来」的样子，全有或全无那条
    判据要靠它来验。
    """
    from PIL import Image

    width, height = panel_size()
    image = Image.new("L", (width, height), BACKGROUND_LUMINANCE)
    pixels = image.load()
    regions = panel_cell_regions()
    if len(cells) != len(regions):
        raise ValueError(f"要 {len(regions)} 格，给了 {len(cells)} 格")
    for region, text in zip(regions, cells, strict=True):
        cursor = region.left + BAND_LEFT
        for char in text:
            rows = RESOURCE_GLYPHS[char]
            for offset_y, row in enumerate(rows):
                for offset_x, digit in enumerate(row):
                    level = int(digit)
                    if level:
                        pixels[cursor + offset_x, region.top + BAND_TOP + offset_y] = (
                            INK_THRESHOLD + round(level / 9 * _INK_SPAN)
                        )
            cursor += len(rows[0]) + GLYPH_GAP
    return image


def panel_bytes(cells: Sequence[str]) -> bytes:
    """同上，交出可以直接存进 `battle_report_screenshots` 的字节。

    **无损 WEBP**：生产存的是 q90 有损图，而这里要验的是几何与差分账，不是
    压缩后还认不认得出。有损会让合成图逐像素不再等于字模，红起来的将是压缩，
    不是被改坏的代码。
    """
    buffer = BytesIO()
    render_panel(cells).convert("RGB").save(buffer, format="WEBP", lossless=True, quality=100)
    return buffer.getvalue()


__all__ = ["BACKGROUND_LUMINANCE", "panel_bytes", "render_panel"]
