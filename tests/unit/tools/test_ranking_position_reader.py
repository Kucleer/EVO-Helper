"""盲滚的位置读数器：**整列一次读完**，与行对齐无关。

⚠️ **它存在的理由是一次实机失效。** 2026-08-22：闭环盲滚原先拿 `rows_from_image`
（按 `ROW_FIRST_Y + k×ROW_PITCH_PX` 逐行裁剪）读「现在在第几名」，而滚轮会把列表
停在**非整行位置**（实测偏离网格约 12px）——每一格裁出来都横跨两行，名次全糊。
请求 500 行那一趟，第一轮拨完当场「读不出名次」，闭环失效。

所以这一份钉的全是「不按行切」这件事本身：整列一次 OCR、正则抓 `[N]`、取中位数。
榜首三名是奖章图标、没有 `[N]`，所以门槛不能高（`SPIN_MARK_MIN_ROWS` = 4）。
"""

from __future__ import annotations

from evo_helper.game.ranking_ui import (
    RANK_COLUMN,
    RANK_STRIP_PAD_PX,
    RANK_STRIP_TOP_PAD_PX,
    ROW_FIRST_Y,
    ROW_PITCH_PX,
    ROWS_PER_SCREEN,
    SPIN_MARK_MIN_ROWS,
)
from evo_helper.tools.ranking_scan import RankingColumns, position_from_image


class _Strip:
    """假裁剪块。记下 `convert` 的模式——配方的一部分。"""

    def __init__(self, text: str, log: list[str]) -> None:
        self.text = text
        self.width = 80
        self.height = 600
        self._log = log

    def convert(self, mode: str) -> _Strip:
        self._log.append(mode)
        return self

    def resize(self, size: tuple[int, int], _resample: object) -> _Strip:
        self._log.append(f"resize{size}")
        return self


class _Image:
    """一张只认得「整列一次裁」的假面板：`crop` 交出的永远是同一段文本。

    ⚠️ **它不按行摆内容，这是有意的**：读数器要是又按行切，就会裁出 13 个小块
    分别去 OCR，而这里数得出来——`crops` 只该有一个框。
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.modes: list[str] = []
        self.crops: list[tuple[int, int, int, int]] = []

    def crop(self, box: tuple[int, int, int, int]) -> _Strip:
        self.crops.append(box)
        return _Strip(self.text, self.modes)


class _Ocr:
    """假 OCR。**记下 config 和 lang**——整条列要 `--psm 6` 而不是单行的 `--psm 7`。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def image_to_string(self, strip: _Strip, **kwargs: object) -> str:
        self.calls.append((str(kwargs.get("lang", "")), str(kwargs.get("config", ""))))
        return strip.text


def _read(text: str, columns: RankingColumns | None = None) -> tuple[int | None, _Image, _Ocr]:
    image, ocr = _Image(text), _Ocr()
    return position_from_image(image, ocr, columns), image, ocr


# -- 读出来的是什么 ------------------------------------------------------------


def test_a_whole_column_read_gives_the_median_rank() -> None:
    """整列一次读到五个名次，答的是中位数。"""
    mark, _image, _ocr = _read("[701]\n[702]\n[703]\n[704]\n[705]")

    assert mark == 703


def test_the_medal_rows_carry_no_rank_and_do_not_break_the_reading() -> None:
    """⚠️ **榜首三名是奖章图标，名次那一格一个字都读不出来**（实机
    `var/logs/rankv/21-panel.png`）。它们在整列读数里就是几行没有 `[N]` 的文本——
    不该被算成样本，也不该把这一次读数判死。

    这也正是 `SPIN_MARK_MIN_ROWS` 不许调高的原因：满屏 13 行里先扣掉这三行。
    """
    mark, _image, _ocr = _read("\n\n\n[4]\n[5]\n[6]\n[7]\n[8]")

    assert mark == 6


def test_a_reading_below_the_threshold_is_unknown_not_a_guess() -> None:
    """只抓到两个 `[N]` 时，「中位数」就是其中一个——而那两个都可能是串出来的
    高位噪声（实机当场读到过 `[4781]`，那一屏真实名次只到 20）。

    ⚠️ 交 `None` 而不是猜一个：上一层拿 `None` 退回开环，拿一个假名次则会**算错
    还差多少行**，而算错是静默的。
    """
    mark, _image, _ocr = _read("[701]\n[702]")

    assert SPIN_MARK_MIN_ROWS > 2, "门槛掉到 2 以下，这条用例就没在验东西了"
    assert mark is None


