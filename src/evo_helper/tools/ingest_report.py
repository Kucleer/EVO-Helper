"""Read a captured attack report from screenshots and persist it.

Read-only against the game: the input is a directory of already-captured PNGs,
not a live browser. This is how a report reaches the local Web UI while live
screenshot capture is still done by hand.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evo_helper.application.report_ingest import to_battle_report, ui_observations_for
from evo_helper.config import Settings
from evo_helper.infrastructure.artifacts import SqlAlchemyUiObservationStore
from evo_helper.infrastructure.logging import configure_logging
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.live_reports import LiveBattleReport, LiveReportReader
from evo_helper.vision.models import PageObservation
from evo_helper.vision.report_layout import layout_for_viewport

DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


class _CapturedScreens:
    """A ``ReportScreens`` stitched from one detail and one replay screenshot."""

    def __init__(self, detail: Path, replay: Path, tesseract_cmd: str) -> None:
        from PIL import Image

        from evo_helper.vision.optional.report_screens import ImageReportScreens

        detail_image = Image.open(detail)
        replay_image = Image.open(replay)
        self._detail = ImageReportScreens(
            detail_image,
            layout_for_viewport(detail_image.width, detail_image.height),
            tesseract_cmd=tesseract_cmd,
        )
        self._replay = ImageReportScreens(
            replay_image,
            layout_for_viewport(replay_image.width, replay_image.height),
            tesseract_cmd=tesseract_cmd,
        )

    def mail_rows(self) -> list[str]:
        return []

    def report_header(self) -> str:
        return self._detail.report_header()

    def versus_block(self) -> str:
        return self._replay.replay_versus_block()

    def participating_columns(self) -> tuple[str, str]:
        return self._replay.participating_columns()

    def round_columns(self) -> list[tuple[int, str, str]]:
        # Round sections need scroll-driven capture; a single screenshot holds
        # only the participating list, so no round is claimed here.
        return []


def read_report(detail: Path, replay: Path, tesseract_cmd: str) -> LiveBattleReport:
    reader = LiveReportReader(_CapturedScreens(detail, replay, tesseract_cmd))
    return reader.read_report(
        PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=1.0),
        PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=1.0),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", type=Path, required=True, help="attack report screenshot")
    parser.add_argument("--replay", type=Path, required=True, help="battle replay screenshot")
    parser.add_argument("--tesseract", default=DEFAULT_TESSERACT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the parsed report without writing to the database",
    )
    args = parser.parse_args(argv)
    log_path = configure_logging()

    for path in (args.detail, args.replay):
        if not path.is_file():
            parser.error(f"screenshot not found: {path}")

    live = read_report(args.detail, args.replay, args.tesseract)
    report = to_battle_report(live, report_id=uuid4())

    print(f"subject kind : {live.kind.value}")
    print(f"reported (raw): {live.raw_time_text}")
    print(f"reported (UTC): {live.reported_at_utc.isoformat()}")
    print(f"attacker      : {live.attacker.player} {live.attacker.coordinate.value}")
    print(f"defender      : {live.defender.player} {live.defender.coordinate.value}")
    print(f"fleet rows    : {len(report.fleet)}")
    print(f"read time     : {live.timing.summary()}")
    print(f"slowest stage : {live.timing.slowest[0]}")
    print(f"log           : {log_path}")
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    settings = Settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    SqlAlchemyRepository(session_factory).append_report(report)
    observation_store = SqlAlchemyUiObservationStore(session_factory)
    for observation in ui_observations_for(live, observed_at=datetime.now(UTC)):
        observation_store.save(observation)

    target = live.defender.coordinate.value
    print(f"stored report {report.report_id}")
    print(f"view: /targets/{target.galaxy}:{target.system}:{target.position}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
