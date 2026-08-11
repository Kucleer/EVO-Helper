from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from evo_helper.domain.intel_query import (
    ConditionGroup,
    FleetCondition,
    GroupOperator,
    Operator,
    QueryField,
    parse_coordinate_span,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import BattleReport, FleetSnapshotEntry
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.intel import IntelSearchQuery, SqlAlchemyIntelRepository
from evo_helper.storage.repository import SqlAlchemyRepository

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'intel.db'}")
    Base.metadata.create_all(engine)
    return create_session_factory(engine)


def add_target(session_factory, coordinate: Coordinate, player: str) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(
            orm.BotTargetRow(
                id=uuid4(),
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                latest_owner_name=player,
                is_bot=True,
            )
        )
        session.commit()


def add_report(
    session_factory,  # type: ignore[no-untyped-def]
    coordinate: Coordinate,
    counts: dict[str, int],
    *,
    at: datetime,
) -> None:
    SqlAlchemyRepository(session_factory).append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=at,
            attacker_origin=Coordinate(2, 137, 18),
            defender_target=coordinate,
            fleet=tuple(
                FleetSnapshotEntry(side="defender", ship_type=name, count=count)
                for name, count in counts.items()
            ),
        )
    )


def guardians_over(value: int) -> FleetCondition:
    return FleetCondition(field=QueryField.ship("钛能守卫者"), operator=Operator.GT, value=value)


def total_over(value: int) -> FleetCondition:
    return FleetCondition(field=QueryField.total(), operator=Operator.GT, value=value)


@pytest.fixture
def populated(session_factory):  # type: ignore[no-untyped-def]
    """Three in-range bots plus one outside the range."""
    hit = Coordinate(1, 150, 4)
    low_total = Coordinate(1, 160, 7)
    no_guardians = Coordinate(1, 170, 2)
    outside = Coordinate(2, 150, 4)
    for coordinate, player in (
        (hit, "bot_1_150_4"),
        (low_total, "bot_1_160_7"),
        (no_guardians, "bot_1_170_2"),
        (outside, "bot_2_150_4"),
    ):
        add_target(session_factory, coordinate, player)
    add_report(session_factory, hit, {"钛能守卫者": 6, "轻型战斗机": 2500}, at=BASE_TIME)
    add_report(session_factory, low_total, {"钛能守卫者": 9, "轻型战斗机": 10}, at=BASE_TIME)
    add_report(session_factory, no_guardians, {"轻型战斗机": 5000}, at=BASE_TIME)
    add_report(session_factory, outside, {"钛能守卫者": 99, "轻型战斗机": 9000}, at=BASE_TIME)
    return session_factory


def search(session_factory, **kwargs):  # type: ignore[no-untyped-def]
    defaults = {
        "span": parse_coordinate_span("1:100", "1:200"),
        "conditions": ConditionGroup(
            operator=GroupOperator.AND, children=(total_over(2000), guardians_over(5))
        ),
    }
    defaults.update(kwargs)
    return SqlAlchemyIntelRepository(session_factory).search(IntelSearchQuery(**defaults))


class TestWorkedExample:
    """1:100-1:200, total > 2000, 钛能守卫者 > 5 — the spec's example."""

    def test_returns_only_the_matching_target(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated)
        assert [str(row.coordinate) for row in page.rows] == ["1:150:4"]

    def test_excludes_targets_outside_the_range(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated)
        assert all(row.coordinate.galaxy == 1 for row in page.rows)

    def test_row_carries_the_snapshot_summary(self, populated) -> None:  # type: ignore[no-untyped-def]
        row = search(populated).rows[0]
        assert row.player == "bot_1_150_4"
        assert row.total == 2506
        assert row.snapshot_at == BASE_TIME

    def test_row_reports_which_conditions_hit(self, populated) -> None:  # type: ignore[no-untyped-def]
        row = search(populated).rows[0]
        assert "舰队总数" in row.matched_summary
        assert "钛能守卫者" in row.matched_summary


