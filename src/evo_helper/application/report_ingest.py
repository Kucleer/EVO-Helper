"""Convert a read report into the domain records the repository persists."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from evo_helper.domain.records import BattleReport, FleetSnapshotEntry, UiObservation
from evo_helper.vision.live_reports import LiveBattleReport
from evo_helper.vision.models import FleetLine
from evo_helper.vision.pirate_reports import PirateReportReading

# The repository and Web service filter snapshots on these exact strings.
ATTACKER = "attacker"
DEFENDER = "defender"


def to_battle_report(live: LiveBattleReport, *, report_id: UUID) -> BattleReport:
    """Map a read report onto :class:`BattleReport`.

    The report is stored unmatched and unreviewed. Matching a dispatch is the
    repository's job, and it needs the origin, target and time to agree — this
    function must not pre-assert a confidence it has not checked.
    """
    if not live.kind.is_dispatch_matchable:
        raise ValueError(f"refusing to ingest a non-attack report: {live.kind.value}")
    if live.reported_at_utc.tzinfo is None:
        raise ValueError("reported_at_utc must be timezone-aware")

    fleet: list[FleetSnapshotEntry] = []
    fleet.extend(_entries(ATTACKER, live.participating_attacker, None))
    fleet.extend(_entries(DEFENDER, live.participating_defender, None))
    for round_ in live.rounds:
        fleet.extend(_entries(ATTACKER, round_.attacker, round_.round_number))
        fleet.extend(_entries(DEFENDER, round_.defender, round_.round_number))

    return BattleReport(
        report_id=report_id,
        attacker_units=live.attacker_units,
        defender_units=live.defender_units,
        reported_at_utc=live.reported_at_utc,
        attacker_origin=live.attacker.coordinate.value,
        defender_target=live.defender.coordinate.value,
        raw_time_text=live.raw_time_text,
        # The row holds one version, so it holds the report screen's own.
        # Section 3 forbids one label for the whole chain, so the replay
        # version is recorded separately by ui_observations_for().
        ui_version=live.ui_versions.get("battle_detail_ui_version"),
        fleet=tuple(fleet),
    )


#: 海盗战报详情页的界面版本标签。海盗那条链路只看详情页，没有回放页那一屏，
#: 所以它的观测只有一条——不能沿用 `battle-detail-v2`：版本标签是「这一屏长什么样」
#: 的凭据，两条读法不同的链路共用一个标签，日后界面改版时分不清是哪一条失效了。
PIRATE_DETAIL_UI_VERSION = "pirate-detail-v1"


def to_pirate_battle_report(reading: PirateReportReading, *, report_id: UUID) -> BattleReport:
    """把海盗战报的轻量读数落成 `BattleReport`。

    **`fleet` 一定是空的**，这是用户定的口径（2026-08-09）：海盗战报只记胜负与
    战损总数。不要「顺手」把参战明细也塞进来——那需要进回放页，一份报告多花两三秒，
    而这条链路的存在理由就是省掉它。
    """
    if reading.reported_at_utc.tzinfo is None:
        raise ValueError("reported_at_utc must be timezone-aware")
    return BattleReport(
        report_id=report_id,
        reported_at_utc=reading.reported_at_utc,
        attacker_origin=reading.attacker_origin,
        defender_target=reading.defender_target,
        raw_time_text=reading.raw_time_text,
        ui_version=PIRATE_DETAIL_UI_VERSION,
        attacker_units=reading.attacker_units,
        defender_units=reading.defender_units,
        outcome=reading.outcome,
        attacker_losses=reading.attacker_losses,
        defender_losses=reading.defender_losses,
    )


def ui_observations_for(
    live: LiveBattleReport, *, observed_at: datetime
) -> tuple[UiObservation, ...]:
    """One observation per screen, so no version is implied by another's."""
    screens = {
        "battle_detail": live.ui_versions.get("battle_detail_ui_version"),
        "battle_replay": live.ui_versions.get("battle_replay_ui_version"),
    }
    return tuple(
        UiObservation(
            observation_id=uuid4(),
            screen=screen,
            ui_version=version,
            detection_result="report ingested",
            confidence=1.0,
            observed_at_utc=observed_at,
        )
        for screen, version in screens.items()
    )


def _entries(
    side: str, lines: tuple[FleetLine, ...], round_no: int | None
) -> list[FleetSnapshotEntry]:
    return [
        FleetSnapshotEntry(side=side, ship_type=line.ship_type, count=line.count, round_no=round_no)
        for line in lines
    ]
