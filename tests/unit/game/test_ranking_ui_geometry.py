"""排行榜那几个魔数自己的凭据：把它们钉在 2026-08-14 实机量到的像素上。

`test_ranking_nav.py` 里的用例都从这份几何出发（它们写的是量出来的绝对像素），
这一份是几何本身的凭据——改坏它，实机上点的就是别的东西。

⚠️ 这里的断言**不许拿被守的常量当尺子**（比如
`assert MILITARY_TAB[1] == MILITARY_TAB[1]` 那一类）：那样常量被改坏时
断言跟着改口，变异那一轮会是绿的。所以右边全是字面量。
"""

from __future__ import annotations

from math import hypot

from evo_helper.game.ranking_ui import (
    ALLIANCE_TAB,
    BLIND_SCROLLS,
    FIRST_BOT_RANK,
    MILITARY_TAB,
    NAV_BAR_Y,
    NAV_DRAG_FROM_X,
    NAV_DRAG_TO_X,
    NAV_LABEL_ROI,
    NAV_LABEL_WORD_GAP_PX,
    PLAYER_TAB,
    RANKING_CLOSE,
    RANKING_LIST_MAX_Y,
    RANKS_PER_SCROLL_MAX,
    RANKS_PER_SCROLL_MEASURED,
    RECHARGE_KEEPOUT_CENTER,
    RECHARGE_KEEPOUT_PX,
    ROW_FIRST_Y,
    ROW_LAST_Y,
    ROW_PITCH_PX,
    ROWS_PER_SCROLL,
    SCROLL_FROM_Y,
    SCROLL_TO_Y,
    SCROLL_X,
    SELF_ROW_BOTTOM_Y,
)

#: 拖完导航条之后，五个标签的中心 x（实机量）。
LABEL_CENTRES = (830, 918, 998, 1079, 1159)


def test_the_calibrated_pixels_are_the_ones_measured_on_the_live_client() -> None:
    assert (NAV_DRAG_FROM_X, NAV_BAR_Y) == (1122, 862)
    assert (NAV_DRAG_TO_X, NAV_BAR_Y) == (860, 862)
    assert NAV_LABEL_ROI == (760, 880, 1220, 906)
    assert MILITARY_TAB == (1084, 212)
    assert RANKING_CLOSE == (750, 71)
    assert (SCROLL_X, SCROLL_FROM_Y, SCROLL_TO_Y) == (960, 700, 300)


def test_the_nav_bar_is_dragged_leftwards_and_the_board_is_dragged_upwards() -> None:
    """方向搞反了照样跑得通，只是永远翻不到东西：往右拖露不出「排名」，往下拖是回到第 1 名。"""
    assert NAV_DRAG_TO_X < NAV_DRAG_FROM_X
    assert SCROLL_TO_Y < SCROLL_FROM_Y


def test_the_label_row_sits_below_the_row_that_gets_clicked() -> None:
    """图标在 862，文字在 880–906。拿标签行的 y 去点，点的是文字下面的空白。"""
    assert NAV_LABEL_ROI[1] > NAV_BAR_Y


def test_every_measured_label_falls_inside_the_row_that_gets_read() -> None:
    """ROI 收窄到把某个标签切掉，那个标签就永远读不出来——「排名」正好在右边第二个。"""
    assert all(NAV_LABEL_ROI[0] < x < NAV_LABEL_ROI[2] for x in LABEL_CENTRES)


def test_the_word_gap_splits_characters_from_their_neighbours() -> None:
    """合并阈值必须落在「同一标签内的字距」与「跨标签的字距」之间。

    两字标签被拆开时每个字离本标签中心约半个字宽（≈12px），于是同一标签内两字
    相距 ≈24px，而跨标签最近的两字相距 ≈ 81 - 24 = 57px（排 ← 盟）。

    上界是 57 **不是** 80：按中心距挑阈值会把「联盟」「排名」合成 `联盟排名`，
    贴不回词表，「排名」就永远找不到。
    """
    closest_centres = min(
        right - left for left, right in zip(LABEL_CENTRES, LABEL_CENTRES[1:], strict=False)
    )
    within_label = 24
    across_labels = closest_centres - within_label

    assert within_label < NAV_LABEL_WORD_GAP_PX < across_labels