class TestLatestSnapshotWins:
    def test_only_the_newest_report_is_matched(self, populated) -> None:  # type: ignore[no-untyped-def]
        """An old matching snapshot must not keep a target in the results."""
        coordinate = Coordinate(1, 150, 4)
        add_report(
            populated,
            coordinate,
            {"钛能守卫者": 1, "轻型战斗机": 10},
            at=BASE_TIME + timedelta(days=1),
        )
        assert search(populated).rows == ()

    def test_a_newer_snapshot_can_bring_a_target_in(self, populated) -> None:  # type: ignore[no-untyped-def]
        coordinate = Coordinate(1, 170, 2)
        add_report(
            populated,
            coordinate,
            {"钛能守卫者": 8, "轻型战斗机": 4000},
            at=BASE_TIME + timedelta(days=1),
        )
        assert [str(row.coordinate) for row in search(populated).rows] == ["1:150:4", "1:170:2"]


class TestTargetsWithoutSnapshots:
    def test_a_bot_with_no_report_is_not_a_hit(self, populated) -> None:  # type: ignore[no-untyped-def]
        add_target(populated, Coordinate(1, 155, 1), "bot_1_155_1")
        assert [str(row.coordinate) for row in search(populated).rows] == ["1:150:4"]

    def test_it_is_still_listed_when_no_conditions_are_given(self, populated) -> None:  # type: ignore[no-untyped-def]
        add_target(populated, Coordinate(1, 155, 1), "bot_1_155_1")
        page = search(populated, conditions=None)
        rows = {str(row.coordinate): row for row in page.rows}
        assert "1:155:1" in rows
        assert rows["1:155:1"].total is None
        assert rows["1:155:1"].snapshot_at is None


class TestPagination:
    def test_limit_caps_the_page(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated, conditions=None, limit=2)
        assert len(page.rows) == 2
        assert page.next_cursor is not None

    def test_cursor_resumes_without_repeating(self, populated) -> None:  # type: ignore[no-untyped-def]
        first = search(populated, conditions=None, limit=2)
        second = search(populated, conditions=None, limit=2, cursor=first.next_cursor)
        assert {str(r.coordinate) for r in first.rows} & {
            str(r.coordinate) for r in second.rows
        } == set()

    def test_last_page_has_no_cursor(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated, conditions=None, limit=50)
        assert page.next_cursor is None

    def test_total_counts_every_hit_not_just_this_page(self, populated) -> None:  # type: ignore[no-untyped-def]
        """页码要靠总数算，而「这一页有几行」算不出总页数。"""
        page = search(populated, conditions=None, limit=2)
        assert len(page.rows) == 2
        assert page.total == 3

    def test_offset_says_where_the_page_starts(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated, conditions=None, limit=2, cursor="2")
        assert page.offset == 2
        assert page.total == 3

    def test_a_cursor_past_the_end_yields_an_empty_page(self, populated) -> None:  # type: ignore[no-untyped-def]
        """结果变少时前端要能发现「这一页越界了」，好退回最后一页。"""
        page = search(populated, conditions=None, limit=2, cursor="99")
        assert page.rows == ()
        assert page.total == 3
        assert page.offset == 99


class TestSorting:
    def test_default_order_is_by_coordinate(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated, conditions=None)
        assert [str(r.coordinate) for r in page.rows] == [
            "1:150:4",
            "1:160:7",
            "1:170:2",
        ]

    def test_can_sort_by_total_descending(self, populated) -> None:  # type: ignore[no-untyped-def]
        page = search(populated, conditions=None, sort="total_desc")
        totals = [row.total for row in page.rows if row.total is not None]
        assert totals == sorted(totals, reverse=True)


