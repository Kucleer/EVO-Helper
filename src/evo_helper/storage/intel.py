"""Intel search over the latest defender fleet snapshot per bot target.

Filtering happens on the server. The coordinate span is pushed into SQL, and so
is "latest report per target", so the browser never receives a target's whole
fleet history just to filter it.

The condition tree is then evaluated by the domain evaluator rather than
translated into SQL. The candidate set is already bounded by the span, and
reusing :meth:`ConditionGroup.matches` means the API and the tested domain
semantics cannot drift apart — an AND/OR tree compiled into SQL twice is two
implementations of the same rule.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from evo_helper.domain.intel_query import (
    ConditionGroup,
    FleetCondition,
    GroupOperator,
    InvalidQueryError,
    Operator,
    QueryField,
)
from evo_helper.domain.models import Coordinate, CoordinateRange
from evo_helper.storage import models as orm

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

SORT_COORDINATE = "coordinate"
SORT_TOTAL_DESC = "total_desc"
SORT_TOTAL_ASC = "total_asc"
SORT_SNAPSHOT_DESC = "snapshot_desc"
SORTS = (SORT_COORDINATE, SORT_TOTAL_DESC, SORT_TOTAL_ASC, SORT_SNAPSHOT_DESC)


@dataclass(frozen=True)
class IntelSearchQuery:
    span: CoordinateRange | None = None
    conditions: ConditionGroup | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    sort: str = SORT_COORDINATE

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > MAX_LIMIT:
            raise InvalidQueryError(f"limit must be between 1 and {MAX_LIMIT}")
        if self.sort not in SORTS:
            raise InvalidQueryError(
                f"unknown sort {self.sort!r}; expected one of {', '.join(SORTS)}"
            )


@dataclass(frozen=True)
class IntelRow:
    coordinate: Coordinate
    player: str | None
    last_scan_at: datetime | None
    snapshot_at: datetime | None
    total: int | None
    counts: dict[str, int]
    matched_summary: str
    match_confidence: float | None
    review_status: str | None

    @property
    def has_fleet_data(self) -> bool:
        """有没有**舰队数字**，不是「有没有战报」。

        原先判的是 `snapshot_at is not None`，也就是「这个目标有战报」。bot 探路
        战报只读详情页、不写逐舰种行（打开逐舰种要进回放页，而那个入口按钮全仓
        没有标定坐标），于是 `counts` 为空、`total` 从「逐舰种求和」得到 0——
        页面上就成了「有舰队数据，总计 0」，而报告里明明写着守方单位 319。

        判 `total` 而不判 `snapshot_at`：读到了数才算有数。0 是合法的（对方真没船），
        所以比的是 `is not None` 而不是真值。
        """
        return self.total is not None


@dataclass(frozen=True)
class IntelSearchPage:
    rows: tuple[IntelRow, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class SavedFilter:
    filter_id: UUID
    name: str
    conditions: ConditionGroup
    span: CoordinateRange | None
    created_at_utc: datetime
    updated_at_utc: datetime


class SqlAlchemyIntelRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # -- search ------------------------------------------------------------

    def search(self, query: IntelSearchQuery) -> IntelSearchPage:
        with self._session_factory() as session:
            rows = [
                self._row_for(session, target)
                for target in self._targets_in_span(session, query.span)
            ]
        if query.conditions is not None:
            matched = []
            for row in rows:
                counts = row.counts or None
                if query.conditions.matches(counts):
                    summary = ", ".join(query.conditions.matched_labels(counts))
                    matched.append(replace(row, matched_summary=summary))
            rows = matched
        rows = _sorted(rows, query.sort)
        return _paginate(rows, cursor=query.cursor, limit=query.limit)

    def _targets_in_span(
        self, session: Session, span: CoordinateRange | None
    ) -> list[orm.BotTargetRow]:
        statement = select(orm.BotTargetRow).where(orm.BotTargetRow.is_bot)
        if span is not None:
            # Compare the packed coordinate so the span is one SQL range test
            # rather than a per-component comparison, which would wrongly
            # exclude e.g. 1:150:4 from 1:100:1 - 1:200:999.
            statement = statement.where(
                _packed_column().between(_pack(span.start), _pack(span.end))
            )
        return list(session.scalars(statement))

    def _row_for(self, session: Session, target: orm.BotTargetRow) -> IntelRow:
        coordinate = Coordinate(target.galaxy, target.system, target.position)
        report = session.scalars(
            select(orm.BattleReportRow)
            .where(
                orm.BattleReportRow.defender_target_galaxy == coordinate.galaxy,
                orm.BattleReportRow.defender_target_system == coordinate.system,
                orm.BattleReportRow.defender_target_position == coordinate.position,
            )
            .order_by(orm.BattleReportRow.reported_at_utc.desc(), orm.BattleReportRow.id.desc())
            .limit(1)
        ).first()
        if report is None:
            return IntelRow(
                coordinate=coordinate,
                player=target.latest_owner_name,
                last_scan_at=target.last_scanned_at_utc,
                snapshot_at=None,
                total=None,
                counts={},
                matched_summary="",
                match_confidence=None,
                review_status=None,
            )
        counts = _defender_counts(session, report.id)
        return IntelRow(
            coordinate=coordinate,
            player=target.latest_owner_name,
            last_scan_at=target.last_scanned_at_utc,
            snapshot_at=report.reported_at_utc,
            # 逐舰种有行就按行求和；一行都没有时退回战报详情页上的守方「单位」总数。
            # 这两个是**两个独立来源**，不是同一个数的两种写法：大舰队的逐行数量是
            # 四舍五入显示的，相加凑不出精确总数（见 `records.BattleReport` 的注释）。
            # 所以优先用逐行和——它带着构成信息；没有逐行时用总数，总比显示 0 强。
            total=sum(counts.values()) if counts else report.defender_units,
            counts=counts,
            matched_summary="",
            match_confidence=report.match_confidence,
            review_status=report.manual_review_status,
        )

    # -- saved filters -----------------------------------------------------

    def save_filter(
        self,
        *,
        name: str,
        conditions: ConditionGroup,
        span: CoordinateRange | None = None,
        filter_id: UUID | None = None,
    ) -> SavedFilter:
        cleaned = name.strip()
        if not cleaned:
            raise InvalidQueryError("a saved filter needs a name")
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id) if filter_id else None
            if row is None:
                row = orm.IntelFilterRow(id=filter_id or uuid4(), created_at_utc=now)
                session.add(row)
            row.name = cleaned
            row.condition_tree = json.dumps(encode_group(conditions), ensure_ascii=False)
            row.span_start = str(span.start) if span else None
            row.span_end = str(span.end) if span else None
            row.updated_at_utc = now
            session.commit()
            return _to_saved_filter(row)

    def get_filter(self, filter_id: UUID) -> SavedFilter | None:
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id)
            return _to_saved_filter(row) if row else None

    def list_filters(self) -> list[SavedFilter]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.IntelFilterRow).order_by(orm.IntelFilterRow.name)
            ).all()
            return [_to_saved_filter(row) for row in rows]

    def delete_filter(self, filter_id: UUID) -> None:
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def known_ship_names(self) -> set[str]:
        """Every defender ship type the project has actually recorded."""
        with self._session_factory() as session:
            return set(
                session.scalars(
                    select(orm.FleetSnapshotRow.ship_type)
                    .where(orm.FleetSnapshotRow.side == "defender")
                    .distinct()
                )
            )


# -- condition tree serialisation ------------------------------------------


def encode_group(group: ConditionGroup) -> dict[str, object]:
    return {
        "type": "group",
        "operator": group.operator.value,
        "children": [
            encode_group(child) if isinstance(child, ConditionGroup) else _encode_condition(child)
            for child in group.children
        ],
    }


def decode_group(payload: dict[str, object]) -> ConditionGroup:
    if payload.get("type") != "group":
        raise InvalidQueryError("expected a condition group at the top of the tree")
    raw_children = payload.get("children")
    if not isinstance(raw_children, list):
        raise InvalidQueryError("a condition group needs a list of children")
    children: list[FleetCondition | ConditionGroup] = []
    for child in raw_children:
        if not isinstance(child, dict):
            raise InvalidQueryError("each condition must be an object")
        children.append(
            decode_group(child) if child.get("type") == "group" else _decode_condition(child)
        )
    return ConditionGroup(
        operator=_decode_group_operator(payload.get("operator")), children=tuple(children)
    )


def _encode_condition(condition: FleetCondition) -> dict[str, object]:
    return {
        "type": "condition",
        "field": condition.field.ship_type or "__total__",
        "operator": condition.operator.value,
        "value": condition.value,
    }


def _decode_condition(payload: dict[str, object]) -> FleetCondition:
    field_name = payload.get("field")
    if not isinstance(field_name, str) or not field_name:
        raise InvalidQueryError("a condition needs a field")
    field = QueryField.total() if field_name == "__total__" else QueryField.ship(field_name)
    raw_value = payload.get("value")
    if not isinstance(raw_value, int) or isinstance(raw_value, bool):
        raise InvalidQueryError(f"{field.label} needs a whole-number value")
    return FleetCondition(
        field=field, operator=_decode_operator(payload.get("operator")), value=raw_value
    )


def _decode_operator(raw: object) -> Operator:
    if not isinstance(raw, str):
        raise InvalidQueryError(f"unknown operator {raw!r}")
    try:
        return Operator(raw)
    except ValueError as exc:
        raise InvalidQueryError(f"unknown operator {raw!r}") from exc


def _decode_group_operator(raw: object) -> GroupOperator:
    if not isinstance(raw, str):
        raise InvalidQueryError(f"unknown group operator {raw!r}; expected AND or OR")
    try:
        return GroupOperator(raw)
    except ValueError as exc:
        raise InvalidQueryError(f"unknown group operator {raw!r}; expected AND or OR") from exc


# -- helpers ----------------------------------------------------------------


def _pack(coordinate: Coordinate) -> int:
    return (coordinate.galaxy * 1000 + coordinate.system) * 1000 + coordinate.position


def _packed_column() -> ColumnElement[int]:
    return (
        orm.BotTargetRow.galaxy * 1_000_000
        + orm.BotTargetRow.system * 1000
        + orm.BotTargetRow.position
    )


def _defender_counts(session: Session, report_id: UUID) -> dict[str, int]:
    """Counts from the participating fleet, which is the pre-battle holding.

    Per-round rows carry a ``round_no`` and describe what survived each round;
    including them would multiply-count every ship type.
    """
    rows = session.execute(
        select(orm.FleetSnapshotRow.ship_type, orm.FleetSnapshotRow.count).where(
            orm.FleetSnapshotRow.report_id == report_id,
            orm.FleetSnapshotRow.side == "defender",
            orm.FleetSnapshotRow.round_no.is_(None),
        )
    ).all()
    return {ship_type: count for ship_type, count in rows}


def _to_saved_filter(row: orm.IntelFilterRow) -> SavedFilter:
    span = None
    if row.span_start and row.span_end:
        span = CoordinateRange(start=_parse_stored(row.span_start), end=_parse_stored(row.span_end))
    return SavedFilter(
        filter_id=row.id,
        name=row.name,
        conditions=decode_group(json.loads(row.condition_tree)),
        span=span,
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
    )


def _parse_stored(text: str) -> Coordinate:
    galaxy, system, position = (int(part) for part in text.split(":"))
    return Coordinate(galaxy, system, position)


def _sorted(rows: list[IntelRow], sort: str) -> list[IntelRow]:
    if sort == SORT_TOTAL_DESC:
        return sorted(rows, key=lambda r: (-(r.total or -1), _pack(r.coordinate)))
    if sort == SORT_TOTAL_ASC:
        return sorted(
            rows, key=lambda r: ((r.total if r.total is not None else 1 << 30), _pack(r.coordinate))
        )
    if sort == SORT_SNAPSHOT_DESC:
        return sorted(
            rows,
            key=lambda r: (
                -(r.snapshot_at.timestamp() if r.snapshot_at else float("-inf")),
                _pack(r.coordinate),
            ),
        )
    return sorted(rows, key=lambda r: _pack(r.coordinate))


def _paginate(rows: list[IntelRow], *, cursor: str | None, limit: int) -> IntelSearchPage:
    """Cursor is the index into the ordered result, encoded as a string.

    The candidate set is already bounded by the coordinate span, so an offset
    cursor is honest here; a keyset cursor would buy nothing and would have to
    encode the active sort key.
    """
    start = _decode_cursor(cursor)
    page = rows[start : start + limit]
    next_index = start + limit
    return IntelSearchPage(
        rows=tuple(page),
        next_cursor=str(next_index) if next_index < len(rows) else None,
    )


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise InvalidQueryError(f"invalid cursor {cursor!r}") from exc
    if value < 0:
        raise InvalidQueryError("cursor cannot be negative")
    return value
