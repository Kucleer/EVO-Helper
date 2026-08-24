"""备胎：航线预算之外多配的目标，用来顶替撞上保护期的那些。

用户口径（2026-08-24）：「如果目标是保护状态无法攻击，也需要继续根据军力列表进行
攻击。原则上这次的攻击必须发出去，并且需要是新鲜的数据」，以及「先按 2 倍来执行」。

保护期**只能撞上了才知道**（游戏的 8 小时保护期任何人打过都会触发）。而分配阶段
原先只按航线数配同样多的目标，于是两条航线配两个目标、两个都在保护期时这一轮就
空转——实测 2026-08-18 20:29 那一轮当场确认四个目标全在保护期、11.5 分钟一发未发。
"""

from __future__ import annotations

from datetime import UTC, datetime

from evo_helper.domain.military_attack import (
    MILITARY_SPARE_FACTOR,
    AttackOrigin,
    assign_by_capacity_and_value,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)
PRESET = "BBB"


def _target(system: int, score: float) -> ScoredTarget:
    return ScoredTarget(
        coordinate=Coordinate(galaxy=4, system=system, position=5),
        military_score=score,
        military_score_at_utc=NOW,
    )


def _origin(lines: int) -> AttackOrigin:
    return AttackOrigin(coordinate=Coordinate(galaxy=4, system=277, position=15), fleet_lines=lines)


def test_the_default_stays_at_one_target_per_line() -> None:
    """不传 `spare_factor` 就是 2026-08-24 之前的行为：一条航线一个目标。

    ⚠️ 默认值留在 1 是有意的：备胎该配几个是**调用方**的决定（应用层按
    `MILITARY_SPARE_FACTOR` 传），而这个函数本身不替谁做那个决定。
    """
    assigned = assign_by_capacity_and_value(
        [_target(100, 9000), _target(200, 8000), _target(300, 7000)],
        [_origin(2)],
        fallback_preset=PRESET,
    )

    assert len(assigned) == 2
    assert [item.reserve for item in assigned] == [False, False]


def test_two_times_gives_each_line_one_spare() -> None:
    """2 倍 = 每条航线备一个。"""
    assigned = assign_by_capacity_and_value(
        [_target(100, 9000), _target(200, 8000), _target(300, 7000), _target(400, 6000)],
        [_origin(2)],
        fallback_preset=PRESET,
        spare_factor=2,
    )

    assert len(assigned) == 4
    assert sum(1 for item in assigned if not item.reserve) == 2
    assert sum(1 for item in assigned if item.reserve) == 2


def test_adding_spares_does_not_change_who_the_primaries_are() -> None:
    """⚠️⚠️ **加备胎不许改变正选是谁，也不许改变它们的次序。**

    这是这个改动唯一必须守住的安全性质：备胎是为了「撞上保护期还能往下顶」加的，
    它不该顺带把这一轮本来要打谁给动了。所以基准就是**不配备胎那一版的结果**。

    ⚠️ 顺带钉住「组内先正选、后备胎」：runner 是按命令行上的次序往下试的，
    备胎混在前面就等于用价值低的顶掉了价值高的——「按得分出击」会在最后一步被
    悄悄抹掉，正如从前「按军力截断」被第 5 步按距离重排抹掉过一次。

    ⚠️ 别在这里断言「正选就是军力最高的那两个」。挑选按的是
    **`军力 ÷ 往返小时`**，不是军力本身——第一版就这么写错了：`4:100:5` 军力 9000
    但距离 177，价值不如 `4:300:5`（军力 7000、距离 23）。
    """
    pool = [_target(100, 9000), _target(200, 8000), _target(300, 7000), _target(400, 6000)]
    baseline = assign_by_capacity_and_value(pool, [_origin(2)], fallback_preset=PRESET)

    assigned = assign_by_capacity_and_value(
        pool, [_origin(2)], fallback_preset=PRESET, spare_factor=2
    )

    assert [item.reserve for item in assigned] == [False, False, True, True]
    primaries = [item for item in assigned if not item.reserve]
    assert [item.coordinate for item in primaries] == [item.coordinate for item in baseline]
    assert [item.preset for item in primaries] == [item.preset for item in baseline]


def test_a_thin_pool_simply_yields_fewer_spares() -> None:
    """池子不够就少配几个备胎，**正选一个都不许少**。

    钉的是次序：贪心先把正选填满。反过来的话，池子只有 3 个、航线 2 条时可能变成
    「1 个正选 + 2 个备胎」——而备胎不计入 `budget`，于是这一轮只派 1 发。
    """
    assigned = assign_by_capacity_and_value(
        [_target(100, 9000), _target(200, 8000), _target(300, 7000)],
        [_origin(2)],
        fallback_preset=PRESET,
        spare_factor=2,
    )

    assert sum(1 for item in assigned if not item.reserve) == 2
    assert sum(1 for item in assigned if item.reserve) == 1


def test_spares_are_counted_per_origin() -> None:
    """每颗出发星球各按自己的航线数配备胎，不是全局摊一份。"""
    first = Coordinate(galaxy=4, system=277, position=15)
    second = Coordinate(galaxy=9, system=250, position=8)
    assigned = assign_by_capacity_and_value(
        [_target(system, 9000 - system) for system in (100, 200, 300, 400, 500, 600)],
        [
            AttackOrigin(coordinate=first, fleet_lines=1),
            AttackOrigin(coordinate=second, fleet_lines=2),
        ],
        fallback_preset=PRESET,
        spare_factor=2,
    )

    for origin, lines in ((first, 1), (second, 2)):
        group = [item for item in assigned if item.origin == origin]
        assert sum(1 for item in group if not item.reserve) == lines, origin
        assert sum(1 for item in group if item.reserve) == lines, origin


def test_a_factor_below_one_is_refused() -> None:
    """0 或负数会让「一条航线一个目标」这条底线失效，当场拒绝而不是悄悄当成 1。"""
    try:
        assign_by_capacity_and_value(
            [_target(100, 9000)], [_origin(1)], fallback_preset=PRESET, spare_factor=0
        )
    except ValueError as error:
        assert "spare_factor" in str(error)
    else:  # pragma: no cover - 上面必须抛
        raise AssertionError("spare_factor=0 应该被拒绝")


def test_the_configured_factor_is_two() -> None:
    """用户口径（2026-08-24）：「先按 2 倍来执行」。

    钉住这个数本身，是因为它是**运维决定**而不是推导出来的——改它要有新的口径。
    """
    assert MILITARY_SPARE_FACTOR == 2
