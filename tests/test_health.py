"""Liveness/readiness endpoint behaviour."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_healthz_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_ok_when_service_ready(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_healthz_ok_even_when_unready(unready_client: TestClient) -> None:
    # Liveness must stay green even without an LLM configured.
    response = unready_client.get("/healthz")
    assert response.status_code == 200


def test_readyz_503_when_unready(unready_client: TestClient) -> None:
    response = unready_client.get("/readyz")
    assert response.status_code == 503
