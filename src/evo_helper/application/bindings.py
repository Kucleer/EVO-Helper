"""Resolve a run's persisted scan-range configuration into dispatch bindings."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, CoordinateRange, FleetPresetRef
from evo_helper.storage import models as orm

from .workflow import AttackBinding


class DatabaseBindingResolver:
    """Read the exact origin and preset signature stored for one run's plan."""

    def __init__(self, session_factory: sessionmaker[Session], run_id: UUID) -> None:
        self._session_factory = session_factory
        self._run_id = run_id

    def for_target(self, coordinate: Coordinate) -> AttackBinding | None:
        with self._session_factory() as session:
            run = session.get(orm.RunInstance, self._run_id)
            if run is None:
                return None
            rows = session.scalars(
                select(orm.ScanRangeRow)
                .where(orm.ScanRangeRow.plan_id == run.plan_id)
                .order_by(orm.ScanRangeRow.priority, orm.ScanRangeRow.id)
            ).all()
            for row in rows:
                if CoordinateRange(_start(row), _end(row)).contains(coordinate):
                    return AttackBinding(
                        origin=Coordinate(
                            row.origin_galaxy, row.origin_system, row.origin_position
                        ),
                        preset=FleetPresetRef(row.fleet_preset_name, row.fleet_preset_signature),
                    )
        return None


def _start(row: orm.ScanRangeRow) -> Coordinate:
    return Coordinate(row.start_galaxy, row.start_system, row.start_position)


def _end(row: orm.ScanRangeRow) -> Coordinate:
    return Coordinate(row.end_galaxy, row.end_system, row.end_position)
