"""浮层左上角那个 ✕：全仓唯一一处「把盖在画面上的面板关掉」。

信箱、消息详情、飞行中列表、派遣面板、行星列表、太空舱（材料仓库）——这些浮层
共用一套外框，关闭键都在同一个像素上。所以关浮层这件事**不需要先认出是哪一种
浮层**，也正因为如此，它必须是一份代码：认出来的路径各不相同，关掉的动作只有一种。

## 为什么下沉到 `game/`

原先这三样（坐标、上限、点击循环）住在 `tools/scan_coordinates.py`，只有坐标扫描
那条链路用得上。攻击链路要用就得 `game/` 反过来 import `tools/`——那是环
（`game.planet_list.PlanetSwitcher.say` 保留 `print` 默认值就是为了躲这个环）。
于是把这一份沉到 `game/`，两条链路各自 import 它，谁也不用认识谁。

## 最坏情况下点到了什么

那个位置在恒星系视图上什么都不是，点空无害——这是 `tools.scan_coordinates`
那条恢复阶梯早就写下的判断，本模块沿用。⚠️ 但**星球地表上那个位置本仓没有标定
过**（见 `game.planet_list.PlanetSwitcher.switch_to` 里的告警）。所以调用方在
地表上用它之前，得先有「画面上确实压着一层什么」的证据，而不是每次顺手点一下。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

#: 各种浮层左上角的关闭键（client 空间绝对像素）。
#: `game.pirate_ui.PLANET_LIST_CLOSE` 与 `tools.pirate_loop.MAIL_BACK` 是同一个点。
OVERLAY_CLOSE_BUTTON = (750, 71)

#: 关浮层最多点这么多下。每种浮层最多套两层（列表 → 详情），4 下留了余量。
#:
#: ⚠️ **这不是偏好项，是标定常量**（2026-08-17 审计）。它编码的是**游戏版面**的
#: 一个事实——这套外框最多套两层——不是「用户想点几下」。调小会在双层浮层上关不
#: 干净，后续读屏读到的是浮层内容；调大则是在一屏**根本不是浮层**的东西上多点几下
#: （维护公告、界面改版），而那个位置在星球地表上本仓从没标定过。
#: 真要改，先重新数一遍游戏里的浮层层数，别按「多点几下更保险」调。
OVERLAY_CLOSE_ATTEMPTS = 4

#: 每一下之后等画面收回去。
OVERLAY_CLOSE_WAIT_S = 2.0


class OverlayDriver(Protocol):
    """关浮层要的全部操作面：点一下、等一会儿。

    收得这么窄是有意的——`LiveDriver`、`game.planet_list.PlanetListDriver`、
    测试里的假驱动都能直接喂进来，而这一层碰不到拖动、截图、OCR。
    """

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def wait(self, seconds: float) -> None: ...


def dismiss_overlays(
    driver: OverlayDriver,
    *,
    attempts: int = OVERLAY_CLOSE_ATTEMPTS,
    wait_s: float = OVERLAY_CLOSE_WAIT_S,
    is_clear: Callable[[], bool] | None = None,
) -> int:
    """朝 `OVERLAY_CLOSE_BUTTON` 点最多 `attempts` 下，返回真的点了几下。

    `is_clear` 是「已经关干净了吗」的回读；给了就每点一下问一次，答是就停手。
    不给就闷头点满 `attempts` 下——调用方那时没有便宜的判据可用（行星列表那条
    就是这样：浮层盖着时读什么都是空，唯一能问的问题贵得等同重来一遍）。

    **有上限，绝不成环。** 关不掉的画面可能压根不是浮层（维护公告、界面改版），
    在上面无限点下去比停下来糟得多。
    """
    clicked = 0
    for _attempt in range(attempts):
        driver.click(*OVERLAY_CLOSE_BUTTON, label="关闭面板")
        driver.wait(wait_s)
        clicked += 1
        if is_clear is not None and is_clear():
            break
    return clicked


__all__ = [
    "OVERLAY_CLOSE_ATTEMPTS",
    "OVERLAY_CLOSE_BUTTON",
    "OVERLAY_CLOSE_WAIT_S",
    "OverlayDriver",
    "dismiss_overlays",
]
