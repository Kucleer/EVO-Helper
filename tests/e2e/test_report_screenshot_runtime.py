"""控制台启动时顺手清一次过期的战报截图。

保留期照 `system_log` 那套路子挂在启动上：这是本进程唯一一个「每次开机跑一次」
的现成时机，而这张表按设计只增不改，攒着迟早把库撑大（约 40 KB/张、每天 80 张）。

⚠️ 这里走的是真正的 `create_runtime_app`，也就是**跑一遍 alembic 迁移**——
新表建不出来的话这几条会当场红，而不是等到实机上写截图时才发现。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import shutdown_system_log_sink
from evo_helper.storage import models as orm
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.web.runtime import _upgrade_database, create_runtime_app

PIXELS = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


def _seed(session_factory, *, days_ago: int) -> UUID:  # type: ignore[no-untyped-def]
    """一份战报 + 挂在它上面的一张 `days_ago` 天前截的图。"""
    report_id = uuid4()
    captured = datetime.now(UTC) - timedelta(days=days_ago)
    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=report_id,
                reported_at_utc=captured,
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=2,
                defender_target_system=137,
                defender_target_position=4,
            )
        )
        session.commit()
    ReportScreenshotRepository(session_factory).save(
        report_id, image_bytes=PIXELS, width=520, height=695, captured_at_utc=captured
    )
    return report_id


def test_startup_purges_screenshots_past_the_retention_window(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'shot-retention.db'}"
    # 先起一次把表（含迁移）建出来，才能在下一次启动之前塞进旧行。
    _upgrade_database(database_url)
    create_runtime_app(Settings(database_url=database_url), local_token="t")
    shutdown_system_log_sink()
    session_factory = create_session_factory(create_database_engine(database_url))
    old = _seed(session_factory, days_ago=31)
    fresh = _seed(session_factory, days_ago=1)

    _upgrade_database(database_url)
    create_runtime_app(
        Settings(database_url=database_url, report_screenshot_retention_days=30), local_token="t"
    )
    shutdown_system_log_sink()

    repository = ReportScreenshotRepository(session_factory)
    assert repository.load(old) is None
    assert repository.load(fresh) is not None


def test_zero_retention_keeps_every_screenshot(tmp_path: Path) -> None:
    """0 是「不清理」，不是「全删」。判据与 `system_log` 那一侧共用一套。"""
    database_url = f"sqlite:///{tmp_path / 'shot-retention-off.db'}"
    _upgrade_database(database_url)
    create_runtime_app(Settings(database_url=database_url), local_token="t")
    shutdown_system_log_sink()
    session_factory = create_session_factory(create_database_engine(database_url))
    ancient = _seed(session_factory, days_ago=900)

    _upgrade_database(database_url)
    create_runtime_app(
        Settings(database_url=database_url, report_screenshot_retention_days=0), local_token="t"
    )
    shutdown_system_log_sink()

    assert ReportScreenshotRepository(session_factory).load(ancient) is not None


def test_the_migration_creates_the_table(tmp_path: Path) -> None:
    """迁移真的跑到了：一张空库起完之后就该能存图。

    这一条挡的是「模型加了、迁移忘了」——那种错在开发机上（`create_all`）
    永远是绿的，只在实机上炸。
    """
    database_url = f"sqlite:///{tmp_path / 'shot-migrated.db'}"
    _upgrade_database(database_url)
    create_runtime_app(Settings(database_url=database_url), local_token="t")
    shutdown_system_log_sink()
    session_factory = create_session_factory(create_database_engine(database_url))

    report_id = _seed(session_factory, days_ago=0)

    shot = ReportScreenshotRepository(session_factory).load(report_id)
    assert shot is not None
    assert shot.image_bytes == PIXELS
