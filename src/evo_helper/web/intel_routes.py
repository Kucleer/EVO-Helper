"""Intel search and saved-filter routes.

Filtering runs on the server: the request carries a coordinate span and a
condition tree, and the response carries one row per matching target with its
latest defender snapshot summary. The browser never receives fleet history it
would have to filter itself.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.intel_query import ConditionGroup, InvalidQueryError, parse_coordinate_span
from evo_helper.domain.models import CoordinateRange
from evo_helper.storage.intel import (
    DEFAULT_LIMIT,
    SORT_COORDINATE,
    IntelSearchQuery,
    SqlAlchemyIntelRepository,
    decode_group,
    encode_group,
)
from evo_helper.vision.parsers import UNIT_ORDER


class SpanIn(BaseModel):
    start: str
    end: str


class SearchIn(BaseModel):
    span: SpanIn | None = None
    conditions: dict[str, Any] | None = None
    filter_id: UUID | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    sort: str = SORT_COORDINATE


class ShipCountOut(BaseModel):
    ship_type: str
    count: int


class IntelRowOut(BaseModel):
    coordinate: str
    player: str | None
    last_scan_at: datetime | None
    snapshot_at: datetime | None
    total: int | None
    has_fleet_data: bool
    matched_summary: str
    match_confidence: float | None
    review_status: str | None
    ships: list[ShipCountOut]


class SearchOut(BaseModel):
    rows: list[IntelRowOut]
    next_cursor: str | None


class FilterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    conditions: dict[str, Any]
    span: SpanIn | None = None


class FilterOut(BaseModel):
    filter_id: UUID
    name: str
    conditions: dict[str, Any]
    span: SpanIn | None
    created_at_utc: datetime
    updated_at_utc: datetime


def register_intel_routes(app: FastAPI, session_factory: sessionmaker[Session]) -> None:
    repository = SqlAlchemyIntelRepository(session_factory)
    router = APIRouter(prefix="/api/intel", tags=["intel"])

    @app.exception_handler(InvalidQueryError)
    async def _invalid_query(_request: Request, exc: InvalidQueryError) -> JSONResponse:
        # 422 rather than 400: the request is well-formed JSON that fails the
        # query rules, and the UI shows this message verbatim next to the field.
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @router.get("/ships", response_model=list[str])
    async def list_ships() -> list[str]:
        """Recorded defender unit types, in the order the game lists them.

        Alphabetical order would scatter the catalogue; the picker reads more
        like the in-game list this way. Anything recorded but not in the
        catalogue is appended so it stays visible rather than disappearing.
        """
        recorded = repository.known_ship_names()
        known = [name for name in UNIT_ORDER if name in recorded]
        extra = sorted(recorded - set(UNIT_ORDER))
        return known + extra

    @router.post("/search", response_model=SearchOut)
    async def search(payload: SearchIn) -> SearchOut:
        span, conditions = _resolve(repository, payload)
        if conditions is not None:
            conditions.validate_ship_names(repository.known_ship_names())
        page = repository.search(
            IntelSearchQuery(
                span=span,
                conditions=conditions,
                cursor=payload.cursor,
                limit=payload.limit,
                sort=payload.sort,
            )
        )
        return SearchOut(
            rows=[
                IntelRowOut(
                    coordinate=str(row.coordinate),
                    player=row.player,
                    last_scan_at=row.last_scan_at,
                    snapshot_at=row.snapshot_at,
                    total=row.total,
                    has_fleet_data=row.has_fleet_data,
                    matched_summary=row.matched_summary,
                    match_confidence=row.match_confidence,
                    review_status=row.review_status,
                    ships=[
                        ShipCountOut(ship_type=name, count=count)
                        for name, count in sorted(row.counts.items())
                    ],
                )
                for row in page.rows
            ],
            next_cursor=page.next_cursor,
        )

    @router.get("/filters", response_model=list[FilterOut])
    async def list_filters() -> list[FilterOut]:
        return [_filter_out(saved) for saved in repository.list_filters()]

    @router.post("/filters", response_model=FilterOut, status_code=201)
    async def create_filter(payload: FilterIn) -> FilterOut:
        conditions = decode_group(payload.conditions)
        conditions.validate_ship_names(repository.known_ship_names())
        span = _span(payload.span)
        return _filter_out(
            repository.save_filter(name=payload.name, conditions=conditions, span=span)
        )

    @router.delete("/filters/{filter_id}", status_code=204)
    async def delete_filter(filter_id: UUID) -> None:
        repository.delete_filter(filter_id)

    app.include_router(router)


def _resolve(
    repository: SqlAlchemyIntelRepository, payload: SearchIn
) -> tuple[CoordinateRange | None, ConditionGroup | None]:
    """A saved filter supplies span and conditions; the request may override."""
    span = _span(payload.span)
    conditions = decode_group(payload.conditions) if payload.conditions is not None else None
    if payload.filter_id is not None:
        saved = repository.get_filter(payload.filter_id)
        if saved is None:
            raise InvalidQueryError(f"saved filter {payload.filter_id} not found")
        span = span or saved.span
        conditions = conditions or saved.conditions
    return span, conditions


def _span(span: SpanIn | None) -> CoordinateRange | None:
    return parse_coordinate_span(span.start, span.end) if span is not None else None


def _filter_out(saved: Any) -> FilterOut:
    return FilterOut(
        filter_id=saved.filter_id,
        name=saved.name,
        conditions=encode_group(saved.conditions),
        span=SpanIn(start=str(saved.span.start), end=str(saved.span.end)) if saved.span else None,
        created_at_utc=saved.created_at_utc,
        updated_at_utc=saved.updated_at_utc,
    )
