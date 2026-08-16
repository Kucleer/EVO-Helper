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
    SELF_ROW_BOTTOM_Y,
)
from evo_helper.tools.ranking_scan import (
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
    """假 OCR。**记下 config**——「整条列用 psm 6 而不是 psm 7」是配方的一部分。"""

    def __init__(self) -> None:
        self.configs: list[str] = []

    def image_to_string(self, cell: _Cell, **kwargs: object) -> str:
        self.configs.append(str(kwargs.get("config", "")))
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
