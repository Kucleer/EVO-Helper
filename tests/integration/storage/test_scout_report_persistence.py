"""侦察报告落库：`missing` 原样存回、同一份只写一行、判定不因入库而变。

这三条守的是同一件事——**库里存的是证据，不是结论**。

最要命的是 `missing`：它记的是「这四个触发舰种里，哪几格没读出来」。数量为 0 的
格子在画面上只是一个孤零零的 `0`，实测最容易读空，而读空当成 0 就把「没看清」
记成了「这里是空的」。三值判定（ATTACK / SKIP / UNREADABLE）整个建立在这个区分上，
所以只要存库时把 `missing` 丢掉、或者把没读到的格子补成 0，下一轮就会据此判
「不值得打」，一支实打实的舰队就此被放过。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select

from evo_helper.application.report_ingest import to_scout_reading, to_scout_report
from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.scout_reports import (
    VERDICT_ATTACK,
    VERDICT_SKIP,
    VERDICT_UNREADABLE,
    PirateScoutReading,
)
from support.database import scratch_database_url

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
REPORTED_AT = datetime(2026, 8, 11, 3, 32, 11, tzinfo=UTC)


def _reading(
    *,
    trigger_ships: dict[str, int] | None = None,
    missing: tuple[str, ...] = (),
    target: Coordinate = TARGET,
    reported_at: datetime = REPORTED_AT,
) -> PirateScoutReading:
    return PirateScoutReading(
        raw_time_text="11/08/2026 03:32:11",
        reported_at_utc=reported_at,
        origin=ORIGIN,
        target=target,
        trigger_ships=(
            {"深空吞噬者": 2, "噬能截击者": 4, "钛能守卫者": 0, "收割者": 0}
            if trigger_ships is None
            else trigger_ships
        ),
        missing=missing,
    )


def _repository(tmp_path: Path) -> tuple[SqlAlchemyRepository, object]:
    engine = create_database_engine(scratch_database_url(tmp_path, "scout.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    return SqlAlchemyRepository(factory), factory


def _roundtrip(tmp_path: Path, reading: PirateScoutReading) -> PirateScoutReading:
    """存进去再读回来。中间不许有任何「顺手补个默认值」。"""
    repository, _factory = _repository(tmp_path)
    repository.append_scout_report(to_scout_report(reading, report_id=uuid4()))
    stored = repository.list_scout_reports()
    assert len(stored) == 1
    return to_scout_reading(stored[0])


# -- missing 原样存回 --------------------------------------------------------


def test_a_missing_slot_survives_the_roundtrip(tmp_path: Path) -> None:
    """本文件的重点。没读出来的那一格，读回来还得是「没读出来」。"""
    reading = _reading(
        trigger_ships={"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0},
        missing=("钛能守卫者",),
    )

    back = _roundtrip(tmp_path, reading)

    assert back.missing == ("钛能守卫者",)
    assert "钛能守卫者" not in back.trigger_ships
    assert back.trigger_ships == {"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0}


def test_a_missing_slot_is_stored_as_null_not_zero(tmp_path: Path) -> None:
    """库里那一列必须是 NULL。补成 0 就是把「没看清」记成「这里是空的」。"""
    repository, factory = _repository(tmp_path)
    repository.append_scout_report(
        to_scout_report(
            _reading(
                trigger_ships={"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0},
                missing=("钛能守卫者",),
            ),
            report_id=uuid4(),
        )
    )

    with factory() as session:  # type: ignore[operator]
        rows = session.scalars(select(orm.ScoutTriggerShipRow)).all()
        counts = {row.ship_type: row.count for row in rows}

    assert counts["钛能守卫者"] is None
    # 反面同样要成立：真读出来的 0 存的就是 0，不许被当成「没读到」。
    assert counts["深空吞噬者"] == 0
    assert counts["深空吞噬者"] is not None


def test_a_read_zero_stays_a_read_zero(tmp_path: Path) -> None:
    """四格全读到、全是 0：`missing` 必须是空的，否则判定会从 SKIP 滑成 UNREADABLE。"""
    reading = _reading(trigger_ships={name: 0 for name in ("深空吞噬者", "噬能截击者", "收割者")})

    back = _roundtrip(tmp_path, reading)

    assert back.missing == ()
    assert back.trigger_ships == {"深空吞噬者": 0, "噬能截击者": 0, "收割者": 0}


# -- 判定：库里读出来的 == 直接算的 -------------------------------------------


def test_a_fleet_verdict_survives_the_roundtrip(tmp_path: Path) -> None:
    reading = _reading(
        trigger_ships={"深空吞噬者": 2, "噬能截击者": 0, "钛能守卫者": 0, "收割者": 0}
    )

    assert reading.verdict == VERDICT_ATTACK
    assert _roundtrip(tmp_path, reading).verdict == reading.verdict


def test_an_empty_pirate_verdict_survives_the_roundtrip(tmp_path: Path) -> None:
    reading = _reading(
        trigger_ships={"深空吞噬者": 0, "噬能截击者": 1, "钛能守卫者": 0, "收割者": 1}
    )

    assert reading.verdict == VERDICT_SKIP
    assert _roundtrip(tmp_path, reading).verdict == reading.verdict


def test_an_unreadable_verdict_survives_the_roundtrip(tmp_path: Path) -> None:
    """最危险的一档：小数目 + 有格子没读出来，结论是「不下结论」。

    只要 `missing` 在存或读的路上被抹掉一次，这条就会变成 SKIP——
    也就是把一个可能有舰队的海盗记成「不值得打」。
    """
    reading = _reading(
        trigger_ships={"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0},
        missing=("钛能守卫者",),
    )

    assert reading.verdict == VERDICT_UNREADABLE
    assert _roundtrip(tmp_path, reading).verdict == reading.verdict


def test_every_stored_field_survives_the_roundtrip(tmp_path: Path) -> None:
    """`PirateScoutReading` 的六个字段一个都不能丢。"""
    reading = _reading(
        trigger_ships={"深空吞噬者": 2, "噬能截击者": 4, "钛能守卫者": 0},
        missing=("收割者",),
    )

    back = _roundtrip(tmp_path, reading)

    assert back.raw_time_text == reading.raw_time_text
    assert back.reported_at_utc == reading.reported_at_utc
    assert back.origin == reading.origin
    assert back.target == reading.target
    assert back.trigger_ships == reading.trigger_ships
    assert back.missing == reading.missing


# -- 去重 --------------------------------------------------------------------


def test_the_same_report_read_twice_is_one_row(tmp_path: Path) -> None:
    """活链路每一轮都会翻信箱里同样那几行。没有去重，一份报告会每趟复制一行。"""
    repository, factory = _repository(tmp_path)
    reading = _reading()

    first = repository.append_scout_report(to_scout_report(reading, report_id=uuid4()))
    second = repository.append_scout_report(to_scout_report(reading, report_id=uuid4()))

    assert (first, second) == (True, False)
    with factory() as session:  # type: ignore[operator]
        assert len(session.scalars(select(orm.ScoutReportRow)).all()) == 1
        # 明细也不能重：一份报告四格，写两次就成八格，读回来的 `missing` 全乱。
        assert len(session.scalars(select(orm.ScoutTriggerShipRow)).all()) == 4
    assert len(repository.list_scout_reports()) == 1


def test_has_scout_report_at_uses_target_and_time(tmp_path: Path) -> None:
    """去重口径与 `has_report_at` 一致：目标 + 报告时间，两者都要对上。"""
    repository, _factory = _repository(tmp_path)
    repository.append_scout_report(to_scout_report(_reading(), report_id=uuid4()))

    assert repository.has_scout_report_at(TARGET, REPORTED_AT)
    assert not repository.has_scout_report_at(Coordinate(2, 137, 1), REPORTED_AT)
    assert not repository.has_scout_report_at(TARGET, REPORTED_AT.replace(second=12))


def test_the_same_target_at_a_different_time_is_a_new_report(tmp_path: Path) -> None:
    """重复侦察正是要查的东西——同一颗星球一天被侦察六次，库里就该有六行。"""
    repository, _factory = _repository(tmp_path)
    for minute in (34, 46, 52):
        repository.append_scout_report(
            to_scout_report(
                _reading(reported_at=REPORTED_AT.replace(minute=minute)), report_id=uuid4()
            )
        )

    stored = repository.list_scout_reports(target=TARGET)

    assert [report.reported_at_utc.minute for report in stored] == [34, 46, 52]


# -- 不许溢出到别的表 ---------------------------------------------------------


def test_a_scout_report_writes_neither_battle_reports_nor_fleet_snapshots(tmp_path: Path) -> None:
    """侦察报告认领不了任何派遣，也不是舰队快照。

    写进 `battle_reports` 会凭空多出一行「没认领上的战报」，让判态那一侧以为
    还有一发攻击在等回音；写进 `fleet_snapshots` 会让情报中心把只读了四行的
    触发舰种显示成对方的全部家当。
    """
    repository, factory = _repository(tmp_path)
    repository.append_scout_report(to_scout_report(_reading(), report_id=uuid4()))

    with factory() as session:  # type: ignore[operator]
        assert session.scalars(select(orm.BattleReportRow)).all() == []
        assert session.scalars(select(orm.FleetSnapshotRow)).all() == []


def test_reports_come_back_in_report_time_order(tmp_path: Path) -> None:
    """按报告时间升序，且 `since` / `until` 是 UTC 上的左闭右开区间。"""
    repository, _factory = _repository(tmp_path)
    for hour, position in ((23, 1), (1, 2), (12, 3)):
        repository.append_scout_report(
            to_scout_report(
                _reading(
                    target=Coordinate(2, 137, position),
                    reported_at=datetime(2026, 8, 11, hour, 0, 0, tzinfo=UTC),
                ),
                report_id=uuid4(),
            )
        )

    day = repository.list_scout_reports(
        since=datetime(2026, 8, 11, tzinfo=UTC), until=datetime(2026, 8, 12, tzinfo=UTC)
    )

    assert [report.reported_at_utc.hour for report in day] == [1, 12, 23]
    assert repository.list_scout_reports(until=datetime(2026, 8, 11, 12, tzinfo=UTC)) == day[:1]
