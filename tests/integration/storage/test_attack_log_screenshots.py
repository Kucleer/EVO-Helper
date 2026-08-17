"""攻击日志那一行怎么知道「这份战报有截图」——**而且不把图片字节带出来**。

这一页一次取几十行，每张图约 40 KB。把字节并进列表响应，一页就是几 MB，
页面在手机上直接卡死。所以列表只带一个布尔 + 战报 id，字节由
`report_screenshot()` 一次一张地取。
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

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
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
CREATED = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="小型运输船:1")
PIXELS = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'shots.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory)
    plan = service.create_plan(
        name="shots",
        enabled=True,
        window_start=datetime(2026, 1, 1, 8).time(),
        window_end=datetime(2026, 1, 1, 10).time(),
        ranges=(ScanRangeView(TARGET, TARGET, ORIGIN, PRESET.name, PRESET.signature, 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="idem-shots", created_at_utc=CREATED
    )
    return SqlAlchemyRepository(factory), service, run_id, factory


def _attack_with_report(repo, run_id):  # type: ignore[no-untyped-def]
    """一发打完、战报也回来了的完整链条。返回那份战报的 id。"""
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=CREATED,
        target_kind=TARGET_KIND_PIRATE,
    )
    repo.save_attack_intent(intent)
    dispatched = CREATED + timedelta(minutes=1)
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched,
            accepted=True,
        )
    )
    report_id = uuid4()
    repo.append_report(
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
    return report_id


def _binary_fields(value: object, *, path: str = "entry", depth: int = 0) -> list[str]:
    """`value` 里所有二进制内容的位置，逐层往下找。

    只看顶层字段是不够的：把一整个 `ReportScreenshot`（里面躺着 40 KB）挂在
    `report_screenshot` 上，顶层那一眼看过去只是「一个对象」，而响应已经胖了。
    """
    if isinstance(value, bytes | bytearray | memoryview):
        return [path]
    if depth > 4:
        return []
    if is_dataclass(value) and not isinstance(value, type):
        return [
            found
            for field in fields(value)
            for found in _binary_fields(
                getattr(value, field.name), path=f"{path}.{field.name}", depth=depth + 1
            )
        ]
    if isinstance(value, list | tuple | set):
        return [
            found
            for index, item in enumerate(value)
            for found in _binary_fields(item, path=f"{path}[{index}]", depth=depth + 1)
        ]
    if isinstance(value, dict):
        return [
            found
            for key, item in value.items()
            for found in _binary_fields(item, path=f"{path}[{key!r}]", depth=depth + 1)
        ]
    return []


def test_a_report_with_a_screenshot_is_flagged_on_its_row(tmp_path: Path) -> None:
    """有图的那一行才该出现「查看战报截图」这个入口。"""
    repo, service, run_id, factory = _setup(tmp_path)
    report_id = _attack_with_report(repo, run_id)
    ReportScreenshotRepository(factory).save(
        report_id, image_bytes=PIXELS, width=520, height=695, captured_at_utc=CREATED
    )

    (entry,) = service.list_attack_log(50)

    assert entry.report_id == report_id
    assert entry.report_screenshot is True


def test_a_report_without_a_screenshot_is_not_flagged(tmp_path: Path) -> None:
    """没图时不摆链接：一个点开是 404 的入口比没有入口更难排查。"""
    repo, service, run_id, _factory = _setup(tmp_path)
    report_id = _attack_with_report(repo, run_id)

    (entry,) = service.list_attack_log(50)

    assert entry.report_id == report_id
    assert entry.report_screenshot is False


def test_a_row_without_a_report_has_no_report_id(tmp_path: Path) -> None:
    repo, service, run_id, _factory = _setup(tmp_path)
    repo.save_attack_intent(
        AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=ORIGIN,
            target=TARGET,
            preset=PRESET,
            cycle_start_utc=CYCLE,
            created_at_utc=CREATED,
            target_kind=TARGET_KIND_PIRATE,
        )
    )

    (entry,) = service.list_attack_log(50)

    assert entry.report_id is None
    assert entry.report_screenshot is False


def test_the_list_response_never_carries_the_image_bytes(tmp_path: Path) -> None:
    """⚠️ **这一条钉的是性能，而性能在这里就是可用性。**

    一页几十行、每张图约 40 KB；把字节（或它的 base64）放进列表，一次请求就是
    几 MB。列表里只许出现「有没有图」这一个布尔和战报 id。
    """
    repo, service, run_id, factory = _setup(tmp_path)
    report_id = _attack_with_report(repo, run_id)
    ReportScreenshotRepository(factory).save(
        report_id, image_bytes=PIXELS, width=520, height=695, captured_at_utc=CREATED
    )

    (entry,) = service.list_attack_log(50)

    # **逐层往下找**，不是只看顶层字段：把整个 `ReportScreenshot` 挂在
    # `report_screenshot` 上同样是把几十 KB 塞进了列表，而顶层那一眼看过去
    # 只是「一个对象」。这条断言的第一版正是这样被绕过去的。
    assert _binary_fields(entry) == [], "攻击日志的一行里挂上了二进制内容"
    # 图本身仍然取得到，只是走另一条路径。
    shot = service.report_screenshot(report_id)
    assert shot is not None
    assert shot.image_bytes == PIXELS


def test_asking_for_a_screenshot_that_does_not_exist_returns_none(tmp_path: Path) -> None:
    _repo, service, _run_id, _factory = _setup(tmp_path)

    assert service.report_screenshot(uuid4()) is None
