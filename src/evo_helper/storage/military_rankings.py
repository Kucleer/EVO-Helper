"""Persistence and latest-snapshot filtering for military ranking rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS

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
        """把一次读榜的结果落库。**海盗行在这里就丢掉，不进库。**

        用户口径（2026-08-16）：「军力榜需要删除类型是 pirate 的数据，这是海盗不是
        bot」。1--4 号位是游戏固定生成的海盗，名字虽然也长成 `bot_7_495_1`，但它
        不是 bot 攻击的目标——`is_bot_coordinate` 早就把它挡在目标池外，所以它在
        榜里唯一的作用就是虚增行数、把 `kind` 筛选和「已扫多少 bot」一起算歪。

        ⚠️ **不能靠军力值把它认出来。** 2026-08-16 实测：海盗 100 行 avg 7,581、
        max 43,260；bot 1,705 行 avg 7,830、max 93,920——两者分布基本重合，海盗
        既不是榜首也不是异常值。唯一可靠的判据是位号。

        丢在入库口而不是在页面上过滤：过滤只是眼不见，下一次扫描又会写回来。
        真人行（坐标反解不出来、`coordinate is None`）不受影响——这里只挡海盗位。
        """
        if captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        rows = [
            row
            for row in rows
            if row.coordinate is None or row.coordinate.position not in PIRATE_POSITIONS
        ]
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
        kind: str = "all",
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
            # `bot_only` 保留给旧 API 调用；新的页面用单一下拉枚举，不再把
            # 海盗（固定 1–4 位）误当成 bot。
            effective_kind = "bot" if bot_only else kind
            if effective_kind == "bot":
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.galaxy.is_not(None),
                    orm.MilitaryRankingEntryRow.position.not_in(PIRATE_POSITIONS),
                )
            elif effective_kind == "pirate":
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.galaxy.is_not(None),
                    orm.MilitaryRankingEntryRow.position.in_(PIRATE_POSITIONS),
                )
            elif effective_kind == "player":
                statement = statement.where(orm.MilitaryRankingEntryRow.galaxy.is_(None))
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
