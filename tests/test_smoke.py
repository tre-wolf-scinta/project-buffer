from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_endpoints(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}
    assert client.get("/health/ready").status_code == 200
    # No worker has checked in during tests.
    assert client.get("/health/worker").status_code == 503


def test_sign_in_and_see_empty_inbox(auth_client: TestClient) -> None:
    response = auth_client.get("/inbox")
    assert response.status_code == 200
    assert "No messages in Open." in response.text
