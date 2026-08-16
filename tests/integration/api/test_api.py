import html
import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate
from evo_helper.web.app import create_app
from evo_helper.web.service import (
    FakeApplicationService,
    FleetEntryView,
    PlanetRow,
)


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


# `test_starting_a_run_is_idempotent` 随 `POST /api/runs/start` 与
# `GET /api/runs/{run_id}` 一起删了：它守的是这两个接口本身（201 + 409 + 回读），
# 接口不在了，这条用例也就没有对应的行为可守。
#
# 它顺带守着的那条不变量——**同一个幂等键只许有一条运行实例**——落在库上，由
# `tests/integration/storage/test_repository.py::test_idempotency_key_is_unique`
# 守着（`run_instances.idempotency_key` 的唯一约束）。那也正是生产链路真正依赖的
# 那一层：`tools/scan_coordinates.py` 与 `tools/pirate_loop.py` 用固定的 `RUN_KEY`
# 续上同一条运行，从来不经过 HTTP。


def test_the_runs_api_is_gone() -> None:
    """`/api/runs` 底下不许再剩任何一个接口。

    301/307 只留给页面路径（旧书签），接口没有书签这回事：留一个没人调的写接口
    在那里，下一个人只会以为「运行实例是从这里建的」，而真正建它的是
    `tools/` 里的扫描与海盗链路。
    """
    client, _ = _make_client()
    plan_id = _create_plan(client)

    started = client.post(
        "/api/runs/start",
        headers=_headers(),
        json={"plan_id": plan_id, "idempotency_key": "run-key-0001"},
    )
    assert started.status_code == 404, started.status_code

    read_back = client.get("/api/runs/2ba6d1b8-6f1e-4a2b-9a3f-0d1c2e3f4a5b")
    assert read_back.status_code == 404, read_back.status_code


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

    for path in ("/", "/plans", "/diagnostics"):
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

    for path, destination in (
        ("/", "/missions"),
        ("/plans", "/missions"),
        ("/targets", "/intel"),
        # 「运行详情」关掉之后同样留 307：旧书签不该变成 404。
        ("/runs", "/missions"),
        ("/runs/2ba6d1b8-6f1e-4a2b-9a3f-0d1c2e3f4a5b", "/missions"),
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 307, (path, response.status_code)
        assert response.headers["location"] == destination


def test_legacy_paths_land_on_a_rendered_page() -> None:
    client, _ = _make_client()

    assert "任务中心" in client.get("/").text
    assert "情报中心" in client.get("/targets").text


def test_auxiliary_pages_render_in_the_console_shell() -> None:
    client, _ = _make_client()

    for path, marker in (("/diagnostics", "诊断"),):
        body = client.get(path).text
        assert response_ok(client, path), path
        assert marker in body
        # The console shell, not the old bare markup.
        assert "/static/console.css" in body


def test_the_run_detail_page_is_gone_and_leaves_no_dead_link() -> None:
    """「运行详情」这一页已关闭（用户口径 2026-08-11：「实际已经没有作用」）。

    两件事一起钉：导航里不再有入口，且**没有任何一页还指向 /runs**——
    留一条指向 307 的链接，点下去会莫名其妙地跳到任务中心。
    """
    client, _ = _make_client()

    for path in ("/missions", "/intel", "/planets", "/logs", "/diagnostics"):
        body = client.get(path).text
        assert "运行详情" not in body, path
        assert 'href="/runs' not in body, path


def response_ok(client: TestClient, path: str) -> bool:
    return client.get(path).status_code == 200


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


def test_console_never_mentions_a_rehearsal_mode() -> None:
    """演习模式这个概念已经整体删掉了，界面上不许再冒出来。

    它曾经是每一页顶栏上那枚 🔒 徽标。留一条断言守着，是因为「派遣其实没真派」
    这种暗示一旦回到界面上，用户就会去找那个并不存在的开关。
    """
    client, _ = _make_client()

    for path in ("/missions", "/intel", "/diagnostics", "/logs"):
        body = client.get(path).text
        for wording in ("演习", "dry run", "dry_run"):
            assert wording not in body, (path, wording)


def _seed_planets(service: FakeApplicationService, specs) -> None:
    for galaxy, system, position, owner, is_bot in specs:
        service._planets.append(
            PlanetRow(
                coordinate=Coordinate(galaxy, system, position),
                owner_name=owner,
                is_bot=is_bot,
                last_scan_at=datetime(2026, 8, 8, tzinfo=UTC),
            )
        )


MIXED = [
    (2, 1, 5, "bot_2_1_5", True),
    (2, 1, 6, None, False),
    (2, 1, 7, "LilGriffith", False),
    (3, 1, 5, "bot_3_1_5", True),
    (3, 1, 6, None, False),
]


def test_planets_page_defaults_to_bots_only() -> None:
    """全量扫描里绝大多数坐标是空位，默认全展开没有信息量。"""
    client, service = _make_client()
    _seed_planets(service, MIXED)

    body = client.get("/planets").text

    assert "bot_2_1_5" in body
    assert "bot_3_1_5" in body
    assert "LilGriffith" not in body


def test_planets_page_filters_by_galaxy() -> None:
    client, service = _make_client()
    _seed_planets(service, MIXED)

    body = client.get("/planets?galaxy=2").text

    assert "bot_2_1_5" in body
    assert "bot_3_1_5" not in body


def test_planets_page_searches_owner_names() -> None:
    client, service = _make_client()
    _seed_planets(service, MIXED)

    body = client.get("/planets?kind=all&owner=lilgriffith").text

    assert "LilGriffith" in body
    assert "bot_2_1_5" not in body
    assert 'name="owner" value="lilgriffith"' in body


def test_planets_page_rejects_the_retired_free_slot_filter() -> None:
    client, service = _make_client()
    _seed_planets(service, MIXED)

    body = client.get("/planets?kind=free").text

    # `free` 不再是星球列表类型，旧链接安全回落到默认的 bot 视图。
    assert "2:1:6" not in body
    assert "bot_2_1_5" in body


def test_planets_page_reports_the_filtered_total_not_the_page_size() -> None:
    """页面必须说得出当前筛选共多少颗，而不是拿本页行数冒充总数。

    踩过：情报中心那张表只渲染前 500 条又不声明，扫描跑到 2:138 时页面停在 2:32。
    """
    client, service = _make_client()
    _seed_planets(service, [(2, 1, 5 + i, f"bot_2_1_{5 + i}", True) for i in range(12)])

    body = client.get("/planets?limit=5").text

    assert "共 <strong>12</strong> 颗" in body
    assert "1–5 颗" in body


def test_planets_pagination_reaches_every_row() -> None:
    """翻页要能走到最后一颗——分页不是另一种静默截断。"""
    client, service = _make_client()
    _seed_planets(service, [(2, 1, 5 + i, f"bot_2_1_{5 + i}", True) for i in range(12)])

    seen: set[str] = set()
    url = "/planets?limit=5"
    for _ in range(10):
        response = client.get(url)
        body = response.text
        seen |= {f"2:1:{5 + i}" for i in range(12) if f"2:1:{5 + i}" in body}
        match = re.search(r'href="(/planets[?][^"]*)">下一页', body)
        if not match:
            break
        url = html.unescape(match.group(1))

    assert seen == {f"2:1:{5 + i}" for i in range(12)}


def test_planets_page_drops_the_default_hint_once_a_filter_is_chosen() -> None:
    """「默认只看 bot」这句在别的筛选下就是一句和当前视图矛盾的噪声。"""
    client, service = _make_client()
    _seed_planets(service, MIXED)

    assert "默认只看 bot" in client.get("/planets").text
    assert "默认只看 bot" not in client.get("/planets?kind=owned").text


def test_planet_rows_link_to_the_coordinate_detail_page() -> None:
    client, service = _make_client()
    _seed_planets(service, MIXED)

    body = client.get("/planets").text

    assert 'href="/targets/2:1:5?back=' in body


def test_the_detail_page_returns_to_the_page_you_came_from() -> None:
    """翻到第 7 页再点进去、回来从头开始，等于逼人重新翻一遍。"""
    client, _service = _make_client()

    body = client.get("/targets/2:1:5", params={"back": "/planets?kind=free&offset=300"}).text

    assert 'href="/planets?kind=free&amp;offset=300"' in body


def test_the_detail_page_refuses_an_offsite_back_target() -> None:
    """back 来自查询参数，也就是来自任何人都能构造的链接。

    原样塞进 href 就等于在本地控制台上开了个跳转到站外的口子。
    """
    for hostile in ("//evil.example", "https://evil.example", "javascript:alert(1)"):
        body = client_for_back(hostile)
        assert "evil.example" not in body
        assert "javascript:" not in body
        assert 'href="/planets"' in body


def client_for_back(back: str) -> str:
    client, _service = _make_client()
    return client.get("/targets/2:1:5", params={"back": back}).text


def test_the_detail_page_says_so_when_there_is_no_report_yet() -> None:
    # 「还没有战报」是常态而不是异常：舰队组成要等真打过一仗才有。
    client, _service = _make_client()

    body = client.get("/targets/2:1:5").text

    assert "还没有战报" in body
