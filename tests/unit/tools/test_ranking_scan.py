"""军力榜采集工具：读一屏、合成入库清单。

每一条钉的都是「改坏了也不报错」的那种规矩——变异测试逐个验过。
实机依据来自 2026-08-14 的 `var/logs/rankv/21-panel.png`。
"""

from __future__ import annotations

from datetime import UTC, datetime

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_ui import (
    NAME_COLUMN,
    RANK_COLUMN,
    ROW_FIRST_Y,
    ROW_PITCH_PX,
    SCORE_COLUMN,
    SCROLL_STALL_CONFIRMATIONS,
    SELF_ROW_BOTTOM_Y,
)
from evo_helper.tools.ranking_scan import (
    furthest_rank,
    is_self_row,
    keep_screens,
    parse_score,
    rows_from_image,
    targets_from_rows,
    track_progress,
)

NOW = datetime(2026, 8, 14, tzinfo=UTC)


class _Cell:
    """假裁剪块。记下自己被 `convert` 成了什么模式——配方的一部分。"""

    def __init__(self, text: str, log: list[str]) -> None:
        self.text = text
        self.width = 20
        self.height = 20
        self._log = log

    def convert(self, mode: str) -> _Cell:
        self._log.append(mode)
        return self

    def resize(self, _size: tuple[int, int], _resample: object) -> _Cell:
        return self


class _Image:
    """按 y 摆行的假面板。`rows` 是 {行号: (名次文字, 名字, 分数文字)}。"""

    def __init__(self, rows: dict[int, tuple[str, str, str]]) -> None:
        self.rows = rows
        self.modes: list[str] = []
        self.crops: list[tuple[int, int, int, int]] = []

    def crop(self, box: tuple[int, int, int, int]) -> _Cell:
        self.crops.append(box)
        left, top, _right, bottom = box
        centre = (top + bottom) / 2
        index = round((centre - ROW_FIRST_Y) / ROW_PITCH_PX)
        cells = self.rows.get(index)
        if cells is None:
            return _Cell("", self.modes)
        column = {RANK_COLUMN[0]: 0, NAME_COLUMN[0]: 1, SCORE_COLUMN[0]: 2}.get(left)
        return _Cell("" if column is None else cells[column], self.modes)


class _Ocr:
    def image_to_string(self, cell: _Cell, **_kwargs: object) -> str:
        return cell.text


def _read(rows: dict[int, tuple[str, str, str]]) -> tuple[list[RankingRow], _Image]:
    image = _Image(rows)
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


def test_progress_is_measured_by_the_furthest_rank_not_by_string_equality() -> None:
    """⚠️ **「两屏 OCR 完全相等」这条实机上一次都不会触发。**

    榜单上大量是中文玩家名（`探险12`、`资源32`），而名字列跑的是 `eng`——
    同一行连读两次就是两个不同的噪声串。2026-08-15 实机滚了 55 次，
    `scroll_once` 的 `EXHAUSTED` 一次都没触发。

    名次是数字，拿「最大名次有没有往前走」当进度指针才结实。
    """
    noisy_a = [RankingRow(237, "=- ,, _ -", None, None), RankingRow(249, "??", None, None)]
    noisy_b = [RankingRow(237, "= -. _ ~", None, None), RankingRow(249, "?7", None, None)]

    assert list(noisy_a) != list(noisy_b)  # 字符串比：看着「变了」
    assert furthest_rank(noisy_a) == furthest_rank(noisy_b) == 249  # 名次比：没往前走


def test_a_screen_with_no_readable_rank_reports_no_progress() -> None:
    """一个名次都读不出来时返回 0——那不构成「又往前了」，只会累计停滞次数。"""
    assert furthest_rank([RankingRow(None, "noise", None, None)]) == 0
    assert furthest_rank([]) == 0


def test_one_stalled_drag_is_not_enough_to_call_it_finished() -> None:
    """⚠️ 卡顿时单次拖动没生效是常态（2026-08-15 有游戏活动，实测就很卡）。
    1 次就判到底，会把**半截榜单当成完整榜单**收工，而且日志上看着一切正常。
    """
    furthest, stalled, done = track_progress(furthest=249, stalled=0, reach=249)

    assert (furthest, stalled, done) == (249, 1, False)


def test_it_gives_up_after_enough_confirmations() -> None:
    """攒够了才收工——次数由 `SCROLL_STALL_CONFIRMATIONS` 定，不是写死的。"""
    assert track_progress(249, SCROLL_STALL_CONFIRMATIONS - 1, 249)[2] is True
    assert track_progress(249, SCROLL_STALL_CONFIRMATIONS - 2, 249)[2] is False


def test_any_progress_at_all_clears_the_stall_counter() -> None:
    """卡顿是间歇的：停了两次又走了一次，不该接着按停滞累计。"""
    assert track_progress(furthest=249, stalled=2, reach=257) == (257, 0, False)
