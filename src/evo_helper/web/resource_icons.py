"""稀有资源图标：**运行时从库里的战报面板上切一次，然后一直缓存在内存里。**

## 为什么不把切好的图提交进仓库

`Kucleer/EVO-Helper` 是**公开仓库**，而这几个图标是游戏素材。切成 PNG 放进
`web/static/` 也好、base64 内联进模板也好，本质都是把游戏美术资源提交到一个
公开的地方——只是后者不容易被 `git ls-files | grep -i png` 抓到而已。
这个仓已经为此吃过一次亏（有人把 34 张生产截图提进了公开仓库）。

所以走运行时：图标的来源是**用户自己库里**的战报面板
（`battle_report_screenshots`，实测 79 张 520×695 的 WEBP），谁的库谁的图，
仓库里一个字节都不多。代价是控制台进程第一次渲染这一页时要解一张 WEBP，
之后走进程内缓存，**每个进程只切一次**。

## 依赖缺了就不显示图标，不报错

解 WEBP 要 Pillow，而它在 `[dev]` / `[vision]` 两组可选依赖里，控制台那台机器
不一定装（CLAUDE.md：可选依赖缺失时降级运行而非报错）。缺了就返回 None，
页面上那三张卡片照常显示数字，只是左边没有小图。

## ⚠️ 下面这几个数是标定常量，不是偏好项

裁切框是对着 520×695 的战报面板量的（那个尺寸由
`vision.report_layout.LIVE_LAYOUT.report_panel` 决定），容差是对着实拍试出来的。
改动它们不会让结果「更适合我」，只会让结果**错**——所以它们不做成配置项
（判据见 CLAUDE.md「引入阈值就先判断该不该做成可配置」那一条）。
"""

from __future__ import annotations

import io
import logging
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

#: 图标是从这个尺寸的面板上量的。**尺寸对不上就不切**——版面漂了的时候，
#: 宁可没有图标，也不要在页面上摆三块切歪了的像素。
PANEL_SIZE = (520, 695)

#: 槽位 → 面板坐标系里的裁切框 `(left, top, right, bottom)`。
#:
#: 用户 2026-08-19 对着实拍量的。三个都在「获得资源」那 4×3 网格上：
#: slot 5 合金碎片（第 2 行第 2 列）、slot 8 泰坦立方（第 3 行第 1 列）、
#: slot 9 收割者碎片（第 3 行第 2 列）。**标定常量，见模块头。**
ICON_CROPS: Mapping[int, tuple[int, int, int, int]] = {
    5: (143, 429, 179, 459),
    8: (26, 457, 60, 487),
    9: (148, 457, 182, 487),
}

#: 抠底的容差（每通道的最大差值）。**标定常量，见模块头。**
#:
#: ⚠️ **不要往上调。** 用户实测：放到 16 以上，漫水会顺着图标内部的暗缝漏进去，
#: 把图标掏出洞来——而那种失败在小图上不明显，页面上看着只是「这个图标怎么有点
#: 花」。10 是能把四周的面板底色去干净、又不咬进图标的那一档。
FLOOD_TOLERANCE = 10


@dataclass(frozen=True, slots=True)
class ResourceIcon:
    """切好的一张图标。`media_type` 固定 PNG——要透明底就不能是 JPEG。"""

    slot: int
    image_bytes: bytes
    media_type: str = "image/png"


class ResourceIconCache:
    """进程内的图标缓存。**只切一次。**

    缓存的是「切的结果」，包括「切不出来」（`None`）——不然每一次轮询都会去库里
    捞一张 40KB 的 WEBP、解一遍、再失败一次。这一页每几秒刷新一次，那是白烧。

    ⚠️ **失败也缓存**意味着：往一个空库里补进第一张战报面板之后，要重启控制台
    才会有图标。这是刻意的取舍——图标是装饰，而每 tick 一次的失败重试会把
    `system_log` 刷爆（PR #188 修过一次，当时两条日志占了全表 44%）。
    """

    def __init__(self, load_panel: Callable[[], bytes | None]) -> None:
        self._load_panel = load_panel
        self._icons: dict[int, ResourceIcon | None] | None = None

    def icon(self, slot: int) -> ResourceIcon | None:
        if self._icons is None:
            self._icons = self._cut_all()
        return self._icons.get(slot)

    def _cut_all(self) -> dict[int, ResourceIcon | None]:
        panel = self._load_panel()
        if panel is None:
            _LOGGER.info("库里没有 %sx%s 的战报面板，数据概览页不显示资源图标", *PANEL_SIZE)
            return dict.fromkeys(ICON_CROPS)
        cut: dict[int, ResourceIcon | None] = {}
        for slot in ICON_CROPS:
            image_bytes = cut_icon(panel, slot)
            cut[slot] = None if image_bytes is None else ResourceIcon(slot, image_bytes)
        return cut


