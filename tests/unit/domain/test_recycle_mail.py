"""回收报告邮件的解析判据。

⚠️ 这里每一条都对应一次实拍打脸，不是设计出来的规则。
语料 8 封在 `var/fixtures/vision/recycle-mail/`（实拍不进 Git）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.quantities import Quantity
from evo_helper.domain.recycle_mail import (
    NOMINAL_SHIP_CAPACITY,
    RecycleHaul,
    haul_is_consistent,
    looks_monotonic,
    parse_amounts,
    vote,
)

NOW = datetime(2026, 9, 12, 5, 47, 4, tzinfo=UTC)


def _haul(metal: float, crystal: float, gas: float, ships: int) -> RecycleHaul:
    return RecycleHaul(
        reported_at_utc=NOW,
        origin=Coordinate(8, 117, 6),
        target=Coordinate(8, 361, 15),
        amounts=(
            Quantity(Decimal(str(metal)), approximate=True, uncertainty=0),
            Quantity(Decimal(str(crystal)), approximate=True, uncertainty=0),
            Quantity(Decimal(str(gas)), approximate=True, uncertainty=0),
        ),
        ships=ships,
    )


# -- 容量不变量：唯一挡得住「像样的错数」的闸门 ----------------------------------


@pytest.mark.parametrize(
    ("metal", "crystal", "gas", "ships"),
    [
        (4_990_000, 2_930_000, 365_300, 415),
        (3_750_000, 2_440_000, 371_950, 328),
        (4_520_000, 2_620_000, 368_150, 376),
        (3_750_000, 2_460_000, 386_350, 330),
        (6_350_000, 3_610_000, 509_750, 524),
        (6_480_000, 3_770_000, 429_950, 535),
    ],
)
def test_the_six_good_readings_all_pass(metal: int, crystal: int, gas: int, ships: int) -> None:
    """实拍 8 封里读对的那六封，偏差都 ≤0.2%。"""
    assert haul_is_consistent(_haul(metal, crystal, gas, ships))


@pytest.mark.parametrize(
    ("metal", "crystal", "gas", "ships", "why"),
    [
        (3_070_000, 1_000_000, 280_250, 273, "晶体丢了首位：1M 应该是 2.11M 那一档"),
        (413_000_000, 2_800_000, 428_200, 368, "金属丢了小数点：413M 应该是 4.13M"),
    ],
)
def test_the_two_bad_readings_are_rejected(
    metal: int, crystal: int, gas: int, ships: int, why: str
) -> None:
    """⚠️⚠️ **这两封是这条判据存在的全部理由。**

    它们肉眼看着都像真数，`金属 > 晶体 > 气体` 也都成立——只有容量对不上。
    实测偏差 20.3% 与 5555%，而好的六封 ≤0.2%，中间隔着两个数量级。
    """
    assert not haul_is_consistent(_haul(metal, crystal, gas, ships)), why


def test_monotonic_alone_would_have_let_both_bad_readings_through() -> None:
    """⚠️ 用户给的「金属 > 晶体 > 气体」**挡不住错数**，所以它不能当闸门。

    这一条钉的就是「为什么还要容量不变量」——少了它，下面两封会被当成真数
    记进日收入，而那是**静默**的：页面上只会多出一笔不存在的收入。
    """
    assert looks_monotonic(_haul(3_070_000, 1_000_000, 280_250, 273))
    assert looks_monotonic(_haul(413_000_000, 2_800_000, 428_200, 368))


def test_the_capacity_is_self_calibrating_not_hardcoded() -> None:
    """⚠️ **每艘容量不许写死。** 货舱科技一升它就变。

    传入实测中位数时，同一封在旧常量下会被拒、在新容量下照过——
    这正是「科技升级那天不会把每一封都判成读失败」的意思。
    """
    upgraded = _haul(6_000_000, 3_000_000, 300_000, 310)  # 每艘约 30,000

    assert not haul_is_consistent(upgraded)
    assert haul_is_consistent(upgraded, capacity_per_ship=30_000)
    # 默认值仍然是那个标定出来的数，改动它要有实测。
    assert NOMINAL_SHIP_CAPACITY == 20_000


def test_a_haul_without_ships_is_rejected_rather_than_divided_by_zero() -> None:
    """船数读不出来时**拒绝**，不是放行——没有船数就没有这条闸门。"""
    assert not haul_is_consistent(_haul(4_000_000, 2_000_000, 200_000, 0))


# -- 投票 ------------------------------------------------------------------------


def test_the_vote_takes_the_majority_because_the_noise_moves() -> None:
    """⚠️ 背景那层漂浮文字每帧不一样，所以它的垃圾各不相同、真数字每帧都一样。

    实测同一封信 5 帧之间变了 255→1157 个像素；单帧 OCR 8 格错 2 格。
    """
    assert vote(["6.04M", "6.04M", "5.04M", "6.04M", "6.04M"]) == "6.04M"


def test_the_vote_gives_none_when_nothing_was_read() -> None:
    """全读不出时 None ⇒ 整封作废。**不给兜底值**：0 和「没读出来」是两件事。"""
    assert vote([]) is None
    assert vote(["", "   "]) is None


# -- 三格全有或全无 ---------------------------------------------------------------


def test_all_three_amounts_are_required() -> None:
    """⚠️ 少一格在库里长得和「那一格是 0」一模一样，而这里少一格就是少三分之一收入。"""
    assert parse_amounts(("4.42M", "3.21M", "501.05K")) is not None
    assert parse_amounts(("4.42M", "", "501.05K")) is None
    assert parse_amounts(("4.42M", "3.21M", "看不清")) is None


def test_the_abbreviated_values_are_marked_approximate() -> None:
    """`4.42M` 是近似值，页面要标「约」。**接受误差不等于可以显示得像精确值。**"""
    parsed = parse_amounts(("4.42M", "3.21M", "501.05K"))

    assert parsed is not None
    assert all(item.approximate for item in parsed)
