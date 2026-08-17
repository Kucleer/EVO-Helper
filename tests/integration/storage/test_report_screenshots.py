"""战报截图落库、回读与保留期清理。

图存**字节**而不是路径：实机 runner 跑在另一台机器上，存路径等于在控制台上
点开一个必然打不开的链接。所以这些用例守的第一件事就是「取回来的确实是那串
字节」，而不是「有一行记录」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.storage import models as orm
from evo_helper.storage.report_screenshots import (
    ReportScreenshotRepository,
    purge_report_screenshots,
)

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
PIXELS = b"RIFF\x00\x00\x00\x00WEBPVP8 fake-bytes"


def _report(session_factory: sessionmaker[Session], *, at: datetime = NOW) -> object:
    """一份最小可用的战报行。截图要挂在它上面（外键）。"""
    report_id = uuid4()
    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=report_id,
                reported_at_utc=at,
                attacker_origin_galaxy=2,
                attacker_origin_system=137,
                attacker_origin_position=18,
                defender_target_galaxy=2,
                defender_target_system=137,
                defender_target_position=4,
            )
        )
        session.commit()
    return report_id


class TestStoringAndReadingBack:
    def test_the_exact_bytes_come_back(self, session_factory: sessionmaker[Session]) -> None:
        """存进去什么，取出来就是什么。**这条是整个功能的全部意义。**

        存路径的方案在这里就死了：另一台机器上那个路径不存在。
        """
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)

        assert repository.save(
            report_id, image_bytes=PIXELS, width=520, height=695, captured_at_utc=NOW
        )

        shot = repository.load(report_id)
        assert shot is not None
        assert shot.image_bytes == PIXELS
        assert (shot.width, shot.height) == (520, 695)
        assert shot.captured_at_utc == NOW

    def test_the_media_type_follows_what_was_stored(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """`Content-Type` 按库里记的格式填，不写死——猜错就是浏览器下载而不是显示。"""
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)
        repository.save(report_id, image_bytes=PIXELS, width=1, height=1, captured_at_utc=NOW)

        shot = repository.load(report_id)
        assert shot is not None
        assert shot.media_type == "image/webp"

    def test_a_report_without_a_screenshot_reads_back_as_none(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        assert ReportScreenshotRepository(session_factory).load(uuid4()) is None

    def test_empty_bytes_are_refused(self, session_factory: sessionmaker[Session]) -> None:
        """空 blob 会让页面上出现一个点开是破图的链接，比没有链接更难排查。"""
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)

        with pytest.raises(ValueError, match="empty"):
            repository.save(report_id, image_bytes=b"", width=1, height=1, captured_at_utc=NOW)

    def test_a_naive_timestamp_is_refused(self, session_factory: sessionmaker[Session]) -> None:
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)

        with pytest.raises(ValueError, match="timezone-aware"):
            repository.save(
                report_id,
                image_bytes=PIXELS,
                width=1,
                height=1,
                captured_at_utc=datetime(2026, 8, 17, 12, 0),  # noqa: DTZ001 - 故意的
            )


class TestOnePerReport:
    def test_a_second_save_keeps_the_first_image(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """一份战报最多一张图，重复存不覆盖、也不报错。

        不报错是刻意的：截图是旁路数据，一条旁路绝不许把战报入库那条主路径搅黄——
        而这个 PR 存在的全部理由正是战报没能入库。
        """
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)
        repository.save(report_id, image_bytes=PIXELS, width=1, height=1, captured_at_utc=NOW)

        assert not repository.save(
            report_id, image_bytes=b"second", width=2, height=2, captured_at_utc=NOW
        )

        shot = repository.load(report_id)
        assert shot is not None
        assert shot.image_bytes == PIXELS


class TestAskingWhichReportsHaveOne:
    def test_only_the_ids_come_back_not_the_bytes(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """列表页只问「有没有图」。取字节的只有 `load` 一条路径。"""
        repository = ReportScreenshotRepository(session_factory)
        with_image = _report(session_factory)
        without_image = _report(session_factory)
        repository.save(with_image, image_bytes=PIXELS, width=1, height=1, captured_at_utc=NOW)

        found = repository.has_screenshots([with_image, without_image])

        assert found == {with_image}

    def test_an_empty_list_never_hits_the_database(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        assert ReportScreenshotRepository(session_factory).has_screenshots([]) == set()


class TestRetention:
    def _rows(self, session_factory: sessionmaker[Session]) -> int:
        with session_factory() as session:
            return int(
                session.scalar(select(func.count()).select_from(orm.BattleReportScreenshotRow)) or 0
            )

    def test_images_older_than_the_retention_window_are_deleted(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """保留 30 天（用户口径 2026-08-17）。约 40 KB/张、每天 80 张，攒着会撑大库。"""
        repository = ReportScreenshotRepository(session_factory)
        old = _report(session_factory, at=NOW - timedelta(days=40))
        fresh = _report(session_factory)
        repository.save(
            old,
            image_bytes=PIXELS,
            width=1,
            height=1,
            captured_at_utc=NOW - timedelta(days=31),
        )
        repository.save(fresh, image_bytes=PIXELS, width=1, height=1, captured_at_utc=NOW)

        deleted = purge_report_screenshots(session_factory, retention_days=30, now=NOW)

        assert deleted == 1
        assert repository.load(old) is None
        assert repository.load(fresh) is not None

    def test_an_image_inside_the_window_survives(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory)
        repository.save(
            report_id,
            image_bytes=PIXELS,
            width=1,
            height=1,
            captured_at_utc=NOW - timedelta(days=29),
        )

        assert purge_report_screenshots(session_factory, retention_days=30, now=NOW) == 0
        assert self._rows(session_factory) == 1

    def test_a_zero_retention_means_keep_everything_not_delete_everything(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """⚠️ 判据与 `purge_system_log` 共用一套：`<= 0` 是「不清理」。

        当成「全删」的话，一个手滑的配置值就能把整批图清空。
        """
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory, at=NOW - timedelta(days=400))
        repository.save(
            report_id,
            image_bytes=PIXELS,
            width=1,
            height=1,
            captured_at_utc=NOW - timedelta(days=400),
        )

        assert purge_report_screenshots(session_factory, retention_days=0, now=NOW) == 0
        assert self._rows(session_factory) == 1

    def test_a_broken_database_never_takes_the_console_down(self) -> None:
        """清理挂在控制台启动上，失败不该让控制台起不来。"""
        assert purge_report_screenshots(None, retention_days=30, now=NOW) == 0  # type: ignore[arg-type]

    def test_the_purge_cuts_on_capture_time_not_report_time(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """补录会把很旧的战报读进来。按战报时刻切的话，那张图一入库就已经过期。"""
        repository = ReportScreenshotRepository(session_factory)
        report_id = _report(session_factory, at=NOW - timedelta(days=200))
        repository.save(report_id, image_bytes=PIXELS, width=1, height=1, captured_at_utc=NOW)

        assert purge_report_screenshots(session_factory, retention_days=30, now=NOW) == 0
        assert repository.load(report_id) is not None


class TestDiagnostics:
    def test_the_total_size_can_be_measured_without_loading_the_images(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """保留期这件事要能先量再调。"""
        repository = ReportScreenshotRepository(session_factory)
        for _ in range(3):
            repository.save(
                _report(session_factory),
                image_bytes=PIXELS,
                width=1,
                height=1,
                captured_at_utc=NOW,
            )

        assert repository.total_bytes() == 3 * len(PIXELS)
