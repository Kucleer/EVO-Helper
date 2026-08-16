from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import PlanetScoutAlert
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository


def _alert() -> PlanetScoutAlert:
    return PlanetScoutAlert(
        alert_id=uuid4(),
        reported_at_utc=datetime(2026, 8, 15, 8, 34, 48, tzinfo=UTC),
        raw_time_text="15/08/2026 16:34:48",
        source=Coordinate(2, 144, 18),
        target=Coordinate(2, 137, 18),
        subject="你的行星被侦察",
        raw_body="一枚来自 [2:144:18] 的侦察探测器扫描了 [2:137:18]。",
    )


def test_security_alert_is_persisted_once_and_its_delivery_is_recorded(
    repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
) -> None:
    alert = _alert()
    assert repository.append_planet_scout_alert(alert) is True
    assert repository.append_planet_scout_alert(alert) is False

    repository.record_planet_scout_alert_delivery(
        alert.alert_id,
        status="NOT_CONFIGURED",
        error="SMTP 邮件配置不完整",
    )
    with session_factory() as session:
        rows = session.scalars(select(orm.PlanetScoutAlertRow)).all()
    assert len(rows) == 1
    assert rows[0].delivery_status == "NOT_CONFIGURED"
    assert rows[0].delivery_error == "SMTP 邮件配置不完整"
