"""战报详情只看战前的参战舰队，不能把逐回合的剩余战舰混进来。

实测事故：2:137:14 的舰队详情弹窗显示「合计 157」、`重型战斗机` 出现两次，
而同一份数据在情报中心列表里是「8 种 / 81」。157 = 参战 81 + 第 1 回合 76。

列表页（`intel._defender_counts`）从一开始就过滤了 `round_no`，详情页
（`persistent_service._report_view`）没有——**同一条判据在两个地方各写一份**，
于是一处对、一处错，而且两处都不报错。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import BattleReport, FleetSnapshotEntry
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.persistent_service import PersistentApplicationService
from support.database import scratch_database_url

TARGET = Coordinate(2, 137, 14)
ORIGIN = Coordinate(2, 137, 18)

#: 参战 81 = 17 + 31 + 33；第 1 回合另有 76。两者相加正是那个假的 157。
PARTICIPATING = (("轻型战斗机", 17), ("重型战斗机", 31), ("巡洋舰", 33))
ROUND_ONE = ("轻型战斗机", 45), ("重型战斗机", 31)


def _service(tmp_path: Path) -> PersistentApplicationService:
    engine = create_database_engine(scratch_database_url(tmp_path, "detail.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    fleet = [
        FleetSnapshotEntry(side="defender", ship_type=ship, count=count, round_no=None)
        for ship, count in PARTICIPATING
    ]
    fleet += [
        FleetSnapshotEntry(side="defender", ship_type=ship, count=count, round_no=1)
        for ship, count in ROUND_ONE
    ]
    SqlAlchemyRepository(factory).append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=datetime(2026, 8, 8, 13, 9, 51, tzinfo=UTC),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            raw_time_text="08/08/2026 13:09:51",
            fleet=tuple(fleet),
        )
    )
    return PersistentApplicationService(factory)


def test_the_detail_view_counts_the_pre_battle_fleet_only(tmp_path: Path) -> None:
    (snapshot,) = _service(tmp_path).get_history(TARGET)

    assert snapshot.total == 81


def test_a_ship_type_is_never_listed_twice(tmp_path: Path) -> None:
    """`重型战斗机` 在参战区和第 1 回合各有一行，弹窗里当时并排显示了两遍。"""
    (snapshot,) = _service(tmp_path).get_history(TARGET)

    names = [entry.ship_type for entry in snapshot.ships]
    assert sorted(names) == sorted({ship for ship, _ in PARTICIPATING})
