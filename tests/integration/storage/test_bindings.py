from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evo_helper.application.bindings import DatabaseBindingResolver
from evo_helper.domain.models import Coordinate
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import RunInstance, ScanPlan, ScanRangeRow
from support.database import scratch_database_url


def test_database_binding_resolver_uses_range_origin_and_exact_signature(tmp_path: Path) -> None:
    engine = create_database_engine(scratch_database_url(tmp_path, "bindings.db"))
    Base.metadata.create_all(engine)
    sessions = create_session_factory(engine)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    with sessions() as session:
        plan = ScanPlan(name="bindings", created_at_utc=now)
        session.add(plan)
        session.flush()
        session.add(
            ScanRangeRow(
                plan_id=plan.id,
                start_galaxy=2,
                start_system=3,
                start_position=4,
                end_galaxy=2,
                end_system=3,
                end_position=8,
                origin_galaxy=1,
                origin_system=1,
                origin_position=1,
                fleet_preset_name="fleet-alpha",
                fleet_preset_signature="alpha-signature",
                priority=0,
            )
        )
        run = RunInstance(
            id=uuid4(),
            plan_id=plan.id,
            idempotency_key="binding-run-0001",
            state="SCANNING",
            created_at_utc=now,
        )
        session.add(run)
        session.commit()

    resolver = DatabaseBindingResolver(sessions, run.id)

    binding = resolver.for_target(Coordinate(2, 3, 6))
    assert binding is not None
    assert binding.origin == Coordinate(1, 1, 1)
    assert binding.preset.name == "fleet-alpha"
    assert binding.preset.signature == "alpha-signature"
    assert resolver.for_target(Coordinate(2, 3, 9)) is None
