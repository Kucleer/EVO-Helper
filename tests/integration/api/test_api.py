from datetime import UTC, datetime

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate
from evo_helper.web.app import create_app
from evo_helper.web.service import FakeApplicationService, FleetEntryView


def _make_client() -> tuple[TestClient, FakeApplicationService]:
    clock = FakeApplicationService(now_utc=lambda: datetime(2026, 8, 6, 1, 0, tzinfo=UTC))
    app = create_app(service=clock, local_token="test-token")
    return TestClient(app), clock


def _headers() -> dict[str, str]:
    return {"X-Evo-Helper-Token": "test-token"}


def _create_plan(client: TestClient) -> str:
    payload = {
        "name": "daily-scan",
        "enabled": True,
        "window_start": "08:00",
        "window_end": "20:00",
        "dry_run": True,
        "ranges": [
            {
                "start": {"galaxy": 1, "system": 1, "position": 1},
                "end": {"galaxy": 1, "system": 1, "position": 20},
                "origin": {"galaxy": 1, "system": 1, "position": 1},
                "fleet_preset": "main-fleet",
                "fleet_preset_signature": "main-fleet-v1",
                "priority": 0,
            }
        ],
    }
    response = client.post("/api/plans", headers=_headers(), json=payload)
    assert response.status_code == 201
    return response.json()["id"]


def test_plan_crud_flow() -> None:
    client, _ = _make_client()

    plan_id = _create_plan(client)
    assert client.get("/api/plans").json()[0]["name"] == "daily-scan"
    assert client.get(f"/api/plans/{plan_id}").json()["dry_run"] is True

    updated = client.put(
        f"/api/plans/{plan_id}",
        headers=_headers(),
        json={"name": "renamed-scan", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "renamed-scan"

    deleted = client.delete(f"/api/plans/{plan_id}", headers=_headers())
    assert deleted.status_code == 204
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_run_lifecycle_and_idempotency() -> None:
    client, _ = _make_client()
    plan_id = _create_plan(client)

    started = client.post(
        "/api/runs/start",
        headers=_headers(),
        json={"plan_id": plan_id, "idempotency_key": "run-key-0001"},
    )
    assert started.status_code == 201
    assert started.json()["state"] == "SCANNING"
    run_id = started.json()["run_id"]

    duplicate = client.post(
        "/api/runs/start",
        headers=_headers(),
        json={"plan_id": plan_id, "idempotency_key": "run-key-0001"},
    )
    assert duplicate.status_code == 409

    paused = client.post(f"/api/runs/{run_id}/pause", headers=_headers())
    assert paused.json()["state"] == "PAUSED"

    resumed = client.post(f"/api/runs/{run_id}/resume", headers=_headers())
    assert resumed.json()["state"] == "ARMED"

    stopped = client.post(f"/api/runs/{run_id}/emergency-stop", headers=_headers())
    assert stopped.json()["state"] == "EMERGENCY_STOPPED"

    invalid = client.post(f"/api/runs/{run_id}/pause", headers=_headers())
    assert invalid.status_code == 409


def test_targets_history_and_diff() -> None:
    client, clock = _make_client()
    coordinate = Coordinate(4, 5, 6)
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 10),),
        captured_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
    )
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 8), FleetEntryView("cruiser", 3)),
        captured_at_utc=datetime(2026, 8, 6, 2, 0, tzinfo=UTC),
    )

    targets = client.get("/api/targets").json()
    assert len(targets) == 1
    assert targets[0]["coordinate"]["galaxy"] == 4

    history = client.get("/api/targets/4:5:6/history")
    assert history.status_code == 200
    assert len(history.json()) == 2

    diff = client.get("/api/targets/4:5:6/diff")
    assert diff.status_code == 200
    body = diff.json()
    assert body["total_before"] == 10
    assert body["total_after"] == 11
    assert "cruiser" in body["first_seen"]


