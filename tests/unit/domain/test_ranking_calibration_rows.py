"""盲滚**行数**的自标定，以及「翻了 N 行到达 bot 区」那句话的往返。

口径从「屏」改到「行」（见 `docs/superpowers/specs/2026-08-22-ranking-blind-scroll-wheel-design.md`
第二节）：屏只是慢拖的副产品，滚轮根本没有「屏」这个概念，而名次天然就是行。

⚠️ 这个文件里最要紧的一条不是换算，是**两套正文不许互相串味**：库里存着一整年
「翻了 N 屏到达 bot 区」的历史，行版解析器把「78 屏」读成 78 行的话，自标定会
拿一个荒谬的小值去设盲滚——小值本身是安全的，但那是撞上的，不是算出来的，
而下一次噪声换个方向就未必安全了。
"""

import pytest

from evo_helper.domain import ranking
from evo_helper.domain.ranking import (
    BOT_AREA_REACHED_PREFIX,
    bot_area_reached_message,
    bot_area_reached_rows_message,
    bot_area_rows,
    bot_area_scrolls,
    calibrated_blind_rows,
)

#: 生产实测（2026-08-17 同一天六趟）的屏数 77/78/73/74/72/78 换算成行
#: （× `ROWS_PER_SCROLL` 8.3）大约就是这个量级，按新到旧排。
_RECENT_ROWS = [560, 548, 591, 570, 566]


def test_calibrated_blind_rows_takes_min_minus_margin() -> None:
    # min = 548，余量 73 行（= 原来的 10 屏），所以盲滚 475 行。
    assert calibrated_blind_rows(_RECENT_ROWS, sample_size=5, margin=73) == 475


def test_only_the_recent_window_counts() -> None:
    # ⚠️ 陈年样本必须被截掉：这个数随玩家增长往上漂，把半年前的小值算进来
    # 只会把盲滚压得越来越保守——安全，但白花检测段那 4.6 秒/屏。
    older_and_much_smaller = [*_RECENT_ROWS, 120, 130]
    assert calibrated_blind_rows(older_and_much_smaller, sample_size=5, margin=73) == 475


def test_not_enough_samples_returns_none() -> None:
    # None 的意思是「这次不给答案，用写死的默认值」，而不是「盲滚 0 行」。
    assert calibrated_blind_rows([560, 548], sample_size=5, margin=73) is None


def test_result_is_clamped_at_zero_instead_of_going_negative() -> None:
    # 样本比余量还小（榜单极短）时，答案是「一行都别盲滚」。
    assert calibrated_blind_rows([50, 50, 50, 50, 50], sample_size=5, margin=73) == 0


def test_sample_size_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        calibrated_blind_rows(_RECENT_ROWS, sample_size=0, margin=73)


def test_margin_is_a_parameter_not_a_hardcoded_constant() -> None:
    # 余量的取值住在 `game.ranking_ui`，这一层只会算——换个余量就得换个答案。
    assert calibrated_blind_rows(_RECENT_ROWS, sample_size=5, margin=0) == 548


def test_rows_message_round_trips() -> None:
    assert bot_area_rows(bot_area_reached_rows_message(566)) == 566


def test_rows_message_wording_is_pinned() -> None:
    # 措辞就是库里那条实测记录的格式，改了等于把历史样本一次性作废，
    # 而作废之后自标定静悄悄退回默认值——页面上、日志里都看不出异常。
    assert bot_area_reached_rows_message(700) == "翻了 700 行到达 bot 区"


def test_rows_message_keeps_the_shared_sql_prefix() -> None:
    # 读侧是「按前缀捞一批、再逐条解析」，前缀捞不到就等于没有样本。
    assert bot_area_reached_rows_message(700).startswith(BOT_AREA_REACHED_PREFIX)


def test_old_screen_message_is_not_misread_as_rows() -> None:
    # ⚠️ 库里存着一整年「翻了 N 屏到达 bot 区」的历史。把 78 屏当成 78 行会让
    # 自标定给出一个荒谬的小值，而小值是**安全**的——但必须是有意的，不是撞上的。
    assert bot_area_rows("翻了 78 屏到达 bot 区") is None


def test_new_rows_message_is_not_misread_as_screens() -> None:
    # 反方向也不许串：行版正文被屏版解析器吃进去的话，等于把 566 当成 566 屏，
    # 那是往「盲拖过头、榜首整段跳过」的方向错，比上一条严重得多。
    assert bot_area_scrolls(bot_area_reached_rows_message(566)) is None


def test_rows_parser_is_anchored_at_both_ends() -> None:
    # 免得把「…之后翻了 3 行到达 bot 区」这种复述也当成一次实测。
    assert bot_area_rows("重试之后翻了 3 行到达 bot 区") is None
    assert bot_area_rows(bot_area_reached_rows_message(566) + "（估算）") is None


def test_rows_parser_tolerates_surrounding_whitespace() -> None:
    # 日志正文进库时两端可能带空白，但中间的措辞一个字都不许变。
    assert bot_area_rows(f"  {bot_area_reached_rows_message(566)}\n") == 566


def test_screen_era_helpers_still_work_for_the_history_in_the_database() -> None:
    # 屏版留着不是为了新代码，是因为库里那一年样本还得读得出来。
    assert bot_area_scrolls(bot_area_reached_message(77)) == 77


def test_new_names_are_exported() -> None:
    for name in ("bot_area_reached_rows_message", "bot_area_rows", "calibrated_blind_rows"):
        assert name in ranking.__all__
