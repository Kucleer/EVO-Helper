"""飞行时间模型的护栏。**每一条钉的都是「算错了也不报错」的那种。**

这个模块的产出只喂给一个地方：选靶得分的分母（`domain.target_order.attack_value`）。
算错了不会抛异常、不会在页面上留痕，只会让一夜的航线安静地排错顺序——
所以判据必须在这里钉死。

模型与实测证据在 `domain.flight_time` 与 `domain.distance` 的模块头上。
"""

from __future__ import annotations

from evo_helper.domain.flight_time import distance_units, one_way_seconds, round_trip_hours
from evo_helper.domain.models import Coordinate

HOME = Coordinate(2, 137, 18)


def test_the_cross_galaxy_times_match_the_four_measured_points() -> None:
    """⚠️ **四个实测点（用户 2026-08-14，从 2 系出发）必须全中，误差 ±1 秒。**

    | 目标银河 | 环形银河距 | 实测单程秒 |
    |---|---|---|
    | 3 | 1 | 3752 |
    | 9 | 2 | 5305 |
    | 8 | 3 | 6497 |
    | 6 | 4 | 7502 |

    这四个数是整条链路里唯一一批「不靠推理、直接量出来」的事实。把系数改成
    「差不多」的另一个数，只有这条用例会红。
    """
    measured = {3: 3752, 9: 5305, 8: 6497, 6: 7502}

    for galaxy, seconds in measured.items():
        actual = one_way_seconds(Coordinate(galaxy, 250, 1), HOME)
        assert abs(actual - seconds) <= 1.0, f"{galaxy} 系应当是 {seconds} 秒，算出来 {actual:.0f}"


def test_a_galaxy_hop_goes_round_the_ring_not_by_subtraction() -> None:
    """⚠️ **从 2 系去 9 系是两步，不是七步。**

    写成 `abs(9 - 2)` 不会报错：9 系会被算成 9924 秒（实测 5305），于是它的得分被
    压掉将近一半，一夜都轮不到——而页面上看不出任何异常。

    这里同时钉住那条**判别性**证据：`2→6` 正着走 4 步、`2→7` 倒着走 4 步，
    环形说两者一模一样，线性说两者应当不同。实测（飞行时间与气体消耗）选了环形。
    """
    assert one_way_seconds(Coordinate(9, 250, 1), HOME) < one_way_seconds(
        Coordinate(6, 250, 1), HOME
    ), "9 系是第二近的银河，线性减法会把它排到最远"
    assert one_way_seconds(Coordinate(6, 250, 1), HOME) == one_way_seconds(
        Coordinate(7, 250, 1), HOME
    ), "环形距离都是 4，实测飞行时间一模一样"


def test_a_system_hop_goes_round_the_ring_too() -> None:
    """⚠️ **从 2:137 去 2:499 是 137 步，不是 362 步。**

    实测：`2:499`（1969 秒）比线性差只有 150 的 `2:287`（2042 秒）**还快 73 秒**。
    任何单调的线性模型都给不出这个。
    """
    assert one_way_seconds(Coordinate(2, 499, 1), HOME) < one_way_seconds(
        Coordinate(2, 287, 1), HOME
    )
    # `2:499` 与 `2:1` 在环上相邻（137 步 vs 136 步），实测只差 5 秒。
    gap = one_way_seconds(Coordinate(2, 499, 1), HOME) - one_way_seconds(Coordinate(2, 1, 1), HOME)
    assert 0 < gap < 10, "环上相邻的两个恒星系只该差一个步长"


def test_the_same_galaxy_times_match_the_three_measured_points() -> None:
    """同银河三个实测点（用户 2026-08-14，从 2:137 出发），误差 ±1 秒。"""
    measured = {287: 2042, 499: 1969, 1: 1964}

    for system, seconds in measured.items():
        actual = one_way_seconds(Coordinate(2, system, 1), HOME)
        assert abs(actual - seconds) <= 1.0, f"2:{system} 应当是 {seconds} 秒，算出来 {actual:.0f}"


def test_the_system_number_drops_out_once_you_cross_a_galaxy() -> None:
    """跨银河之后恒星系号**不进算式**。

    实测（用户 2026-08-14）：三个恒星系号各不相同的跨银河目标，一个只含银河环距
    的函数把它们全部命中在 2 秒内。把恒星系那一段也加进跨银河的算式，这条会红。
    """
    far_side = {distance_units(Coordinate(9, system, 1), HOME) for system in (1, 137, 250, 499)}

    assert len(far_side) == 1


def test_a_round_trip_is_exactly_twice_the_one_way() -> None:
    """59 发实测，三个银河距档完全一致：一次派遣占住航线的时长 = 单程 × 2.00。"""
    target = Coordinate(2, 200, 3)

    assert round_trip_hours(target, HOME) * 3600 == one_way_seconds(target, HOME) * 2


def test_the_round_trip_is_never_zero_so_it_can_be_a_denominator() -> None:
    """得分拿它当分母（`军力 ÷ 往返小时`），所以它必须恒大于 0。

    最近的目标也要飞一千多个距离单位，加上 2 秒的起降开销——连「打自己」都是正数。
    """
    assert round_trip_hours(HOME, HOME) > 0
