"""用实拍守住导航栏三个**值框**的 ROI 与配方。

单元测试喂的是三个字符串，字符串对不对只有真实像素回答得了。这一块新加的像素
决定的是「切完出发星球之后要不要省字段」——读错一次的代价写在
`game.system_navigator.SystemNavigator` 的类注释里（缓存与导航栏分岔，
连续 44 个目标核对全不过、13 分钟一发没派）。

两张图（client 空间，1920×917，不进 Git）：

- `calib-恒星系-client.png`：**刚登录、一个字都没往框里打过**，从星球地表切到恒星系
  视图之后的那一屏。三个框读的是当前星球 `2:137:18`。这张图同时是「导航栏显示的
  就是当前所在坐标」这件事的唯一直接证据——正因为这一局里没人打过字，框里那三个
  数只可能是游戏自己填的。
- `dump-bot-coord-mismatch-014034.png`：另一组数 `9:137:12`。它守的是**逐框换配方**
  这条规矩：第一套读得出 `137`，银河系的 `9` 与行星的 `12` 要换一套才读得出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.game.system_navigator import NAV_VALUE_RECIPES, NAV_VALUE_ROIS

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")

FRESH_SYSTEM_VIEW = Path("var/logs/calib-恒星系-client.png")
OTHER_GALAXY_SHOT = Path("var/logs/dump-bot-coord-mismatch-014034.png")

#: 两张图上人工核对过的真值。
FRESH_VALUES = ("2", "137", "18")
OTHER_VALUES = ("9", "137", "12")

pytestmark = pytest.mark.skipif(
    not (FRESH_SYSTEM_VIEW.exists() and OTHER_GALAXY_SHOT.exists()),
    reason="缺实拍截图（var/logs/calib-恒星系-client.png / dump-bot-coord-mismatch-014034.png）",
)


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def _values(ocr, path: Path) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    """照 `tools.pirate_loop._navigation_bar_values` 的规矩读：每个框各自换配方。"""
    image = Image.open(path)
    read = []
    for roi in NAV_VALUE_ROIS:
        text = ""
        for upscale, threshold in NAV_VALUE_RECIPES:
            text = ocr(image.crop(roi), digits=True, upscale=upscale, threshold=threshold)
            if text:
                break
        read.append(text)
    return tuple(read)


def test_a_freshly_entered_system_view_reads_the_current_planet(ocr) -> None:  # type: ignore[no-untyped-def]
    """三个框读出来就是当前站着的那颗星球。

    ⚠️ 这一条同时是**功能的前提**：切完出发星球再切回恒星系视图之后，导航栏里
    应该就是那颗星球的坐标，于是同银河的下一个目标不必重设银河系。这张图是刚登录
    那一屏——没人往框里打过字，所以框里的数只可能来自游戏本身。
    """
    assert _values(ocr, FRESH_SYSTEM_VIEW) == FRESH_VALUES


def test_the_recipes_are_tried_per_box_because_one_box_can_need_another(ocr) -> None:  # type: ignore[no-untyped-def]
    """**逐框换配方**的凭据：这张图上第一套只读得出中间那个框。

    三个框绑在一起换配方的话，这张图会整份读空——功能不会出错，但一次都不生效。
    """
    image = Image.open(OTHER_GALAXY_SHOT)
    upscale, threshold = NAV_VALUE_RECIPES[0]
    first_pass = tuple(
        ocr(image.crop(roi), digits=True, upscale=upscale, threshold=threshold)
        for roi in NAV_VALUE_ROIS
    )

    assert not all(first_pass), f"第一套就全读出来的话这条注释该重写：{first_pass}"
    assert _values(ocr, OTHER_GALAXY_SHOT) == OTHER_VALUES


def test_no_recipe_ever_reads_a_wrong_number(ocr) -> None:  # type: ignore[no-untyped-def]
    """**每一套单独看也不许读错**，读空可以。

    这是挑这三套而不是别的的理由。被剔掉的 `(3,140)` / `(4,140)` 在实拍上把 `11`
    读成 `1`、把 `9` 读成 `93`——读空只是白设两个字段，读错是往缓存里塞一份假证据。
    """
    for path, truth in ((FRESH_SYSTEM_VIEW, FRESH_VALUES), (OTHER_GALAXY_SHOT, OTHER_VALUES)):
        image = Image.open(path)
        for upscale, threshold in NAV_VALUE_RECIPES:
            for roi, wanted in zip(NAV_VALUE_ROIS, truth, strict=True):
                text = ocr(image.crop(roi), digits=True, upscale=upscale, threshold=threshold)
                assert text in ("", wanted), f"{upscale}x/th{threshold} 在 {roi} 读到 {text!r}"


def test_the_value_boxes_sit_above_the_label_row_and_never_overlap_it() -> None:
    """值框与标签行分工不同，几何上也必须分开。

    `NAV_LABEL_ROI` 读的是「银河系 / 恒星系 / 行星」这几个字，用来判断在不在恒星系
    视图；值框读的是框里的数。谁把值框往下放宽到标签上，中文就会挤进数字白名单，
    读出来的是噪声——而噪声与空串在这条链路上是两码事：空串安全，噪声会不确认，
    但更糟的是万一噪声恰好是几个数字。
    """
    from evo_helper.game.system_navigator import NAV_LABEL_ROI

    for _left, _top, _right, bottom in NAV_VALUE_ROIS:
        assert bottom <= NAV_LABEL_ROI[1]
