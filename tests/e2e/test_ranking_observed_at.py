"""军力榜每一行的「更新时间」要一路穿到接口和页面上。

用户口径（2026-08-16）：「军力榜我需要的是每条数据的更新时间」。快照级的
`captured_at_utc` 回答不了这个问题——一趟读榜要滚几十屏，行与行之间差得开。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.web.app import create_persistent_app

CAPTURED = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)
#: 写接口要本地令牌，读接口不要。
HEADERS = {"X-Evo-Helper-Token": "test-token"}


def _client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'rankings.db'}")
    Base.metadata.create_all(engine)
    return TestClient(
        create_persistent_app(create_session_factory(engine), local_token="test-token")
    )


def test_the_api_returns_the_time_each_row_was_read(tmp_path: Path) -> None:
    """逐行给的读取时刻要原样出现在 `GET /api/military-rankings` 的每一行上。

    两行**故意隔开 42 分钟**：只回传快照时刻的实现会让它们塌成同一个值。
    """
    client = _client(tmp_path)
    late = CAPTURED + timedelta(minutes=42)
    created = client.post(
        "/api/military-rankings/snapshots",
        headers=HEADERS,
        json={
            "captured_at_utc": CAPTURED.isoformat(),
            "rows": [
                {
                    "rank": 1,
                    "name": "bot_2_137_5",
                    "score": 99.0,
                    "observed_at_utc": CAPTURED.isoformat(),
                },
                {
                    "rank": 2,
                    "name": "bot_2_137_6",
                    "score": 98.0,
                    "observed_at_utc": late.isoformat(),
                },
            ],
        },
    )
    assert created.status_code == 201

    rows = client.get("/api/military-rankings").json()["rows"]

    assert [row["name"] for row in rows] == ["bot_2_137_5", "bot_2_137_6"]
    assert [datetime.fromisoformat(row["observed_at_utc"]) for row in rows] == [CAPTURED, late]


def test_a_row_posted_without_a_time_reports_the_snapshot_moment(tmp_path: Path) -> None:
    """整榜一次 POST（不逐行给时刻）仍然要能用：回落到快照时刻。

    ⚠️ 判据建在「`CAPTURED` 是一个固定的过去时刻」上——任何拿 `datetime.now()`
    去填的实现都对不上它。入库时刻不是读取时刻，补录场景下两者能差好几天。
    """
    client = _client(tmp_path)
    created = client.post(
        "/api/military-rankings/snapshots",
        headers=HEADERS,
        json={
            "captured_at_utc": CAPTURED.isoformat(),
            "rows": [{"rank": 1, "name": "bot_2_137_5", "score": 99.0}],
        },
    )
    assert created.status_code == 201

    rows = client.get("/api/military-rankings").json()["rows"]

    assert datetime.fromisoformat(rows[0]["observed_at_utc"]) == CAPTURED


def test_the_rankings_page_has_a_column_for_it(tmp_path: Path) -> None:
    """页面上得看得见——字段只存不显等于没做。

    ⚠️ **判据必须落在表头那一格上**，不能只搜「更新时间」四个字：页面顶上本来就
    有一句「数据更新时间（UTC+8）：…」（那是快照级的），裸搜会被它满足。变异测试
    当场逮到了这一点——把 `<th>更新时间</th>` 整个删掉，裸搜的版本照样绿。
    """
    body = _client(tmp_path).get("/rankings").text

    assert "<th>更新时间</th>" in body
    assert "r.observed_at_utc" in body
