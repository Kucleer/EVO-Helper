"""Convert a read report into the domain records the repository persists."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from evo_helper.domain.battle_outcome import OUTCOME_PROTECTED
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    BattleReport,
    FleetSnapshotEntry,
    PlanetScoutAlert,
    ScoutReport,
    ScoutTriggerShip,
    UiObservation,
)
from evo_helper.vision.live_reports import LiveBattleReport
from evo_helper.vision.models import FleetLine
from evo_helper.vision.pirate_reports import PirateReportReading
from evo_helper.vision.planet_scout_alert import PlanetScoutAlertReading
from evo_helper.vision.scout_reports import PirateScoutReading

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
        # 战果与战损一起落库。`outcome` **优先来自画面横幅**，横幅读不出来才回落到
        # 按剩余舰艇数算的结果（用户口径 2026-08-17，仲裁见
        # `vision.pirate_reports.decide_outcome`）；两条都不成时是 None——
        # **不能填 `FAIL` 顶替**，那会在攻击日志的战果列上凭空造出一场败仗
        # （见 `BattleReport.outcome`）。
        # 战损那两个数既是页面上「战损 我 X · 敌 Y」的来源，也是算式的输入之一，
        # 落库之后才能回头核「当初凭什么判成这个结果」。
        outcome=live.outcome,
        attacker_losses=live.attacker_losses,
        defender_losses=live.defender_losses,
        fleet=tuple(fleet),
        # 「获得资源」那 12 格里非零的几格。**空元组照样原样传下去**：
        # 库里「没有行 = 这一格是 0」，而读不全的那种情况在读的那一层就
        # 已经整块作废了（`domain.battle_resources.parse_resource_grid`）。
        resources=live.resources,
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
        resources=reading.resources,
    )


def to_scout_report(reading: PirateScoutReading, *, report_id: UUID) -> ScoutReport:
    """把一份侦察报告的读数落成 `ScoutReport`。**一个字段都不许丢。**

    尤其是 `missing`：它记的是「这四个触发舰种里，哪几格没读出来」，在库里落成
    `count IS NULL`。**不要把它折叠成 0**——读空当成 0 就是把「没看清」记成
    「这里是空的」，下一轮据此判「不值得打」，一支实打实的舰队就此被放过
    （`PirateScoutReading.missing` 与 `verdict` 的注释写着完整的来龙去脉）。

    条目顺序是「读出来的在前、没读出来的在后」，与 `to_scout_reading` 成对——
    这样 `missing` 那个有序元组读回来能一模一样。这里**不引用
    `PIRATE_TRIGGER_SHIPS`**：读数里有什么就存什么，规则表以后增删舰种，
    已经存下来的报告仍然是当时读到的那份。
    """
    if reading.reported_at_utc.tzinfo is None:
        raise ValueError("reported_at_utc must be timezone-aware")
    entries = [
        ScoutTriggerShip(ship_type=name, count=count)
        for name, count in reading.trigger_ships.items()
    ]
    entries.extend(ScoutTriggerShip(ship_type=name, count=None) for name in reading.missing)
    return ScoutReport(
        report_id=report_id,
        reported_at_utc=reading.reported_at_utc,
        raw_time_text=reading.raw_time_text,
        origin=reading.origin,
        target=reading.target,
        trigger_ships=tuple(entries),
    )


def to_protection_bounce_report(
    *,
    report_id: UUID,
    target: Coordinate,
    origin: Coordinate,
    mail_at_utc: datetime,
    raw_time_text: str | None,
) -> BattleReport:
    """「到达时撞保护期」那一发的结账行。

    ⚠️ **除了两个坐标、时刻和 `outcome`，其余一律留空——那些数不存在，
    不是没读到。** 没有战斗就没有参战舰队、没有单位数、没有战损、没有收获格。
    填 0 会让这一发变成「打赢了但一无所获」，直接污染收益统计；那正是
    `domain.battle_outcome.OUTCOME_PROTECTED` 上写着要防的事。

    `match_confidence` 也留 0：认领由 `repository.append_report` 自己按
    「出发点 + 目标 + 抵达窗口」现认，这里不许预先断言一个没核过的置信度
    （同 `to_battle_report` 的规矩）。出发点是从**认出来的那一发派遣**上取的，
    不是从画面上读的——这封信里根本没写出发点。
    """
    if mail_at_utc.tzinfo is None:
        raise ValueError("mail_at_utc must be timezone-aware")
    return BattleReport(
        report_id=report_id,
        reported_at_utc=mail_at_utc,
        attacker_origin=origin,
        defender_target=target,
        raw_time_text=raw_time_text,
        outcome=OUTCOME_PROTECTED,
    )


def to_planet_scout_alert(reading: PlanetScoutAlertReading, *, alert_id: UUID) -> PlanetScoutAlert:
    """Map a parsed foreign-reconnaissance mail into immutable local evidence."""
    return PlanetScoutAlert(
        alert_id=alert_id,
        reported_at_utc=reading.reported_at_utc,
        raw_time_text=reading.raw_time_text,
        source=reading.source,
        target=reading.target,
        subject=reading.subject,
        raw_body=reading.raw_body,
        source_name=reading.source_name,
        intercepted_probes=reading.intercepted_probes,
    )


def to_scout_reading(record: ScoutReport) -> PirateScoutReading:
    """把库里那份读回成 `PirateScoutReading`，好让 `verdict` 现算。

    库里**不存 verdict**（见 `ScoutReport` 的注释：那是一条会变的规则）。
    要问「当时那份报告算下来是打还是不打」，就把证据读回来、按现行规则算一遍——
    这样库里的行与活链路当场的判定永远出自同一段代码。
    """
    return PirateScoutReading(
        raw_time_text=record.raw_time_text,
        reported_at_utc=record.reported_at_utc,
        origin=record.origin,
        target=record.target,
        trigger_ships={
            entry.ship_type: entry.count
            for entry in record.trigger_ships
            if entry.count is not None
        },
        missing=tuple(entry.ship_type for entry in record.trigger_ships if entry.count is None),
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
