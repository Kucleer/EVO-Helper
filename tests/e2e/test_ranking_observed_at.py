"""军力榜每一行的「更新时间」要一路穿到接口和页面上。

用户口径（2026-08-16）：「军力榜我需要的是每条数据的更新时间」。快照级的
`captured_at_utc` 回答不了这个问题——一趟读榜要滚几十屏，行与行之间差得开。

⚠️ **页面读的是 `bot_targets`，不是快照表。** 快照表没有活着的写入方（详见
`storage.military_rankings` 的模块头），读它只会永远显示迁移播种时那一份。
所以这里全部用真实的写入路径 `save_ranking_targets` 造数据——它正是
`ranking_scan` 每读一屏调的那个方法。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import RankingTarget
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

READ_AT = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)


def _client(tmp_path: Path) -> tuple[TestClient, SqlAlchemyRepository]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'rankings.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    client = TestClient(create_persistent_app(factory, local_token="test-token"))
    return client, SqlAlchemyRepository(factory)


def test_the_api_returns_the_time_each_row_was_read(tmp_path: Path) -> None:
    """逐屏采到的读取时刻要原样出现在 `GET /api/military-rankings` 的每一行上。

    两行**故意隔开 42 分钟**，模拟一趟读榜里相隔很远的两屏：任何拿单一时刻
    （快照时刻、`now()`）去填所有行的实现都会让它们塌成同一个值。
    """
    client, repository = _client(tmp_path)
    late = READ_AT + timedelta(minutes=42)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 99.0, READ_AT),
            RankingTarget(Coordinate(2, 137, 6), 98.0, late),
        ]
    )

    rows = client.get("/api/military-rankings").json()["rows"]

    assert [row["name"] for row in rows] == ["bot_2_137_5", "bot_2_137_6"]
    assert [datetime.fromisoformat(row["observed_at_utc"]) for row in rows] == [READ_AT, late]


def test_the_board_shows_what_the_scan_wrote_not_a_frozen_snapshot(tmp_path: Path) -> None:
    """⚠️ **这条钉住 2026-08-16 查明的那个坑。**

    页面曾经读 `military_ranking_entries`，而那张表的唯一写入者是一个没人调用的
    POST 接口——扫描一直在正常采数，页面却停在迁移播种的那一刻，看起来「有数据、
    只是不更新」，比整页报错难发现得多。

    所以判据是：**只经过扫描的写入路径**（一行 POST 都不发），榜单就必须看得见。
    """
    client, repository = _client(tmp_path)
    repository.save_ranking_targets([RankingTarget(Coordinate(4, 30, 12), 29_590.0, READ_AT)])

    payload = client.get("/api/military-rankings").json()

    assert payload["total"] == 1
    assert payload["rows"][0]["coordinate"] == "4:30:12"
    assert datetime.fromisoformat(payload["refreshed_at_utc"]) == READ_AT


def test_an_interpolated_score_is_marked_as_such(tmp_path: Path) -> None:
    """插值补出来的军力值必须标出来。

    这个仓库有一条硬规矩：猜出来的数不许长得像量出来的。实测库里 1,721 行有
    军力值的数据中有 127 行是插值所得，混在一起看不出来就等于把估算当实测用。
    """
    client, repository = _client(tmp_path)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 99.0, READ_AT),
            RankingTarget(Coordinate(2, 137, 6), 98.0, READ_AT, military_score_estimated=True),
        ]
    )

    rows = client.get("/api/military-rankings").json()["rows"]

    assert [row["estimated"] for row in rows] == [False, True]


def test_a_historic_float_tail_never_reaches_the_page(tmp_path: Path) -> None:
    """⚠️ **这条钉的是接线，不是那个函数。**

    `web.display.settled_score` 自己有单元测试，但那一层全绿也挡不住有人把
    `ranking_routes` 里那一次调用删掉——脏值照样一路到页面。所以判据必须落在
    接口返回的 `score` 上，而且**恰好相等**（用 `pytest.approx` 等于没测）。

    输入是 2026-08-17 页面上原样出现的三个值。库里存的就是这样，
    而用户口径是开发过程不碰生产库、历史值不许 UPDATE——只能在读出来时收。
    同一份数据里混一个 `72252.5`：它是插值取的中点，**合法**，不许被一起抹掉。
    """
    client, repository = _client(tmp_path)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 64959.99999999999, READ_AT),
            RankingTarget(Coordinate(2, 137, 6), 64260.00000000001, READ_AT),
            RankingTarget(Coordinate(2, 137, 7), 64180.00000000001, READ_AT),
            RankingTarget(Coordinate(2, 137, 8), 72252.5, READ_AT, military_score_estimated=True),
        ]
    )

    rows = client.get("/api/military-rankings").json()["rows"]

    # 榜单按军力降序，所以那个插值出来的 72252.5 排在最前面。
    assert [row["score"] for row in rows] == [72252.5, 64960, 64260, 64180]


def test_a_bot_name_query_finds_it_by_coordinate(tmp_path: Path) -> None:
    """搜索框里写 `bot_2_137_6` 要找得到。

    ⚠️ 名称是**从坐标推**出来的，库里没有这一列可以 `LIKE`（`latest_owner_name`
    是坐标扫描 OCR 出来的，实测有错读：2:3:9 那一行的名字存成了 `bot_2_3_3`）。
    所以这种最常见的输入必须走坐标解析，否则搜索框对 bot 名字永远是空结果。
    """
    client, repository = _client(tmp_path)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 99.0, READ_AT),
            RankingTarget(Coordinate(2, 137, 6), 98.0, READ_AT),
        ]
    )

    hit = client.get("/api/military-rankings", params={"q": "bot_2_137_6"}).json()

    assert hit["total"] == 1
    assert hit["rows"][0]["coordinate"] == "2:137:6"


def test_the_rankings_page_has_a_column_for_it(tmp_path: Path) -> None:
    """页面上得看得见——字段只存不显等于没做。

    ⚠️ **判据必须落在表头那一格上**，不能只搜「更新时间」四个字：页面顶上本来就
    有一句时间说明，裸搜会被它满足。变异测试当场逮到了这一点——把
    `<th>更新时间（UTC+8）</th>` 整个删掉，裸搜的版本照样绿。

    时区标注也一并钉在这里（用户口径 2026-08-17：页面上的时刻一律 UTC+8）：
    这一格显示的是换算过的现实时间，表头不写清楚就得让人猜是哪一套。
    """
    client, _ = _client(tmp_path)
    body = client.get("/rankings").text

    assert "<th>更新时间（UTC+8）</th>" in body
    assert "r.observed_at_utc" in body
