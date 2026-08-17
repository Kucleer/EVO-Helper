"""Military ranking snapshot ingestion and fast-filter API."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.ranking import RankingRow, coordinate_of
from evo_helper.storage.military_rankings import (
    BOARD_WINDOW_HOURS,
    BoardDirection,
    BoardSort,
    BoardWindow,
    MilitaryRankingRepository,
)


class RankingEntryIn(BaseModel):
    rank: int | None = Field(default=None, ge=1)
    name: str = Field(min_length=1, max_length=128)
    score: float | None = Field(default=None, ge=0)
    #: 这一行**是什么时候读到的**。逐屏采集的调用方应当逐行给出自己那一屏的
    #: 截屏时刻；不给则回落到整个快照的 `captured_at_utc`（见
    #: `storage.military_rankings.append_snapshot`）。留成可选是为了不废掉
    #: 已有的「整榜一次 POST」用法。
    observed_at_utc: datetime | None = None


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
        for row in payload.rows:
            if row.observed_at_utc is not None and row.observed_at_utc.tzinfo is None:
                raise ValueError("observed_at_utc must include a timezone")
        snapshot_id = repository.append_snapshot(
            [
                RankingRow(
                    row.rank,
                    row.name,
                    row.score,
                    coordinate_of(row.name),
                    observed_at_utc=(
                        None if row.observed_at_utc is None else row.observed_at_utc.astimezone(UTC)
                    ),
                )
                for row in payload.rows
            ],
            captured_at_utc=payload.captured_at_utc.astimezone(UTC),
        )
        return {"snapshot_id": str(snapshot_id)}

    @router.get("")
    async def board(
        rank_min: int | None = Query(default=None, ge=1),
        rank_max: int | None = Query(default=None, ge=1),
        score_min: float | None = Query(default=None, ge=0),
        score_max: float | None = Query(default=None, ge=0),
        galaxy: int | None = Query(default=None, ge=1, le=9),
        q: str | None = None,
        sort: BoardSort = Query(default="observed_at"),
        direction: BoardDirection = Query(default="desc"),
        window: BoardWindow = Query(default="24h"),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        """当前军力榜。**读 `bot_targets`，不读快照表。**

        快照表没有活着的写入方，页面读它等于永远显示迁移播种时那一份（详见
        `storage.military_rankings` 的模块头）。这里读的是扫描逐屏写进去的实时数据，
        每行自带自己的读取时刻。

        默认按「更新时间」倒序、只出最近 24 小时的行（用户口径 2026-08-17）。
        `window=all` 放开时间窗——排障时要看更早的数据。

        `sort` / `direction` / `window` 声明成 `Literal`：不认识的值由 FastAPI 当场
        422，压根到不了 SQL 那一层。白名单本身在 `storage.military_rankings`，这里
        只是把同一份 `Literal` 引过来，免得两处各写一份、日后分家。

        没有 `kind` / `bot_only` 参数：这张榜按构造只可能有 bot。
        """
        page = repository.live_board(
            rank_min=rank_min,
            rank_max=rank_max,
            score_min=score_min,
            score_max=score_max,
            galaxy=galaxy,
            query=q,
            sort=sort,
            direction=direction,
            window_hours=BOARD_WINDOW_HOURS[window],
            offset=offset,
            limit=limit,
        )
        return {
            "refreshed_at_utc": page.refreshed_at_utc,
            # 当前时间窗的下界（`window=all` 时是 null）。页面靠它把「命中 N 条」
            # 说成「最近 24 小时命中 N 条」，不然这个数会被当成库里的全部。
            "window_start_utc": page.window_start_utc,
            "total": page.total,
            "rows": [
                {
                    "rank": row.rank,
                    "name": row.name,
                    "score": row.score,
                    # 行级的「这条数据是什么时候读到的」。页面上按 UTC+8 显示。
                    "observed_at_utc": row.observed_at_utc,
                    "coordinate": str(row.coordinate),
                    # 插值补出来的军力值必须标出来，不能和实读的长得一样。
                    "estimated": row.estimated,
                    "source": row.source,
                }
                for row in page.rows
            ],
        }

    app.include_router(router)
