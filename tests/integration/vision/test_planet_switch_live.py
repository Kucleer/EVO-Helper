"""用两张实拍守住切换星球那两个 ROI 与它们的配方。

这两块像素是整条链路上唯一「不可能靠单元测试证明」的部分：单元测试喂的是
读数清单，读数本身对不对只有真实像素回答得了。上一次没做这件事的代价记在
`pirate_ui.FLIGHT_RECIPES` 的注释里——那个 ROI 从落地起就**从来没读出过东西**，
单元测试全绿、变异全红，唯独没人拿真实像素验过。

三张图（client 空间，1920×917，不进 Git）：

- `calib-切换星球-基准.png`：行星列表浮层，三颗星球。守坐标列 ROI + 配方，
  以及**每一行读出来的 y**——那个 y 待会儿要拿去点「前往此处」、要拿去按下拖动。
- `dump-planet-list-unreadable-153847.png`：同一个浮层，第一行换成了 `[4:277:15]`。
  它的第一屏与 2026-08-19 生产日志里那一屏**逐字相同**，所以「读多一位」那条
  就钉在它上面。
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
    PLANET_LIST_COORD_ROI,
    PLANET_LIST_COORD_WHITELIST,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytesseract = pytest.importorskip("pytesseract", reason="requires the vision extra")

PLANET_LIST_SHOT = Path("var/logs/calib-切换星球-基准.png")
FLEET_PANEL_SHOT = Path("var/logs/calib-舰队面板-client.png")
#: 2026-08-19 那一趟的第一屏：`['4:277:15', '9:250:88', '4:96:7']`。
MISREAD_SHOT = Path("var/logs/dump-planet-list-unreadable-153847.png")

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


def _words(upscale: int, resample: str, *, shot: Path = PLANET_LIST_SHOT, whitelist: str = ""):  # type: ignore[no-untyped-def]
    from evo_helper.game.planet_list import coordinate_words
    from evo_helper.tools.scan_coordinates import tesseract_path

    pytesseract.pytesseract.tesseract_cmd = str(tesseract_path())
    return coordinate_words(
        Image.open(shot),
        pytesseract,
        upscale=upscale,
        resample=resample,
        whitelist=whitelist or PLANET_LIST_COORD_WHITELIST,
    )


def _rows(upscale: int, resample: str, *, shot: Path = PLANET_LIST_SHOT):  # type: ignore[no-untyped-def]
    return rows_from_words(_words(upscale, resample, shot=shot))


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


class TestWhyACoordinateGrewADigit:
    """⚠️ **2026-08-19：`9:250:8` 读成 `9:250:88`，用户两颗出发星球一颗都切不过去。**

    这一节是那一位「多出来的数字」的凭据，全部量在
    `dump-planet-list-unreadable-153847.png` 上——它的第一屏
    （`[4:277:15]` / `[9:250:8]` / `[4:96:7]`）与那天生产日志里那一屏逐字相同。

    结论：**词框从头到尾都罩着方括号**，而纯数字白名单里没有 `[` `]`，
    Tesseract 只能从白名单里挑个数字顶上去；顶出来的那一位再被宽松正则粘进
    相邻的一段，就是「多读一位」。对策因此是**反过来**——把方括号放进白名单，
    再要求一行必须成对括起来。
    """

    pytestmark = pytest.mark.skipif(
        not MISREAD_SHOT.exists(),
        reason="缺实拍截图（var/logs/dump-planet-list-unreadable-153847.png）",
    )

    def test_the_first_recipe_reads_that_screen_exactly(self) -> None:
        """稳态路径在这张图上必须读全三颗，`9:250:8` 一位不多。"""
        upscale, resample = PLANET_LIST_COORD_RECIPES[0]

        rows = _rows(upscale, resample, shot=MISREAD_SHOT)

        assert [row.coordinate for row in rows] == [Coordinate(4, 277, 15), SECOND, THIRD]
        assert [row.text for row in rows] == ["[4:277:15]", "[9:250:8]", "[4:96:7]"]

    def test_the_word_box_covers_the_brackets_whatever_the_whitelist_says(self) -> None:
        """**这是「多一位」的直接凭据，不是推断。**

        同一块像素、同一套配方，只换白名单：

            纯数字   '14:277:15'   词框 (1129, 1189)
            带括号   '[4:277:15]'  词框 (1129, 1189)

        词框一模一样——也就是说 Tesseract 两次都看见了那对方括号，区别只在于
        白名单允不允许它把它们写出来。不允许时它没有「跳过」这个选项，
        只能挑个数字顶上，于是 `[` 变成了 `1`。

        （对比度那一下是为了把这个错法稳定地逼出来：原图上这套配方读得对，
        而实机上的画面是半透明面板压着会动的星空，明暗每一帧都在变。）
        """
        from PIL import ImageEnhance

        from evo_helper.tools.scan_coordinates import tesseract_path

        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path())
        image = ImageEnhance.Contrast(Image.open(MISREAD_SHOT)).enhance(1.56)
        crop = image.crop(PLANET_LIST_COORD_ROI).convert("L")
        grey = crop.resize((crop.width * 4, crop.height * 4), Image.Resampling.LANCZOS)

        boxes = {}
        for whitelist in ("0123456789:", PLANET_LIST_COORD_WHITELIST):
            data = pytesseract.image_to_data(
                grey,
                lang="eng",
                config=f"--psm 6 -c tessedit_char_whitelist={whitelist}",
                output_type=pytesseract.Output.DICT,
            )
            boxes[whitelist] = [
                (text.strip(), data["left"][index] // 4, data["width"][index] // 4)
                for index, text in enumerate(data["text"])
                if "277" in text
            ]

        digits_only = boxes["0123456789:"]
        bracketed = boxes[PLANET_LIST_COORD_WHITELIST]
        assert digits_only and bracketed, "这张图上那一行必须两种白名单都读得出来"
        assert digits_only[0][0] == "14:277:15", "纯数字白名单把 `[` 顶成了 `1`"
        assert bracketed[0][0] == "[4:277:15]", "带括号白名单原样读出来"
        assert digits_only[0][1:] == bracketed[0][1:], "同一个词框——括号一直都在框里"

    def test_the_misread_never_becomes_a_row(self) -> None:
        """顶出来那一位一旦出现，这一行必须**不成行**，而不是变成另一颗星球。

        老规则会把 `14:277:15` 认成坐标 14:277:15。那还算「认不到目标」；
        真正致命的是同族的另一半——`[2:137:1` 认成 `2:137:1`，**一颗真的星球**。
        """
        from PIL import ImageEnhance

        from evo_helper.game.planet_list import coordinate_words
        from evo_helper.tools.scan_coordinates import tesseract_path

        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path())
        image = ImageEnhance.Contrast(Image.open(MISREAD_SHOT)).enhance(1.56)
        words = coordinate_words(
            image, pytesseract, upscale=4, resample="lanczos", whitelist="0123456789:"
        )

        assert any(text == "14:277:15" for _y, text in words), "先确认这一帧真的读坏了"
        assert Coordinate(4, 277, 15) not in [row.coordinate for row in rows_from_words(words)]
        assert Coordinate(14, 277, 15) not in [row.coordinate for row in rows_from_words(words)]


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
