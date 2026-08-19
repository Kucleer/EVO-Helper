"""切换星球的纯判据：认哪一行、要不要切、拖到底没有、切没切成。

这几条一错，代价不是「切不过去」，而是**点在那一排的别的图标上**：
运输 / 部署 / 传送 / 转移 / 投送 / 保护 / 扩张，每一个都是真实操作。
所以这里守的是「认不出就交不出东西来」这条方向性——宁可空手，不可猜。
"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.domain.planet_switch import (
    PlanetRow,
    find_row,
    list_exhausted,
    origin_confirmed,
    origin_in,
    reached_top,
    reads_as_a_planet_row,
    rows_from_words,
    switch_needed,
)

HOME = Coordinate(2, 137, 18)
SECOND = Coordinate(9, 250, 8)
THIRD = Coordinate(4, 96, 7)

#: 一屏三行的真实读数，取自 `var/logs/calib-切换星球-基准.png` 的离线试读
#: （坐标列 ROI + 3× 最近邻 + `pirate_ui.PLANET_LIST_COORD_WHITELIST`）。
#: 噪声也是真的：行星大小 `155/223` 的斜杠被白名单吃掉、图标漏出来的零星数字。
#:
#: ⚠️ **坐标连方括号一起读回来**，那对括号是这一屏唯一能证明「这一位不是多出来的」
#: 的东西，理由整段在 `domain.planet_switch._PLANET_ROW_RE`。
BASELINE_WORDS = [
    (191, "[2:137:18]"),
    (211, "155223"),
    (353, "5"),
    (421, "[9:250:8]"),
    (442, "158200"),
    (651, "[4:96:7]"),
    (672, "153200"),
    (813, "5"),
]


class TestReadingAScreen:
    def test_only_three_segment_numbers_become_rows(self) -> None:
        """行星大小与零星噪声一律不成行。

        留成占位行的话，调用方手上就有了一个「有行、但不知道是哪颗星球」的东西，
        而那正是会被按行号点出去的那种东西。
        """
        rows = rows_from_words(BASELINE_WORDS)

        assert [row.coordinate for row in rows] == [HOME, SECOND, THIRD]

    def test_each_row_keeps_the_y_it_was_read_at(self) -> None:
        """y 必须原样带出来：待会儿要按在这一行上拖、要在它下面点「前往此处」。"""
        rows = rows_from_words(BASELINE_WORDS)

        assert [row.name_row_y for row in rows] == [191, 421, 651]

    def test_rows_come_back_top_down_even_if_the_ocr_shuffles_them(self) -> None:
        """OCR 的词序不保证是从上到下，而「最下面那一行」是拖动的按下点。"""
        rows = rows_from_words([(651, "[4:96:7]"), (191, "[2:137:18]"), (421, "[9:250:8]")])

        assert [row.name_row_y for row in rows] == [191, 421, 651]

    def test_a_screen_nothing_parses_on_yields_no_rows(self) -> None:
        """读不出坐标就一行都不给——调用方于是什么都不点。"""
        assert rows_from_words([(191, "155223"), (211, "8"), (353, "")]) == ()


class TestARowMustBeBracketed:
    """⚠️ **实机 2026-08-19：`9:250:8` 读成 `9:250:88`，两颗出发星球一颗都切不过去。**

    生产 `system_log` 13:48:41 那一趟把全过程录了下来——第一屏就是
    `['4:277:15', '9:250:88', '4:96:7']`，目标明明在眼前，`find_row` 按坐标精确
    匹配却对不上，于是一路拖到底判 `NOT_FOUND`。

    多出来那一位是**方括号被白名单顶成了数字**。离线在
    `var/logs/dump-planet-list-unreadable-153847.png`（第一屏与那天日志逐字相同）
    上量到的直接凭据：同一块像素、同一个词框 (1129, 1189)，纯数字白名单读出
    `'14:277:15'`，带括号白名单读出 `'[4:277:15]'`。

    所以现在一行必须**成对括起来**才算数。方向是「宁可读不出，不可读错」：
    读不出走「什么都不点」那条安全路径，而读错会真的把舰队切到另一颗星球上去。
    """

    def test_an_extra_digit_where_a_bracket_was_is_not_a_row(self) -> None:
        """`[9:250:8]` 的 `]` 被顶成 `8` → `9:250:88`。**这一行必须不成行。**

        老规则会把它认成 `9:250:88` 这颗**并不存在**的星球，于是既切不过去、
        日志还要指着用户的配置说「这颗星球不在你的行星列表里」。
        """
        assert rows_from_words([(421, "9:250:88")]) == ()

    def test_an_extra_digit_where_the_opening_bracket_was_is_not_a_row_either(self) -> None:
        """`[4:277:15]` 的 `[` 被顶成 `1` → `14:277:15`（4×/LANCZOS 实拍读数）。"""
        assert rows_from_words([(191, "14:277:15")]) == ()

    def test_a_word_the_ocr_cut_in_half_is_not_a_row(self) -> None:
        """实拍上出现过 `['[2:137:1', '5]']`：一个词被拦腰切开。

        老规则会把 `[2:137:1` 认成 `2:137:1`——**一颗真实存在的别的星球**。
        右括号不在同一个词里，就说不出这一段是不是完整的。
        """
        assert rows_from_words([(191, "[2:137:1"), (191, "5]")]) == ()

    def test_the_same_judgement_decides_which_recipe_gets_believed(self) -> None:
        """挑配方与成行必须用同一条判据（`tools.pirate_loop._planet_rows`）。

        两边各写一遍的话，会出现「这套配方被采信了，可它给出的一行都不成行」——
        实机上的样子就是「读到了东西」却逐屏全空，最难查的那一类。
        """
        for text in ("[9:250:8]", "9:250:88", "14:277:15", "155223", ""):
            assert reads_as_a_planet_row(text) is bool(rows_from_words([(191, text)]))


class TestFindingTheTarget:
    def test_the_row_is_matched_by_the_whole_coordinate(self) -> None:
        rows = rows_from_words(BASELINE_WORDS)

        assert find_row(rows, SECOND) is not None
        assert find_row(rows, SECOND).name_row_y == 421  # type: ignore[union-attr]

    def test_a_coordinate_that_is_a_substring_of_another_is_not_a_hit(self) -> None:
        """`2:13:7` 的文字是 `2:137:1` 的子串。按文字包含匹配会把两颗星球当成一颗——
        而「点错行」在这个面板上等于「把资源送去别处」。"""
        rows = rows_from_words([(191, "[2:137:18]")])

        assert find_row(rows, Coordinate(2, 13, 7)) is None
        assert find_row(rows, Coordinate(2, 137, 1)) is None

    def test_a_planet_that_is_not_on_this_screen_is_simply_absent(self) -> None:
        assert find_row(rows_from_words([(191, "[2:137:18]")]), SECOND) is None


class TestSwitchingOnlyOncePerRound:
    def test_a_fresh_process_always_switches(self) -> None:
        """进程刚起来时**不知道**游戏停在哪颗星球上，所以开工那一次一定要切。"""
        assert switch_needed(HOME, None) is True

    def test_the_same_planet_is_not_switched_to_twice(self) -> None:
        """这就是「一轮只切一次」：切换属于开工阶段，不挂在每个目标前面。"""
        assert switch_needed(HOME, HOME) is False

    def test_a_different_planet_still_switches(self) -> None:
        assert switch_needed(SECOND, HOME) is True


class TestReachingTheBottom:
    def test_the_same_planets_twice_means_the_list_stopped_moving(self) -> None:
        rows = rows_from_words(BASELINE_WORDS)

        assert list_exhausted(rows, rows) is True

    def test_the_judgement_ignores_the_few_pixels_the_rows_drift(self) -> None:
        """拖动带惯性，同一批行两屏之间 y 会差几个像素。

        按 y 比的话「还能拖」会永远成立，于是每次都拖满上限才罢休——一轮白花
        六次慢拖（每次一秒多），而且每一次都在星球名那一行上按下手指。
        """
        before = rows_from_words([(191, "[2:137:18]"), (421, "[9:250:8]")])
        after = rows_from_words([(188, "[2:137:18]"), (418, "[9:250:8]")])

        assert list_exhausted(before, after) is True

    def test_new_planets_coming_into_view_means_keep_dragging(self) -> None:
        before = rows_from_words([(191, "[2:137:18]"), (421, "[9:250:8]")])
        after = rows_from_words([(191, "[9:250:8]"), (421, "[4:96:7]")])

        assert list_exhausted(before, after) is False


class TestGettingBackToTheTop:
    """⚠️ **实机 2026-08-19：列表关掉再打开并不复位，于是两颗出发星球都切不过去。**

    13:48:41 那一趟从 `['4:277:15', '9:250:88', '4:96:7']` 一路拖到
    `['7:228:15', '1:55:6', '9:411:17']`；13:49:11 与 13:49:40 那两趟**第一屏读到的
    就是那个底部**。而找目标只会往下翻，排在顶部的两颗于是永远够不着——
    一屏判到底、`NOT_FOUND`、这一轮一发都不派。**而且会自我延续**：拖到底一次，
    之后每一趟都从底部开始。
    """

    def test_the_list_is_at_the_top_when_dragging_back_changes_nothing(self) -> None:
        rows = rows_from_words(BASELINE_WORDS)

        assert reached_top(rows, rows) is True

    def test_a_list_that_is_still_moving_is_not_at_the_top(self) -> None:
        before = rows_from_words([(191, "[9:250:8]"), (421, "[4:96:7]")])
        after = rows_from_words([(191, "[2:137:18]"), (421, "[9:250:8]")])

        assert reached_top(before, after) is False

    def test_the_very_first_screen_is_never_the_proof(self) -> None:
        """⚠️ **没拖过就说在顶部 = 根本不回顶**，也就是这次要修的那个缺陷本身。

        第一屏是「拖之前」的样子，没有任何证据说明它就是顶部——2026-08-19 那两趟
        的第一屏恰恰是底部。
        """
        assert reached_top(None, rows_from_words(BASELINE_WORDS)) is False

    def test_a_screen_that_read_nothing_is_not_proof_of_the_top_either(self) -> None:
        """⚠️ **整屏读空不算到顶。**

        空的时候两屏的坐标序列都是 `[]`，直接比会当成「没动」而停手。可读空的
        意思是「这一帧没认出来」（浮层盖着、OCR 失手），列表滚到哪根本无从谈起——
        停在那儿就等于把一次识别失败当成了一个位置事实。信箱那条链路
        （`tools.pirate_loop._scroll_mail_list_to_top`）的注释里写死了同一条。
        """
        assert reached_top((), ()) is False
        assert reached_top(rows_from_words(BASELINE_WORDS), ()) is False


class TestConfirmingTheSwitch:
    def test_the_origin_line_confirms_the_planet_it_names(self) -> None:
        """派遣面板「起点」那一行的真实读数（离线试读 `calib-舰队面板-client.png`）。"""
        assert origin_confirmed("2:137:18", HOME) is True

    def test_a_prefix_the_roi_dragged_in_does_not_break_it(self) -> None:
        """ROI 左界蹭到「起点：」时白名单会压出零星数字/冒号，实测出现过 `:2:137:18`。"""
        assert origin_confirmed(":2:137:18", HOME) is True

    def test_another_planet_is_not_confirmed(self) -> None:
        assert origin_confirmed("2:137:18", SECOND) is False

    def test_an_unreadable_line_counts_as_not_switched(self) -> None:
        """**方向只能是这一个。** 漏判的代价是白等一轮；误判的代价是整轮的台账
        都在撒谎——舰队从这颗星球飞出去，`attack_intents.origin_*` 上写着另一颗，
        战报永远配不上那一发。"""
        assert origin_confirmed("", HOME) is False
        assert origin_confirmed("2137:18", HOME) is False
        assert origin_confirmed("起点", HOME) is False


class TestSayingWhichPlanetWasRead:
    """`origin_in` 把「读到的是哪一颗」单独交出来，判定与日志共用同一个解析器。

    两边各写一遍解析，迟早会出现「判据说对不上、日志说对得上」这种自相矛盾的
    记录——而那正是 2026-08-17 那条「日志说假话比不说更糟」要防的东西。
    """

    def test_it_names_the_planet_on_the_line(self) -> None:
        assert origin_in("2:137:18") == HOME

    def test_it_skips_the_prefix_noise_the_roi_dragged_in(self) -> None:
        assert origin_in(":2:137:18") == HOME

    def test_unreadable_is_none_not_a_guess(self) -> None:
        """⚠️ **None 的意思是「读不出」，不是「不是这一颗」。**

        调用方必须把这两件事分开：读不出时该重读几帧，重读仍读不出才按核不过
        收场；而绝不许当成「对上了」。凑不出三段数字的噪声挑不出一个假坐标来，
        这一条是那个保证的另一面。
        """
        assert origin_in("") is None
        assert origin_in("2137:18") is None
        assert origin_in("起点") is None
        assert origin_in("1 7 5") is None

    def test_the_verdict_is_expressed_in_terms_of_the_same_reading(self) -> None:
        """判定就是「读到的那一颗等于目标」，不是第二份实现。"""
        for raw in ("2:137:18", ":2:137:18", "", "起点", "9:250:8"):
            assert origin_confirmed(raw, HOME) is (origin_in(raw) == HOME)


def test_a_row_carries_the_text_it_was_read_from() -> None:
    """找不到时要把逐屏读到的原文说出去，否则「翻不到」和「读坏了」分不开。"""
    row = rows_from_words([(191, "[2:137:18]")])[0]

    assert row == PlanetRow(coordinate=HOME, text="[2:137:18]", name_row_y=191)
