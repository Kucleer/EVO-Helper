"""军力榜那一屏的判据：坐标反解、名次自纠、降序自查、军力值插值。

用的是**实机 2026-08-14 的两屏真数据**（用户手机截图 + 我自己的实机截图），
不是编出来的例子——这一层的每条规矩都是被那两屏逼出来的。
"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import (
    RankingRow,
    bot_rows,
    coordinate_of,
    descending_breaks,
    interpolate_scores,
    repair_ranks,
)

#: 实机军事榜那一屏（`ScreenShot_2026-08-14_113055`），名次 639–650。
#: 三列的真值我逐行肉眼核过。
LIVE_NAMES = [
    "bot_4_30_12",
    "bot_4_100_13",
    "bot_4_183_20",
    "bot_4_160_9",
    "bot_4_360_15",
    "bot_4_44_10",
    "bot_4_410_9",
    "bot_4_33_19",
    "bot_4_458_8",
    "bot_4_194_11",
    "bot_4_427_14",
    "bot_4_154_10",
]


# -- 坐标反解：唯一一列读错就会派错兵的 ----------------------------------------


def test_a_bot_name_yields_its_coordinate() -> None:
    """名字直接编码坐标，这是整张榜最值钱的一点。"""
    assert coordinate_of("bot_4_30_12") == Coordinate(4, 30, 12)


def test_every_name_on_the_live_screen_resolves() -> None:
    """实机那一屏 12 行全都反解得出——配方（灰度 3× LANCZOS psm7）是按这个选的。"""
    assert all(coordinate_of(name) is not None for name in LIVE_NAMES)


def test_a_duplicated_digit_is_refused_rather_than_believed() -> None:
    """**实机真实的错读形态：数字重复。**

    用户库里 435 个已扫 bot，35 个的名字反解不出原坐标，形态都是这个：

        2:121:7   名字读作 bot_2_1121_7    ← 1121 > 499
        2:123:12  名字读作 bot_2_123_122   ← 122 > 20

    榜单里名字是坐标的**唯一**来源，所以这道闸是硬的：宁可这一行不要，
    也不能往 2:1121:7 派一支舰队。
    """
    assert coordinate_of("bot_2_1121_7") is None
    assert coordinate_of("bot_2_123_122") is None


def test_a_plausible_but_wrong_name_still_gets_through() -> None:
    """⚠️ **这道闸挡不住「合法但错」**，这条把界限钉死，免得有人以为它是万能的。

    `bot_2_121_7` 读成 `bot_2_127_7` 完全合法，区间校验无能为力。所以榜单发现的
    新坐标在库里必须标成「未验证」，与坐标扫描扫出来的分开记。
    """
    assert coordinate_of("bot_2_127_7") == Coordinate(2, 127, 7)


def test_a_human_name_is_not_a_coordinate() -> None:
    """榜单前 638 名是真人，`[638] GoudanLi --- 0` 这种也在里面。"""
    assert coordinate_of("GoudanLi") is None
    assert coordinate_of("Player0654420690") is None


def test_the_separator_may_be_read_as_a_space() -> None:
    """OCR 会把 `_` 读成空格或读丢。宽在分隔符上不引入歧义——把关的是区间。"""
    assert coordinate_of("bot 4 30 12") == Coordinate(4, 30, 12)


# -- 名次：免费的校验和 --------------------------------------------------------


def test_a_misread_rank_is_repaired_from_its_neighbours() -> None:
    """**实机 2026-08-14 那一屏当场抓到的两处。**

        [638] [639] [640] [641] [642] [5] [644]
                                       ↑ 应是 643

    名次严格连续，所以真值能从邻居推出来——不花一次额外的 OCR。
    """
    assert repair_ranks([638, 639, 640, 641, 642, 5, 644]) == [
        638,
        639,
        640,
        641,
        642,
        643,
        644,
    ]


def test_an_unreadable_rank_is_filled_in_too() -> None:
    """读不出来（None）和读错一样，都能被序列补上。"""
    assert repair_ranks([700, None, 702]) == [700, 701, 702]


def test_a_misread_at_the_very_first_row_does_not_poison_the_rest() -> None:
    """⚠️ **锚必须两边都验过。**

    错读恰好落在首行时，只看后邻居会把错的当成锚，整屏跟着错。这里第一行的
    `375` 与后面对不上，所以锚要落在 638 上，而 375 被改回 637。
    """
    assert repair_ranks([375, 638, 639]) == [637, 638, 639]


def test_a_screen_with_no_trustworthy_anchor_is_left_alone() -> None:
    """一个都对不上就不猜。猜出来的名次会安静地指向另一行。"""
    assert repair_ranks([12, 900, 5]) == [12, 900, 5]


# -- 军力值：只挡大错 ----------------------------------------------------------


def test_a_score_that_climbs_is_reported() -> None:
    """榜单按军力降序排，比上一行大一定是读错了。"""
    assert descending_breaks([29.59, 28.73, 99.9, 28.51]) == [2]


def test_the_small_misreads_from_the_live_screen_are_not_caught() -> None:
    """⚠️ **这条是刻意钉住这道闸的局限。**

    实机那一屏两处错读（`28.67K→28.57K`、`27.3K→27.45K`）都仍然保持降序，
    降序校验一个都抓不住。按用户口径军力值不需要精确，但不要以为有了它就准了。
    """
    assert descending_breaks([28.73, 28.57, 28.51]) == []


def test_a_gap_is_filled_with_the_midpoint() -> None:
    """用户口径：读出了 650 和 660 的军力，中间直接插值。"""
    assert interpolate_scores([30.0, None, 20.0]) == [30.0, 25.0, 20.0]


def test_a_gap_with_nothing_on_one_side_stays_empty() -> None:
    """**留 None 而不是外推。** 榜首/榜尾之外没有邻居，编一个出来就是凭空造数据。"""
    assert interpolate_scores([None, 20.0, None]) == [None, 20.0, None]


# -- 谁是 bot ------------------------------------------------------------------


def test_bots_are_told_apart_by_name_not_by_rank() -> None:
    """**判据是「名字反解得出坐标」，不是「名次大于 638」。**

    真人数量会变，写死名次在下一次刷新之后就是错的；而 `[638] GoudanLi --- 0`
    这种 0 分真人正好夹在交界处。
    """
    rows = [
        RankingRow(rank=638, name="GoudanLi", score=0.0, coordinate=None),
        RankingRow(rank=639, name="bot_4_30_12", score=29.59, coordinate=Coordinate(4, 30, 12)),
    ]

    assert [row.name for row in bot_rows(rows)] == ["bot_4_30_12"]
