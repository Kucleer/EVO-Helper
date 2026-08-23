"""军力榜采集工具：读一屏、合成入库清单。

每一条钉的都是「改坏了也不报错」的那种规矩——变异测试逐个验过。
实机依据来自 2026-08-14 的 `var/logs/rankv/21-panel.png`。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_ui import (
    NAME_COLUMN,
    RANK_COLUMN,
    RANKING_LIST_MAX_Y,
    ROW_CROP_HALF_HEIGHT,
    ROW_FIRST_Y,
    ROW_PITCH_PX,
    ROWS_PER_SCREEN,
    SCORE_COLUMN,
    SELF_ROW_BOTTOM_Y,
)
from evo_helper.tools.ranking_scan import (
    coordinates_of,
    is_self_row,
    keep_screens,
    name_column_text,
    parse_score,
    progress_mark,
    rows_from_image,
    take_batch_targets,
    targets_from_rows,
    track_progress,
)

NOW = datetime(2026, 8, 14, tzinfo=UTC)

#: 假 `image_to_data` 的表头。列序照 tesseract 的 TSV 原样排：`_words_with_boxes`
#: 只取 6–9（left/top/width/height）、10（conf）、11（text），但它对**列数少于 12**
#: 的行整行跳过，所以前面那六列必须占位齐全，不能只给用得上的那几列。
_TSV_HEADER = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
)

#: 假词框的尺寸（原图像素）与置信度。高度要小于 `ROW_CROP_HALF_HEIGHT × 2`，
#: 否则「裁剪窗口装得下一整行字」这个前提本身就不成立。置信度取高值：
#: `COLUMN_WORD_MIN_CONF` 那道闸不是这些用例要考的东西。
_WORD_HEIGHT_PX = 20
_WORD_WIDTH_PX = 40
_WORD_CONF = "96.00"

#: 裁剪中心偏离**实际**行中心超过这个数，`--psm 7` 就整格读不出来。
#:
#: ⚠️ 这是替身里最要紧的一笔，别为了让用例好过而放宽它。2026-08-23 语料实测的
#: 门槛约 13px：±`ROW_CROP_HALF_HEIGHT`(15) 的窗口一旦偏出这么多就把字切掉一截，
#: 那一格整格读不出。替身要是「不管裁在哪都照样把文字交出来」，那么
#: 「整屏漂了 21px」那几条用例在**旧的按网格裁剪**的实现上也会绿——护栏就成了摆设。
_CELL_READ_TOLERANCE_PX = 13.0


def _tsv(words: list[tuple[float, int, str]], *, top_offset: int, upscale: int = 3) -> str:
    """把 `(原图 y 中心, 列内 x 左沿, 文本)` 编回 tesseract 的 TSV。

    坐标要**反着**走 `_words_with_boxes` 的换算：TSV 里的数是放大过的裁剪块里的
    坐标，所以先减掉裁剪偏移再乘放大倍数。取整带来的误差不到 1/6 像素。
    """
    lines = [_TSV_HEADER]
    height = _WORD_HEIGHT_PX * upscale
    for y, x, text in words:
        top = round((y - top_offset) * upscale - height / 2)
        lines.append(
            "\t".join(
                [
                    "5",  # level：词一级
                    "1",  # page_num
                    "1",  # block_num
                    "1",  # par_num
                    "1",  # line_num
                    "1",  # word_num
                    str(x * upscale),
                    str(top),
                    str(_WORD_WIDTH_PX * upscale),
                    str(height),
                    _WORD_CONF,
                    text,
                ]
            )
        )
    return "\n".join(lines)


class _Cell:
    """假裁剪块。记下自己被 `convert` 成了什么模式——配方的一部分。

    `text` 是 `image_to_string` 拿到的东西，`tsv` 是 `image_to_data` 拿到的东西。
    """

    def __init__(self, text: str, log: list[str], *, tsv: str = "") -> None:
        self.text = text
        self.tsv = tsv
        self.width = 20
        self.height = 20
        self._log = log

    def convert(self, mode: str) -> _Cell:
        self._log.append(mode)
        return self

    def resize(self, _size: tuple[int, int], _resample: object) -> _Cell:
        return self


class _Image:
    """按 y 摆行的假面板。`rows` 是 {行号: (名次文字, 名字, 分数文字)}。

    `offset` 是**整屏行位置相对行网格的偏移**（像素）：第 k 行的真实中心是
    `ROW_FIRST_Y + k × ROW_PITCH_PX + offset`。它不是替身多给的一个自由度，
    是这一层要治的病本身——2026-08-23 语料里这个偏移每屏 -7.5px、以行距为模回绕。

    `off_grid` 是名字列上**不在行网格上**的字：`((y, 文本), ...)`。屏上真有两种
    ——固定的标题行（y≈235.7）和透过半透明面板的星球地表文字（y 落在两行之间）。
    它们跟着整列那一次 OCR 一起被读进来，所以替身必须交得出它们，否则「这两样
    到底被谁挡住的」根本没法验。它们**不随 `offset` 漂**：一个是固定 UI，
    一个是底下那一层画面。
    """

    def __init__(
        self,
        rows: dict[int, tuple[str, str, str]],
        *,
        offset: float = 0.0,
        off_grid: tuple[tuple[float, str], ...] = (),
    ) -> None:
        self.rows = rows
        self.offset = offset
        self.off_grid = off_grid
        self.modes: list[str] = []
        #: 逐格裁的窗口（名字、名次、军力）。
        self.crops: list[tuple[int, int, int, int]] = []
        #: 整条名字列裁的窗口。跟 `crops` 分开记：它比一行高得多，混在一起会把
        #: 「每一格都裁得比行距窄」那条断言变成永远为假。
        self.strips: list[tuple[int, int, int, int]] = []

    def centre_of(self, index: int) -> float:
        """第 `index` 行的**真实**中心 y（含整屏偏移）。"""
        return ROW_FIRST_Y + index * ROW_PITCH_PX + self.offset

    def _words(self, top: int, bottom: int) -> list[tuple[float, int, str]]:
        """落在 `[top, bottom]` 之间的名字列词框。

        坐标口径同 `_words_with_boxes` 交出来的那份：y 是原图的，x 是**列内**的
        左沿（那一步没有加回裁剪偏移，因为它只用来排左右顺序）。

        ⚠️ **名字读不出来的行照样交得出词框。** 行在那儿、整列 `--psm 6` 认得出
        有个词，而逐格 `--psm 7` 读出来是空的——这是实机常态，也正是
        `rows_from_image` 里 `if not name` 那一支要挡的东西。替身要是「名字为空
        就连位置都不给」，那一支就永远没人走过。
        """
        placed = [
            (self.centre_of(index), name or "~") for index, (_r, name, _s) in self.rows.items()
        ]
        words: list[tuple[float, int, str]] = []
        for centre, text in sorted(placed + [(y, t) for y, t in self.off_grid]):
            if not top <= centre <= bottom:
                continue
            words.extend(
                (centre, order * _WORD_WIDTH_PX, token) for order, token in enumerate(text.split())
            )
        return words

    def crop(self, box: tuple[int, int, int, int]) -> _Cell:
        left, top, _right, bottom = box
        if left == NAME_COLUMN[0] and bottom - top > ROW_PITCH_PX:
            # 整条名字列：`locate_rows` 从这里走 `image_to_data` 量行位置，
            # `name_column_text` 从这里走 `image_to_string` 只要文本。
            self.strips.append(box)
            words = self._words(top, bottom)
            text = "\n".join(token for _y, _x, token in words)
            return _Cell(text, self.modes, tsv=_tsv(words, top_offset=top))
        self.crops.append(box)
        centre = (top + bottom) / 2
        column = {RANK_COLUMN[0]: 0, NAME_COLUMN[0]: 1, SCORE_COLUMN[0]: 2}.get(left)
        index = round((centre - ROW_FIRST_Y - self.offset) / ROW_PITCH_PX)
        cells = self.rows.get(index)
        if cells is not None and abs(centre - self.centre_of(index)) <= _CELL_READ_TOLERANCE_PX:
            return _Cell("" if column is None else cells[column], self.modes)
        # 网格上没有行，但可能裁在了标题或透字上——只有名字列看得见它们，
        # 名次列和军力列那两个横向区间上没有这些字。
        for y, text in self.off_grid:
            if column == 1 and abs(centre - y) <= _CELL_READ_TOLERANCE_PX:
                return _Cell(text, self.modes)
        return _Cell("", self.modes)


class _Ocr:
    """假 OCR。**记下 config**——「整条列用 psm 6 而不是 psm 7」是配方的一部分。

    `image_to_data` 的 config 单独记在 `data_configs` 里，好让「单格一律 psm 7」
    那条断言仍旧只盯逐格那一路。
    """

    def __init__(self) -> None:
        self.configs: list[str] = []
        self.data_configs: list[str] = []

    def image_to_string(self, cell: _Cell, **kwargs: object) -> str:
        self.configs.append(str(kwargs.get("config", "")))
        return cell.text

    def image_to_data(self, cell: _Cell, **kwargs: object) -> str:
        self.data_configs.append(str(kwargs.get("config", "")))
        return cell.tsv


def _read(
    rows: dict[int, tuple[str, str, str]],
    *,
    offset: float = 0.0,
    off_grid: tuple[tuple[float, str], ...] = (),
) -> tuple[list[RankingRow], _Image]:
    image = _Image(rows, offset=offset, off_grid=off_grid)
    return rows_from_image(image, _Ocr()), image


# -- 哪一行算数 ----------------------------------------------------------------


def test_the_top_three_have_medals_instead_of_rank_numbers() -> None:
    """⚠️ **实机第一屏就打脸的那条。**

    `var/logs/rankv/21-panel.png`：榜首前三名（unkn0wn / XXxxNAZIMxxXX / halo）
    显示的是**奖章图标**，名次列一个字都读不出来。名次是从 `[4]` 才开始的。

    所以「名次读不出就丢掉整行」会把**最强的三个**直接扔了。名次是校验和
    （`repair_ranks` 能从邻居补回来），名字才是这一层唯一的产物。
    """
    rows, _image = _read(
        {
            0: ("", "unkn0wn", "404.17M"),  # 奖章，没有名次
            1: ("", "XXxxNAZIMxxXX", "160.12M"),
            2: ("", "halo", "115.9M"),
            3: ("[4]", "Cocyte", "93.29M"),
        }
    )

    assert [row.name for row in rows] == ["unkn0wn", "XXxxNAZIMxxXX", "halo", "Cocyte"]
    assert [row.rank for row in rows] == [None, None, None, 4]


def test_a_row_whose_name_is_unreadable_is_dropped_without_a_placeholder() -> None:
    """名字读不出来才丢，而且不留占位——上层拿「读到 0 行」当「已经不在榜单页上」。"""
    rows, _image = _read({0: ("[1]", "", "404.17M"), 1: ("[2]", "halo", "115.9M")})

    assert [row.name for row in rows] == ["halo"]


def test_the_bottom_pinned_self_row_is_outside_the_read_window() -> None:
    """自己那一行**贴底**那一档，靠 `RANKING_LIST_MAX_Y` 就挡住了。"""
    self_index = round((SELF_ROW_BOTTOM_Y - ROW_FIRST_Y) / ROW_PITCH_PX)
    rows, image = _read({0: ("[1]", "halo", "115.9M"), self_index: ("[34]", "Kucleer", "13.12M")})

    assert [row.name for row in rows] == ["halo"]
    assert all(bottom <= SELF_ROW_BOTTOM_Y for _l, _t, _r, bottom in image.crops)


def test_the_self_row_sticks_to_the_top_once_you_scroll_past_yourself() -> None:
    """⚠️⚠️ **这条推翻了「自己那一行钉在 y=837」。**

    2026-08-15 实机：滚过自己名次之后，`[44] Kucleer` **跳到了列表最上面**
    （y≈254，也就是 `ROW_FIRST_Y`）。而那正是「首行变没变」这条到底判据看的地方
    ——于是每滚一屏首行都读成自己，判据被骗成「一直没动」，我因此误判过
    「榜单滚不动」（其实一直在滚，55 滚推进了 600 多名）。

    所以剔除必须**按名字**：按 y 排不掉它，它换个位置继续混进来。
    """
    screen = {0: ("[44]", "Kucleer", "1.56M"), 1: ("[237]", "bot_4_155_13", "7.55K")}

    without_name = rows_from_image(_Image(screen), _Ocr())
    with_name = rows_from_image(_Image(screen), _Ocr(), player_name="Kucleer")

    assert [row.name for row in without_name] == ["Kucleer", "bot_4_155_13"]
    assert [row.name for row in with_name] == ["bot_4_155_13"]


def test_the_self_row_is_matched_through_the_ocr_noise_glued_to_it() -> None:
    """实机读到过 `', Kucleer'`、`'| Kucleer'`、`': Kucleer'`——名字那一格前面
    常粘上一点噪声。用相等去比就漏了，所以用**包含**、且忽略大小写。
    """
    assert is_self_row(", Kucleer", "Kucleer")
    assert is_self_row("| kucleer", "Kucleer")
    assert not is_self_row("bot_4_155_13", "Kucleer")
    assert not is_self_row("Kucleer", "")  # 没配名字就不剔，宁可多读不要错剔


# -- 行位置是量出来的，不是按网格算的 ------------------------------------------


def _full_screen() -> dict[int, tuple[str, str, str]]:
    """满满一屏 bot，军力严格降序（免得撞上 `descending_breaks` 那道安全网）。"""
    return {
        index: (f"[{600 + index}]", f"bot_4_{30 + index}_12", f"{29.5 - index / 10:.2f}K")
        for index in range(ROWS_PER_SCREEN)
    }


@pytest.mark.parametrize("offset", [0.0, 8.0, 21.0])
def test_a_screen_that_drifted_off_the_row_grid_is_still_read_in_full(offset: float) -> None:
    """⚠️⚠️ **21px 那一档是这次改动的护栏：旧实现在它上面整屏归零。**

    2026-08-23 的 15 屏语料：bot 名字相对行网格的中位偏移逐屏走
    `-6.1 → -13.7 → -21.1 → +16.1 → +8.6 → +1.1 → -6.4 → …`——每屏 -7.5px、
    以行距 44.8px 为模回绕、周期 6 屏。成因是一次慢拖推进的不是整数行
    （实测约 8.17 行），那 0.17 行 ≈ 7.5px 的零头逐屏累积。

    偏移一旦超过约 13px，按 `ROW_FIRST_Y + k × ROW_PITCH_PX` 算出来的裁剪窗口
    就把字切掉一截，`--psm 7` **整屏读不出**——那一屏的 bot 全部静默丢弃。
    实测每 6 屏里约 3 屏被整屏丢掉，语料 15 屏里 7 屏归零；生产日志上
    「12, 8, 6 → 0, 0, 0」那个周期 6 的形状就是它。

    三档一起钉：0px 是不漂时的底线，8px 是「还勉强读得出但已经开始把邻行数字
    碎片裁进来」的那一档，21px 是**整屏失效**的那一档。三档都必须读满 13 行。
    """
    rows, image = _read(_full_screen(), offset=offset)

    assert len(rows) == ROWS_PER_SCREEN, "有行被整屏丢掉了"
    assert [row.name for row in rows] == [
        f"bot_4_{30 + index}_12" for index in range(ROWS_PER_SCREEN)
    ]
    assert [row.rank for row in rows] == [600 + index for index in range(ROWS_PER_SCREEN)]
    assert all(row.score is not None for row in rows)

    # 裁剪窗口必须**跟着漂**，也就是中心来自实测而不是网格。差 1px 以内是
    # 裁剪边界取整的零头。
    wanted = [image.centre_of(index) for index in range(ROWS_PER_SCREEN)]
    read_at = [(top + bottom) / 2 for _l, top, _r, bottom in image.crops]
    assert all(min(abs(c - w) for w in wanted) <= 1.0 for c in read_at), "裁剪窗口没跟着漂"

    # 搜索窗口上下各放宽一个行距，量行位置只量这一次。21px 那一档全靠这个放宽
    # 才够得着最后一行（815.6 已经越过 `RANKING_LIST_MAX_Y`）。
    ((_left, strip_top, _right, strip_bottom),) = image.strips
    assert strip_top <= ROW_FIRST_Y - ROW_PITCH_PX
    assert strip_bottom >= RANKING_LIST_MAX_Y + ROW_PITCH_PX


#: 标题那一行的实测 y（2026-08-23 语料）。它是**固定的 UI 元素**，不随列表相位漂，
#: 在相位 0 上离第一条列表行（`ROW_FIRST_Y` 257）只有 21.3px。
_TITLE_Y = 235.7
_TITLE = ((_TITLE_Y, "PLAYER"),)


@pytest.mark.parametrize("offset", [0.0, -6.1, 8.6])
def test_the_title_row_is_dropped_without_taking_the_first_list_row_with_it(
    offset: float,
) -> None:
    """⚠️⚠️ **这条钉的是「按跨度成组」那次改动，相位 0 上旧的单链聚类会红。**

    `locate_rows` 上下各放宽一个行距（为了容纳漂到网格上方约 22px 的行），
    代价是窗口顶上多出标题那一行（实测 y≈235.7，固定不漂）。它不在行网格上，
    该由 `_row_bands` 的网格一致性判据剔掉。

    问题出在**聚类**这一步：标题离第一条列表行只有 21.3px（相位 0），而原先的
    单链聚类阈值是半个行距 22.4px——于是两者并成一条带，中心被拽偏约 10px，
    这条带的相位跟着变得不一致，网格判据把它整条剔掉，**第一条列表行连带没了**。
    更根子的说法：标题到最近那条列表行的距离恒等于相位差的回绕值，按定义
    不超过半个行距，所以「标题单独成带」和「标题被网格判据剔掉」在单链下互斥。

    按跨度成组（`y − 这一组的第一个 <= ROW_CROP_HALF_HEIGHT`，15px）就解开了：
    同一行的词共享基线、y 散布只有 1–2px，而标题与列表行差 21.3px。

    三档相位都是语料实测的（相位每屏 -7.5px、以行距为模回绕）：

        相位  0.0   标题离第一行 21.3px < 22.4  单链下并带、整条被网格判据剔掉
                                                → **第一条列表行整个丢掉**，这条
                                                   用例是它的护栏（实测会红）
        相位 -6.1   标题离第一行 15.2px          单链下也并带，但并出来的相位偏差
                                                   只有 7.6px、没超容差，所以带留着、
                                                   中心被拽偏 7.6px。替身宽容到 13px
                                                   看不出来（实机上那一格会把标题的字
                                                   一起裁进来），这一档只是陪跑
        相位  8.6   标题离第一行 29.9px          两种聚类都好，留着防「修过头把好
                                                   相位改坏」
    """
    rows, _image = _read(_full_screen(), offset=offset, off_grid=_TITLE)

    assert [row.name for row in rows] == [
        f"bot_4_{30 + index}_12" for index in range(ROWS_PER_SCREEN)
    ], "第一条列表行不许跟着标题一起没"
    assert "PLAYER" not in [row.name for row in rows]
    assert all(row.coordinate is not None for row in rows), "标题并进来会把名字拼坏"


def test_a_title_row_that_slips_through_never_becomes_a_target() -> None:
    """⚠️ **网格判据并不是每个相位都剔得掉标题——漏进来的那一格必须无害。**

    +16.1px 那个相位上（语料实测的第四屏），标题相对列表行的偏差回绕成 7.4px，
    落在 `ROW_GRID_TOLERANCE_PX`(8) 之内，于是它作为**第 14 条带**留了下来。
    `locate_rows` 的注释认了这笔代价：白裁一格。

    这条钉的是「代价到此为止」：那一格读出来的是标题的字，`coordinate_of`
    反解不出坐标，于是 `targets_from_rows` 一条都不会为它落库。真出问题的是
    **反过来**——要是哪天标题的字碰巧反解得出坐标，舰队就会飞过去。
    """
    rows, _image = _read(_full_screen(), offset=16.1, off_grid=_TITLE)

    assert "PLAYER" in [row.name for row in rows], "这个相位上标题确实漏进来了"
    assert [row.coordinate for row in rows].count(None) == 1

    targets = targets_from_rows(rows, observed_at=NOW)

    assert all(target.coordinate is not None for target in targets)
    assert len(targets) == len(rows) - 1


def test_text_bleeding_through_between_two_rows_never_becomes_a_row() -> None:
    """⚠️ **行间透字要挡掉——整列读那一次会把它一起读进来。**

    实机词框：星球地表的 `COMMAND OFFICERS` / `-17003` 透过半透明面板落在
    x 769–949（正压在名字列上），y 在 500 和 548，而真实行在 525。

    挡它的是**两道**判据叠在一起，都不是名字归行那种事后拼接：

    1. `_row_bands` 的网格一致性——透字不在行网格上（500 偏 19.8px、
       548 偏 21.8px，都超 `ROW_GRID_TOLERANCE_PX`），成不了一条带，
       于是根本没有哪一格会裁在它身上；
    2. 逐格裁剪只有 ±`ROW_CROP_HALF_HEIGHT`(15) 而不是半个行距(22.4)——
       真实行那一格裁 510–540，把 490–510 的透字关在外面。

    挡不住的后果是名字被拼成 `COMMAND OFFICERS bot_4_100_13` 之类，
    `coordinate_of` 反解不出坐标，这一行的舰队就没处可去。
    """
    # 480.2 / 525.0 / 569.8 就是语料里那三行（相位 -0.8，行号 5/6/7）。
    screen = {
        5: ("[605]", "bot_4_30_12", "29.50K"),
        6: ("[606]", "bot_4_100_13", "29.40K"),
        7: ("[607]", "bot_4_183_20", "29.30K"),
    }
    bleed = ((500.0, "COMMAND OFFICERS"), (548.0, "-17003"))

    rows, image = _read(screen, offset=-0.8, off_grid=bleed)

    assert [row.name for row in rows] == ["bot_4_30_12", "bot_4_100_13", "bot_4_183_20"]
    # 一格都没裁在透字上——第一道判据就把它挡在成带之前了。
    assert all(
        all(abs((top + bottom) / 2 - y) > ROW_CROP_HALF_HEIGHT for y, _text in bleed)
        for _l, top, _r, bottom in image.crops
    ), "有一格裁在了透字上"


# -- 配方（实机换来的那一条） --------------------------------------------------


def test_the_cells_are_greyscale_and_never_binarised() -> None:
    """⚠️ **不要二值化。** 用户实机口径：「这里的背景极易干扰」。

    面板是半透明的，星球地表透过来；二值化之后背景和文字一起变白，更糟。
    这条是这一层唯一一个靠实机试出来的参数，改成 `"1"` 不会报错、只会读不准。
    """
    _rows, image = _read({0: ("[1]", "halo", "115.9M")})

    assert set(image.modes) == {"L"}


def test_each_row_is_cropped_tighter_than_the_row_pitch() -> None:
    """⚠️ **背景文字落在两行之间。**

    实机词框：真实行在 y=525，而背景的 `COMMAND OFFICERS` 在 500、`-17003` 在 548，
    横向 769–949 正压在名字列上。按 `ROW_PITCH_PX / 2` = 22.4 裁会把两侧各吃进一点。
    """
    _rows, image = _read({0: ("[1]", "halo", "115.9M")})
    heights = {bottom - top for _l, top, _r, bottom in image.crops}

    assert heights, "一格都没裁"
    assert max(heights) < ROW_PITCH_PX


# -- 分数 ----------------------------------------------------------------------


def test_parse_score_reads_the_suffixes_and_refuses_junk() -> None:
    assert parse_score("29.59K") == 29_590.0
    assert parse_score("404.17M") == 404_170_000.0
    assert parse_score("not a score") is None


def test_the_k_suffix_lands_exactly_on_the_listed_value() -> None:
    """⚠️ **恰好相等，不许用 `pytest.approx`。** 近似断言等于没修。

    这三个是 2026-08-17 军力榜页面上**原样**出现的脏值。榜上的原文是
    `64.96K` / `64.26K` / `64.18K`，而 `float("64.96") * 1000` 给出
    64959.99999999999——`64.96` 在二进制里没有精确表示。三个都不是随机偏差，
    是同一个成因，所以三个一起钉。

    换算走 `Decimal` 之后按十进制乘，误差根本不产生。
    """
    assert parse_score("64.96K") == 64_960
    assert parse_score("64.26K") == 64_260
    assert parse_score("64.18K") == 64_180


def test_the_m_suffix_is_exact_too_and_a_bare_decimal_survives_intact() -> None:
    """M 量级同病同治；而**没有单位的小数不许被取整抹平**。

    ⚠️ 后半句是这条测试真正的用意。「乘完 `round()` 一下」也能让 K 值变干净，
    但这条正则同时认裸数（`([KM])?` 是可选的），取整那一支就会把 `1.5` 变成 `2`。
    `Decimal` 对三种单位一视同仁地精确，不靠「最小刻度是 10」这个前提兜底。
    """
    assert parse_score("404.17M") == 404_170_000
    assert parse_score("115.9M") == 115_900_000
    assert parse_score("1.5") == 1.5
    assert parse_score("0") == 0.0


def test_an_unreadable_score_stays_none_and_never_becomes_zero() -> None:
    """⚠️ **猜出来的数不许长得像量出来的。** 0 分在这个榜上是有含义的
    （经济榜上的 bot 就是 0），把「读不出来」写成 0 就是在造一条假数据。
    """
    rows, _image = _read({0: ("[1]", "bot_4_30_12", "???")})

    assert rows[0].score is None
    targets = targets_from_rows(rows, observed_at=NOW)
    assert targets[0].military_score is None
    assert targets[0].military_score_estimated is False  # 没插出来就不是估算


def test_an_interpolated_score_is_marked_estimated() -> None:
    targets = targets_from_rows(
        [
            RankingRow(639, "bot_4_30_12", 30.0, Coordinate(4, 30, 12)),
            RankingRow(640, "bot_4_100_13", None, Coordinate(4, 100, 13)),
            RankingRow(641, "bot_4_183_20", 20.0, Coordinate(4, 183, 20)),
        ],
        observed_at=NOW,
    )

    assert [(t.military_score, t.military_score_estimated) for t in targets] == [
        (30.0, False),
        (25.0, True),
        (20.0, False),
    ]


def test_only_rows_that_resolve_to_a_coordinate_are_stored() -> None:
    """真人不进星球列表——判据是名字反解得出坐标，不是名次。"""
    targets = targets_from_rows(
        [
            RankingRow(638, "GoudanLi", 12.0, None),
            RankingRow(639, "bot_4_30_12", 11.0, Coordinate(4, 30, 12)),
        ],
        observed_at=NOW,
    )

    assert [t.coordinate for t in targets] == [Coordinate(4, 30, 12)]


def test_fixed_pirate_positions_are_not_written_as_ranking_targets() -> None:
    targets = targets_from_rows(
        [
            RankingRow(639, "bot_2_137_1", 12.0, Coordinate(2, 137, 1)),
            RankingRow(640, "bot_2_137_5", 11.0, Coordinate(2, 137, 5)),
        ],
        observed_at=NOW,
    )

    assert [target.coordinate for target in targets] == [Coordinate(2, 137, 5)]


def test_batch_limit_counts_unique_bots_and_stops_exactly_at_the_limit() -> None:
    first = _target("first")
    second = RankingTarget(
        coordinate=Coordinate(4, 31, 12), military_score=1.0, military_score_at_utc=NOW
    )
    third = RankingTarget(
        coordinate=Coordinate(4, 32, 12), military_score=1.0, military_score_at_utc=NOW
    )
    seen: set[Coordinate] = set()

    picked = take_batch_targets([first, first, second, third], seen=seen, limit=2)

    assert [target.coordinate for target in picked] == [
        Coordinate(4, 30, 12),
        Coordinate(4, 31, 12),
    ]
    assert seen == {Coordinate(4, 30, 12), Coordinate(4, 31, 12)}


# -- 断线 ----------------------------------------------------------------------


def _target(name: str) -> RankingTarget:
    del name
    return RankingTarget(
        coordinate=Coordinate(4, 30, 12), military_score=1.0, military_score_at_utc=NOW
    )


def test_a_disconnect_keeps_everything_except_the_last_screen() -> None:
    """⚠️ **离页不等于这一趟白跑。**

    原先 `return 2` 排在 `save_ranking_targets` 前面，断线就把整趟扔了——而断线是
    **预期结果**（实机滚到第 473 名就断过）。照那个写法实机上一条都存不下来。

    只丢最后一屏：它是画面已经变了之后读的。之前那些和正常到底的一样可信。
    """
    screens = [[_target("a")], [_target("b")], [_target("c")]]

    assert len(keep_screens(screens, off_page=True)) == 2
    assert len(keep_screens(screens, off_page=False)) == 3


def test_a_disconnect_before_the_first_screen_keeps_nothing() -> None:
    """一屏都没采到就断了，不该崩在「丢掉最后一屏」上。"""
    assert keep_screens([], off_page=True) == []


# -- 滚到底了没有 --------------------------------------------------------------


def test_progress_is_measured_by_the_progress_mark_not_by_string_equality() -> None:
    """⚠️ **「两屏 OCR 完全相等」这条实机上一次都不会触发。**

    榜单上大量是中文玩家名（`探险12`、`资源32`），而名字列跑的是 `eng`——
    同一行连读两次就是两个不同的噪声串。2026-08-15 实机滚了 55 次，
    `scroll_once` 的 `EXHAUSTED` 一次都没触发。

    名次是数字，拿「最大名次有没有往前走」当进度指针才结实。
    """
    noisy_a = [RankingRow(237, "=- ,, _ -", None, None), RankingRow(249, "??", None, None)]
    noisy_b = [RankingRow(237, "= -. _ ~", None, None), RankingRow(249, "?7", None, None)]

    assert list(noisy_a) != list(noisy_b)  # 字符串比：看着「变了」
    assert progress_mark(noisy_a) == progress_mark(noisy_b) == 249  # 名次比：没往前走


def test_one_wild_rank_misread_must_not_freeze_the_progress_marker() -> None:
    """⚠️⚠️ **这条是我自己造的事故的墓碑。**

    进度指针先写的是 `max()`。实机 2026-08-15：名次列串出 `[401]`（那一屏真实
    名次只到 20 左右），`max` 被顶到 401，此后真实推进永远超不过它，
    **113 秒就判成「到底了」收工**——正是这条判据本该防住的那种事故。

    取中位数：一屏十二行里错一两个，中间那个不动。
    """
    real = [RankingRow(rank, "x", None, None) for rank in range(14, 26)]
    with_noise = [*real[:-1], RankingRow(401, "串了", None, None)]

    assert max(r.rank or 0 for r in with_noise) == 401  # max 被顶飞
    assert progress_mark(with_noise) == progress_mark(real)  # 中位数纹丝不动


def test_a_screen_with_no_readable_rank_reports_no_progress() -> None:
    """一个名次都读不出来时返回 0——那不构成「又往前了」，只会累计停滞次数。"""
    assert progress_mark([RankingRow(None, "noise", None, None)]) == 0
    assert progress_mark([]) == 0


def test_progress_is_compared_across_a_window_not_against_the_previous_screen() -> None:
    """跨窗口比而不是逐屏比：三屏的信号约 24 名，而指针噪声不变。

    下面这串指针每屏都在抖（+8 / −3 / +9 / −2），逐屏比会判成停滞两次，
    跨窗口比一次都不判。

    ⚠️ **但这条判据整体仍不可靠**，实机连着假阳性四次（见 `track_progress`
    的注释）。调用方必须另外带预算兜底，别拿它当收工的唯一依据。
    """
    window: tuple[int, ...] = ()
    verdicts = []
    for mark in (100, 108, 105, 114, 112, 121):
        window, done = track_progress(window, mark)
        verdicts.append(done)

    assert verdicts == [False] * 6


def test_a_board_that_really_stopped_moving_is_called() -> None:
    """真到底了：指针不再往前，攒够一个窗口就收工。"""
    window: tuple[int, ...] = ()
    for mark in (700, 700, 700):
        window, done = track_progress(window, mark)
        assert not done, "窗口还没攒满就不许判"

    _window, done = track_progress(window, 700)

    assert done


def test_a_half_scrolled_board_is_never_called_finished_early() -> None:
    """⚠️ 窗口没攒满一律不判——这条挡住「刚开榜就说读完了」。"""
    window: tuple[int, ...] = ()
    for mark in (10, 10):
        window, done = track_progress(window, mark)
        assert not done


def test_the_whole_name_column_is_read_as_multiple_lines() -> None:
    """⚠️ **整条名字列要用 `--psm 6`（多行），不是 `--psm 7`（单行）。**

    翻真人段时靠一次整列 OCR 回答「到 bot 区了没有」。用单行模式的话，
    十三行里只读得出一行——bot 可能就在没读到的那十二行里，于是一路翻到
    预算耗尽也「没见到 bot」。

    单格细读仍然是 `--psm 7`：那才是真的单行。
    """
    ocr = _Ocr()

    name_column_text(_Image({0: ("[1]", "bot_4_30_12", "29.59K")}), ocr)

    assert ocr.configs == ["--psm 6"]


def test_single_cells_are_still_read_as_one_line() -> None:
    ocr = _Ocr()

    rows_from_image(_Image({0: ("[1]", "halo", "115.9M")}), ocr)

    assert set(ocr.configs) == {"--psm 7"}


def test_the_measuring_pass_reads_the_name_column_as_multiple_lines_too() -> None:
    """量行位置那一次也走 `--psm 6`（多行），而且只走一次。

    用 `--psm 7` 的话，`image_to_data` 只吐得出一行的词框——聚出来就一条带，
    一屏十三行里十二行连带位置都没有，全部静默丢弃。这跟
    `test_the_whole_name_column_is_read_as_multiple_lines` 是同一颗雷的另一个入口。
    """
    ocr = _Ocr()

    rows_from_image(_Image(_full_screen()), ocr)

    assert ocr.data_configs == ["--psm 6"]


# -- 降序异常必须丢，不能只打印 ------------------------------------------------


def test_a_score_that_breaks_the_descending_order_is_dropped_not_stored() -> None:
    """⚠️⚠️ **2026-08-15 的实账：只打印不丢，18 个错值进了库。**

    库里 30 个 bot 的军力值飞到 10 万以上（最高 177 万），而每一个除以 100 都
    精确落回正常区间（P95 是 19,730）——`17.73K` 读成 `1773K`，**丢小数点**，
    不是随机偏差，是整整齐齐的两个数量级。

    榜单按军力降序排，所以「比上一行大」一眼就认得出来，`descending_breaks`
    当时也确实在报——可代码只 `print` 了一行就往下走。

    丢的是**分数不是行**：坐标仍然是好的（那 30 个里有 2 个是坐标扫描验证过的）。
    """
    rows = [
        RankingRow(639, "bot_4_30_12", 17_730.0, Coordinate(4, 30, 12)),
        RankingRow(640, "bot_4_100_13", 1_773_000.0, Coordinate(4, 100, 13)),  # 丢了小数点
        RankingRow(641, "bot_4_183_20", 17_000.0, Coordinate(4, 183, 20)),
    ]

    targets = targets_from_rows(rows, observed_at=NOW)

    assert [t.coordinate for t in targets] == [c.coordinate for c in targets], "行不许丢"
    assert len(targets) == 3
    assert targets[1].military_score != 1_773_000.0, "破坏降序的读数不许原样入库"


def test_a_dropped_score_is_refilled_from_its_neighbours_and_marked_estimated() -> None:
    """丢完之后走插值——用上下两个好邻居补一个中点，并**标成估算**。

    ⚠️ 标记必须看「丢完之后」那份，不是「读到的」那份：看后者的话，
    被判据丢掉的行会伪装成实读，而它恰恰是最不可信的一条。
    """
    rows = [
        RankingRow(639, "bot_4_30_12", 20_000.0, Coordinate(4, 30, 12)),
        RankingRow(640, "bot_4_100_13", 999_999.0, Coordinate(4, 100, 13)),  # 错读
        RankingRow(641, "bot_4_183_20", 10_000.0, Coordinate(4, 183, 20)),
    ]

    targets = targets_from_rows(rows, observed_at=NOW)

    assert targets[1].military_score == 15_000.0  # 20000 与 10000 的中点
    assert targets[1].military_score_estimated is True
    assert targets[0].military_score_estimated is False


def test_a_well_behaved_descending_screen_keeps_every_score() -> None:
    """判据只挡爬升。正常的降序一屏一个都不许动。"""
    rows = [
        RankingRow(639, "bot_4_30_12", 29_590.0, Coordinate(4, 30, 12)),
        RankingRow(640, "bot_4_100_13", 28_730.0, Coordinate(4, 100, 13)),
        RankingRow(641, "bot_4_183_20", 28_510.0, Coordinate(4, 183, 20)),
    ]

    targets = targets_from_rows(rows, observed_at=NOW)

    assert [t.military_score for t in targets] == [29_590.0, 28_730.0, 28_510.0]
    assert not any(t.military_score_estimated for t in targets)


def test_the_rank_is_carried_into_storage() -> None:
    """⚠️ **名次是免费的校验和，存下来才复核得了。**

    2026-08-15 那批错值查不下去，正因为名次没进库——事后没法再拿降序验一遍。
    修好的名次（`repair_ranks` 从邻居补出来的那份）就是要存的那份。
    """
    rows = [
        RankingRow(639, "bot_4_30_12", 29_590.0, Coordinate(4, 30, 12)),
        RankingRow(None, "bot_4_100_13", 28_730.0, Coordinate(4, 100, 13)),  # 名次读不出
        RankingRow(641, "bot_4_183_20", 28_510.0, Coordinate(4, 183, 20)),
    ]

    targets = targets_from_rows(rows, observed_at=NOW)

    assert [t.military_rank for t in targets] == [639, 640, 641]


# -- 这一屏读出了哪些坐标：给重叠自查当尺子 ------------------------------------


def test_the_ruler_is_the_coordinate_column_not_the_rank_column() -> None:
    """⚠️⚠️ **重叠自查量的是坐标，不是名次。**

    2026-08-23 生产（run `91c7f9ec`）那 12 条「漏掉 N 名」全是名次列的高位噪声
    造出来的（整段账在 `domain.ranking.screens_overlap`）。所以这把尺子刻意不看
    `row.rank`：这一屏三行名次分别是奖章（读不出）、串成 `4781`、正常，
    而交出来的坐标一个不少、也一个不多。
    """
    rows = [
        RankingRow(None, "bot_4_30_12", 29_590.0, Coordinate(4, 30, 12)),
        RankingRow(4781, "bot_4_100_13", 28_730.0, Coordinate(4, 100, 13)),
        RankingRow(851, "bot_4_183_20", 28_510.0, Coordinate(4, 183, 20)),
    ]

    assert coordinates_of(rows) == {
        Coordinate(4, 30, 12),
        Coordinate(4, 100, 13),
        Coordinate(4, 183, 20),
    }


def test_rows_whose_name_yields_no_coordinate_stay_out_of_the_ruler() -> None:
    """名字读错的行（区间越界、真人名、榜首那几个）解不出坐标，就不参与比较。

    ⚠️ **不参与 ≠ 造一个假数。** 原先那道名次判据拿一个读错的数去减，减出来的
    就是「漏掉 996 名」；这里读不出的行只是不在集合里，最坏的结果是这一屏的
    尺子短一点。
    """
    rows = [
        RankingRow(None, "unkn0wn", 404_170_000.0, None),
        RankingRow(850, "bot_4_30_12", 29_590.0, Coordinate(4, 30, 12)),
    ]

    assert coordinates_of(rows) == {Coordinate(4, 30, 12)}


def test_a_screen_with_no_coordinate_at_all_answers_an_empty_ruler() -> None:
    """整屏读不出来（离页、面板没铺开）时 `read_rows()` 交的就是空列表。

    这一路是**常态**而不是异常：`rows_from_image` 只要名字读不出就丢行，
    一屏全丢就是空的。空集会让 `screens_overlap` 答「不知道」，
    而不是「重叠断了」——那两件事在日志上必须分得开。
    """
    assert coordinates_of([]) == set()
    assert coordinates_of([RankingRow(850, "unkn0wn", None, None)]) == set()
