"""用两张实拍守住切换星球那两个 ROI 与它们的配方。

这两块像素是整条链路上唯一「不可能靠单元测试证明」的部分：单元测试喂的是
读数清单，读数本身对不对只有真实像素回答得了。上一次没做这件事的代价记在
`pirate_ui.FLIGHT_RECIPES` 的注释里——那个 ROI 从落地起就**从来没读出过东西**，
单元测试全绿、变异全红，唯独没人拿真实像素验过。

两张图（client 空间，1920×917，不进 Git）：

- `calib-切换星球-基准.png`：行星列表浮层，三颗星球。守坐标列 ROI + 配方，
  以及**每一行读出来的 y**——那个 y 待会儿要拿去点「前往此处」、要拿去按下拖动。
- `calib-舰队面板-client.png`：派遣面板，「起点: [2:137:18] [奥格瑞玛]」。
  守回读那一行。它是全仓唯一一处用坐标说出「当前星球是哪一颗」的地方。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.planet_switch import origin_confirmed, rows_from_words
from evo_helper.game.pirate_ui import (
    FLEET_ORIGIN_RECIPES,
    FLEET_ORIGIN_ROI,
    PLANET_GOTO_COLUMN_X,
    PLANET_ICON_ROW_OFFSET_Y,
    PLANET_LIST_COORD_RECIPES,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytesseract = pytest.importorskip("pytesseract", reason="requires the vision extra")

PLANET_LIST_SHOT = Path("var/logs/calib-切换星球-基准.png")
FLEET_PANEL_SHOT = Path("var/logs/calib-舰队面板-client.png")

HOME = Coordinate(2, 137, 18)
SECOND = Coordinate(9, 250, 8)
THIRD = Coordinate(4, 96, 7)

#: 基准图上量出来的「前往此处」。识别出来的行 + 偏移量必须落回这三个点上。
GOTO_POINTS = [(1166, 250), (1166, 480), (1166, 710)]

#: 量出来的 y 与 OCR 词框中心难免差一两像素。图标高约 40px，容差 5 远小于它，
#: 也远小于图标排之间的 70px——放宽到那个量级就等于允许点到下一排。
Y_TOLERANCE_PX = 5

pytestmark = pytest.mark.skipif(
    not (PLANET_LIST_SHOT.exists() and FLEET_PANEL_SHOT.exists()),
    reason="缺实拍截图（var/logs/calib-切换星球-基准.png / calib-舰队面板-client.png）",
)


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def _rows(upscale: int, resample: str):  # type: ignore[no-untyped-def]
    from evo_helper.game.planet_list import coordinate_words
    from evo_helper.tools.scan_coordinates import tesseract_path
    from evo_helper.vision.scan_reading import COORD_WHITELIST

    pytesseract.pytesseract.tesseract_cmd = str(tesseract_path())
    words = coordinate_words(
        Image.open(PLANET_LIST_SHOT),
        pytesseract,
        upscale=upscale,
        resample=resample,
        whitelist=COORD_WHITELIST,
    )
    return rows_from_words(words)


def test_the_first_recipe_reads_all_three_planets() -> None:
    """第一套配方就要读全三颗——它是稳态路径，读不出会让每一轮都白试四套。"""
    upscale, resample = PLANET_LIST_COORD_RECIPES[0]

    assert [row.coordinate for row in _rows(upscale, resample)] == [HOME, SECOND, THIRD]


def test_the_go_to_here_points_land_where_they_were_measured() -> None:
    """识别出来的行 + 偏移量 = 量出来的那三个点。

    ⚠️ 这条守的是**整条几何链**：ROI 框歪了、配方读错了、偏移量改坏了，
    三者任何一个出问题都会让点击落到别的图标上，而这里都会红。
    """
    upscale, resample = PLANET_LIST_COORD_RECIPES[0]

    points = [
        (PLANET_GOTO_COLUMN_X, row.name_row_y + PLANET_ICON_ROW_OFFSET_Y)
        for row in _rows(upscale, resample)
    ]

    assert [x for x, _y in points] == [x for x, _y in GOTO_POINTS]
    for (_x, got), (_wanted_x, wanted) in zip(points, GOTO_POINTS, strict=True):
        assert abs(got - wanted) <= Y_TOLERANCE_PX


def test_the_nearest_neighbour_recipe_comes_first_because_lanczos_misreads_a_digit() -> None:
    """**这是配方顺序的凭据。**

    3× LANCZOS 在这张图上把 `[9:250:8]` 读成 `8:250:8`——不是读不出，是**读成
    另一颗星球**。第一套要是它，`find_row(rows, 9:250:8)` 会当成「这一屏没有」
    接着拖（还算安全），而配了 8:250:8 的人会被带到 9 号那一行去（不安全）。

    所以这条不许放松成「反正后面几套能救回来」：救得回来的前提是它先失败，
    而这里它是**成功地读错**。
    """
    assert PLANET_LIST_COORD_RECIPES[0][1] == "nearest"
    misread = [row.coordinate for row in _rows(3, "lanczos")]

    assert SECOND not in misread, "LANCZOS 这一套要是哪天也读对了，这条注释就该重写"


def test_every_recipe_reads_the_origin_line_on_the_fleet_panel(ocr) -> None:  # type: ignore[no-untyped-def]
    """回读那一行八套配方全对（实测），所以这里逐套都要过。

    读不出来的后果不是「切不成」而是**当作没切成**：整轮不派。
    一套读不出就退化成「配了别的星球就永远派不出去」。
    """
    image = Image.open(FLEET_PANEL_SHOT)

    for upscale, resample in FLEET_ORIGIN_RECIPES:
        text = ocr(image.crop(FLEET_ORIGIN_ROI), digits=True, upscale=upscale, resample=resample)
        assert origin_confirmed(text, HOME), f"{upscale}x/{resample} 读到 {text!r}"


def test_the_origin_roi_stops_short_of_the_planet_name(ocr) -> None:  # type: ignore[no-untyped-def]
    """右界必须把 `[奥格瑞玛]` 关在外面：中文进数字白名单只会压出噪声。

    往右放宽 60px 就把名字框进来了——这条钉的是「别顺手加宽」。
    """
    left, top, right, bottom = FLEET_ORIGIN_ROI
    image = Image.open(FLEET_PANEL_SHOT)
    upscale, resample = FLEET_ORIGIN_RECIPES[0]

    tight = ocr(image.crop(FLEET_ORIGIN_ROI), digits=True, upscale=upscale, resample=resample)
    widened = ocr(
        image.crop((left, top, right + 60, bottom)), digits=True, upscale=upscale, resample=resample
    )

    assert origin_confirmed(tight, HOME)
    assert tight != widened, "放宽之后读数没变的话，这道边界就是白设的"
