"""在星球地表上找那座星门。**交候选，不交答案。**

## ⚠️ 为什么不能写死坐标

星门是一座建筑，建筑在基地里的格子不动 —— 可**地表的镜头会平移**。
2026-09-13 实机打回来的：标定那一帧星门在 `(1018, 455)`，切了一次出发星球之后
同一颗星球上它在 `(864, 420)`，差了 150 多像素。写死的那一下点在了空地上，
整趟在「点了建筑却没读到『星门』标题」上安全停住。

⚠️ 这正是记忆里那条「按钮靠读文字定位」的同一类：**写死坐标会点到隔壁**，
而地表上隔壁那一格是另一座建筑 —— 点下去会开出一个完全正常、却不相干的面板。

## ⚠️⚠️ 判据是「有洞的环」，不是「青蓝色」

传送门是个亮青蓝的**环**。光按颜色找会选中三类东西（实拍掩码上看得清清楚楚）：

- 右边那列 UI 圆钮（同色）
- 建筑上方的倒计时气泡（同色，**实心**圆角矩形）
- 基地里的水流（同色，长条）

环有洞，气泡和水流没有 —— `RETR_CCOMP` 里「有子轮廓」这一条把它们一刀切开。

## ⚠️ 为什么出候选而不是出一个答案

闭运算的核大小**没有一个值同时对两帧**：k=3–7 能框住标定帧那个完整的环，
而现场帧那个环有豁口、要 k=11 才封得上；k=11 反过来把标定帧那个搞坏。
在两帧上调一个核就是过拟合 —— 同回收那次「哪个框更好是伪问题」。

所以这里**每个核各出一遍候选**，按洞的面积排序合并，交给调用方逐个试；
真正裁决的是外部事实：点下去之后面板标题是不是「星门」。
实测两帧里真值都排第 1，候选总共只有 2–3 个。
"""

from __future__ import annotations

from typing import Any

#: 地表上可能长着建筑的那一块（917 空间）。
#:
#: ⚠️ **右边界切在 1150**：再往右是那列 UI 圆钮，它们和传送门同色同亮度，
#: 而且个个都是环形 —— 框进来就是给候选表塞三个必错的第一名。
SURFACE_AREA = (700, 180, 1150, 700)

#: 传送门那圈蓝的判据。实测两帧的主色都在 `(25, 170, 255)` 一带。
BLUE_MIN = 180
BLUE_OVER_RED = 90
GREEN_RANGE = (110, 235)

#: 逐个试的闭运算核。**故意给一串而不是挑一个**，理由见模块头。
CLOSE_KERNELS = (3, 5, 7, 9, 11)

#: 传送门在屏幕上的大致尺寸（含环）。
SIZE_RANGE = (40, 160, 40, 200)
ASPECT_RANGE = (0.4, 1.6)

#: 两个候选离得这么近就当成同一个。
MERGE_DISTANCE = 45


def _children(hierarchy: Any, first: int) -> list[int]:
    out: list[int] = []
    index = first
    while index != -1:
        out.append(index)
        index = hierarchy[index][0]
    return out


def stargate_candidates(image: Any) -> list[tuple[int, int]]:
    """地表截图 → 可能是星门的那几个位置，**最像的排在前面**。

    交回来的坐标是 917 空间的整窗坐标，可以直接点。一个都没有时交回空表 ——
    调用方该据此停下来，而不是回退到某个写死的位置。
    """
    import cv2
    import numpy as np

    crop = np.array(image.convert("RGB").crop(SURFACE_AREA))
    red = crop[:, :, 0].astype(int)
    green = crop[:, :, 1].astype(int)
    blue = crop[:, :, 2].astype(int)
    base = (
        (blue > BLUE_MIN)
        & (blue - red > BLUE_OVER_RED)
        & (green > GREEN_RANGE[0])
        & (green < GREEN_RANGE[1])
    ).astype(np.uint8) * 255

    min_w, max_w, min_h, max_h = SIZE_RANGE
    found: list[tuple[float, tuple[int, int]]] = []
    for size in CLOSE_KERNELS:
        mask = cv2.morphologyEx(base, cv2.MORPH_CLOSE, np.ones((size, size), np.uint8))
        contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if hierarchy is None:
            continue
        hierarchy = hierarchy[0]
        for index, contour in enumerate(contours):
            # 自己是别人的洞、或者自己没有洞 —— 两种都不是环。
            if hierarchy[index][3] != -1 or hierarchy[index][2] == -1:
                continue
            x, y, width, height = cv2.boundingRect(contour)
            if not (min_w <= width <= max_w and min_h <= height <= max_h):
                continue
            if not ASPECT_RANGE[0] <= width / height <= ASPECT_RANGE[1]:
                continue
            hole = max(
                cv2.contourArea(contours[child])
                for child in _children(hierarchy, hierarchy[index][2])
            )
            centre = (
                x + width // 2 + SURFACE_AREA[0],
                y + height // 2 + SURFACE_AREA[1],
            )
            found.append((hole, centre))

    merged: list[tuple[int, int]] = []
    for _hole, centre in sorted(found, key=lambda item: -item[0]):
        if all(
            abs(centre[0] - kept[0]) > MERGE_DISTANCE or abs(centre[1] - kept[1]) > MERGE_DISTANCE
            for kept in merged
        ):
            merged.append(centre)
    return merged


__all__ = ["SURFACE_AREA", "stargate_candidates"]
