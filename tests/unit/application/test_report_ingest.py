from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from evo_helper.application.report_ingest import to_battle_report, ui_observations_for
from evo_helper.domain.models import Coordinate
from evo_helper.vision.live_reports import LiveBattleReport
from evo_helper.vision.models import CoordinateParse, FleetLine, ReplayRound, VersusSide
from evo_helper.vision.parsers import ReportKind

REPORTED_AT = datetime(2026, 8, 6, 11, 45, 3, tzinfo=UTC)


def side(player: str, planet: str, coordinate: Coordinate) -> VersusSide:
    return VersusSide(
        player=player,
        planet=planet,
        coordinate=CoordinateParse(value=coordinate, confidence=1.0, sources=("ocr",)),
    )


def line(name: str, count: int, category: str = "ship") -> FleetLine:
    return FleetLine(
        ship_type=name, count=count, confidence=1.0, sources=("ocr",), category=category
    )


def live_report(**overrides: object) -> LiveBattleReport:
    defaults: dict[str, object] = {
        "kind": ReportKind.ATTACK,
        "raw_time_text": "06/08/2026 11:45:03",
        "reported_at_utc": REPORTED_AT,
        "attacker": side("Kucleer", "奥格瑞玛", Coordinate(2, 137, 18)),
        "defender": side("bot_2_149_17", "bot_2_149_17's Planet", Coordinate(2, 149, 17)),
        "participating_attacker": (line("深空吞噬者", 265), line("钛能守卫者", 178)),
        "participating_defender": (line("轻型战斗机", 461), line("离子炮", 35, "defence")),
        "rounds": (
            ReplayRound(
                round_number=1,
                attacker=(line("深空吞噬者", 265),),
                defender=(line("轻型战斗机", 0),),
            ),
        ),
        "ui_versions": {
            "battle_detail_ui_version": "battle-detail-v2",
            "battle_replay_ui_version": "battle-replay-v2",
        },
    }
    defaults.update(overrides)
    return LiveBattleReport(**defaults)  # type: ignore[arg-type]


class TestToBattleReport:
    def test_maps_both_coordinates(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())

        assert report.attacker_origin == Coordinate(2, 137, 18)
        assert report.defender_target == Coordinate(2, 149, 17)

    def test_keeps_raw_text_and_utc_time(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())

        assert report.raw_time_text == "06/08/2026 11:45:03"
        assert report.reported_at_utc == REPORTED_AT

    def test_participating_fleet_has_no_round(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())
        participating = [entry for entry in report.fleet if entry.round_no is None]

        assert {(e.side, e.ship_type, e.count) for e in participating} == {
            ("attacker", "深空吞噬者", 265),
            ("attacker", "钛能守卫者", 178),
            ("defender", "轻型战斗机", 461),
            ("defender", "离子炮", 35),
        }

    def test_round_entries_carry_their_round_number(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())
        rounds = [entry for entry in report.fleet if entry.round_no is not None]

        assert {(e.side, e.ship_type, e.count, e.round_no) for e in rounds} == {
            ("attacker", "深空吞噬者", 265, 1),
            ("defender", "轻型战斗机", 0, 1),
        }

    def test_zero_count_survives_the_conversion(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())
        zeros = [entry for entry in report.fleet if entry.count == 0]

        assert len(zeros) == 1

    def test_report_screen_version_is_the_detail_version(self) -> None:
        """One column cannot represent the chain; the replay version goes to observations."""
        report = to_battle_report(live_report(), report_id=uuid4())
        assert report.ui_version == "battle-detail-v2"

    def test_uses_the_given_report_id(self) -> None:
        report_id = UUID("11111111-2222-3333-4444-555555555555")
        assert to_battle_report(live_report(), report_id=report_id).report_id == report_id

    def test_refuses_a_non_attack_report(self) -> None:
        with pytest.raises(ValueError, match="attack"):
            to_battle_report(live_report(kind=ReportKind.PIRATE), report_id=uuid4())

    def test_refuses_a_naive_timestamp(self) -> None:
        naive = datetime(2026, 8, 6, 11, 45, 3)
        with pytest.raises(ValueError, match="timezone-aware"):
            to_battle_report(live_report(reported_at_utc=naive), report_id=uuid4())

    def test_starts_unreviewed(self) -> None:
        report = to_battle_report(live_report(), report_id=uuid4())
        assert report.manual_review_status == "PENDING"
        assert report.match_confidence == 0.0


class TestUiObservations:
    def test_records_each_screen_separately(self) -> None:
        observations = ui_observations_for(live_report(), observed_at=REPORTED_AT)

        assert {(o.screen, o.ui_version) for o in observations} == {
            ("battle_detail", "battle-detail-v2"),
            ("battle_replay", "battle-replay-v2"),
        }

    def test_observation_ids_are_distinct(self) -> None:
        observations = ui_observations_for(live_report(), observed_at=REPORTED_AT)
        assert len({o.observation_id for o in observations}) == len(observations)

    def test_observations_are_timezone_aware(self) -> None:
        for observation in ui_observations_for(live_report(), observed_at=REPORTED_AT):
            assert observation.observed_at_utc.tzinfo is not None
