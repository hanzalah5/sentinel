"""API-key authentication: configuration parsing, the authenticator, and the API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sentinel.api.app import create_app
from sentinel.api.auth import ApiKeyAuthenticator, Principal
from sentinel.config import Settings, parse_api_keys
from sentinel.service import InvestigationService
from tests.conftest import AUTH_HEADERS, TEST_API_KEY

KEY_A = "a" * 24
KEY_B = "b" * 32

ALERT = {
    "alert_id": "auth-1",
    "title": "High p99 latency",
    "description": "p99 up",
    "fired_at": "2026-07-12T09:31:00Z",
}

# Every route except liveness/readiness must require a key.
PROTECTED = [
    ("post", "/investigate", ALERT),
    ("post", "/incidents", ALERT),
    ("get", "/incidents/t-1", None),
    ("post", "/incidents/t-1/decision", {"decision": "approved"}),
]


# --------------------------------------------------------------------------- parsing


def test_parse_api_keys() -> None:
    assert parse_api_keys(f" ci:{KEY_A} , oncall-bob:{KEY_B},, ") == {
        "ci": KEY_A,
        "oncall-bob": KEY_B,
    }
    assert parse_api_keys("") == {}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (KEY_A, "must be 'name:key'"),
        (f"bad name:{KEY_A}", "must be 'name:key'"),
        ("ci:tooshort", "at least 24 characters"),
        (f"ci:{KEY_A[:-1]} x", "no whitespace"),
        (f"ci:{KEY_A},ci:{KEY_B}", "duplicate name 'ci'"),
        (f"ci:{KEY_A},cd:{KEY_A}", "reuses the same key"),
    ],
)
def test_parse_api_keys_rejects_bad_config_without_leaking_keys(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message) as excinfo:
        parse_api_keys(raw)
    assert KEY_A not in str(excinfo.value) and KEY_B not in str(excinfo.value)


def test_settings_fail_fast_on_bad_keys_and_never_print_them() -> None:
    with pytest.raises(ValidationError):
        Settings(sentinel_api_keys="ci:tooshort", json_logs=False)
    settings = Settings(sentinel_api_keys=f"ci:{KEY_A}", json_logs=False)
    assert settings.api_keys() == {"ci": KEY_A}
    assert KEY_A not in repr(settings)
    assert Settings(json_logs=False).api_keys() == {}


# --------------------------------------------------------------------------- authenticator


def test_authenticator_maps_keys_to_names() -> None:
    auth = ApiKeyAuthenticator({"ci": KEY_A, "oncall": KEY_B})
    assert auth.configured
    assert auth.authenticate(KEY_B) == Principal("oncall")
    assert auth.authenticate("not-a-key") is None
    assert not ApiKeyAuthenticator({}).configured


# --------------------------------------------------------------------------- API


@pytest.fixture
def anonymous(ready_settings: Settings, fake_service: InvestigationService) -> Iterator[TestClient]:
    """The real app with keys configured, but a client that sends no credentials."""
    with TestClient(create_app(settings=ready_settings, service=fake_service)) as client:
        yield client


def test_health_endpoints_need_no_key(anonymous: TestClient) -> None:
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/readyz").status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), PROTECTED)
def test_protected_routes_reject_a_missing_key(
    anonymous: TestClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    response = anonymous.request(method, path, json=body)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "header",
    ["Bearer wrong-key-0123456789abcdefgh", f"Basic {TEST_API_KEY}", TEST_API_KEY],
)
def test_wrong_key_or_scheme_is_401(anonymous: TestClient, header: str) -> None:
    response = anonymous.post("/investigate", json=ALERT, headers={"Authorization": header})
    assert response.status_code == 401


def test_valid_key_is_accepted(anonymous: TestClient) -> None:
    response = anonymous.post("/investigate", json=ALERT, headers=AUTH_HEADERS)
    assert response.status_code == 200


def test_auth_is_checked_before_service_availability() -> None:
    """An unauthenticated caller learns nothing about which services are up."""
    settings = Settings(google_api_key=None, json_logs=False, sentinel_api_keys=f"t:{TEST_API_KEY}")
    with TestClient(create_app(settings=settings, service=None)) as client:
        assert client.post("/investigate", json=ALERT).status_code == 401


def test_no_keys_configured_fails_closed(fake_service: InvestigationService) -> None:
    settings = Settings(google_api_key="k", json_logs=False, sentinel_api_keys=None)
    with TestClient(create_app(settings=settings, service=fake_service)) as client:
        response = client.post("/investigate", json=ALERT, headers=AUTH_HEADERS)
        assert response.status_code == 503
        assert "SENTINEL_API_KEYS" in response.json()["detail"]
        readyz = client.get("/readyz")
        assert readyz.status_code == 503
        assert "SENTINEL_API_KEYS" in readyz.json()["detail"]


def test_openapi_documents_bearer_auth(anonymous: TestClient) -> None:
    spec = anonymous.get("/openapi.json").json()
    assert spec["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    assert spec["paths"]["/investigate"]["post"]["security"] == [{"HTTPBearer": []}]
    assert "security" not in spec["paths"]["/healthz"]["get"]
