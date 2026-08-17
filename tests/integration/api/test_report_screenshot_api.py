"""`GET /api/reports/{id}/screenshot` 与攻击日志页上那个入口。

这条接口存在的理由是**不要把图片字节塞进列表响应**：攻击日志一页几十行、每张图
约 40 KB，并进去一次请求就是几 MB。列表只带一个布尔，点了才来这里取一张。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CREATED = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="小型运输船:1")
PIXELS = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


@pytest.fixture
def wired(tmp_path):  # type: ignore[no-untyped-def]
    """一份「打完 + 战报回来 + 存了截图」的库，外加一个连上它的客户端。"""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'shot-api.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory)
    plan = service.create_plan(
        name="shot-api",
        enabled=True,
        window_start=datetime(2026, 1, 1, 8).time(),
        window_end=datetime(2026, 1, 1, 10).time(),
        ranges=(ScanRangeView(TARGET, TARGET, ORIGIN, PRESET.name, PRESET.signature, 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="idem-shot-api", created_at_utc=CREATED
    )
    repository = SqlAlchemyRepository(factory)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=PRESET,
        cycle_start_utc=CREATED,
        created_at_utc=CREATED,
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(intent)
    dispatched = CREATED + timedelta(minutes=1)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched,
            accepted=True,
        )
    )
    report_id = uuid4()
    repository.append_report(
        BattleReport(
            report_id=report_id,
            reported_at_utc=dispatched + timedelta(minutes=20),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            outcome=OUTCOME_VICTORY,
            attacker_losses=0,
            defender_losses=783,
        )
    )
    ReportScreenshotRepository(factory).save(
        report_id, image_bytes=PIXELS, width=520, height=695, captured_at_utc=CREATED
    )
    app = create_persistent_app(factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client, report_id


def test_the_screenshot_comes_back_as_an_image(wired) -> None:  # type: ignore[no-untyped-def]
    client, report_id = wired

    response = client.get(f"/api/reports/{report_id}/screenshot")

    assert response.status_code == 200
    assert response.content == PIXELS
    assert response.headers["content-type"] == "image/webp"


def test_a_missing_screenshot_is_a_404(wired) -> None:  # type: ignore[no-untyped-def]
    client, _report_id = wired

    assert client.get(f"/api/reports/{uuid4()}/screenshot").status_code == 404


def test_the_attack_log_page_links_to_the_screenshot(wired) -> None:  # type: ignore[no-untyped-def]
    """有图的那一行上要有入口，否则这张图存了也没人看得见。"""
    client, report_id = wired

    body = client.get("/logs").text

    assert f"/api/reports/{report_id}/screenshot" in body


def test_the_attack_log_page_never_inlines_the_image_bytes(wired) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **页面里只许出现链接，不许出现图。**

    一页几十行、每张约 40 KB。内联（`<img src="data:...">` 或把字节塞进列表
    响应）就是几 MB 的首屏，控制台常常是手机在看。
    """
    client, _report_id = wired

    body = client.get("/logs").content

    assert PIXELS not in body
    assert b"data:image/webp;base64" not in body


def test_a_row_without_a_screenshot_has_no_link(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """没图就不摆入口：点开是 404 的链接比没有链接更难排查。"""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'no-shot.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    app = create_persistent_app(factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})

    body = client.get("/logs").text

    assert "查看战报截图" not in body
