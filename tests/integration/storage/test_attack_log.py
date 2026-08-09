"""攻击日志必须把「没派出去的意图」也算进去。

日志存在的意义是事后复盘「这一发是打谁的、用的什么预设、什么时候打的」。
被闸门拦下、或者简报核对没通过的意图**恰恰是最需要看到的那几条**——
用内连接把它们滤掉，日志会显得一片干净，而真相是那几发压根没打出去。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView

ORIGIN = Coordinate(2, 137, 18)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
CREATED = datetime(2026, 8, 8, 13, 0, tzinfo=UTC)
PRESET = FleetPresetRef(name="探路", signature="小型运输船:1")


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'log.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory)
    plan = service.create_plan(
        name="log",
        enabled=True,
        window_start=datetime(2026, 1, 1, 8).time(),
        window_end=datetime(2026, 1, 1, 10).time(),
        dry_run=True,
        ranges=(
            ScanRangeView(
                Coordinate(2, 137, 1),
                Coordinate(2, 137, 4),
                ORIGIN,
                PRESET.name,
                PRESET.signature,
                0,
            ),
        ),
    )
    run = service.start_run(plan.id, "idem-log")
    return SqlAlchemyRepository(factory), service, run.run_id


def _intent(run_id, target: Coordinate, kind: str, minutes: int) -> AttackIntent:  # type: ignore[no-untyped-def]
    return AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=target,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=CREATED + timedelta(minutes=minutes),
        target_kind=kind,
    )


def test_an_intent_that_never_dispatched_still_appears(tmp_path: Path) -> None:
    repo, service, run_id = _setup(tmp_path)
    repo.save_attack_intent(_intent(run_id, Coordinate(2, 137, 2), TARGET_KIND_PIRATE, 0))

    (entry,) = service.list_attack_log(50)

    assert entry.dispatched_at_utc is None
    assert entry.target_kind == TARGET_KIND_PIRATE


def test_a_dispatched_intent_carries_the_dispatch_facts(tmp_path: Path) -> None:
    repo, service, run_id = _setup(tmp_path)
    intent = _intent(run_id, Coordinate(2, 137, 14), TARGET_KIND_BOT, 0)
    repo.save_attack_intent(intent)
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=CREATED + timedelta(minutes=1),
            dry_run=False,
            accepted=True,
        )
    )

    (entry,) = service.list_attack_log(50)

    assert entry.dispatched_at_utc == CREATED + timedelta(minutes=1)
    assert entry.accepted is True
    assert entry.dry_run is False
    assert entry.preset_name == "探路"


def test_the_newest_attack_is_listed_first(tmp_path: Path) -> None:
    """日志页第一眼要看的是刚刚发生了什么。"""
    repo, service, run_id = _setup(tmp_path)
    repo.save_attack_intent(_intent(run_id, Coordinate(2, 137, 1), TARGET_KIND_PIRATE, 0))
    repo.save_attack_intent(_intent(run_id, Coordinate(2, 137, 3), TARGET_KIND_PIRATE, 30))

    positions = [entry.target.position for entry in service.list_attack_log(50)]

    assert positions == [3, 1]


def test_the_battle_result_reaches_the_log(tmp_path: Path) -> None:
    """打完之后日志要能回答「打赢了吗、损了多少」。

    海盗战报只记胜负与战损总数（用户口径 2026-08-09），所以这两样就是战果的全部；
    日志页取的正是同一份数据，不另算一遍。
    """
    repo, service, run_id = _setup(tmp_path)
    target = Coordinate(2, 137, 4)
    intent = _intent(run_id, target, TARGET_KIND_PIRATE, 0)
    repo.save_attack_intent(intent)
    dispatched = CREATED + timedelta(minutes=1)
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=dispatched,
            dry_run=False,
            accepted=True,
        )
    )
    repo.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=dispatched + timedelta(minutes=20),
            attacker_origin=ORIGIN,
            defender_target=target,
            outcome=OUTCOME_VICTORY,
            attacker_losses=0,
            defender_losses=783,
        )
    )

    (entry,) = service.list_attack_log(50)

    assert entry.outcome == OUTCOME_VICTORY
    assert (entry.attacker_losses, entry.defender_losses) == (0, 783)


def test_an_attack_still_in_flight_has_no_result_yet(tmp_path: Path) -> None:
    """还没回战报的那一发，战果必须是空的——不能显示成「零损失」。"""
    repo, service, run_id = _setup(tmp_path)
    intent = _intent(run_id, Coordinate(2, 137, 4), TARGET_KIND_PIRATE, 0)
    repo.save_attack_intent(intent)
    repo.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=CREATED + timedelta(minutes=1),
            dry_run=False,
            accepted=True,
        )
    )

    (entry,) = service.list_attack_log(50)

    assert entry.outcome is None
    assert entry.attacker_losses is None


def test_existing_rows_default_to_bot(tmp_path: Path) -> None:
    """这个字段加进来之前，只有 bot 攻击链路会写意图。"""
    repo, service, run_id = _setup(tmp_path)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=Coordinate(2, 137, 14),
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=CREATED,
    )
    repo.save_attack_intent(intent)

    (entry,) = service.list_attack_log(50)

    assert entry.target_kind == TARGET_KIND_BOT