def test_every_point_this_module_may_click_stays_clear_of_the_recharge_button() -> None:
    """⚠️ (1170, 70) 是「$ 充值」。点错就是充值页。

    导航条上的点击 x 来自 OCR，可落在标签行 ROI 的任何位置，所以两端都量一遍。
    """
    reachable = (
        RANKING_CLOSE,
        MILITARY_TAB,
        PLAYER_TAB,
        ALLIANCE_TAB,
        (NAV_LABEL_ROI[0], NAV_BAR_Y),
        (NAV_LABEL_ROI[2], NAV_BAR_Y),
        (NAV_DRAG_FROM_X, NAV_BAR_Y),
        (NAV_DRAG_TO_X, NAV_BAR_Y),
        (SCROLL_X, SCROLL_FROM_Y),
        (SCROLL_X, SCROLL_TO_Y),
    )
    recharge_x, recharge_y = RECHARGE_KEEPOUT_CENTER

    too_close = [
        point
        for point in reachable
        if hypot(point[0] - recharge_x, point[1] - recharge_y) < RECHARGE_KEEPOUT_PX
    ]

    assert too_close == []
    assert RECHARGE_KEEPOUT_CENTER == (1170, 70)
    assert RECHARGE_KEEPOUT_PX >= 40, "余量要远大于 human_input.CLICK_JITTER_PX(±4)"


def test_the_pinned_self_row_is_below_everything_the_reader_is_told_to_read() -> None:
    """⚠️ 自己那一行钉在 y=837，不随滚动移动。

    把它读进榜单，它会每滚一屏重复出现一次，还会让「拖了一下内容没变」这条
    到底判据变迟钝。所以读数的下界必须卡在它上面。
    """
    assert ROW_LAST_Y < RANKING_LIST_MAX_Y < SELF_ROW_BOTTOM_Y


def test_the_row_pitch_matches_the_two_ends_that_were_measured() -> None:
    """行距、第一行、最后一行是三个独立量出来的数，必须互相对得上。"""
    gaps = (ROW_LAST_Y - ROW_FIRST_Y) / ROW_PITCH_PX

    assert round(gaps) == 12
    assert abs(gaps - 12) < 0.1


def test_one_scroll_advances_less_than_a_screenful_so_no_rank_is_skipped() -> None:
    """滚一次推进 ≈8.3 名，而一屏看得见 13 行——两屏之间必有重叠。

    反过来（一次推进比一屏还多）会在两屏之间留下静默缺口：名次照样连续递增，
    从数据上根本看不出中间少了几个人。
    """
    visible_rows = (ROW_LAST_Y - ROW_FIRST_Y) / ROW_PITCH_PX + 1

    assert ROWS_PER_SCROLL < visible_rows


def test_the_blind_drag_can_never_overshoot_the_first_bot() -> None:
    """⚠️ **宁可少拖，不可多拖。**

    开榜之后先盲拖几屏省掉检测（用户建议 2026-08-15：「直接默认先拖个 1 分钟」）。
    少拖只是多花几次廉价检测；**多拖会直接跳过榜首那批 bot**——而那批正是军力
    最高、最该先看到的，也正是用户「先扫前 10-20 就出发」要的那批。

    所以这个数不是按时间定的，是按余量定的：按推进速率的**上界**算，盲拖那一段
    也够不到 bot 起点。速率上界取 12 名/屏（实测 8.0，那还是卡顿时的数）。
    """
    assert BLIND_SCROLLS * RANKS_PER_SCROLL_MAX < FIRST_BOT_RANK


def test_the_upper_bound_really_is_above_the_measured_rate() -> None:
    """⚠️ 上界拿实测值填就没有余量了。

    实测那天（2026-08-15）有游戏活动、卡顿明显，8.0 名/屏是**偏慢**的一天。
    不卡时只会更快，而余量没了就意味着盲拖可能越过 bot 起点。
    """
    assert RANKS_PER_SCROLL_MAX > RANKS_PER_SCROLL_MEASURED
