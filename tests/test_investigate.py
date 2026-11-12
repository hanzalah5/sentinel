"""End-to-end behaviour of the /investigate endpoint."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

ALERT_PAYLOAD: dict[str, Any] = {
    "alert_id": "alert-1024",
    "title": "High p99 latency on checkout-api",
    "description": (
        "p99 latency rose from 180ms to 2.4s over 5 minutes. Error rate steady. "
        "DB connection pool saturation warnings in checkout-api logs."
    ),
    "severity": "critical",
    "service": "checkout-api",
    "source": "prometheus",
    "fired_at": "2026-07-12T09:31:00Z",
    "labels": {"region": "us-east-1", "deployment": "v3.14.2"},
}


def test_investigate_returns_structured_result(client: TestClient) -> None:
    response = client.post("/investigate", json=ALERT_PAYLOAD)

    assert response.status_code == 200
    body = response.json()
    assert body["alert_id"] == "alert-1024"
    assert body["model"] == "gemini-3.8-flash"
    assert body["hypothesis"]["candidate_causes"]
    assert body["hypothesis"]["recommended_next_step"]
    # The correlation ID is echoed on the response and matches the result body.
    assert response.headers["X-Correlation-ID"] == body["correlation_id"]


def test_investigate_honours_inbound_correlation_id(client: TestClient) -> None:
    response = client.post(
        "/investigate", json=ALERT_PAYLOAD, headers={"X-Correlation-ID": "abc123"}
    )

    assert response.status_code == 200
    assert response.headers["X-Correlation-ID"] == "abc123"
    assert response.json()["correlation_id"] == "abc123"


def test_investigate_503_when_unready(unready_client: TestClient) -> None:
    response = unready_client.post("/investigate", json=ALERT_PAYLOAD)
    assert response.status_code == 503


def test_investigate_422_on_invalid_payload(client: TestClient) -> None:
    response = client.post("/investigate", json={"title": "missing required fields"})
    assert response.status_code == 422
