"""攻击日志页要能把战果显示出来：胜负 + 战损总数。

海盗战报只记这两样（用户口径 2026-08-09），所以日志页上的「战果」一格就是
这条链路的全部产出——渲染不出来，等于白记。

顺带守住另一件事：**还没回战报的那一发不能显示成「零损失」**。
页面上「—」和「0」是两个完全不同的结论。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 9, 3, 55, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="深空吞噬者:70")


def _client(tmp_path: Path, *, with_report: bool) -> TestClient:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        dry_run=False,
        ranges=(
            ScanRangeView(
                Coordinate(2, 137, 1), TARGET, ORIGIN, PRESET.name, PRESET.signature, 0
            ),
        ),
    )
    run = service.start_run(plan.id, "log-page-0001")
    repository = SqlAlchemyRepository(factory)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run.run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=DISPATCHED,
            dry_run=False,
            accepted=True,
        )
    )
    if with_report:
        repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=DISPATCHED + timedelta(minutes=43),
                attacker_origin=ORIGIN,
                defender_target=TARGET,
                raw_time_text="09/08/2026 04:38:46",
                outcome=OUTCOME_VICTORY,
                attacker_losses=0,
                defender_losses=783,
            )
        )
    return TestClient(create_persistent_app(factory))


def test_the_log_page_shows_the_battle_result(tmp_path: Path) -> None:
    response = _client(tmp_path, with_report=True).get("/logs")

    assert response.status_code == 200
    body = response.text
    assert "战果" in body
    assert "胜" in body
    assert "战损 我 0" in body
    assert "敌 783" in body


def test_an_attack_without_a_report_yet_shows_pending(tmp_path: Path) -> None:
    response = _client(tmp_path, with_report=False).get("/logs")

    assert response.status_code == 200
    assert "待战报" in response.text
    assert "战损 我 0" not in response.text
