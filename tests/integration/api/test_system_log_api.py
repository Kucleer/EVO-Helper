"""`/api/system-log` 与 `/system-log` 页面。

⚠️ 这一页**不是** `/logs`。那一页是「攻击日志」，读的是
`attack_intents ⟕ attack_dispatches ⟕ battle_reports`。这里顺带钉住这一点：
两条路由都还在，各读各的表。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.infrastructure.system_log import SystemLogRecord
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

BASE_TIME = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(scratch_database_url(tmp_path, "system-log.db"))
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _seed(session_factory)
    app = create_persistent_app(session_factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client


def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    rows = [
        ("live-pc", "tools.pirate_loop", "INFO", "pirate", "扫到 2:137:1"),
        ("live-pc", "tools.pirate_loop", "ERROR", "pirate", "简报认不出，安全地不派"),
        ("console-pc", "application.mission_scheduler", "INFO", None, "补认 3 份战报"),
        ("console-pc", "web.app", "WARNING", "bot", "调度器 tick 失败"),
    ]
    SystemLogRepository(session_factory).append(
        [
            SystemLogRecord(
                logged_at_utc=BASE_TIME + timedelta(minutes=index),
                level=level,
                source=source,
                host=host,
                pid=1000 + index,
                message=message,
                mission_kind=kind,
                payload_json='{"coordinate": "2:137:1"}' if index == 0 else "{}",
            )
            for index, (host, source, level, kind, message) in enumerate(rows)
        ]
    )


def test_the_api_returns_the_newest_first_with_a_server_side_total(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/system-log").json()

    assert body["total"] == 4
    assert body["rows"][0]["message"] == "调度器 tick 失败"
    assert body["hosts"] == ["console-pc", "live-pc"]
    assert not body["has_more"]


def test_every_filter_is_honoured(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/system-log?level=ERROR").json()["total"] == 1
    assert client.get("/api/system-log?host=live-pc").json()["total"] == 2
    assert client.get("/api/system-log?source=web.app").json()["total"] == 1
    assert client.get("/api/system-log?mission_kind=pirate").json()["total"] == 2
    assert client.get("/api/system-log?q=2:137").json()["total"] == 1


def test_a_time_window_narrows_the_page(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/system-log?since=2026-08-16T12:02:00&until=2026-08-16T12:03:00").json()

    assert [row["message"] for row in body["rows"]] == ["调度器 tick 失败", "补认 3 份战报"]


def test_paging_walks_the_whole_set_without_repeats(client) -> None:  # type: ignore[no-untyped-def]
    first = client.get("/api/system-log?limit=2").json()
    second = client.get("/api/system-log?limit=2&offset=2").json()

    assert first["has_more"] and not second["has_more"]
    ids = [row["id"] for row in first["rows"] + second["rows"]]
    assert len(set(ids)) == 4


def test_blank_query_parameters_do_not_empty_the_page(client) -> None:  # type: ignore[no-untyped-def]
    """浏览器提交表单必然带上 `level=&host=`。当成「等于空串」就永远是 0 条。"""
    response = client.get("/api/system-log?level=&source=&host=&mission_kind=&q=&run_id=")

    assert response.status_code == 200
    assert response.json()["total"] == 4


def test_an_unknown_run_id_matches_nothing_rather_than_erroring(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/api/system-log?run_id={uuid4()}").json()

    assert body["total"] == 0


def test_the_page_renders_with_its_own_nav_entry(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/system-log")

    assert response.status_code == 200
    assert "系统日志" in response.text
    assert "简报认不出，安全地不派" in response.text
    # 导航里两条日志入口各是各的，别把「攻击日志」占了。
    assert 'href="/system-log"' in response.text
    assert 'href="/logs"' in response.text


def test_the_page_says_so_when_it_could_not_use_a_filter(client) -> None:  # type: ignore[no-untyped-def]
    """认不出的 run_id 照常渲染全部记录，但必须说清「没按它筛」。

    默默地不筛才是最坏的一种：用户会把下面那些行当成筛出来的结果。
    """
    response = client.get("/system-log?run_id=not-a-uuid")

    assert response.status_code == 200
    assert "run_id 不是合法 UUID" in response.text
    assert "简报认不出，安全地不派" in response.text


def test_a_broken_limit_does_not_turn_the_page_into_json(client) -> None:  # type: ignore[no-untyped-def]
    """手改链接写出 `?limit=` 时也要是一张页面，不是一页 422。"""
    assert client.get("/system-log?limit=999999").status_code == 200


def test_the_attack_log_page_is_untouched(client) -> None:  # type: ignore[no-untyped-def]
    """`/logs` 仍然是攻击日志。占用它会让「哪一页看得到 runner 报错」永远说不清。"""
    response = client.get("/logs")

    assert response.status_code == 200
    assert "攻击日志" in response.text
    assert "简报认不出，安全地不派" not in response.text
