"""持久化扫描计划必须随星球列表重排，不能继续拿旧游标续扫。"""

from __future__ import annotations

from sqlalchemy import select

from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.tools.scan_coordinates import (
    configured_priority_planets,
    ensure_run,
)


def test_existing_legacy_plan_is_rebuilt_from_planet_list_and_resets_cursor(
    session_factory,
) -> None:  # type: ignore[no-untyped-def]
    """旧计划在 2 系中段续扫时，升级后也必须先回到星球 1 周边。"""
    first_run_id, _ = ensure_run(session_factory, priority_planets=())
    with session_factory() as session:
        run = session.get(orm.RunInstance, first_run_id)
        assert run is not None
        run.cursor_galaxy = 2
        run.cursor_system = 438
        run.cursor_position = 13
        session.add_all(
            [
                orm.AttackPlanetRow(sort_index=2, galaxy=2, system=137, position=18),
                orm.AttackPlanetRow(sort_index=1, galaxy=9, system=250, position=8),
            ]
        )
        session.commit()

    planets = configured_priority_planets(session_factory)
    assert planets == (Coordinate(9, 250, 8), Coordinate(2, 137, 18))

    run_id, cursor = ensure_run(session_factory, priority_planets=planets)
    assert run_id == first_run_id
    assert cursor is None

    with session_factory() as session:
        ranges = session.scalars(
            select(orm.ScanRangeRow)
            .where(orm.ScanRangeRow.plan_id == session.get(orm.RunInstance, run_id).plan_id)
            .order_by(orm.ScanRangeRow.priority)
        ).all()
    assert (ranges[0].start_galaxy, ranges[0].start_system) == (9, 250)
    assert (ranges[1].start_galaxy, ranges[1].start_system) == (9, 249)
    assert (ranges[2].start_galaxy, ranges[2].start_system) == (9, 251)
