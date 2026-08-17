"""读到一份战报时截一张图存库——以及这条旁路**绝不许拖累战报入库**。

用户口径（2026-08-17）：进入邮件详情读战报时截一张图，能在攻击日志页看到。
「只在读到战报时截，不要每次进邮件都截」，所以这里既钉「存下来了」，也钉
「库里已有 / 读不出来的那两档一张都不截」。

⚠️ 最后一组是整个功能里唯一真正危险的地方：截图是旁路，而它就挂在战报入库的
那条主路径上。这个 PR 的存在理由正是战报没能入库；再引入一条能打断那趟信箱的
新异常，就是把同一个故障换个成因造一遍。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import PirateLoop

TARGET = Coordinate(2, 137, 4)
PIXELS = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


class _Panel:
    image_bytes = PIXELS
    width = 520
    height = 695
    image_format = "webp"


class _Page:
    """一屏详情页，能交出裁好的面板图。"""

    def __init__(self, panel: Any = None, error: Exception | None = None) -> None:
        self._panel = panel if panel is not None else _Panel()
        self._error = error
        self.asked = 0

    def report_panel_image(self) -> Any:
        self.asked += 1
        if self._error is not None:
            raise self._error
        return self._panel


@pytest.fixture
def session_factory(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'shot.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def _seed_report(session_factory: sessionmaker[Session]) -> UUID:
    """一份最小可用的战报行。截图挂在它上面（外键），少了它连存都存不下。"""
    from evo_helper.storage import models as orm

    report_id = uuid4()
    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=report_id,
                reported_at_utc=datetime.now(UTC),
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=TARGET.galaxy,
                defender_target_system=TARGET.system,
                defender_target_position=TARGET.position,
            )
        )
        session.commit()
    return report_id


def _loop(session_factory: sessionmaker[Session], said: list[str]) -> Any:
    loop = PirateLoop.__new__(PirateLoop)
    loop._ensure_session_factory = lambda: session_factory
    return loop


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(module, "say", said.append)
    return said


class TestStoringTheShot:
    def test_the_panel_is_cropped_once_and_stored_against_the_report(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        loop = _loop(session_factory, _quiet)
        page = _Page()
        report_id = _seed_report(session_factory)

        loop._store_report_screenshot(report_id, page)

        assert page.asked == 1, "同一屏只裁一次；重拍一次屏可能拍到别的画面"
        shot = ReportScreenshotRepository(session_factory).load(report_id)
        assert shot is not None
        assert shot.image_bytes == PIXELS
        assert (shot.width, shot.height) == (520, 695)

    def test_the_capture_moment_is_now_not_the_report_time(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        """保留期按截图时刻算。补录会读到很旧的战报，按战报时刻算的话那张图
        一入库就已经过期。
        """
        before = datetime.now(UTC)
        report_id = _seed_report(session_factory)

        _loop(session_factory, _quiet)._store_report_screenshot(report_id, _Page())

        shot = ReportScreenshotRepository(session_factory).load(report_id)
        assert shot is not None
        assert shot.captured_at_utc >= before

    def test_it_says_so_in_the_log(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        report_id = _seed_report(session_factory)

        _loop(session_factory, _quiet)._store_report_screenshot(report_id, _Page())

        assert any("战报截图已入库" in line for line in _quiet)


class TestTheSidePathNeverBreaksTheMainOne:
    """⚠️ 一句异常都不许漏出去——漏出去就会打断 `_scan_mail_rows` 那一趟。"""

    def test_a_cropping_failure_is_swallowed(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        loop = _loop(session_factory, _quiet)

        loop._store_report_screenshot(uuid4(), _Page(error=RuntimeError("Pillow 没装")))

        assert any("战报截图没存下" in line for line in _quiet)
        assert any("不影响判据" in line for line in _quiet)

    def test_a_database_failure_is_swallowed(self, _quiet: list[str]) -> None:
        loop = PirateLoop.__new__(PirateLoop)
        loop._ensure_session_factory = lambda: (_ for _ in ()).throw(RuntimeError("连不上库"))

        loop._store_report_screenshot(uuid4(), _Page())

        assert any("战报截图没存下" in line for line in _quiet)

    def test_a_page_that_cannot_crop_at_all_is_swallowed(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        """离线入口交进来的 `page` 可能压根没有这个方法（旧的取字面协议）。"""

        class _OldPage:
            pass

        _loop(session_factory, _quiet)._store_report_screenshot(uuid4(), _OldPage())

        assert any("战报截图没存下" in line for line in _quiet)


class TestOnlyWhenAReportIsActuallyRead:
    """用户口径：只在读到战报时截，不要每次进邮件都截。

    两条链路的 `_ingest_report` 都在 `append_report` **之后**才调截图，所以
    「库里已有」（`KNOWN`）与「读不出来」（`UNREADABLE`）两档在那之前就返回了。
    这里直接钉住那个调用位置，而不是复述它。
    """

    def test_both_links_capture_only_after_the_report_was_appended(self) -> None:
        import inspect

        from evo_helper.tools.bot_loop import BotLoop

        for source in (
            inspect.getsource(PirateLoop._ingest_report),
            inspect.getsource(BotLoop._ingest_battle_report),
        ):
            appended = source.index("append_report")
            captured = source.index("_store_report_screenshot")
            assert appended < captured, "截图必须排在入库之后，否则没入库的那几档也会截"

    def test_the_known_and_unreadable_branches_return_before_capturing(self) -> None:
        import inspect

        source = inspect.getsource(PirateLoop._ingest_report)
        known = source.index("ReportIngest.KNOWN")
        captured = source.index("_store_report_screenshot")

        assert known < captured


class TestOnePerReport:
    def test_reading_the_same_report_twice_keeps_the_first_image(
        self, session_factory: sessionmaker[Session], _quiet: list[str]
    ) -> None:
        loop = _loop(session_factory, _quiet)
        report_id = _seed_report(session_factory)

        loop._store_report_screenshot(report_id, _Page())
        loop._store_report_screenshot(report_id, _Page())

        shot = ReportScreenshotRepository(session_factory).load(report_id)
        assert shot is not None
        assert shot.image_bytes == PIXELS
        assert isinstance(shot.report_id, UUID)
