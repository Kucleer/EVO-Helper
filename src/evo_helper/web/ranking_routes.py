"""Military ranking snapshot ingestion and fast-filter API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.ranking import RankingRow, coordinate_of, is_bot_coordinate

# 故意不从 `domain.ranking` 取 `PIRATE_POSITIONS`：那边只是为了自己用而转手 import，
# 没有再导出，strict mypy 的 `no_implicit_reexport` 会拒绝。直接从定义它的模块取。
# 同一条成例写在 `domain/missions.py` 的 import 段里。
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS
from evo_helper.storage.military_rankings import MilitaryRankingRepository


class RankingEntryIn(BaseModel):
    rank: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=128)
    score: float | None = Field(default=None, ge=0)


class RankingSnapshotIn(BaseModel):
    captured_at_utc: datetime
    rows: list[RankingEntryIn] = Field(min_length=1, max_length=10000)


def register_ranking_routes(app: FastAPI, session_factory: sessionmaker[Session]) -> None:
    repository = MilitaryRankingRepository(session_factory)
    router = APIRouter(prefix="/api/military-rankings", tags=["military-rankings"])

    @router.post("/snapshots", status_code=201)
    async def append_snapshot(payload: RankingSnapshotIn) -> dict[str, str]:
        if payload.captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must include a timezone")
        snapshot_id = repository.append_snapshot(
            [
                RankingRow(row.rank, row.name, row.score, coordinate_of(row.name))
                for row in payload.rows
            ],
            captured_at_utc=payload.captured_at_utc.astimezone(UTC),
        )
        return {"snapshot_id": str(snapshot_id)}

    @router.get("")
    async def latest(
        rank_min: int | None = Query(default=None, ge=1),
        rank_max: int | None = Query(default=None, ge=1),
        score_min: float | None = Query(default=None, ge=0),
        score_max: float | None = Query(default=None, ge=0),
        galaxy: int | None = Query(default=None, ge=1, le=9),
        bot_only: bool = False,
        kind: str = Query(default="all", pattern="^(all|bot|pirate|player)$"),
        q: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        page = repository.latest(
            rank_min=rank_min,
            rank_max=rank_max,
            score_min=score_min,
            score_max=score_max,
            galaxy=galaxy,
            bot_only=bot_only,
            kind=kind,
            query=q,
            offset=offset,
            limit=limit,
        )
        return {
            "snapshot_id": None if page.snapshot_id is None else str(page.snapshot_id),
            "captured_at_utc": page.captured_at_utc,
            "total": page.total,
            "rows": [
                {
                    "rank": row.rank,
                    "name": row.name,
                    "score": row.score,
                    "coordinate": None if row.coordinate is None else str(row.coordinate),
                    "is_bot": is_bot_coordinate(row.coordinate),
                    "kind": (
                        "bot"
                        if is_bot_coordinate(row.coordinate)
                        else "pirate"
                        if (
                            row.coordinate is not None
                            and row.coordinate.position in PIRATE_POSITIONS
                        )
                        else "player"
                    ),
                }
                for row in page.rows
            ],
        }

    app.include_router(router)
