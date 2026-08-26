"""候选池按银河拆开：分档、成色、以及三格共用的那道新鲜度闸。

用户口径（2026-08-26，逐字）：

    「我是让你改往返时长那个标签，改成按星系」
    「候选池 · 按星系 这里的数据范围也要新鲜度一致」
    「统一为读取攻击配置，跟着配置走。我这里要看的就是动态数据来让我决策的」
    「记得你是需要按各个 bot 星球去单独做除法，再合计。而不是反过来」

最后那一句是这个文件里最要紧的一条：成色是 `mean(军力ᵢ ÷ 往返ᵢ)`，
**不是** `Σ军力 ÷ Σ往返`。两者在真实数据上差得很远，而且都是「看着合理」的式子。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.application.mission_scheduler import ConfiguredOrigin
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget
from evo_helper.web.overview_routes import (
    POOL_BUCKETS,
    _fresh_enough,
    _galaxy_freshness,
    _pool_by_galaxy,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
HOME = Coordinate(4, 277, 15)


def _target(coordinate: Coordinate, score: float, *, hours_ago: float = 0.0) -> ScoredTarget:
    return ScoredTarget(
        coordinate=coordinate,
        military_score=score,
        military_score_at_utc=NOW - timedelta(hours=hours_ago),
    )


def _origin(coordinate: Coordinate = HOME, *, lines: int = 2, enabled: bool = True):  # type: ignore[no-untyped-def]
    return ConfiguredOrigin(coordinate=coordinate, fleet_lines=lines, enabled=enabled)


def _one(targets, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("configured", (_origin(),))
    kwargs.setdefault("enabled_tasks", (_origin(),))
    kwargs.setdefault("floor", 500)
    return _pool_by_galaxy(targets, **kwargs)


# -- 新鲜度闸：三格共用的那一份 ---------------------------------------------------


def test_only_readings_inside_the_window_survive() -> None:
    """⚠️ 窗口之外的读数一律不算。

    池子里有几千个读数早就过期的目标 —— 选靶那一步根本不认它们
    （`score_is_fresh`），把它们算进来，这几格会报出一个攻击链此刻用不上的数。
    """
    kept = _fresh_enough(
        [
            _target(Coordinate(4, 1, 1), 100.0, hours_ago=0.5),
            _target(Coordinate(4, 2, 1), 100.0, hours_ago=9.0),
        ],
        now_utc=NOW,
        window=timedelta(hours=3),
    )

    assert [t.coordinate.system for t in kept] == [1]


def test_a_target_that_was_never_on_the_board_is_not_fresh() -> None:
    """⚠️ 从没上过榜（`military_score_at_utc` 是 None）**不等于**新鲜。

    `ScoredTarget` 那一行注释写着「None 表示榜单从未见过，不伪造『新鲜』」。
    少了这一档，那 700 多个没有军力读数的目标会混进成色里 —— 而它们连军力都没有。
    """
    kept = _fresh_enough(
        [ScoredTarget(coordinate=Coordinate(4, 1, 1), military_score=None)],
        now_utc=NOW,
        window=timedelta(hours=3),
    )

    assert kept == ()


def test_the_freshness_card_counts_the_same_targets() -> None:
    """⚠️⚠️ 「各银河新鲜读数」那一格**从同一份上数**，不另查一遍库。

    2026-08-26 实测：那一格从前直接数 `bot_targets`（一条排除都不带），而候选池那格
    排掉了近期打过的、撞过保护期的。两张卡摆在一起、数的却不是同一批东西 ——
    而当天两边数字**碰巧一模一样**（891 = 891），因为刚扫到的 bot 恰好都还在池子里。
    **巧合不是保证**，所以口径要靠「同一个元组」而不是「两处各写一遍筛选」来保证。
    """
    fresh = (_target(Coordinate(4, 1, 1), 100.0), _target(Coordinate(9, 1, 1), 100.0))

    assert [(g.galaxy, g.fresh) for g in _galaxy_freshness(fresh)] == [(4, 1), (9, 1)]


# -- 成色 ------------------------------------------------------------------------


def test_quality_divides_per_bot_then_averages() -> None:
    """⚠️⚠️ **每个 bot 各自「军力 ÷ 往返」，再取均值。不是总军力 ÷ 总往返。**

    用户口径（2026-08-26）：「记得你是需要按各个 bot 星球去单独做除法，再合计。
    而不是反过来」。

    ⚠️ 这一条的构造是**刻意让两个式子给出不同答案**的：一个近而瘦、一个远而肥。
    `Σ军力 ÷ Σ往返` 会被那个又远又肥的拉高，而选靶真正按的是逐个算出来的价值
    （`domain.target_order` 那条「军力 ÷ 往返小时」）——页面要和派遣同一个口径，
    否则它排在最上面的银河并不是调度器下一轮真会挑的那个。

    两个目标同在 4 系、出发点就是 4 系那颗星球，往返时长由坐标决定，所以这里只断言
    「逐个算」与「先合计再算」不相等，且拿到的是前者。
    """
    from evo_helper.domain.flight_time import round_trip_hours

    near = Coordinate(4, 278, 3)
    far = Coordinate(4, 460, 9)
    targets = (_target(near, 1_000.0), _target(far, 90_000.0))

    per_bot = (
        sum(score / round_trip_hours(at, HOME) for at, score in ((near, 1_000.0), (far, 90_000.0)))
        / 2
    )
    lumped = 91_000.0 / (round_trip_hours(near, HOME) + round_trip_hours(far, HOME))
    assert abs(per_bot - lumped) > 1.0, "构造没造出差别，这条用例证明不了任何事"

    (row,) = _one(targets)

    assert row.quality is not None
    assert abs(row.quality - per_bot) < 0.5
    assert abs(row.quality - lumped) > 1.0


def test_only_the_top_floor_many_count_towards_quality() -> None:
    """⚠️ 取前 N 的 N 是**攻击配置里的窗口门限**，不是页面自己定的数。

    用户口径（2026-08-26）：「前 100 应该取任务的门限与新鲜读数一致」。
    页面自己写死一个 100 的话，用户在攻击配置页把门限改了，成色的口径却不动，
    页面上的排序就和派遣真正会挑的次序对不上。

    构造：三个目标、门限 1 —— 成色只该反映最肥的那一个。
    """
    targets = tuple(
        _target(Coordinate(4, 278 + i, 3), score) for i, score in enumerate((10.0, 99_000.0, 20.0))
    )

    (row,) = _one(targets, floor=1)

    assert row.counted == 1
    assert row.quality is not None and row.quality > 10_000


def test_a_floor_larger_than_the_pool_just_takes_everything() -> None:
    """门限比池子还大时取全部 —— 生产上就是这样（门限 500，各银河新鲜读数几十个）。"""
    targets = (_target(Coordinate(4, 278, 3), 100.0), _target(Coordinate(4, 279, 3), 200.0))

    (row,) = _one(targets, floor=500)

    assert row.counted == 2


# -- 分档与排序 ------------------------------------------------------------------


def test_the_bands_line_up_with_the_configured_buckets() -> None:
    """档数比 `POOL_BUCKETS` 多一个 —— 最后一档是「以上」，页面表头也照这个数列。"""
    (row,) = _one((_target(Coordinate(4, 278, 3), 100.0),))

    assert len(row.bands) == len(POOL_BUCKETS) + 1
    assert sum(row.bands) == 1


def test_a_galaxy_without_a_planet_is_still_listed(  # noqa: D103
) -> None:
    """⚠️⚠️ **没配星球、任务停用的银河照样列出来。**

    用户口径（2026-08-26）：「候选池不用跟着走，我就是根据候选池的情况来调整攻击
    航路以达到最大化」。要判断「9 系那条航路值不值得开」，就得先看得见它开了之后
    有多少目标、成色如何 —— 跟着「未启用不显示」走的话，那个问题再也问不出来。
    """
    targets = (_target(Coordinate(4, 278, 3), 100.0), _target(Coordinate(9, 250, 1), 100.0))

    rows = _one(targets)

    listed = {row.galaxy: row for row in rows}
    assert set(listed) == {4, 9}
    assert listed[9].configured is False
    assert listed[9].lines == 0


def test_a_disabled_galaxy_keeps_its_line_count(  # noqa: D103
) -> None:
    """停用的银河要能看出**它配着几条线** —— 那正是「要不要把线挪走」的分母。"""
    off = _origin(Coordinate(9, 250, 8), lines=3, enabled=True)
    rows = _one(
        (_target(Coordinate(9, 250, 1), 100.0),),
        configured=(_origin(), off),
        enabled_tasks=(_origin(),),  # 9 系那条任务停用了，没出现在这一份里
    )

    (row,) = [r for r in rows if r.galaxy == 9]
    assert row.configured is True
    assert row.enabled is False
    assert row.lines == 3


def test_rows_come_back_richest_first() -> None:
    """⚠️ 按成色降序。这一格是拿来挑「该给谁加线」的，最肥的要在最上面。

    并列时按银河号定序 —— 次序不定的话同一份数据每次刷新排法都可能不同，
    而这一页 5 秒一轮。
    """
    targets = (
        _target(Coordinate(4, 278, 3), 100.0),
        _target(Coordinate(9, 250, 1), 90_000.0),
    )

    rows = _one(targets, configured=(_origin(), _origin(Coordinate(9, 250, 8))))

    assert [row.galaxy for row in rows] == [9, 4]


def test_nothing_comes_back_when_no_planet_is_configured() -> None:
    """⚠️ 一颗星球都没配时整格交空，不摆一个算不出往返时长的假表。

    同 `_pool_view` 那一支：没有出发点就没有「往返时长」这个概念，
    硬凑一个出来只会让人以为那些数字有意义。
    """
    assert _one((_target(Coordinate(4, 278, 3), 100.0),), configured=(), enabled_tasks=()) == ()
