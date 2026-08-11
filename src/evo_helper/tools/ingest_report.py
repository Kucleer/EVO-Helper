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
from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport


class _CapturedScreens:
    """A ``ReportScreens`` stitched from one detail and one replay screenshot."""

    def __init__(self, detail: Path, replay: Path, tesseract_cmd: str) -> None:
        from PIL import Image

        from evo_helper.vision.optional.report_screens import (
            ImageReportScreens,
            locate_sections,
        )

        # 整窗截图带着 Chrome --app 那条 38px 标题栏（1920x917），版面标定是
        # 裁掉它之后的 1920x879。不裁就整份报告读不出来。
        detail_image = crop_to_viewport(Image.open(detail))
        replay_image = crop_to_viewport(Image.open(replay))
        # 参战区与各回合的行界按亮带现场定位。回放内容会滚动，布局里写死的下界
        # 会穿透到下一节——同一批数量被读两遍，合计凭空变形。
        sections = locate_sections(replay_image, layout_for_viewport(*replay_image.size))
        participating = sections[0] if sections else None
        rounds = [(index + 1, top, bottom) for index, (top, bottom) in enumerate(sections[1:])]
        self._detail = ImageReportScreens(
            detail_image,
            layout_for_viewport(detail_image.width, detail_image.height),
            tesseract_cmd=tesseract_cmd,
        )
        self._replay = ImageReportScreens(
            replay_image,
            layout_for_viewport(replay_image.width, replay_image.height),
            rounds=rounds,
            participating_rows=participating,
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
        # 一张截图里能看到几个回合就报几个；再往下的回合要滚动才拍得到。
        return self._replay.round_columns()

    def unit_totals(self) -> tuple[str, str]:
        # 「单位」总数在**详情页**，不在回放页。
        return self._detail.unit_totals()

    def loss_totals(self) -> tuple[str, str]:
        # 「损失单位」同样在详情页。⚠️ 只有那张截图是**拖到底**之后拍的才读得到；
        # 没拖过的详情页上这一行被面板下沿切掉，读回来是半行字 → 留空 → 算不出胜负。
        return self._detail.loss_totals()

    def outcome_banner(self) -> str:
        # 胜负横幅同样只在详情页上；回放页那一屏顶部是 VS 块。
        # 现在只做交叉校验，判据是 `domain.battle_outcome` 那条算式。
        return self._detail.outcome_banner()


def read_report(detail: Path, replay: Path, tesseract_cmd: str) -> LiveBattleReport:
    reader = LiveReportReader(_CapturedScreens(detail, replay, tesseract_cmd))
    return reader.read_report(
        PageObservation(screen="mail_detail", ui_version="battle-detail-v2", confidence=1.0),
        PageObservation(screen="battle_replay", ui_version="battle-replay-v2", confidence=1.0),
    )


def build_parser() -> argparse.ArgumentParser:
    """`--tesseract` 的默认值从配置读，不写死。

    在这里读（而不是模块级常量）是有意的：常量在 import 那一刻就定死了，
    `.env` 或环境变量之后再改都不生效，而那种不生效不报错。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", type=Path, required=True, help="attack report screenshot")
    parser.add_argument("--replay", type=Path, required=True, help="battle replay screenshot")
    parser.add_argument("--tesseract", default=Settings().tesseract_path)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the parsed report without writing to the database",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
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
    print(f"outcome       : {live.outcome or '(not computable)'}")
    print(
        f"units / losses: 我 {live.attacker_units}/{live.attacker_losses}, "
        f"敌 {live.defender_units}/{live.defender_losses}"
    )
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
