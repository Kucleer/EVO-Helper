"""战报收获明细的落库。

⚠️ 这里守的是那条**只写在 docstring 里的语义**：**没有行 = 那一格是 0**。
库里只存非零的格子，所以「一份战报有几行」这件事本身就是数据——多写一行 0
或者少写一行，事后都没有任何办法分辨。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import BattleReport, BattleResourceEntry
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository

REPORTED_AT = datetime(2026, 8, 17, 3, 24, tzinfo=UTC)

#: 用户 2026-08-17 那份 VICTORY 战报，非零的八格。
HAUL = (
    BattleResourceEntry(slot=0, amount=928_000, approximate=True, uncertainty=500),
    BattleResourceEntry(slot=1, amount=501_100, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=2, amount=342_900, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=3, amount=7_700, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=5, amount=1_200, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=6, amount=233),
    BattleResourceEntry(slot=8, amount=66),
    BattleResourceEntry(slot=9, amount=4),
)


def _report(resources: tuple[BattleResourceEntry, ...]) -> BattleReport:
    return BattleReport(
        report_id=uuid4(),
        reported_at_utc=REPORTED_AT,
        attacker_origin=Coordinate(2, 137, 18),
        defender_target=Coordinate(2, 137, 14),
        resources=resources,
    )


def _rows(
    session_factory: sessionmaker[Session], report_id: object
) -> list[orm.BattleReportResourceRow]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(orm.BattleReportResourceRow)
                .where(orm.BattleReportResourceRow.report_id == report_id)
                .order_by(orm.BattleReportResourceRow.slot)
            )
        )


class TestWritingTheHaul:
    def test_every_non_zero_slot_lands_with_its_precision_marks(
        self, repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
    ) -> None:
        """槽位、数量、近似标记、误差，四样一起进库。

        ⚠️ **`approximate` 与 `uncertainty` 不是装饰。** `928K` 的真值取不回来了，
        误差是 ±500；`233` 是精确读到的。两者在页面上必须看得出区别。
        """
        report = _report(HAUL)

        repository.append_report(report)

        rows = _rows(session_factory, report.report_id)
        assert [(row.slot, row.amount, row.approximate, row.uncertainty) for row in rows] == [
            (0, 928_000, True, 500),
            (1, 501_100, True, 50),
            (2, 342_900, True, 50),
            (3, 7_700, True, 50),
            (5, 1_200, True, 50),
            (6, 233, False, 0),
            (8, 66, False, 0),
            (9, 4, False, 0),
        ]

    def test_zero_slots_leave_no_row_at_all(
        self, repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
    ) -> None:
        """槽位 4/7/10/11 是 0，库里一行都没有——**这就是那条语义本身**。"""
        report = _report(HAUL)

        repository.append_report(report)

        assert {row.slot for row in _rows(session_factory, report.report_id)} == {
            0,
            1,
            2,
            3,
            5,
            6,
            8,
            9,
        }

    def test_a_blank_haul_still_stores_the_report(
        self, repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
    ) -> None:
        """⚠️ **12 格全 0 的战报照样要入库。**

        收获是附加项，不是战报的存在条件。因为「没有明细行」而拒收战报，
        等于把白打的那些发次整个从账上抹掉——而它们正是要看的数据之一。
        """
        report = _report(())

        repository.append_report(report)

        assert _rows(session_factory, report.report_id) == []
        with session_factory() as session:
            assert session.get(orm.BattleReportRow, report.report_id) is not None

    def test_amounts_survive_past_the_32_bit_ceiling(
        self, repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
    ) -> None:
        """`B` 后缀已经在解析器里，`BigInteger` 就得真的是 64 位。"""
        report = _report(
            (
                BattleResourceEntry(
                    slot=0, amount=9_400_000_000, approximate=True, uncertainty=50_000_000
                ),
            )
        )

        repository.append_report(report)

        assert _rows(session_factory, report.report_id)[0].amount == 9_400_000_000


class TestTheUniqueConstraint:
    def test_one_slot_cannot_be_written_twice(
        self, repository: SqlAlchemyRepository, session_factory: sessionmaker[Session]
    ) -> None:
        """同一份战报的同一格只能有一行。

        重复读到同一份战报走的是「库里已有」那条早停路径，本来就到不了这里；
        真撞上了，宁可写失败也不要在库里攒出两份收获——那会让「这一发捞了多少」
        变成一个要靠去重才答得出的问题。
        """
        report = _report((BattleResourceEntry(slot=0, amount=66),))
        repository.append_report(report)

        with pytest.raises(IntegrityError), session_factory() as session:
            session.add(
                orm.BattleReportResourceRow(
                    report_id=report.report_id, slot=0, amount=99, approximate=False, uncertainty=0
                )
            )
            session.commit()
