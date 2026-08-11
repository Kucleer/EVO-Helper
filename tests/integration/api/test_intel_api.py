from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import BattleReport, FleetSnapshotEntry
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

BASE_TIME = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

TOTAL_OVER_2000 = {"type": "condition", "field": "__total__", "operator": ">", "value": 2000}
GUARDIANS_OVER_5 = {"type": "condition", "field": "钛能守卫者", "operator": ">", "value": 5}


def and_group(*children: dict[str, object]) -> dict[str, object]:
    return {"type": "group", "operator": "AND", "children": list(children)}


@pytest.fixture
def client(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'api.db'}")
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _seed(session_factory)
    app = create_persistent_app(session_factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client


def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    targets = {
        Coordinate(1, 150, 4): {"钛能守卫者": 6, "轻型战斗机": 2500},
        Coordinate(1, 160, 7): {"钛能守卫者": 9, "轻型战斗机": 10},
        Coordinate(2, 150, 4): {"钛能守卫者": 99, "轻型战斗机": 9000},
    }
    with session_factory() as session:
        for coordinate in targets:
            session.add(
                orm.BotTargetRow(
                    id=uuid4(),
                    galaxy=coordinate.galaxy,
                    system=coordinate.system,
                    position=coordinate.position,
                    latest_owner_name=f"bot_{coordinate.galaxy}_{coordinate.system}_{coordinate.position}",
                    is_bot=True,
                )
            )
        session.commit()
    repository = SqlAlchemyRepository(session_factory)
    for coordinate, counts in targets.items():
        repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=BASE_TIME,
                attacker_origin=Coordinate(2, 137, 18),
                defender_target=coordinate,
                fleet=tuple(
                    FleetSnapshotEntry(side="defender", ship_type=name, count=count)
                    for name, count in counts.items()
                ),
            )
        )


class TestSearch:
    def test_worked_example_returns_one_row(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={
                "span": {"start": "1:100", "end": "1:200"},
                "conditions": and_group(TOTAL_OVER_2000, GUARDIANS_OVER_5),
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert [row["coordinate"] for row in body["rows"]] == ["1:150:4"]

    def test_row_carries_the_summary_fields(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={
                "span": {"start": "1:100", "end": "1:200"},
                "conditions": and_group(TOTAL_OVER_2000, GUARDIANS_OVER_5),
            },
        )
        row = response.json()["rows"][0]

        assert row["player"] == "bot_1_150_4"
        assert row["total"] == 2506
        assert row["snapshot_at"] is not None
        assert "钛能守卫者" in row["matched_summary"]

    def test_search_without_conditions_lists_the_span(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search", json={"span": {"start": "1:100", "end": "1:200"}}
        )
        assert {row["coordinate"] for row in response.json()["rows"]} == {"1:150:4", "1:160:7"}

    def test_response_carries_what_the_pager_needs(self, client: TestClient) -> None:
        """总数、页起点、每页行数——页码在浏览器里由这三个数算出来。"""
        response = client.post(
            "/api/intel/search",
            json={"span": {"start": "1:100", "end": "1:200"}, "limit": 1},
        )
        page = response.json()

        assert len(page["rows"]) == 1
        assert page["total"] == 2
        assert page["offset"] == 0
        assert page["limit"] == 1

    def test_a_later_page_reports_its_own_offset(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={"span": {"start": "1:100", "end": "1:200"}, "limit": 1, "cursor": "1"},
        )
        page = response.json()

        assert page["offset"] == 1
        assert page["total"] == 2
        assert page["next_cursor"] is None


class TestValidationErrors:
    def test_unknown_ship_is_a_readable_error(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={
                "conditions": and_group(
                    {"type": "condition", "field": "星门要塞", "operator": ">", "value": 1}
                )
            },
        )
        assert response.status_code == 422
        assert "星门要塞" in response.json()["detail"]

    def test_negative_value_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={
                "conditions": and_group(
                    {"type": "condition", "field": "__total__", "operator": ">", "value": -5}
                )
            },
        )
        assert response.status_code == 422
        assert "negative" in response.json()["detail"]

    def test_empty_group_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={"conditions": {"type": "group", "operator": "AND", "children": []}},
        )
        assert response.status_code == 422
        assert "empty" in response.json()["detail"]

    def test_incomplete_range_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/intel/search", json={"span": {"start": "1:100", "end": ""}})
        assert response.status_code == 422
        assert "coordinate" in response.json()["detail"]

    def test_reversed_range_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search", json={"span": {"start": "1:200", "end": "1:100"}}
        )
        assert response.status_code == 422

    def test_unknown_operator_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/search",
            json={
                "conditions": and_group(
                    {"type": "condition", "field": "__total__", "operator": "~", "value": 1}
                )
            },
        )
        assert response.status_code == 422


class TestSavedFilters:
    def test_create_then_list(self, client: TestClient) -> None:
        created = client.post(
            "/api/intel/filters",
            json={
                "name": "厚防守",
                "span": {"start": "1:100", "end": "1:200"},
                "conditions": and_group(TOTAL_OVER_2000, GUARDIANS_OVER_5),
            },
        )
        assert created.status_code == 201

        listed = client.get("/api/intel/filters")
        assert [f["name"] for f in listed.json()] == ["厚防守"]

    def test_saved_filter_can_be_applied(self, client: TestClient) -> None:
        filter_id = client.post(
            "/api/intel/filters",
            json={
                "name": "厚防守",
                "span": {"start": "1:100", "end": "1:200"},
                "conditions": and_group(TOTAL_OVER_2000, GUARDIANS_OVER_5),
            },
        ).json()["filter_id"]

        response = client.post("/api/intel/search", json={"filter_id": filter_id})

        assert [row["coordinate"] for row in response.json()["rows"]] == ["1:150:4"]

    def test_delete_removes_it(self, client: TestClient) -> None:
        filter_id = client.post(
            "/api/intel/filters",
            json={"name": "tmp", "conditions": and_group(TOTAL_OVER_2000)},
        ).json()["filter_id"]

        assert client.delete(f"/api/intel/filters/{filter_id}").status_code == 204
        assert client.get("/api/intel/filters").json() == []

    def test_saving_an_invalid_tree_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/intel/filters",
            json={
                "name": "bad",
                "conditions": and_group(
                    {"type": "condition", "field": "星门要塞", "operator": ">", "value": 1}
                ),
            },
        )
        assert response.status_code == 422


class TestShipVocabulary:
    def test_lists_the_recorded_defender_ship_types(self, client: TestClient) -> None:
        response = client.get("/api/intel/ships")
        assert set(response.json()) == {"钛能守卫者", "轻型战斗机"}
