"""浮层左上角那个 ✕：全仓唯一一处「把盖在画面上的面板关掉」。

信箱、消息详情、飞行中列表、派遣面板、行星列表、太空舱（材料仓库）——这些浮层
共用一套外框，关闭键都在同一个像素上。所以关浮层这件事**不需要先认出是哪一种
浮层**，也正因为如此，它必须是一份代码：认出来的路径各不相同，关掉的动作只有一种。

## 为什么下沉到 `game/`

原先这三样（坐标、上限、点击循环）住在 `tools/scan_coordinates.py`，只有坐标扫描
那条链路用得上。攻击链路要用就得 `game/` 反过来 import `tools/`——那是环
（`game.planet_list.PlanetSwitcher.say` 保留 `print` 默认值就是为了躲这个环）。
于是把这一份沉到 `game/`，两条链路各自 import 它，谁也不用认识谁。

## ⚠️ 点之前必须先认出那个 ✕（2026-08-18 用户亲自指出）

原先这里是**盲点**：不看 `OVERLAY_CLOSE_BUTTON` 上是什么，闷头点满 4 下。
实机 2026-08-18 10:04 与 10:05 各一次，那一刻屏幕上其实是**军力排行榜面板**，
4 下全落进榜单里。用户原话：「点 4 下关闭，应校验按钮形态，不然就会点到排行榜中去」。

而同一批代码里 `game.planet_list` 的模块头写着「**绝不按行号盲点**——那一排里
转移/投送/保护/扩张点错任何一个都是真实操作」。两条规矩自相矛盾，本模块这一条
是错的那一条。

`OVERLAY_CLOSE_BUTTON` 上**没有浮层时坐着什么**，实拍图里查得到，而且不是「什么
都没有」：恒星系视图上那个位置正压在导航栏第一个输入框（`银河系`）里
（`var/logs/atk-0-panel.png`、`plist-0.png`），星球地表上是等级徽章那一格
（`var/logs/rank-closed.png`）。所以「点空无害」这句话从来就不成立。

现在的规矩：**认出 ✕ 才点，认不出一下都不点**，并把「认不出」如实报给调用方
（`DismissOutcome.recognised`）。认不出的代价只是这一轮不派；点错的代价是在
军力榜 / 行星列表 / 导航输入框上触发真实操作。

## ⚠️ 同一个位置上还坐过一个 «，它不是 ✕

实拍里有 107 张（战报详情、简报、信箱详情，如 `var/logs/atk-4-dispatched.png`）
在这个像素上放的是一个**双箭头「返回」**，不是 ✕。两者外框一模一样，只有里面
那个图形不同——而「返回」和「关闭」在游戏里是两个动作。

本模块**故意只认 ✕**（« 的实测 IoU 只有 0.348，落在阈值外）。要是哪天真需要
在那些画面上收手，正确的做法是给 « 单独标一份点阵、单独一个函数、由调用方明说
自己要的是哪一个，而不是把阈值调低到两个都能过。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

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
#:
#: 从 2026-08-18 起它是**上限而不是次数**：每一下之前都要重新看一眼 ✕ 还在不在，
#: ✕ 没了就停手。所以关干净的那一刻自然就停了，不再有「多点的那几下落到哪里」。
OVERLAY_CLOSE_ATTEMPTS = 4

#: 每一下之后等画面收回去。
OVERLAY_CLOSE_WAIT_S = 2.0

# -- 「那个 ✕ 在不在」的判据 ---------------------------------------------------
#
# ⚠️ **判据是图形，不是 OCR。** 那个 ✕ 不是字，`image_to_string` 读它只会读出
# 各种噪声（`x` / `X` / `%` / 空），拿字符去比等于把判据建在最不稳的一环上。
#
# 判据用的是**这一小块画面的白色像素形状**，理由是它在实拍图上完全稳定：游戏把
# 这个按钮渲染成同一张位图，330 张实拍里认出来的那 92 张**逐像素相同**（IoU 精确
# 等于 1.000），唯一的例外是浮层还在滑入动画里、整块偏了几像素的那一张
# （`var/logs/dump-mail-list-unrecognised-175927.png`，IoU 0.873）。
#
# 三个数（下面三个常量）是 2026-08-18 在 `var/logs/` 全部 330 张 client 空间
# （1920×917）实拍上量出来的，判据两侧的间隔大得离谱：
#
#     认出来的 92 张   IoU 0.873 – 1.000，静默环白占比 ≤ 0.012
#     其余 238 张      IoU ≤ 0.546，而那唯一的 0.546 静默环白占比 = 1.000
#
# 中间隔着 0.33 的空档，阈值取 0.60 落在正中。

#: ✕ 图形的左上角（client 空间绝对像素）与它的点阵。
#:
#: 从 `var/logs/rankv/21-panel.png`（2026-08-14 实机，军力榜面板开着）上按
#: 「亮且不带颜色」二值化出来的，18 列 × 17 行，共 167 个白点。
#:
#: ⚠️ **写成看得见的点阵，不是一串校验和。** 下一个人改这里之前得先看出它是个 ✕；
#: 而版面真的改了时，重新量一遍就是把新的点阵贴回来，不必再逆推任何数字。
OVERLAY_CLOSE_GLYPH_ORIGIN = (741, 63)
OVERLAY_CLOSE_GLYPH = (
    ".###..........###.",
    ".####........#####",
    "######......######",
    ".######....######.",
    "..######..######..",
    "...############...",
    "....##########....",
    ".....########.....",
    "......#######.....",
    ".....########.....",
    "....##########....",
    "...############...",
    "..######..######..",
    ".######....######.",
    "######......######",
    ".####........####.",
    ".###..........##..",
)

#: 「白」的判据：亮度足够高、而且**几乎不带颜色**。
#:
#: 两条缺一不可。只看亮度的话，浮层外框那圈青色高光（R 低 G/B 高）也会算成白；
#: 只看饱和度的话，面板底色那一大片暗灰同样「不带颜色」。取 200/40 是因为实拍上
#: ✕ 的笔画是纯白（250 以上）、它周围最亮的青色高光饱和度在 60 以上，两边都远离。
OVERLAY_GLYPH_MIN_LUMA = 200
OVERLAY_GLYPH_MAX_SATURATION = 40

#: 点阵重合到这个程度才算「认出来了」（交并比 IoU）。
#:
#: ⚠️ **这不是偏好项，是标定常量。** 取值由实拍上两类画面的间隔决定
#: （认出来的 ≥ 0.873，认错的 ≤ 0.546），调它不会「更适合谁」，只会让判据开始
#: 说谎：调低会把**「返回」那个 «** 也认成 ✕（实测 IoU 0.348，见下），
#: 调高则会把滑入动画中的浮层判成认不出。
OVERLAY_CLOSE_GLYPH_MIN_IOU = 0.60

#: 静默环：紧贴点阵外面这么宽的一圈里，白像素最多占这么多。
#:
#: ⚠️ **这一条挡的是「整块画面泛白」，光靠 IoU 挡不住。** 一屏全白时（浏览器还在
#: 加载，`var/logs/rankv/00-baseline.png`），点阵框里当然「全中」，IoU 恰好等于
#: 167/306 = 0.546——离 0.60 只差一点点，而它离真正的 ✕ 差得远。✕ 是**孤立**的
#: 图形：实拍上认出来的 92 张，这一圈的白占比没有超过 0.012 的。
OVERLAY_CLOSE_QUIET_MARGIN_PX = 3
OVERLAY_CLOSE_QUIET_MAX_RATIO = 0.15


@dataclass(frozen=True)
class CloseButtonLook:
    """朝 `OVERLAY_CLOSE_BUTTON` 看一眼的结果，**带上判据的两个读数**。

    读数要带出来，不然「认不出」在日志里就只是一句没有下文的话——而这一条正是
    2026-08-17 那次通宵空转的教训（日志只说 `unrecognised screen`，没说看到了什么）。
    """

    #: 与 `OVERLAY_CLOSE_GLYPH` 的交并比。
    iou: float
    #: 静默环里白像素的占比。
    quiet_ratio: float

    @property
    def visible(self) -> bool:
        return (
            self.iou >= OVERLAY_CLOSE_GLYPH_MIN_IOU
            and self.quiet_ratio <= OVERLAY_CLOSE_QUIET_MAX_RATIO
        )

    def as_payload(self) -> dict[str, Any]:
        """写进 `system_log` 的 `payload_json` 的那一份。"""
        return {
            "close_button_iou": round(self.iou, 3),
            "close_button_quiet_ratio": round(self.quiet_ratio, 3),
            "close_button_visible": self.visible,
        }


def _is_white(pixel: tuple[int, ...]) -> bool:
    red, green, blue = pixel[0], pixel[1], pixel[2]
    luma = max(red, green, blue)
    return luma >= OVERLAY_GLYPH_MIN_LUMA and luma - min(red, green, blue) <= (
        OVERLAY_GLYPH_MAX_SATURATION
    )


def look_at_close_button(image: Any) -> CloseButtonLook:
    """在一张整窗截图（client 空间 1920×917）上量一量那个 ✕ 像不像。

    纯 Pillow、不碰 numpy/OpenCV：要看的只有 24×23 = 552 个像素，而 `[dev]` 那套
    依赖里本来就只有 Pillow——判据必须在**只装了测试依赖**的机器上跑得起来，
    否则它就没法在实拍图上离线复验。
    """
    left, top = OVERLAY_CLOSE_GLYPH_ORIGIN
    width = len(OVERLAY_CLOSE_GLYPH[0])
    height = len(OVERLAY_CLOSE_GLYPH)
    margin = OVERLAY_CLOSE_QUIET_MARGIN_PX
    # 已经是 RGB 就别再 `convert` 一次：那会整帧复制 1920×917，而这里只看 552 个点。
    frame = image if getattr(image, "mode", None) == "RGB" else image.convert("RGB")

    hit = 0
    read = 0
    for row, line in enumerate(OVERLAY_CLOSE_GLYPH):
        for column, mark in enumerate(line):
            white = _is_white(frame.getpixel((left + column, top + row)))
            if white:
                read += 1
            if mark == "#" and white:
                hit += 1
    expected = sum(line.count("#") for line in OVERLAY_CLOSE_GLYPH)
    union = expected + read - hit

    quiet = 0
    quiet_total = 0
    for y in range(top - margin, top + height + margin):
        for x in range(left - margin, left + width + margin):
            if left <= x < left + width and top <= y < top + height:
                continue
            quiet_total += 1
            if _is_white(frame.getpixel((x, y))):
                quiet += 1

    return CloseButtonLook(
        iou=hit / union if union else 0.0,
        quiet_ratio=quiet / quiet_total if quiet_total else 0.0,
    )


def close_button_visible(image: Any) -> bool:
    """那个 ✕ 在不在。认不出就是 False——**这一档一下都不许点**。"""
    return look_at_close_button(image).visible


class OverlayDriver(Protocol):
    """关浮层要的全部操作面：点一下、等一会儿。

    收得这么窄是有意的——`LiveDriver`、`game.planet_list.PlanetListDriver`、
    测试里的假驱动都能直接喂进来，而这一层碰不到拖动、截图、OCR。

    ⚠️ **看屏不在这个面上**，而是由调用方注入 `see_close_button`：认出 ✕ 的那份
    像素判据在本模块（`look_at_close_button`），但**怎么拿到那一帧**各条链路不同
    （`LiveDriver.capture()` / 测试里的桩），沿用本模块「判据在这里、取图在调用方」
    的分法。
    """

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def wait(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class DismissOutcome:
    """关浮层这一趟做了什么。

    ⚠️ **不能退回成一个 `int`。** 「点了 0 下」有两种截然不同的读法：认出了 ✕、
    点掉了（不可能是 0 下）——和**根本没认出 ✕，所以一下都没点**。后者是要让
    调用方看见并如实说出去的那一种（见 `game.planet_list.SwitchResult.UNREADABLE`）。
    """

    clicked: int
    #: 点满上限之后回看，那个 ✕ 还在不在。True = 关不掉。
    still_visible: bool

    @property
    def recognised(self) -> bool:
        """开工那一刻认出过 ✕ 吗。False = 一下都没点。"""
        return self.clicked > 0


def dismiss_overlays(
    driver: OverlayDriver,
    *,
    see_close_button: Callable[[], bool],
    attempts: int = OVERLAY_CLOSE_ATTEMPTS,
    wait_s: float = OVERLAY_CLOSE_WAIT_S,
    is_clear: Callable[[], bool] | None = None,
) -> DismissOutcome:
    """**认出 ✕ 才点**，最多点 `attempts` 下。

    `see_close_button` 每一下之前都要问一次（同 `game.action_guard` 的「点击前
    重新观察」）：答否就停手，一下都不点。它是**必填**的关键字参数，没有默认值——
    默认成「看得见」等于把盲点偷偷放回来，默认成「看不见」则会让忘了接的调用方
    静默地永远不关浮层。两种都是那类「改坏了不报错」的错，所以逼调用方写出来。

    `is_clear` 是「已经关干净了吗」的回读；给了就每点一下问一次，答是就停手。
    不给也没关系：下一轮开头的 `see_close_button` 本身就是一次回读——✕ 没了
    就说明这一层关掉了。这正是「认出来才点」顺带修掉的东西：原先不给 `is_clear`
    时会闷头点满 4 下，多出来的那几下落在**已经关掉浮层之后**的画面上。

    **有上限，绝不成环。** 关不掉的画面可能压根不是浮层（维护公告、界面改版），
    在上面无限点下去比停下来糟得多。
    """
    clicked = 0
    for _attempt in range(attempts):
        if not see_close_button():
            return DismissOutcome(clicked=clicked, still_visible=False)
        driver.click(*OVERLAY_CLOSE_BUTTON, label="关闭面板")
        driver.wait(wait_s)
        clicked += 1
        if is_clear is not None and is_clear():
            return DismissOutcome(clicked=clicked, still_visible=False)
    return DismissOutcome(clicked=clicked, still_visible=see_close_button())


__all__ = [
    "OVERLAY_CLOSE_ATTEMPTS",
    "OVERLAY_CLOSE_BUTTON",
    "OVERLAY_CLOSE_GLYPH",
    "OVERLAY_CLOSE_GLYPH_MIN_IOU",
    "OVERLAY_CLOSE_GLYPH_ORIGIN",
    "OVERLAY_CLOSE_QUIET_MARGIN_PX",
    "OVERLAY_CLOSE_QUIET_MAX_RATIO",
    "OVERLAY_CLOSE_WAIT_S",
    "OVERLAY_GLYPH_MAX_SATURATION",
    "OVERLAY_GLYPH_MIN_LUMA",
    "CloseButtonLook",
    "DismissOutcome",
    "OverlayDriver",
    "close_button_visible",
    "dismiss_overlays",
    "look_at_close_button",
]
