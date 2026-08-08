"""星球列表的筛选与分页，跑在真库上。

分类规则有两份表述：`planet_kind()` 是 Python 的那份，`_planet_kind_clause()` 是 SQL 的那份。
两份必须给出同一个答案——「同一条规则在两处各写一份，只改一处」的坑已经踩过。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import PLANET_KINDS, planet_kind

SCANNED = datetime(2026, 8, 8, tzinfo=UTC)

#: (银河, 恒星, 位, 归属, is_bot)
PLANETS = [
    (2, 1, 5, "bot_2_1_5", True),
    (2, 1, 6, None, False),
    (2, 1, 7, "LilGriffith", False),
    (2, 2, 5, "bot_2_2_5", True),
    (3, 1, 5, "bot_3_1_5", True),
    (3, 1, 6, None, False),
    (3, 1, 7, "敌对海盗", False),
]


@pytest.fixture
def service(tmp_path: Path) -> PersistentApplicationService:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'planets.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        for galaxy, system, position, owner, is_bot in PLANETS:
            session.add(
                orm.BotTargetRow(
                    galaxy=galaxy,
                    system=system,
                    position=position,
                    is_bot=is_bot,
                    latest_owner_name=owner,
                    last_scanned_at_utc=SCANNED,
                )
            )
        session.commit()
    return PersistentApplicationService(factory, now_utc=lambda: SCANNED)


def coords(page) -> list[str]:  # type: ignore[no-untyped-def]
    return [str(row.coordinate) for row in page.rows]


def test_defaults_to_bots_across_every_galaxy(service) -> None:
    page = service.list_planets(galaxy=None, kind="bot", offset=0, limit=50)
    assert coords(page) == ["2:1:5", "2:2:5", "3:1:5"]
    assert page.total == 3


def test_galaxy_filter_narrows_to_one_galaxy(service) -> None:
    page = service.list_planets(galaxy=3, kind="bot", offset=0, limit=50)
    assert coords(page) == ["3:1:5"]
    assert page.total == 1


def test_owned_excludes_bots_and_free_slots(service) -> None:
    page = service.list_planets(galaxy=None, kind="owned", offset=0, limit=50)
    assert coords(page) == ["2:1:7", "3:1:7"]


def test_free_is_the_slots_with_no_owner(service) -> None:
    page = service.list_planets(galaxy=None, kind="free", offset=0, limit=50)
    assert coords(page) == ["2:1:6", "3:1:6"]


def test_all_covers_every_planet_exactly_once(service) -> None:
    page = service.list_planets(galaxy=None, kind="all", offset=0, limit=50)
    assert len(coords(page)) == len(PLANETS)
    assert page.total == len(PLANETS)


def test_sql_filter_agrees_with_the_python_classifier(service) -> None:
    """两份分类规则对同一批数据必须给出同一个划分。

    改了 `planet_kind()` 忘了 SQL（或反过来），这条当场红。
    """
    expected: dict[str, set[str]] = {kind: set() for kind in PLANET_KINDS if kind != "all"}
    for galaxy, system, position, owner, is_bot in PLANETS:
        expected[planet_kind(owner, is_bot)].add(str(Coordinate(galaxy, system, position)))

    for kind, wanted in expected.items():
        page = service.list_planets(galaxy=None, kind=kind, offset=0, limit=50)
        assert set(coords(page)) == wanted, kind


def test_total_is_the_filtered_count_not_the_page_size(service) -> None:
    # 页面靠这个数说「共多少颗」。拿本页行数冒充总数，就是情报中心那张表的老毛病。
    page = service.list_planets(galaxy=None, kind="all", offset=0, limit=2)
    assert len(page.rows) == 2
    assert page.total == len(PLANETS)
    assert page.has_more


def test_paging_walks_every_planet_without_repeats_or_gaps(service) -> None:
    seen: list[str] = []
    offset = 0
    while True:
        page = service.list_planets(galaxy=None, kind="all", offset=offset, limit=3)
        seen += coords(page)
        if not page.has_more:
            break
        offset += page.limit
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == len(PLANETS)


def test_kind_counts_describe_the_current_galaxy(service) -> None:
    page = service.list_planets(galaxy=2, kind="bot", offset=0, limit=50)
    assert page.kind_counts == {"bot": 2, "owned": 1, "free": 1, "all": 4}


def test_galaxy_counts_cover_every_galaxy_with_data(service) -> None:
    page = service.list_planets(galaxy=2, kind="bot", offset=0, limit=50)
    # 银河系计数不受当前筛选影响——它是用来填下拉框的。
    assert page.galaxy_counts == {2: 4, 3: 3}


def test_an_offset_past_the_end_yields_nothing_rather_than_wrapping(service) -> None:
    page = service.list_planets(galaxy=None, kind="all", offset=999, limit=10)
    assert page.rows == ()
    assert page.total == len(PLANETS)
    assert not page.has_more