def cut_icon(panel_bytes: bytes, slot: int) -> bytes | None:
    """从一张战报面板上切出这个槽位的图标，抠掉底色，编成带透明通道的 PNG。

    切不出来（没装 Pillow、面板尺寸不对、槽位没有标定框、图解不开）一律返回
    None，**绝不抛异常**：一个装饰性的小图不该有能力把整页变成 500。
    """
    box = ICON_CROPS.get(slot)
    if box is None:
        return None
    image_module = _pillow()
    if image_module is None:
        return None
    try:
        panel = image_module.open(io.BytesIO(panel_bytes))
        if panel.size != PANEL_SIZE:
            _LOGGER.info("战报面板是 %sx%s、不是标定的 %sx%s，图标不切", *panel.size, *PANEL_SIZE)
            return None
        icon = panel.convert("RGBA").crop(box)
        _erase_background(icon)
        buffer = io.BytesIO()
        icon.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:  # noqa: BLE001 - 见函数说明：装饰性路径不许把页面弄死
        _LOGGER.exception("切槽位 %s 的资源图标失败，这一格不显示图", slot)
        return None


def _erase_background(icon: Any) -> None:
    """**从四边漫水**把面板底色刷成透明。就地改 `icon`。

    做法：先按四条边上出现最多的那个颜色定下「底色」，再从每一个与底色相近
    （每通道差 ≤ `FLOOD_TOLERANCE`）的边缘像素出发做四连通扩散。

    ⚠️ **必须从四边进、必须连通。** 直接「把所有接近底色的像素都刷透明」会把
    图标内部同色的那几块一起掏空——图标里本来就有暗色区域，而它们和底色的差值
    在同一个量级上。连通性正是把「外面的底」和「里面的暗」分开的那个条件。
    """
    width, height = icon.size
    pixels = icon.load()
    if pixels is None:  # pragma: no cover - Pillow 只在极端情况下给 None
        return
    border = [
        (x, y)
        for x in range(width)
        for y in range(height)
        if x in (0, width - 1) or y in (0, height - 1)
    ]
    counts = Counter(pixels[point][:3] for point in border)
    background = counts.most_common(1)[0][0]

    stack = [point for point in border if _close(pixels[point][:3], background)]
    seen = set(stack)
    while stack:
        x, y = stack.pop()
        red, green, blue, _ = pixels[x, y]
        pixels[x, y] = (red, green, blue, 0)
        for neighbour in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            nx, ny = neighbour
            if not (0 <= nx < width and 0 <= ny < height) or neighbour in seen:
                continue
            if not _close(pixels[nx, ny][:3], background):
                continue
            seen.add(neighbour)
            stack.append(neighbour)


def _close(colour: tuple[int, ...], reference: tuple[int, ...]) -> bool:
    """两个颜色是不是在容差之内。**逐通道取最大差**，不是欧氏距离：

    欧氏距离下,一个通道差 17 也能靠另外两个通道相同而蒙混过关（√(17²)≈17，
    而三通道各差 10 的距离也有 17.3），于是「容差 10」在某些颜色上悄悄变成 17。
    """
    return all(abs(a - b) <= FLOOD_TOLERANCE for a, b in zip(colour, reference, strict=False))


def _pillow() -> Any:
    """`PIL.Image`，没装就是 None。

    在函数里 import 而不是模块顶部：Pillow 是可选依赖，顶部 import 会让整个
    `web` 包在没装它的机器上 import 失败——那不是「没有图标」，那是控制台起不来。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


__all__ = [
    "FLOOD_TOLERANCE",
    "ICON_CROPS",
    "PANEL_SIZE",
    "ResourceIcon",
    "ResourceIconCache",
    "cut_icon",
]
