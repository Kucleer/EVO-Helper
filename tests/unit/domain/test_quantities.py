"""带单位的数只有一份解析器，这里钉住它的每一种写法。

⚠️ **两套格式必须一起验。** 战报屏上超过一千一律缩写（`928K` / `3.7M`），
太空舱材料页上则是点分千位（`5.388.122`）。只测其中一种，另一种会静悄悄读错
三个数量级——而两边读出来的都是「一个合法的数」，事后没有任何办法分辨。

⚠️ **断言一律用 `==`，不用 `pytest.approx`。** 这个模块存在的全部理由就是
`float("64.96") * 1000 == 64959.99999999999` 那种误差；用近似断言等于把要测的
东西关掉。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from evo_helper.domain.quantities import Quantity, parse_quantity


def _quantity(text: str) -> Quantity:
    parsed = parse_quantity(text)
    assert parsed is not None, f"{text!r} 应该读得出来"
    return parsed


class TestBattleReportFormats:
    """战报「获得资源」那一屏的真实样本（用户 2026-08-17 给的原样）。"""

    @pytest.mark.parametrize(
        ("text", "value"),
        [
            ("928K", 928_000),
            ("501.1K", 501_100),
            ("342.9K", 342_900),
            ("7.7K", 7_700),
            ("1.2K", 1_200),
            ("323K", 323_000),
            ("3.7M", 3_700_000),
            ("2.2M", 2_200_000),
            ("233", 233),
            ("66", 66),
            ("4", 4),
            ("0", 0),
        ],
    )
    def test_each_sample_parses_exactly(self, text: str, value: int) -> None:
        assert _quantity(text).value == Decimal(value)
        assert _quantity(text).amount == value

    def test_the_b_suffix_reaches_billions(self) -> None:
        """`B` 是新加的一档。缺它的下场不是报错，是整串判为读不出然后静默丢掉。"""
        assert _quantity("1.2B").amount == 1_200_000_000


class TestDecimalConversion:
    """⚠️ **换算必须走 `Decimal`。**

    这三个是 2026-08-17 军力榜上原样读到的值，也是 `float(x) * 1000` 唯一会露馅
    的那一类——脏值曾经**落进过库**（`bot_targets.military_score`），页面上显示成
    一串小数尾巴。战报那 12 格的样本恰好没有一个是脏的，所以这条只能靠榜上的
    真实值来钉：解析器是共用的，它在哪一侧退回 float 都是同一个缺陷。
    """

    @pytest.mark.parametrize(
        ("text", "value"),
        [("64.96K", 64_960), ("64.26K", 64_260), ("64.18K", 64_180)],
    )
    def test_the_measured_dirty_values_land_exactly(self, text: str, value: int) -> None:
        assert _quantity(text).value == Decimal(value)
        assert _quantity(text).amount == value


class TestInventoryFormats:
    """太空舱材料页：点是**千分位分隔符**。

    这条判据在战报屏上用不着（那一屏没有四位以上的裸数），但解析器是共用的，
    别处读错一样是错。
    """

    @pytest.mark.parametrize(
        ("text", "value"),
        [("5.388.122", 5_388_122), ("1.349.631", 1_349_631)],
    )
    def test_dots_group_thousands(self, text: str, value: int) -> None:
        assert _quantity(text).amount == value

    def test_a_bare_1_349_is_one_thousand_three_hundred_and_forty_nine(self) -> None:
        """⚠️ **这一条是整套判据的支点。**

        `1.349` 没有后缀，所以那个点是千分位分隔符，读作 1349——**不是** 1.349。
        资源数量都是整数，「不带后缀时点是小数点」这个解读天然不成立。
        """
        assert _quantity("1.349").amount == 1_349
        assert _quantity("1.349").value == Decimal(1_349)

    def test_a_suffix_turns_the_same_dot_back_into_a_decimal_point(self) -> None:
        """同一串加上后缀，点就是小数点了：`1.349K` = 1349，而不是 1349000。"""
        assert _quantity("1.349K").amount == 1_349


class TestPrecisionMarking:
    """⚠️ 用户接受误差，不等于可以把近似值显示得像精确值。"""

    @pytest.mark.parametrize("text", ["928K", "501.1K", "3.7M", "1.2B"])
    def test_a_suffix_means_approximate(self, text: str) -> None:
        assert _quantity(text).approximate is True

    @pytest.mark.parametrize("text", ["233", "66", "4", "0", "5.388.122", "1.349"])
    def test_a_plain_number_is_exact(self, text: str) -> None:
        assert _quantity(text).approximate is False
        assert _quantity(text).uncertainty == 0

    @pytest.mark.parametrize(
        ("text", "uncertainty"),
        [("928K", 500), ("501.1K", 50), ("3.7M", 50_000), ("1.2B", 50_000_000)],
    )
    def test_the_error_follows_the_displayed_digits(self, text: str, uncertainty: int) -> None:
        """有效位数不同，误差差三个数量级。

        `928K` 只有三位有效数字（±500），`501.1K` 有四位（±50）。对所有 K 值
        统一按一个数算，页面上给出的误差范围就是编的。
        """
        assert _quantity(text).uncertainty == uncertainty


class TestRefusals:
    @pytest.mark.parametrize("text", ["not a score", "", "   ", "K", ".", "1.2.3K", "12x"])
    def test_junk_reads_as_nothing(self, text: str) -> None:
        """读不出就是 None，**不给兜底值**：0 和「没读出来」在下游是两件事。"""
        assert parse_quantity(text) is None


class TestAmountRefusesToTruncate:
    def test_a_non_integer_raises_instead_of_rounding(self) -> None:
        """`1.5` 是合法读数（军力榜插值会产生），但它不是一个**数量**。

        悄悄截成 1 会让它看起来像一次正常的读数，而那正是最难查的一类错。
        """
        half = _quantity("1.5")
        assert half.value == Decimal("1.5")
        with pytest.raises(ValueError, match="不是整数"):
            _ = half.amount
