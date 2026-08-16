"""Persistence and latest-snapshot filtering for military ranking rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow

from . import models as orm


@dataclass(frozen=True)
class MilitaryRankingPage:
    snapshot_id: UUID | None
    captured_at_utc: datetime | None
    rows: tuple[RankingRow, ...]
    total: int


class MilitaryRankingRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append_snapshot(self, rows: list[RankingRow], *, captured_at_utc: datetime) -> UUID:
        if captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        snapshot_id = uuid4()
        with self._session_factory() as session:
            session.add(
                orm.MilitaryRankingSnapshotRow(
                    id=snapshot_id, captured_at_utc=captured_at_utc, row_count=len(rows)
                )
            )
            # SQLite enforces the foreign key immediately; flush the parent
            # before SQLAlchemy batches the child inserts.
            session.flush()
            for ordinal, row in enumerate(rows):
                coordinate = row.coordinate
                session.add(
                    orm.MilitaryRankingEntryRow(
                        snapshot_id=snapshot_id,
                        ordinal=ordinal,
                        rank=row.rank,
                        player_name=row.name,
                        score=row.score,
                        galaxy=None if coordinate is None else coordinate.galaxy,
                        system=None if coordinate is None else coordinate.system,
                        position=None if coordinate is None else coordinate.position,
                    )
                )
            session.commit()
        return snapshot_id

    def latest(
        self,
        *,
        rank_min: int | None = None,
        rank_max: int | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        galaxy: int | None = None,
        bot_only: bool = False,
        query: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> MilitaryRankingPage:
        with self._session_factory() as session:
            snapshot = session.scalar(
                select(orm.MilitaryRankingSnapshotRow)
                .order_by(orm.MilitaryRankingSnapshotRow.captured_at_utc.desc())
                .limit(1)
            )
            if snapshot is None:
                return MilitaryRankingPage(None, None, (), 0)
            statement = select(orm.MilitaryRankingEntryRow).where(
                orm.MilitaryRankingEntryRow.snapshot_id == snapshot.id
            )
            if rank_min is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.rank >= rank_min)
            if rank_max is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.rank <= rank_max)
            if score_min is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.score >= score_min)
            if score_max is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.score <= score_max)
            if galaxy is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.galaxy == galaxy)
            if bot_only:
                statement = statement.where(orm.MilitaryRankingEntryRow.galaxy.is_not(None))
            if query and query.strip():
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.player_name.contains(query.strip())
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        orm.MilitaryRankingEntryRow.rank, orm.MilitaryRankingEntryRow.ordinal
                    )
                ).all()
            )
        return MilitaryRankingPage(
            snapshot.id,
            snapshot.captured_at_utc,
            tuple(
                RankingRow(
                    rank=row.rank,
                    name=row.player_name,
                    score=row.score,
                    coordinate=(
                        None
                        if row.galaxy is None
                        else Coordinate(
                            row.galaxy,
                            _required_coordinate_part(row.system),
                            _required_coordinate_part(row.position),
                        )
                    ),
                )
                for row in rows[offset : offset + limit]
            ),
            len(rows),
        )


def _required_coordinate_part(value: int | None) -> int:
    assert value is not None
    return value
