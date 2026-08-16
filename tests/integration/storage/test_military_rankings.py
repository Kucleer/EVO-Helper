from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.storage.military_rankings import MilitaryRankingRepository


def test_latest_snapshot_is_persistent_and_filters_bot_rank_score_and_galaxy(
    session_factory,
) -> None:
    repository = MilitaryRankingRepository(session_factory)
    captured = datetime(2026, 8, 16, 1, tzinfo=UTC)
    repository.append_snapshot(
        [
            RankingRow(10, "human", 99.0, None),
            RankingRow(10, "bot_2_137_1", 91.0, Coordinate(2, 137, 1)),
            RankingRow(11, "bot_2_137_18", 90.0, Coordinate(2, 137, 18)),
            RankingRow(12, "bot_9_250_8", 80.0, Coordinate(9, 250, 8)),
        ],
        captured_at_utc=captured,
    )
    repository.append_snapshot(
        [RankingRow(11, "bot_2_137_18", 95.0, Coordinate(2, 137, 18))],
        captured_at_utc=captured + timedelta(minutes=5),
    )

    page = repository.latest(bot_only=True, galaxy=2, score_min=90, rank_min=11)

    assert page.total == 1
    assert page.rows[0].name == "bot_2_137_18"
    assert page.rows[0].score == 95.0


def test_bot_only_excludes_fixed_pirate_positions(session_factory) -> None:
    repository = MilitaryRankingRepository(session_factory)
    repository.append_snapshot(
        [
            RankingRow(639, "bot_2_137_1", 99.0, Coordinate(2, 137, 1)),
            RankingRow(640, "bot_2_137_5", 98.0, Coordinate(2, 137, 5)),
        ],
        captured_at_utc=datetime(2026, 8, 16, 1, tzinfo=UTC),
    )

    page = repository.latest(bot_only=True)

    assert page.total == 1
    assert [row.coordinate for row in page.rows] == [Coordinate(2, 137, 5)]


def test_kind_filter_distinguishes_players_and_bots(session_factory) -> None:
    repository = MilitaryRankingRepository(session_factory)
    repository.append_snapshot(
        [
            RankingRow(1, "human", 99.0, None),
            RankingRow(2, "bot_2_137_1", 98.0, Coordinate(2, 137, 1)),
            RankingRow(3, "bot_2_137_5", 97.0, Coordinate(2, 137, 5)),
        ],
        captured_at_utc=datetime(2026, 8, 16, 1, tzinfo=UTC),
    )

    assert repository.latest(kind="player").total == 1
    assert repository.latest(kind="bot").rows[0].coordinate == Coordinate(2, 137, 5)
    # ⚠️ 这条曾断言 `kind="pirate"` 取得回 2:137:1。用户口径（2026-08-16）改了：
    # 海盗根本不入库，所以这个筛选项现在永远是空的。
    assert repository.latest(kind="pirate").total == 0


def test_a_pirate_row_never_reaches_the_table(session_factory) -> None:
    """**海盗在入库口就被丢掉**，而真人行照留。

    1--4 号位是游戏固定的海盗，不是 bot 攻击目标，留在榜里只会虚增行数、
    把 `kind` 筛选和「已扫多少 bot」一起算歪。

    ⚠️ 判据只能是**位号**：2026-08-16 实测海盗 avg 7,581 / max 43,260，bot
    avg 7,830 / max 93,920，两者军力分布基本重合，按分数根本认不出来。
    所以这里特意用一个高分海盗（43,260）和一个低分 bot（97）做样本。

    挡在入库口而不是页面上——页面过滤下一次扫描就写回来了。
    """
    repository = MilitaryRankingRepository(session_factory)
    repository.append_snapshot(
        [
            RankingRow(1, "human", 99.0, None),
            RankingRow(2, "bot_7_495_1", 43260.0, Coordinate(7, 495, 1)),
            RankingRow(3, "bot_8_471_3", 18340.0, Coordinate(8, 471, 3)),
            RankingRow(4, "bot_2_137_5", 97.0, Coordinate(2, 137, 5)),
        ],
        captured_at_utc=datetime(2026, 8, 16, 1, tzinfo=UTC),
    )

    page = repository.latest()

    assert [row.name for row in page.rows] == ["human", "bot_2_137_5"]
    assert page.total == 2