def test_revisit_and_events() -> None:
    client, _ = _make_client()

    created = client.post(
        "/api/revisits",
        headers=_headers(),
        json={
            "scope": "target",
            "reason": "confirm ownership",
            "target_coordinate": {"galaxy": 1, "system": 2, "position": 3},
        },
    )
    assert created.status_code == 201
    assert created.json()["target_coordinate"]["position"] == 3

    revisits = client.get("/api/revisits").json()
    assert len(revisits) == 1
    events = client.get("/api/diagnostics/events").json()
    assert isinstance(events, list)


def test_pages_render() -> None:
    client, _ = _make_client()
    _create_plan(client)

    for path in ("/", "/plans", "/runs", "/diagnostics"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_target_history_page_renders() -> None:
    client, clock = _make_client()
    coordinate = Coordinate(7, 8, 9)
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 5),),
        captured_at_utc=datetime(2026, 8, 6, 1, 0, tzinfo=UTC),
    )
    clock.add_snapshot(
        coordinate,
        "attacker",
        (FleetEntryView("destroyer", 6),),
        captured_at_utc=datetime(2026, 8, 6, 2, 0, tzinfo=UTC),
    )

    for path in ("/targets", "/targets/7:8:9"):
        response = client.get(path)
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


def test_validation_errors_are_422() -> None:
    client, _ = _make_client()

    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={"name": "x", "window_start": "25:00", "window_end": "20:00", "ranges": []},
    )

    assert response.status_code == 422


def test_plan_requires_a_preset_signature() -> None:
    client, _ = _make_client()
    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={
            "name": "missing-signature",
            "window_start": "08:00",
            "window_end": "20:00",
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 1, "position": 1},
                    "end": {"galaxy": 1, "system": 1, "position": 2},
                    "origin": {"galaxy": 1, "system": 1, "position": 1},
                    "fleet_preset": "main-fleet",
                }
            ],
        },
    )

    assert response.status_code == 422


def test_console_pages_render() -> None:
    """The two console pages must render; a template or service typo is a 500."""
    client, _ = _make_client()

    for path, marker in (("/missions", "任务中心"), ("/intel", "情报中心")):
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)
        assert marker in response.text


def test_console_pages_show_the_dry_run_lock() -> None:
    """锁只是提示，绝不能渲染成开关。"""
    client, _ = _make_client()

    body = client.get("/missions").text
    assert "演习模式 已锁定" in body
    assert 'type="checkbox"' not in body


def test_static_console_stylesheet_is_served() -> None:
    client, _ = _make_client()
    response = client.get("/static/console.css")
    assert response.status_code == 200
    assert "--accent" in response.text


def test_origin_outside_the_scan_range_is_accepted() -> None:
    """The departure planet is the player's own and is normally outside the range.

    Requiring it inside made the plan form unusable for real data: attacking
    bots in 1:100-1:200 from your own planet at 2:137:18 was rejected.
    """
    client, _ = _make_client()

    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={
            "name": "morning-scan",
            "enabled": True,
            "window_start": "08:00",
            "window_end": "10:00",
            "dry_run": True,
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 100, "position": 1},
                    "end": {"galaxy": 1, "system": 200, "position": 15},
                    "origin": {"galaxy": 2, "system": 137, "position": 18},
                    "fleet_preset": "main-fleet",
                    "fleet_preset_signature": "main-fleet-v1",
                    "priority": 0,
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["ranges"][0]["origin"] == {
        "galaxy": 2,
        "system": 137,
        "position": 18,
    }


def test_a_reversed_scan_range_is_still_rejected() -> None:
    """Dropping the origin rule must not loosen the range rule."""
    client, _ = _make_client()

    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={
            "name": "bad-range",
            "enabled": True,
            "window_start": "08:00",
            "window_end": "10:00",
            "dry_run": True,
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 200, "position": 1},
                    "end": {"galaxy": 1, "system": 100, "position": 1},
                    "origin": {"galaxy": 2, "system": 137, "position": 18},
                    "fleet_preset": "main-fleet",
                    "fleet_preset_signature": "main-fleet-v1",
                    "priority": 0,
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "precede" in response.json()["detail"]


