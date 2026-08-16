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