class TestSavedFilters:
    def test_saving_then_loading_round_trips_the_tree(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        repo = SqlAlchemyIntelRepository(session_factory)
        conditions = ConditionGroup(
            operator=GroupOperator.AND, children=(total_over(2000), guardians_over(5))
        )
        saved = repo.save_filter(
            name="厚防守", conditions=conditions, span=parse_coordinate_span("1:100", "1:200")
        )

        loaded = repo.get_filter(saved.filter_id)

        assert loaded is not None
        assert loaded.name == "厚防守"
        assert loaded.conditions == conditions
        assert loaded.span is not None
        assert str(loaded.span.start) == "1:100:1"

    def test_listing_returns_saved_filters(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        repo = SqlAlchemyIntelRepository(session_factory)
        repo.save_filter(name="a", conditions=ConditionGroup(GroupOperator.AND, (total_over(1),)))
        repo.save_filter(name="b", conditions=ConditionGroup(GroupOperator.AND, (total_over(2),)))

        assert {f.name for f in repo.list_filters()} == {"a", "b"}

    def test_deleting_removes_it(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        repo = SqlAlchemyIntelRepository(session_factory)
        saved = repo.save_filter(
            name="tmp", conditions=ConditionGroup(GroupOperator.AND, (total_over(1),))
        )

        repo.delete_filter(saved.filter_id)

        assert repo.get_filter(saved.filter_id) is None

    def test_a_filter_without_a_span_round_trips(self, session_factory) -> None:  # type: ignore[no-untyped-def]
        repo = SqlAlchemyIntelRepository(session_factory)
        saved = repo.save_filter(
            name="any range", conditions=ConditionGroup(GroupOperator.AND, (total_over(10),))
        )
        loaded = repo.get_filter(saved.filter_id)
        assert loaded is not None
        assert loaded.span is None


def _row_for(session_factory, coordinate: Coordinate):  # type: ignore[no-untyped-def]
    """只取这一个坐标那一行。

    不能借用上面的 `search()`：它带着「示例查询」的 span 与筛选条件
    （total > 2000、钛能守卫者 > 5），而这几条测试要看的恰恰是**没有逐舰种数据**
    的行——照那套条件筛，它们一条都留不下来。
    """
    page = SqlAlchemyIntelRepository(session_factory).search(
        IntelSearchQuery(span=parse_coordinate_span("2:1", "2:400"))
    )
    return next(row for row in page.rows if row.coordinate == coordinate)


class TestDetailOnlyReports:
    """探路战报只读详情页，没有逐舰种行——情报中心不能因此显示成「总计 0」。

    bot 探路战报只读详情页（逐舰种明细在回放页，而那个入口按钮全仓没有标定坐标，
    见 `BotLoop.collect_battle_reports` 的取舍）。于是 `fleet_snapshots` 一行都没有，
    而 `total` 原先是「逐舰种求和」——空 dict 求和得 0。

    结果是页面显示「有舰队数据，总计 0」，而报告里明明写着守方单位 319。
    0 和「没读到」在页面上长得一样，但含义相反：前者是「对方没船，随便打」。
    """

    def test_the_unit_total_stands_in_for_missing_per_ship_rows(
        self,
        session_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        coordinate = Coordinate(2, 320, 11)
        add_target(session_factory, coordinate, "bot_2_320_11")
        SqlAlchemyRepository(session_factory).append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=datetime(2026, 8, 11, 1, 32, 37, tzinfo=UTC),
                attacker_origin=Coordinate(2, 137, 18),
                defender_target=coordinate,
                fleet=(),
                defender_units=319,
            )
        )

        row = _row_for(session_factory, coordinate)

        assert row.total == 319
        assert row.counts == {}
        assert row.has_fleet_data is True

    def test_per_ship_rows_still_win_when_they_exist(
        self,
        session_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        """逐舰种和「单位」总数是两个独立来源，不是同一个数的两种写法。

        大舰队的逐行数量四舍五入显示，相加凑不出精确总数（见 `BattleReport`
        的注释）。有逐行时优先用它——它带着构成信息。
        """
        coordinate = Coordinate(2, 321, 5)
        add_target(session_factory, coordinate, "bot_2_321_5")
        SqlAlchemyRepository(session_factory).append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=datetime(2026, 8, 11, 1, 33, 30, tzinfo=UTC),
                attacker_origin=Coordinate(2, 137, 18),
                defender_target=coordinate,
                fleet=(FleetSnapshotEntry(side="defender", ship_type="巡洋舰", count=7),),
                defender_units=999,
            )
        )

        row = _row_for(session_factory, coordinate)

        assert row.total == 7
        assert row.counts == {"巡洋舰": 7}

    def test_a_report_with_no_figures_at_all_is_not_claimed_as_fleet_data(
        self,
        session_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        """两个来源都没有时要老实说没有，而不是显示 0。"""
        coordinate = Coordinate(2, 322, 16)
        add_target(session_factory, coordinate, "bot_2_322_16")
        SqlAlchemyRepository(session_factory).append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=datetime(2026, 8, 11, 1, 34, 23, tzinfo=UTC),
                attacker_origin=Coordinate(2, 137, 18),
                defender_target=coordinate,
                fleet=(),
            )
        )

        row = _row_for(session_factory, coordinate)

        assert row.total is None
        assert row.has_fleet_data is False
