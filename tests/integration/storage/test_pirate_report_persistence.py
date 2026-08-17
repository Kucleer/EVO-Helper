"""海盗战报只落胜负与战损总数，一行舰队明细都不写。

口径是用户定的（2026-08-09）：明细要进回放页，一份报告多花两三秒，
而海盗全是同一个预设打的，逐舰种没有分析价值。所以这里守两件事——
**该存的存下来了**，以及**不该存的没有偷偷存**（明细一旦混进
`fleet_snapshots`，情报中心就会把海盗的预设当成对方的舰队）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from evo_helper.application.report_ingest import (
    PIRATE_DETAIL_UI_VERSION,
    to_pirate_battle_report,
)
from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY, PirateReportReading
from support.database import scratch_database_url

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)

READING = PirateReportReading(
    raw_time_text="09/08/2026 04:38:46",
    reported_at_utc=datetime(2026, 8, 9, 4, 38, 46, tzinfo=UTC),
    attacker_origin=ORIGIN,
    defender_target=TARGET,
    attacker_name="Kucleer",
    defender_name="Pirates",
    outcome=OUTCOME_VICTORY,
    attacker_losses=0,
    defender_losses=783,
    attacker_units=100,
    defender_units=783,
)


def _factory(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(scratch_database_url(tmp_path, "pirate.db"))
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _stored(tmp_path: Path) -> orm.BattleReportRow:
    factory = _factory(tmp_path)
    SqlAlchemyRepository(factory).append_report(to_pirate_battle_report(READING, report_id=uuid4()))
    with factory() as session:
        return session.scalars(select(orm.BattleReportRow)).one()


def test_outcome_and_loss_totals_survive_a_roundtrip(tmp_path: Path) -> None:
    row = _stored(tmp_path)

    assert row.outcome == OUTCOME_VICTORY
    assert (row.attacker_losses, row.defender_losses) == (0, 783)
    assert (row.attacker_units, row.defender_units) == (100, 783)
    assert row.raw_time_text == "09/08/2026 04:38:46"


def test_zero_losses_are_stored_as_zero_not_null(tmp_path: Path) -> None:
    """「一艘没损失」和「没读到」是两件事，库里必须分得开。"""
    row = _stored(tmp_path)

    assert row.attacker_losses == 0
    assert row.attacker_losses is not None


def test_no_fleet_detail_is_written(tmp_path: Path) -> None:
    factory = _factory(tmp_path)
    SqlAlchemyRepository(factory).append_report(to_pirate_battle_report(READING, report_id=uuid4()))

    with factory() as session:
        assert session.scalars(select(orm.FleetSnapshotRow)).all() == []


def test_the_pirate_screen_gets_its_own_ui_version(tmp_path: Path) -> None:
    """版本标签是「这一屏长什么样」的凭据。海盗那条链路只看详情页，
    与 bot 那条读法不同，共用一个标签日后分不清是哪一条失效了。"""
    row = _stored(tmp_path)

    assert row.ui_version == PIRATE_DETAIL_UI_VERSION
    assert row.ui_version != "battle-detail-v2"
