"""从**库里存下来的那张面板图**上重读「获得资源」12 格。

实机那条路径（`optional.report_screens.ImageReportScreens.resource_cells`）读的是
整帧视口；这里读的是 `storage.report_screenshots` 里那张已经裁好的面板
（`ReportLayout.report_panel` = 视口的 `(700, 105)-(1220, 800)`，520×695）。
两者的像素来自同一屏，差的只是坐标原点。

## 为什么是**平移 ROI**，不是重新量一套

版面常量只有一套（`report_layout.LIVE_LAYOUT`），这里把 12 格的 ROI 减去面板
左上角就完事了——`crop(box)` 在「贴回视口的面板」上和在「面板自己」上逐像素等价。
重新量一套面板坐标系的网格，等于把同一件事记两遍：游戏改版时它们会各改各的，
而对不上的那一刻**没有任何症状**，读出来的仍旧是一屏像模像样的数字。

## ⚠️ 尺寸不符一律拒读，不缩放、不猜

面板尺寸不是 520×695，只可能是采集设置漂了（版面改了、ROI 改了、或者这张图
根本不是面板）。这时候按常量去裁，裁到的是别处的像素——**读出来的每一格仍然
是合法数字**，错得毫无征兆。判据与 `report_layout.crop_to_viewport` 是同一条：
响的失败好过静默的成功。

## Pillow 是延迟 import 的

它住在 `vision` extra 里，而这个模块会被离线工具 import。`import` 不该在没装
Pillow 的机器上直接炸——真要用的时候再说，和本仓其余可选依赖的降级口径一致。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any

from evo_helper.vision.report_layout import LIVE_LAYOUT, Region, ReportLayout
from evo_helper.vision.resource_digits import read_resource_cell


def panel_size(layout: ReportLayout = LIVE_LAYOUT) -> tuple[int, int]:
    """存档面板的尺寸 `(宽, 高)`。就是 `report_panel` 那块 ROI 的大小。"""
    panel = layout.report_panel
    return (panel.right - panel.left, panel.bottom - panel.top)


def panel_cell_regions(layout: ReportLayout = LIVE_LAYOUT) -> tuple[Region, ...]:
    """12 格数字 ROI，换算到**面板自己的**坐标系，行优先。

    ⚠️ 换算之后必须整块落在面板内。落不下说明这两组常量已经对不上了
    （面板 ROI 缩了、或者网格挪出去了），这时候裁出来的是被截断的一格——
    读出来多半是个少了一位的合法数字，进了库看不出来。所以这里当场抛错。
    """
    panel = layout.report_panel
    grid = layout.resource_grid
    width, height = panel_size(layout)
    regions: list[Region] = []
    for slot in range(grid.slots):
        cell = grid.cell(slot)
        moved = Region(
            cell.left - panel.left,
            cell.top - panel.top,
            cell.right - panel.left,
            cell.bottom - panel.top,
        )
        if moved.left < 0 or moved.top < 0 or moved.right > width or moved.bottom > height:
            raise ValueError(
                f"第 {slot} 格的 ROI {cell.as_box()} 不在存档面板 {panel.as_box()} 之内；"
                "版面常量对不上了，先修版面"
            )
        regions.append(moved)
    return tuple(regions)


def read_panel_resource_cells(
    image_bytes: bytes, *, layout: ReportLayout = LIVE_LAYOUT
) -> tuple[str, ...]:
    """把一张存档面板读成 12 格原文，行优先；读不出的格子是**空串**。

    空串是「这一格没读出来」，**不是 0**——补 0 的决定不在这一层，整块作废由
    `domain.battle_resources.parse_resource_grid` 判（理由写在那个函数上）。

    尺寸不符、字节解不开一律 `ValueError`：这两种都是「这张图不该按面板来读」，
    而不是「这一格没读出来」，混在一起会让调用方把采集故障当成识别不佳。
    """
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError) as error:  # 半截字节、非图片、编码不认识
        raise ValueError(f"这张存档面板解不开：{error}") from error
    expected = panel_size(layout)
    if (image.width, image.height) != expected:
        raise ValueError(
            f"存档面板是 {image.width}x{image.height}，版面标定的是 "
            f"{expected[0]}x{expected[1]}；采集设置漂了，先修采集"
        )
    return tuple(_read_cell(image, region) for region in panel_cell_regions(layout))


def _read_cell(image: Any, region: Region) -> str:
    """裁出一格、转灰度、交给字模匹配。

    `tobytes()` 是逐行紧排的灰度字节，没有行填充——和
    `report_screens.ImageReportScreens._read_resource_cell` 用的是同一套取像素
    办法，两条路径读同一屏像素时结果必须逐格相同。
    """
    crop = image.crop(region.as_box()).convert("L")
    raw = crop.tobytes()
    width = crop.width
    return read_resource_cell([raw[y * width : (y + 1) * width] for y in range(crop.height)])


__all__ = ["panel_cell_regions", "panel_size", "read_panel_resource_cells"]
