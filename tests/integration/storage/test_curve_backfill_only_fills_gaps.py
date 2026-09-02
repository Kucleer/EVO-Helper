"""曲线补数写库那一层：**只填空，绝不覆盖，一律标估算。**

用户口径（2026-09-02，逐字）：

    「后续的读到正确的判据，可以对之前没有读出的读数进行回填补数，
     这样只需要几个节点 就能补很多的数据了」

## ⚠️ 为什么这三条必须在仓储这一层钉

补进去的是**估算值**（`domain.ranking.curve_reference` 从邻近名次的可信读数推出来的），
而库里已有的可能是**量出来的**。拿估算盖掉实测，就是这个仓库那条硬规矩
「猜出来的数不许长得像量出来的」的反面。

而 `save_ranking_targets` 恰恰是**无条件覆盖**的（它答的是「这一屏读到了什么」）。
两个入口分开就是因为**写错方向的代价完全不同** —— 合成一个迟早有人给它传错一批记录。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.storage.repository import RankingTarget, SqlAlchemyRepository

NOW = datetime(2026, 9, 2, 2, 0, tzinfo=UTC)
MEASURED = Coordinate(4, 300, 7)
EMPTY = Coordinate(4, 301, 8)
UNSEEN = Coordinate(4, 302, 9)


def _target(
    coordinate: Coordinate,
    score: float | None,
    *,
    estimated: bool = False,
    rank: int | None = None,
    at_utc: datetime = NOW,
) -> RankingTarget:
    return RankingTarget(
        coordinate=coordinate,
        military_score=score,
        military_score_at_utc=at_utc,
        military_score_estimated=estimated,
        military_rank=rank,
    )


def _row(repository: SqlAlchemyRepository, coordinate: Coordinate):  # type: ignore[no-untyped-def]
    return next(
        row
        for row in repository.list_bot_targets()
        if Coordinate(row.galaxy, row.system, row.position) == coordinate
    )


def test_an_empty_row_gets_filled_and_marked_as_an_estimate(
    repository: SqlAlchemyRepository,
) -> None:
    """空着的那一行补上，并且**标成估算**。"""
    repository.save_ranking_targets([_target(EMPTY, None, rank=1604)])

    assert repository.backfill_missing_military_scores([_target(EMPTY, 9500.0, rank=1604)]) == 1

    row = _row(repository, EMPTY)
    assert row.military_score == 9500.0
    assert row.military_score_estimated is True


def test_a_measured_reading_is_never_overwritten(repository: SqlAlchemyRepository) -> None:
    """⚠️⚠️ **本文件最要紧的一条：量出来的值一个字都不许动。**

    补数拿的是推算值。盖掉实测就等于把「量到的 9,800」换成「猜的 9,500」，
    而页面和选靶都看不出差别——`military_score_estimated` 还会被一并改成 True，
    连「这是猜的」这条线索都留不下。
    """
    repository.save_ranking_targets([_target(MEASURED, 9800.0, rank=1600)])

    assert repository.backfill_missing_military_scores([_target(MEASURED, 9500.0, rank=1600)]) == 0

    row = _row(repository, MEASURED)
    assert row.military_score == 9800.0, "实测值被估算值盖掉了"
    assert row.military_score_estimated is False


def test_an_earlier_estimate_is_not_overwritten_either(
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ 连**之前补过的估算值**也不再动。

    判据是「有没有值」，不是「值是不是猜的」。反复改写同一行只会让
    `military_score_at_utc` 一路往后飘，而那一列是新鲜度窗口的依据——
    选靶那边会把一个几天没量过的目标当成刚读到的。
    """
    repository.save_ranking_targets([_target(EMPTY, 9400.0, estimated=True, rank=1604)])

    assert repository.backfill_missing_military_scores([_target(EMPTY, 9500.0, rank=1604)]) == 0

    assert _row(repository, EMPTY).military_score == 9400.0


def test_a_coordinate_the_scan_has_never_seen_is_not_created(
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ **不建新行。** 没见过的坐标谈不上「补」，那是扫描该干的事。

    建了的话，一条从没上过榜的坐标会凭空得到一个推算军力值并进入候选池——
    而我们连它是不是 bot 都不知道。
    """
    assert repository.backfill_missing_military_scores([_target(UNSEEN, 9500.0, rank=1604)]) == 0

    assert all(
        Coordinate(row.galaxy, row.system, row.position) != UNSEEN
        for row in repository.list_bot_targets()
    )


def test_a_record_without_a_score_is_skipped(repository: SqlAlchemyRepository) -> None:
    """没算出参照的那些（`curve_reference` 交 None）不该走到这里，走到了也不许写。"""
    repository.save_ranking_targets([_target(EMPTY, None, rank=1604)])

    assert repository.backfill_missing_military_scores([_target(EMPTY, None, rank=1604)]) == 0

    assert _row(repository, EMPTY).military_score is None


def test_the_missing_rank_is_filled_in_but_an_existing_one_is_kept(
    repository: SqlAlchemyRepository,
) -> None:
    """名次空着就顺手补上；已经有名次的不动——那一列是曲线自己的坐标轴。"""
    repository.save_ranking_targets([_target(EMPTY, None, rank=None)])
    repository.backfill_missing_military_scores([_target(EMPTY, 9500.0, rank=1604)])
    assert _row(repository, EMPTY).military_rank == 1604

    repository.save_ranking_targets([_target(MEASURED, None, rank=1600)])
    repository.backfill_missing_military_scores([_target(MEASURED, 9500.0, rank=9999)])
    assert _row(repository, MEASURED).military_rank == 1600


def test_the_moment_comes_from_the_backfill_not_from_the_old_row(
    repository: SqlAlchemyRepository,
) -> None:
    """补上值的同时也把读数时刻推到这一趟——否则它一进库就是「过期的」。

    新鲜度窗口按 `military_score_at_utc` 划线；沿用旧时刻的话，刚补出来的值在选靶
    那边当场就出局，补了等于没补。
    """
    old = NOW - timedelta(days=3)
    repository.save_ranking_targets([_target(EMPTY, None, rank=1604, at_utc=old)])

    repository.backfill_missing_military_scores([_target(EMPTY, 9500.0, rank=1604)])

    assert _row(repository, EMPTY).military_score_at_utc == NOW
