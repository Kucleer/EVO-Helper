from __future__ import annotations

import logging

import pytest

from evo_helper.vision.live_reports import LiveReportReader
from evo_helper.vision.models import PageObservation
from evo_helper.vision.parsers import UnknownUiVersionError

DETAIL = PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=1.0)
REPLAY = PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=1.0)

HEADER = "发件人: System    06/08/2026 11:45:03\n主题: 攻击报告"
VERSUS = "Kucleer    bot_2_149_17\n奥格瑞玛    bot's Planet\n[2:137:18]    [2:149:17]"


class SlowScreens:
    """Each region costs a known, distinct number of ticks."""

    def __init__(self, ticks: dict[str, float] | None = None) -> None:
        self.now = 0.0
        self._ticks = ticks or {"header": 1.0, "versus": 2.0, "fleet": 4.0, "rounds": 8.0}

    def _advance(self, stage: str) -> None:
        self.now += self._ticks[stage]

    def clock(self) -> float:
        return self.now

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        self._advance("header")
        return HEADER

    def versus_block(self) -> str:
        self._advance("versus")
        return VERSUS

    def participating_columns(self) -> tuple[str, str]:
        self._advance("fleet")
        return ("深空吞噬者  265", "轻型战斗机  461")

    def round_columns(self) -> list[tuple[int, str, str]]:
        self._advance("rounds")
        return []


def read(screens: SlowScreens) -> object:
    reader = LiveReportReader(screens, clock=screens.clock)
    return reader.read_report(DETAIL, REPLAY)


class TestTimingIsMeasured:
    def test_report_carries_its_read_timing(self) -> None:
        report = read(SlowScreens())
        assert report.timing is not None  # type: ignore[attr-defined]

    def test_total_is_the_sum_of_the_stages(self) -> None:
        report = read(SlowScreens())
        timing = report.timing  # type: ignore[attr-defined]
        assert timing.total_seconds == pytest.approx(15.0)

    def test_each_stage_is_timed_separately(self) -> None:
        report = read(SlowScreens())
        stages = dict(report.timing.stages)  # type: ignore[attr-defined]
        assert stages["header"] == pytest.approx(1.0)
        assert stages["versus"] == pytest.approx(2.0)
        assert stages["fleet"] == pytest.approx(4.0)
        assert stages["rounds"] == pytest.approx(8.0)

    def test_slowest_stage_is_reported(self) -> None:
        """Which OCR call dominates is the actionable part of the number."""
        report = read(SlowScreens())
        assert report.timing.slowest[0] == "rounds"  # type: ignore[attr-defined]

    def test_timing_reflects_a_different_cost_profile(self) -> None:
        screens = SlowScreens({"header": 9.0, "versus": 1.0, "fleet": 1.0, "rounds": 1.0})
        report = read(screens)
        assert report.timing.slowest[0] == "header"  # type: ignore[attr-defined]
        assert report.timing.total_seconds == pytest.approx(12.0)  # type: ignore[attr-defined]


class TestTimingIsLogged:
    def test_a_successful_read_logs_its_duration(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="evo_helper.vision.live_reports"):
            read(SlowScreens())

        assert any("15.00s" in record.getMessage() for record in caplog.records)

    def test_the_log_names_each_stage(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger="evo_helper.vision.live_reports"):
            read(SlowScreens())

        message = " ".join(record.getMessage() for record in caplog.records)
        for stage in ("header", "versus", "fleet", "rounds"):
            assert stage in message, stage

    def test_a_failed_read_still_logs_how_long_it_took(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A read that fails after 30s is exactly what a timing log should catch."""

        class Blank(SlowScreens):
            def report_header(self) -> str:
                self._advance("header")
                return "发件人: System"

        with caplog.at_level(logging.INFO, logger="evo_helper.vision.live_reports"):
            with pytest.raises(UnknownUiVersionError):
                read(Blank())

        assert any("failed" in record.getMessage() for record in caplog.records)
        assert any("1.00s" in record.getMessage() for record in caplog.records)

    def test_an_unsupported_version_is_logged_as_a_failure(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        screens = SlowScreens()
        reader = LiveReportReader(screens, clock=screens.clock)
        stale = PageObservation(screen="mail_detail", ui_version="battle-detail-v1", confidence=1.0)

        with caplog.at_level(logging.INFO, logger="evo_helper.vision.live_reports"):
            with pytest.raises(UnknownUiVersionError):
                reader.read_report(stale, REPLAY)

        assert any("failed" in record.getMessage() for record in caplog.records)


class TestBackwardsCompatibility:
    def test_the_clock_defaults_to_a_real_monotonic_source(self) -> None:
        """Callers that do not care about timing keep working unchanged."""

        class Screens(SlowScreens):
            def clock(self) -> float:  # pragma: no cover - not used here
                raise AssertionError("default clock should be used")

        reader = LiveReportReader(Screens())
        report = reader.read_report(DETAIL, REPLAY)
        assert report.timing.total_seconds >= 0.0