def test_bare_numbers_without_brackets_are_not_ranks() -> None:
    """⚠️ 正则要**成对的方括号**，不是裸数字。

    整列一次读会把边上的东西一起吃进来（放宽的列边界扫到一点分数、背景里的
    `TOTAL CREWS -17003`）。那些都不带方括号，正则本来就不该收——
    松成 `\\d+` 的话，中位数会被这些数字整个带偏，而带偏是静默的。
    """
    mark, _image, _ocr = _read("404 17M\n-17003\n[701]\n[702]\n[703]\n[704]\n69.32M")

    assert mark == 702


def test_one_wild_high_reading_does_not_move_the_median() -> None:
    """名次列会串出高位噪声（实机读到过 `[4781]`）。中位数对两侧离群免疫。

    ⚠️ 取 `max()` 的话，这一次读数会直接飞到 4781——上一层据此算「还差多少行」，
    算出来是个负数，于是当场收手，而这一趟静默地少走了几百行。
    （同一颗雷 `tools.ranking_scan.progress_mark` 踩过：实机 113 秒就判成到底。）
    """
    clean, _image, _ocr = _read("[701]\n[702]\n[703]\n[704]\n[705]")
    noisy, _image2, _ocr2 = _read("[701]\n[702]\n[4781]\n[703]\n[704]\n[705]")

    assert clean == 703
    assert noisy == clean


def test_the_answer_is_a_real_rank_not_a_half_row() -> None:
    """偶数个样本时不许给两数的平均——那是一个**榜上不存在的半个名次**。"""
    mark, _image, _ocr = _read("[700]\n[701]\n[702]\n[703]")

    assert mark == 701
    assert isinstance(mark, int)


# -- 怎么读的（这才是这次改动的要害） ------------------------------------------


def test_the_column_is_cropped_once_not_row_by_row() -> None:
    """⚠️ **一个框，一次 OCR。**

    逐行裁剪正是实机上失效的那条路：滚轮把列表停在非整行位置，13 个小框
    每一个都横跨两行。这条用例数框，改回按行切当场就红。
    """
    _mark, image, ocr = _read("[701]\n[702]\n[703]\n[704]\n[705]")

    assert len(image.crops) == 1
    assert len(ocr.calls) == 1


def test_the_strip_covers_the_whole_list_with_slack_around_the_rank_column() -> None:
    """裁框自己的凭据：左右各放宽、顶上多留一截、底边盖满一屏。

    顶上多留是因为滚轮会让首行往上探出去一截（实测偏离网格约 12px）；
    左右放宽是因为 `RANK_COLUMN` 是逐行裁剪时量的词框边界，整列读时贴边的字
    容易少读一个方括号——而少一个方括号就等于少一个样本。
    """
    _mark, image, _ocr = _read("[701]\n[702]\n[703]\n[704]\n[705]")
    left, top, right, bottom = image.crops[0]

    assert (left, right) == (RANK_COLUMN[0] - RANK_STRIP_PAD_PX, RANK_COLUMN[1] + RANK_STRIP_PAD_PX)
    assert top == ROW_FIRST_Y - RANK_STRIP_TOP_PAD_PX
    assert bottom == round(ROW_FIRST_Y + ROWS_PER_SCREEN * ROW_PITCH_PX)
    # 底边盖过整整一屏；上下都比逐行裁剪的窗口宽，读的就不是「某一行」。
    assert bottom - top > ROWS_PER_SCREEN * ROW_PITCH_PX


def test_the_column_bounds_follow_the_command_line_override() -> None:
    """列边界命令行可以覆盖（`--rank-column`），读数器得跟着走，不能写死常量。"""
    _mark, image, _ocr = _read("[701]\n[702]\n[703]\n[704]", RankingColumns(rank=(600, 650)))
    left, _top, right, _bottom = image.crops[0]

    assert (left, right) == (600 - RANK_STRIP_PAD_PX, 650 + RANK_STRIP_PAD_PX)


def test_the_recipe_is_the_multi_line_one() -> None:
    """整条列要 `--psm 6`（多行）。`--psm 7` 是单行的，拿它读一整列只会读到一行。"""
    _mark, image, ocr = _read("[701]\n[702]\n[703]\n[704]")

    assert ocr.calls == [("eng", "--psm 6")]
    # 灰度 + 3×：和 `name_column_text` 读整条名字列的是同一套配方。
    assert image.modes[0] == "L"
    assert "resize(240, 1800)" in image.modes
