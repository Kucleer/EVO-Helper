"""导航栏：一次只设一个字段，不变的字段不重设。"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game.system_navigator import (
    GALAXY_FIELD,
    OK_BUTTON,
    POSITION_FIELD,
    SYSTEM_FIELD,
    SYSTEM_VIEW_BUTTON,
    VIEW_MENU_BUTTON,
    SystemNavigator,
    agreed_value,
    crop_reader,
    on_system_view,
    reads_like_a_dropped_digit,
)


class FakeDriver:
    def __init__(self) -> None:
        self.actions: list[tuple[str, Any]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.actions.append(("click", (x, y)))

    def type_number(self, value: int) -> None:
        self.actions.append(("type", value))

    def capture(self) -> Any:
        return object()

    def wait(self, seconds: float) -> None:
        return None


def fields_touched(driver: FakeDriver) -> list[tuple[int, int]]:
    return [payload for kind, payload in driver.actions if kind == "click" and payload != OK_BUTTON]


def test_first_hop_sets_all_three_fields() -> None:
    driver = FakeDriver()
    SystemNavigator(driver).goto(Coordinate(2, 121, 5))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_each_field_is_committed_before_the_next_one_is_opened() -> None:
    # 点开字段会弹出覆盖整条导航栏的浮层；一口气点两个字段，第二个数字会进第一个。
    driver = FakeDriver()
    SystemNavigator(driver).goto(Coordinate(2, 121, 5))
    assert driver.actions == [
        ("click", GALAXY_FIELD),
        ("type", 2),
        ("click", OK_BUTTON),
        ("click", SYSTEM_FIELD),
        ("type", 121),
        ("click", OK_BUTTON),
        ("click", POSITION_FIELD),
        ("type", 5),
        ("click", OK_BUTTON),
    ]


def test_next_position_in_the_same_system_only_sets_the_position() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    navigator.confirm(Coordinate(2, 121, 5))
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [POSITION_FIELD]


def test_crossing_a_system_keeps_the_galaxy() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 20))
    navigator.confirm(Coordinate(2, 121, 20))
    driver.actions.clear()
    navigator.goto(Coordinate(2, 122, 5))
    assert fields_touched(driver) == [SYSTEM_FIELD, POSITION_FIELD]


def test_typing_alone_is_not_evidence_of_where_the_nav_bar_is() -> None:
    """**本文件的重点。** 打完字不算数：省字段只能靠回读确认过的那份记忆。

    实机 2026-08-11：一次「设恒星系」的点击落到了银河系框上，游戏把 136 截断成
    最大值 9。按「我刚才打了什么」记，缓存当场就是错的，而判「一样」用的就是
    那份错记忆——银河系字段再没被重设，连续 44 个目标坐标核对全不过。
    按「面板回读到什么」记，错的记不进来：拿不准就把三个字段全重设一遍。
    """
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    assert navigator.current is None
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_invalidate_forces_all_three_fields_again() -> None:
    # 重连或弹窗之后导航栏里是什么已经不可知，不能再靠记忆省字段。
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 121, 5))
    navigator.confirm(Coordinate(2, 121, 5))
    navigator.invalidate()
    driver.actions.clear()
    navigator.goto(Coordinate(2, 121, 6))
    assert fields_touched(driver) == [GALAXY_FIELD, SYSTEM_FIELD, POSITION_FIELD]


def test_on_system_view_needs_two_labels() -> None:
    # 「行星」在底部导航条上也有，单个标签命中不足以证明在哪一屏。
    assert on_system_view("银河系  恒星系  行星")
    assert on_system_view("银河系 恒星系")
    assert not on_system_view("行星 舰队 太空舱 商店 联盟")
    assert not on_system_view("")


def test_ensure_system_view_does_nothing_when_already_there() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.goto(Coordinate(2, 1, 5))
    navigator.confirm(Coordinate(2, 1, 5))
    driver.actions.clear()
    assert navigator.ensure_system_view(lambda: "银河系 恒星系 行星")
    assert driver.actions == []
    # 没换视图，回读确认过的那份记忆还作数。
    assert navigator.current == Coordinate(2, 1, 5)


def test_ensure_system_view_switches_back_from_the_planet_view() -> None:
    driver = FakeDriver()
    navigator = SystemNavigator(driver)
    navigator.current = Coordinate(2, 1, 5)
    reads = iter(["行星 舰队 太空舱 商店 联盟", "银河系 恒星系 行星"])

    assert navigator.ensure_system_view(lambda: next(reads))
    assert fields_touched(driver) == [VIEW_MENU_BUTTON, SYSTEM_VIEW_BUTTON]
    # 换过视图之后导航栏里是什么已经不可知了。
    assert navigator.current is None


def test_ensure_system_view_gives_up_instead_of_clicking_blind() -> None:
    # 切不回去还往 (795, 71) 点，就是在认不出的画面上乱点。
    driver = FakeDriver()
    assert not SystemNavigator(driver).ensure_system_view(lambda: "什么都没有", attempts=2)
    assert fields_touched(driver) == [
        VIEW_MENU_BUTTON,
        SYSTEM_VIEW_BUTTON,
        VIEW_MENU_BUTTON,
        SYSTEM_VIEW_BUTTON,
    ]


class FakeImage:
    def __init__(self) -> None:
        self.cropped: list[tuple[int, int, int, int]] = []

    def crop(self, box: tuple[int, int, int, int]) -> str:
        self.cropped.append(box)
        return f"crop{box}"


def test_crop_reader_passes_the_recipe_through() -> None:
    image = FakeImage()
    seen: list[tuple[str, bool, int]] = []

    def ocr(crop: str, *, digits: bool, upscale: int, resample: str = "lanczos") -> str:
        seen.append((crop, digits, upscale))
        return "读数"

    read = crop_reader(image, ocr)
    assert read((1, 2, 3, 4), digits=True, upscale=5) == "读数"
    assert image.cropped == [(1, 2, 3, 4)]
    assert seen == [("crop(1, 2, 3, 4)", True, 5)]


class TestAgreedValue:
    """`agreed_value`：一个值框在各套配方下的读数怎么汇成一个能采纳的值。

    ## 这条判据是拿实机反例换来的

    老规则是「第一套配方读出非空就采纳」。生产 `system_log` 2026-08-18：这条回读
    上线以来 **28 次全部对不上**，错法一律是丢掉最左那一位（`277`→`77`、
    `250`→`50`、`15`→`5`）——第一套配方读出 `77`、非空、当场被采纳，后面几套
    根本没机会说话。同一种错法在本机实拍上也复现了（`27`→`7`、`52`→`5`）。

    ⚠️ **改这里的每一条都要先想清楚「会不会让读错的值匹配上」。** 认错一次的代价
    是缓存与导航栏分岔，见 `SystemNavigator` 类注释里 136→9 那次事故：连续 44 个
    目标核对全不过、13 分钟一发没派。
    """

    def test_a_value_two_recipes_agree_on_is_adopted(self) -> None:
        assert agreed_value(["277", "277", "", "", ""]) == "277"

    def test_a_value_only_one_recipe_read_is_refused(self) -> None:
        """⚠️ **一票不通过。** 这一条就是缺陷本体：老规则等价于一票通过。"""
        assert agreed_value(["277", "", "", "", ""]) == ""

    def test_the_longer_reading_wins_over_the_ones_that_dropped_a_digit(self) -> None:
        """生产那三条日志的形状：多数配方漏了首位，够票的完整读数照样该胜出。

        `77` 是 `277` 漏掉一位的样子，所以这份分歧解释得通，不该把整格作废。
        """
        assert agreed_value(["277", "277", "77", "77", "77"]) == "277"

    def test_the_winner_is_the_longest_not_whichever_sorts_first(self) -> None:
        """⚠️ 胜出者按**长度**挑，不许按别的顺序挑。

        `dump-bot-coord-mismatch-123228.png` 的恒星系框就是这个形状：三套读出 `52`、
        两套漏成 `5`。而 `'5' < '52'`，所以任何「按字典序挑一个」的写法都会挑中 `5`，
        接着 `52` 解释不成 `5` 漏字、整格作废——功能不会读错，但会一直读不出，
        也就是这次修之前那个「成功率 0%」的下场。
        """
        assert agreed_value(["52", "52", "5", "5"]) == "52"

    def test_a_longer_reading_without_a_second_witness_is_refused(self) -> None:
        """反过来：只有一套看全了 `277`，其余都读 `77` —— 交空串。

        **方向永远是「拿不准就多设」。** 宁可白设两个字段，也不认一个可能缺位的值。
        """
        assert agreed_value(["277", "77", "77", "77", "77"]) == ""

    def test_a_single_recipe_hallucinating_an_extra_digit_cannot_win(self) -> None:
        """⚠️ 一套配方凭空多读一位（实测 `9` 读成 `93`）既赢不了，也不会被无声吞掉。

        `93` 只有一票当不了候选；而 `9` 当上候选之后 `93` 不是它漏字的样子，
        于是整格作废。**读空是安全的，读错才是这一块最怕的东西。**
        """
        assert agreed_value(["93", "9", "9", "", ""]) == ""

    def test_a_substitution_disagreement_is_refused(self) -> None:
        """`3` 不是 `9` 漏了位，这份分歧解释不通 —— 交空串。"""
        assert agreed_value(["3", "9", "", "", ""]) == ""

    def test_two_equally_long_candidates_are_refused(self) -> None:
        """两个一样长却不同的值都够票 —— 说不清是哪个，交空串。

        管住这一条的是「其余非空必须是漏字」那道闸，不是另开的歧义闸：漏字必然
        更短，等长的对手永远解释不通。所以这里**没有**单独一道歧义闸（写过，
        变异测试证明它是死代码）。
        """
        assert agreed_value(["12", "12", "13", "13"]) == ""

    def test_nothing_read_comes_back_empty(self) -> None:
        assert agreed_value(["", "", "", "", ""]) == ""

    def test_non_digit_reads_do_not_vote(self) -> None:
        """Tesseract 偶尔把白名单里的冒号也吐出来（实拍上见过 `'27 :'`）。

        它不可能等于任何一个坐标分量，所以不参与投票；**但调用方仍要把原文记进
        日志**——「读成了什么」是下次校准唯一的线索。
        """
        assert agreed_value(["27 :", "27", "27", "", ""]) == "27"

    def test_the_vote_threshold_is_honoured(self) -> None:
        assert agreed_value(["7", "7"], min_votes=3) == ""
        assert agreed_value(["7", "7", "7"], min_votes=3) == "7"

    # -- 窄化否决（2026-08-25） ---------------------------------------------

    def test_a_substitution_by_one_recipe_no_longer_vetoes_a_winner(self) -> None:
        """⚠️⚠️ **一套配方读串了，不许否掉三票稳赢的那个值。**

        这是 2026-08-25 那次修的本体。生产实测的形状原样搬来：真值 `15`，
        三套读对，第一套把它读成 `6`（替换），第二套读成 `1`（漏字）。

        老规则要求「其余每个非空读数都能解释成胜出者漏字」，而 `6` 解释不通
        —— 于是 `15` 被一票否决、整格作废。**生产 1290 个值框里这样丢掉 123 个。**

        那条否决的依据是 `NAV_VALUE_RECIPES` 上写的「每套配方错法只有丢位」，
        而这份读数本身就是那句话的反例。
        """
        assert agreed_value(["6", "1", "15", "15", "15"]) == "15"

    def test_the_other_production_substitution_shapes_are_read_too(self) -> None:
        """同一批生产读数里的其余几种形状，一并钉住。

        它们都长得不一样（替换落在不同位置、有的还夹着空读），但都因为同一个
        原因被老规则作废。分开钉是因为**它们各自证明的东西不同**：一条过了
        不代表另一条也过。
        """
        assert agreed_value(["5", "6", "6", "6", "6"]) == "6"
        assert agreed_value(["5", "6", "", "6", "6"]) == "6"
        assert agreed_value(["6", "15", "1", "15", "15"]) == "15"
        assert agreed_value(["15", "18", "15", "5", "15"]) == "15"

    def test_a_recipe_that_saw_more_digits_still_vetoes(self) -> None:
        """⚠️⚠️ **窄化不等于取消。** 有配方看见了更多的位，照旧交空串。

        生产实测：真值 `261`，只有一套看全，两套读成 `26`。`26` 够票胜出，
        但 `261` 比它长、而 `26` 正是它漏字后的样子 —— **说明有人看见的比我多**，
        这一格就不能认。

        ⚠️ 这一条和上面那条是一对，方向相反。少了它，「拿掉否决」会退化成
        「谁票多就认谁」，于是这一格交出 `26`：一个**可能缺了位的坐标**。
        认错一次的代价见 `SystemNavigator` 类注释里 136→9 那次事故。
        """
        assert agreed_value(["261", "26", "26", "6", "61"]) == ""

    def test_the_veto_only_looks_at_readings_that_are_longer(self) -> None:
        """⚠️ 否决只看**更长**的读数，不看等长或更短的。

        构造成两者只差这一点：`5` 和 `6` 一样长，替换型的分歧 —— 不该否决；
        而上一条里的 `261` 更长 —— 该否决。哪天有人把 `_is_dropped_from`
        换成宽松的「不相等就算分歧」，这一条当场红。
        """
        assert agreed_value(["5", "6", "6", "6", "6"]) == "6"
        assert agreed_value(["6", "61", "6", "6", "6"]) == ""

    def test_the_known_hole_is_pinned_so_it_cannot_drift_silently(self) -> None:
        """⚠️ **已知没堵上的洞：五套一致地少读位时，规则交出那个短值。**

        真值 `117`，五套里四套读成 `7`、一套空 —— 规则里没有任何东西能看出屏上
        还有两位。生产 2026-08-25 出现 **98 次**。

        **这不是窄化引入的，老规则一样交 `7`。** 钉住它有两个用处：一是防止有人
        以为这次改动造成了它，二是下一步（数屏上有几位数字）做完之后，
        这条用例会红 —— 那时把它改成 `== ""`，就是那一步生效的证据。
        """
        assert agreed_value(["", "7", "7", "7", "7"]) == "7"


class TestReadsLikeADroppedDigit:
    """回读对不上时给日志定性用的纯函数。**只描述，不放行采纳。**"""

    def test_the_production_shapes_are_recognised(self) -> None:
        """生产 2026-08-18 那 28 次的三种形状。"""
        assert reads_like_a_dropped_digit("77", "277")
        assert reads_like_a_dropped_digit("50", "250")
        assert reads_like_a_dropped_digit("5", "15")

    def test_a_genuinely_different_number_is_not_a_dropped_digit(self) -> None:
        """⚠️ 反向也必须成立：把真的「导航栏在别处」说成「读错了」，会把排障引反。

        这个仓库出过「日志说假话比不说更糟」的事故。
        """
        assert not reads_like_a_dropped_digit("166", "277")
        assert not reads_like_a_dropped_digit("3", "9")

    def test_the_same_value_is_not_a_dropped_digit(self) -> None:
        assert not reads_like_a_dropped_digit("277", "277")

    def test_an_empty_read_is_not_a_dropped_digit(self) -> None:
        """读空是另一支（「读不出」），别混进「疑似漏位」里。"""
        assert not reads_like_a_dropped_digit("", "277")

    def test_a_longer_read_is_not_a_dropped_digit(self) -> None:
        assert not reads_like_a_dropped_digit("2277", "277")
