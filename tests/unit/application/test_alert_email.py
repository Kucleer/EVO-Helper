from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from evo_helper.application.alert_email import format_planet_scout_alert
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import PlanetScoutAlert


def test_security_alert_email_contains_the_recorded_evidence() -> None:
    body = format_planet_scout_alert(
        PlanetScoutAlert(
            alert_id=uuid4(),
            reported_at_utc=datetime(2026, 8, 15, 8, 34, 48, tzinfo=UTC),
            raw_time_text="15/08/2026 16:34:48",
            source=Coordinate(2, 144, 18),
            target=Coordinate(2, 137, 18),
            source_name="GrandSuke's Planet",
            intercepted_probes=1,
            subject="你的行星被侦察",
            raw_body="unused in body rendering",
        )
    )

    assert "[2:144:18]（GrandSuke's Planet）" in body
    assert "[2:137:18]" in body
    assert "已拦截 1 个敌方侦察探测器" in body
