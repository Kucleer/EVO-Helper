"""「获得资源」12 格的字模匹配，跑在**合成字条**上。

## 夹具为什么是合成的，不是实拍

本仓是**公开仓库**，而一份战报面板的截图上写着账号 ID、出发星与目标坐标、
以及逐格的资源数量。这类图**一张都不许进 git**——2026-08-18 有一版正是把 34 份
实拍面板当夹具提交了，只能整个撤回。

所以这里的图是**画出来的**：拿 `RESOURCE_GLYPHS`（字模表本身只是字体，不含任何
账号数据）把给定的一串字符拼成一条 9 像素高的字条，再嵌进一格 76×20 的背景里，
背景上还铺一层 `GHOST_LUMINANCE` 的幽灵文字。整个过程不读任何游戏产物，
完全可复现，CI 里照跑。

用到的数字串（`SYNTHETIC_GRID`）是**随手编的**，刻意不取自任何一份真实战报。

## ⚠️ 这批夹具证得了什么、证不了什么

**证得了**：字条定位、幽灵文字被门槛压掉、DP 逐列切字（包括相邻两个 `1` 之间
没有背景列这种情形）、字模表内部两两可分（`3`/`9`、`5`/`9`、`6`/`8`、`8`/`9`
这几对形近字尤其）、以及一整套失败闭合的判据。字模表被改坏、DP 被改坏、
判据被放松，这里都会红。

**证不了**：真实像素上的准确率。合成图是拿字模自己渲染的，逐像素完全吻合，
似然恒等于 1.0——而实拍上最低只有 0.787。`MATCH_FLOOR` 这个门槛的余量、
以及「34 份读全、29 份逐格全对」这个成绩，只有真图能回答，那一半在
`tests/integration/vision/test_resource_grid_corpus_live.py`：语料放在 `var/` 下
（`.gitignore` 挡着），本机有图才跑，CI 里跳过。

⚠️ **`BAND_PAD` 也在「证不了」这一边，这是量出来的，不是猜的。** 把它改成 0
再跑，这个文件**一条都不红**，实拍那边却整批塌掉——因为无噪声的合成图上，
`_place` 允许的那一列悬挂正好补上了字模右侧的空列。所以别在这里加一条
「`BAND_PAD >= 2`」的断言充数：那只是把常量抄了两遍，验不了任何行为。
它的凭据在实拍语料里，也只能在那里。

两条缺一不可：合成的守住「改坏了会红」，实拍的守住「本来就是对的」。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT, parse_resource_grid
from evo_helper.vision.report_layout import LIVE_LAYOUT
from evo_helper.vision.resource_digits import (
    GLYPH_HEIGHT,
    INK_THRESHOLD,
    MATCH_FLOOR,
    RESOURCE_GLYPHS,
    decode_band,
    ink_band,
    read_resource_cell,
)

#: 合成格子的尺寸，取自生产版面常量——这样「渲染出来的格子」和实机裁出来的
#: ROI 是同一个形状，字条在格子里的相对位置也就有意义。
CELL = LIVE_LAYOUT.resource_grid
CELL_WIDTH = CELL.number_width
CELL_HEIGHT = CELL.number_height

#: 字条在格子里的落点。20 高的格子放 9 高的字条，上下都留得出余量。
BAND_TOP = 5
BAND_LEFT = 6

#: 背景亮度。远在 `INK_THRESHOLD` 之下，归一化之后是 0。
BACKGROUND_LUMINANCE = 38

#: 幽灵文字的亮度。实拍里那层 `-TOTAL CREWS` / `personnel` 底纹最高到 102，
#: 这里照抄一个上界值铺满格子——**它必须被门槛整个压掉**，压不掉的话字条的
#: 上下界就会被它拉宽，行带高度判据随即失效。
GHOST_LUMINANCE = 102

#: 字与字之间空几列背景。实拍的字距是浮动的（同一份语料里量到 −1 到 8 都有），
#: 所以这里**不当成常量**，几种字距都要读得回来，见 `TestKerningIsNotAssumed`。
DEFAULT_GAP = 1

_INK_SPAN = 255 - INK_THRESHOLD

#: 12 格的合成内容。**随手编的数字，不取自任何一份真实战报。**
#:
#: 它一并把字模表里 13 个字形全用上了——`test_the_sample_grid_uses_every_glyph`
#: 把这件事钉成判据：将来字模表添了新字形（比如十亿的 `B`），那条用例会红，
#: 逼着这张表跟着覆盖上，而不是让新字形悄悄没人测。
SYNTHETIC_GRID: tuple[str, ...] = (
    "1.2M",  # 兆后缀 + 小数点
    "907.4K",  # 千后缀 + 小数点 + 六个字符，语料里最宽的形制
    "38.6K",
    "5K",  # 后缀紧跟单个数字：`K` 右边必须留得下 BAND_PAD
    "0",  # 孤零零一个 0，老配方上最难的一格
    "11",  # 相邻两个 1，中间**没有**背景列
    "246",
    "80",
    "593",
    "0",
    "17",
    "0",
)

#: 形近字对。实拍上剩下的 5 处读错全落在这四对上，所以它们在字模层面
#: 必须两两可分——分不开的话，错的就不是运气而是字模表本身。
CONFUSABLE_PAIRS: tuple[tuple[str, str], ...] = (
    ("3", "9"),
    ("5", "9"),
    ("6", "8"),
    ("8", "9"),
)


def blank_cell() -> list[list[int]]:
    """一格纯背景，一点墨迹都没有。"""
    return [[BACKGROUND_LUMINANCE] * CELL_WIDTH for _ in range(CELL_HEIGHT)]


def render_cell(
    text: str,
    *,
    top: int = BAND_TOP,
    left: int = BAND_LEFT,
    gap: int = DEFAULT_GAP,
    ghost: bool = True,
) -> list[list[int]]:
    """把一串字符渲染成一格的灰度像素。

    字模里的 0..9 是灰阶档位，这里按 `INK_THRESHOLD + 档位/9 × (255 − 门槛)`
    还原成亮度——也就是 `ink_band` 归一化的逆运算。档位为 0 的像素**留作背景**，
    不写门槛值：字形的外接框正是靠这些背景列定出来的。
    """
    cell = blank_cell()
    if ghost:
        # 斜铺一层幽灵文字，横跨字条内外。字条上下都有它，行带却必须仍然是 9 高。
        for y in range(2, CELL_HEIGHT, 4):
            for x in range(1, CELL_WIDTH, 3):
                cell[y][x] = GHOST_LUMINANCE
    cursor = left
    for char in text:
        rows = RESOURCE_GLYPHS[char]
        for offset_y, row in enumerate(rows):
            for offset_x, digit in enumerate(row):
                level = int(digit)
                if level:
                    cell[top + offset_y][cursor + offset_x] = INK_THRESHOLD + round(
                        level / 9 * _INK_SPAN
                    )
        cursor += len(rows[0]) + gap
    return cell


def read_grid(texts: tuple[str, ...] = SYNTHETIC_GRID) -> tuple[str, ...]:
    return tuple(read_resource_cell(render_cell(text)) for text in texts)


class TestTheSyntheticGridReadsBack:
    def test_the_sample_grid_uses_every_glyph(self) -> None:
        """12 格合起来把字模表用了个遍。

        添了新字形却不扩这张表，这条就红——新字形因此不可能悄悄溜过测试。
        """
        assert set("".join(SYNTHETIC_GRID)) == set(RESOURCE_GLYPHS)

    def test_all_twelve_cells_read_back_exactly(self) -> None:
        """12 格逐格读回原样，一格都不许差。"""
        assert read_grid() == SYNTHETIC_GRID

    def test_the_grid_survives_the_all_or_nothing_gate(self) -> None:
        """读全了才有资格入库：整块解析得出来，且值为 0 的格子不留行。"""
        entries = parse_resource_grid(read_grid())

        assert entries is not None
        assert [entry.slot for entry in entries] == [0, 1, 2, 3, 5, 6, 7, 8, 10]

    @pytest.mark.parametrize("char", sorted(set(RESOURCE_GLYPHS) - {"."}))
    def test_every_glyph_reads_as_itself(self, char: str) -> None:
        """逐个字模单独渲染、单独读回。

        `.` 不在其中：它的墨迹只占最后一行，单独成条时高度是 1 而不是
        `GLYPH_HEIGHT`，按判据整格作废——那正是 `test_a_short_band_is_refused`
        守的行为。
        """
        assert read_resource_cell(render_cell(char)) == char

    @pytest.mark.parametrize(("left", "right"), CONFUSABLE_PAIRS)
    def test_confusable_pairs_stay_apart(self, left: str, right: str) -> None:
        """形近字对必须两两可分：单独读、并排读都不许串。

        实拍上剩下的读错全在这几对上。字模表要是被改到这两个字形撞了车，
        真图上的错误率会直接翻番，而这里先红。
        """
        assert read_resource_cell(render_cell(left)) == left
        assert read_resource_cell(render_cell(right)) == right
        assert read_resource_cell(render_cell(left + right)) == left + right
        assert read_resource_cell(render_cell(right + left)) == right + left


class TestKerningIsNotAssumed:
    """字距是浮动的，读法不许假定一个固定步进。"""

    @pytest.mark.parametrize("gap", [0, 1, 2])
    @pytest.mark.parametrize("text", ["11", "907.4K", "1.2M"])
    def test_the_same_text_reads_back_at_several_kernings(self, text: str, gap: int) -> None:
        assert read_resource_cell(render_cell(text, gap=gap)) == text

    def test_glyphs_touching_without_a_background_column_still_split(self) -> None:
        """⚠️ 相邻两个 `1` 之间**一列背景都没有**。

        按空列预先切段的写法会把它们并成一个字形，再当成某个 7 列宽的数字——
        其余各位全对、只是少了一位，这种错误进了库看不出来。DP 是逐列决定的，
        所以粘在一起照样分得开。
        """
        assert read_resource_cell(render_cell("11", gap=0)) == "11"
        assert read_resource_cell(render_cell("111", gap=0)) == "111"

    @pytest.mark.parametrize("left", [1, 6, 20])
    def test_the_band_can_sit_anywhere_across_the_cell(self, left: int) -> None:
        """字条在格子里左右挪动不影响读数——定位靠墨迹，不靠固定坐标。"""
        assert read_resource_cell(render_cell("593", left=left)) == "593"


class TestGhostTextIsSquashed:
    def test_a_cell_with_only_ghost_text_reads_as_empty(self) -> None:
        """⚠️ 只有底纹、没有数字 = **没读出来**，不是 0。

        底纹最高到 102，门槛在 150，中间隔着一个数量级。压不掉的话
        每一格都会"读"出一串垃圾。
        """
        cell = blank_cell()
        for y in range(2, CELL_HEIGHT, 4):
            for x in range(1, CELL_WIDTH, 3):
                cell[y][x] = GHOST_LUMINANCE

        assert read_resource_cell(cell) == ""

    def test_ghost_text_does_not_widen_the_ink_band(self) -> None:
        """铺满整格的底纹照样量出 9 高的行带——门槛决定了谁算墨迹。"""
        band = ink_band(render_cell("593", ghost=True))

        assert band is not None
        assert len(band) == GLYPH_HEIGHT


class TestFailingClosed:
    def test_a_cell_without_ink_reads_as_empty_not_as_zero(self) -> None:
        """⚠️ 没有墨迹**不是 0**。

        这一屏上值为 0 的格子照样画着一个 `0`；一点墨迹都没有说明格子挪了位，
        那时候补一个 0 是在编数据。
        """
        assert read_resource_cell(blank_cell()) == ""

    def test_one_blank_cell_voids_the_whole_grid(self) -> None:
        """⚠️ 读不全就**一行都不存**。

        库里「没有这一行 = 这一格是 0」，只有 12 格全读到时这条语义才成立。
        放松成「读到几格存几格」就会凭空造出零，而且不留痕迹。
        """
        holed = list(read_grid())
        holed[6] = read_resource_cell(blank_cell())

        assert holed[6] == ""
        assert len(holed) == GAINED_SLOT_COUNT
        assert parse_resource_grid(tuple(holed)) is None

    def test_a_taller_ink_band_is_refused(self) -> None:
        """⚠️ 行带高度不是 9 就整格作废。

        实拍 408 格全是 9，一格都没例外。高度对不上意味着版面动了
        （网格位移、面板滚了、ROI 吃进了隔壁的东西），这时候读出来的**任何**
        数字都不可信——拒收比交出一个像模像样的错数安全得多。
        """
        cell = render_cell("593")
        assert read_resource_cell(cell) == "593"

        cell[BAND_TOP - 2][BAND_LEFT] = 255

        assert ink_band(cell) is None
        assert read_resource_cell(cell) == ""

    def test_a_short_band_is_refused(self) -> None:
        """反方向同理：只有一个小数点时行带才 1 高，照样作废。"""
        assert ink_band(render_cell(".")) is None
        assert read_resource_cell(render_cell(".")) == ""

    def test_a_glyph_outside_the_table_falls_below_the_floor(self) -> None:
        """⚠️ 字模表里没有的字形必须**读不出来**，不许硬凑。

        眼下缺的是十亿后缀 `B`。真收到十亿量级的数字时，DP 会拿现有字模硬拼
        一串"合法"的数量出来——`MATCH_FLOOR` 就是拦这个的：似然明显塌下去，
        这一格作废，整份战报的收获宁可不记。
        """
        cell = blank_cell()
        for offset_y in range(GLYPH_HEIGHT):
            for offset_x in range(8):
                cell[BAND_TOP + offset_y][BAND_LEFT + offset_x] = 255

        band = ink_band(cell)
        assert band is not None
        _, likelihood = decode_band(band)

        assert likelihood < MATCH_FLOOR
        assert read_resource_cell(cell) == ""

    def test_a_known_glyph_clears_the_floor_by_a_wide_margin(self) -> None:
        """对照组：认识的字形似然贴着 1.0。

        ⚠️ **合成图上恒等于 1.0，因为它就是拿字模渲染的。** 所以这条只说明
        「门槛不会误伤自己」，说明不了门槛留了多少余量——实拍上最低 0.787，
        那个数只有 `test_resource_grid_corpus_live.py` 量得出来。
        """
        band = ink_band(render_cell("907.4K"))
        assert band is not None
        text, likelihood = decode_band(band)

        assert text == "907.4K"
        assert likelihood > MATCH_FLOOR