def test_legacy_pages_redirect_into_the_console() -> None:
    """Nav collapsed to two entries; the old paths must not become dead ends."""
    client, _ = _make_client()

    for path, destination in (("/", "/missions"), ("/plans", "/missions"), ("/targets", "/intel")):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307, (path, response.status_code)
        assert response.headers["location"] == destination


def test_legacy_paths_land_on_a_rendered_page() -> None:
    client, _ = _make_client()

    assert "任务中心" in client.get("/").text
    assert "情报中心" in client.get("/targets").text


def test_auxiliary_pages_render_in_the_console_shell() -> None:
    client, _ = _make_client()

    for path, marker in (("/runs", "运行详情"), ("/diagnostics", "诊断")):
        body = client.get(path).text
        assert response_ok(client, path), path
        assert marker in body
        # The console shell, not the old bare markup.
        assert "/static/console.css" in body


def response_ok(client: TestClient, path: str) -> bool:
    return client.get(path).status_code == 200


def test_run_state_chips_pair_colour_with_a_glyph() -> None:
    """Colour must never be the only signal for a state."""
    from evo_helper.web.app import run_state_glyph, run_state_tone

    for state in ("SCANNING", "PAUSED", "FAILED", "EMERGENCY_STOPPED", "COMPLETED"):
        assert run_state_tone(state) != "", state
        assert run_state_glyph(state) != "•", state

    # An unknown state still renders something rather than blowing up.
    assert run_state_tone("SOMETHING_NEW") == ""
    assert run_state_glyph("SOMETHING_NEW") == "•"


def test_plan_carries_fleet_line_configuration() -> None:
    client, _ = _make_client()

    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={
            "name": "lines",
            "enabled": True,
            "window_start": "08:00",
            "window_end": "10:00",
            "dry_run": True,
            "fleet_line_limit": 6,
            "reserved_lines": 2,
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 100, "position": 1},
                    "end": {"galaxy": 1, "system": 200, "position": 15},
                    "origin": {"galaxy": 2, "system": 137, "position": 18},
                    "fleet_preset": "探路",
                    "fleet_preset_signature": "轻型战斗机:1",
                    "priority": 0,
                }
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["fleet_line_limit"] == 6
    assert body["reserved_lines"] == 2


def test_plan_line_configuration_defaults_are_conservative() -> None:
    """Omitting the fields keeps the previous behaviour: one line, none reserved."""
    client, _ = _make_client()
    plan_id = _create_plan(client)

    body = client.get(f"/api/plans/{plan_id}").json()

    assert body["fleet_line_limit"] == 1
    assert body["reserved_lines"] == 0


def test_reserving_every_line_is_rejected() -> None:
    """A plan that reserves its whole limit could never dispatch."""
    client, _ = _make_client()

    response = client.post(
        "/api/plans",
        headers=_headers(),
        json={
            "name": "all-reserved",
            "enabled": True,
            "window_start": "08:00",
            "window_end": "10:00",
            "dry_run": True,
            "fleet_line_limit": 3,
            "reserved_lines": 3,
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 100, "position": 1},
                    "end": {"galaxy": 1, "system": 200, "position": 15},
                    "origin": {"galaxy": 2, "system": 137, "position": 18},
                    "fleet_preset": "探路",
                    "fleet_preset_signature": "轻型战斗机:1",
                    "priority": 0,
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "never dispatch" in response.json()["detail"]


def test_run_states_render_in_chinese() -> None:
    """英文状态常量对用户有歧义；界面只显示中文。"""
    from evo_helper.web.app import run_state_label

    assert run_state_label("ARMED") == "待命"
    assert run_state_label("DRAINING") == "收取战报"
    assert run_state_label("EMERGENCY_STOPPED") == "已紧急停止"
    # 未知状态回落到原值，宁可显示英文也不要显示空白。
    assert run_state_label("SOMETHING_NEW") == "SOMETHING_NEW"


def test_console_shows_no_english_dry_run_wording() -> None:
    client, _ = _make_client()

    for path in ("/missions", "/runs", "/diagnostics"):
        body = client.get(path).text
        assert "dry run" not in body, path
        assert "演习模式" in body, path
