from fastapi.testclient import TestClient

from evo_helper.web.app import create_app


def _client() -> TestClient:
    return TestClient(create_app(local_token="test-token"))


def test_mutating_request_without_origin_or_token_is_rejected() -> None:
    client = _client()
    response = client.post("/api/revisits", json={"scope": "plan", "reason": "recheck"})

    assert response.status_code == 403
    assert response.json()["detail"] == "missing or invalid local token / origin"


def test_mutating_request_with_local_token_is_allowed() -> None:
    client = _client()
    response = client.post(
        "/api/revisits",
        headers={"X-Evo-Helper-Token": "test-token"},
        json={"scope": "plan", "reason": "recheck"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"


def test_mutating_request_with_wrong_token_is_rejected() -> None:
    client = _client()
    response = client.post(
        "/api/revisits",
        headers={"X-Evo-Helper-Token": "wrong"},
        json={"scope": "plan", "reason": "recheck"},
    )

    assert response.status_code == 403


def test_mutating_request_with_matching_origin_is_allowed() -> None:
    client = _client()
    response = client.post(
        "/api/revisits",
        headers={"Origin": "http://testserver"},
        json={"scope": "plan", "reason": "recheck"},
    )

    assert response.status_code == 201


def test_mutating_request_with_foreign_origin_is_rejected() -> None:
    client = _client()
    response = client.post(
        "/api/revisits",
        headers={"Origin": "http://evil.example"},
        json={"scope": "plan", "reason": "recheck"},
    )

    assert response.status_code == 403


def test_read_requests_are_not_blocked() -> None:
    client = _client()

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/plans").status_code == 200
