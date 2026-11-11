"""Shared test fixtures.

The LLM is replaced with a deterministic runnable so the whole suite runs offline,
with no API key, and CI stays keyless.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import RunnableLambda

from sentinel.agent.graph import build_graph_from_structured
from sentinel.api.app import create_app
from sentinel.config import Settings
from sentinel.contracts import CandidateCause, IncidentCategory, RootCauseHypothesis
from sentinel.service import InvestigationService

FIXED_HYPOTHESIS = RootCauseHypothesis(
    summary="Database connection pool saturation on checkout-api is the likely cause.",
    candidate_causes=[
        CandidateCause(
            cause="Database connection pool exhaustion",
            category=IncidentCategory.db_pool_saturation,
            confidence=0.72,
            evidence=[
                "DB connection pool saturation warnings",
                "p99 latency spike to 2.4s",
            ],
        ),
        CandidateCause(
            cause="Slow downstream dependency",
            category=IncidentCategory.dependency_failure,
            confidence=0.2,
            evidence=["latency rose while error rate stayed flat"],
        ),
    ],
    recommended_next_step="Inspect checkout-api DB pool metrics and max_connections.",
)


def fake_structured_model() -> RunnableLambda[Any, RootCauseHypothesis]:
    """A stand-in for ``model.with_structured_output(RootCauseHypothesis)``."""
    return RunnableLambda(lambda _messages: FIXED_HYPOTHESIS)


@pytest.fixture
def ready_settings() -> Settings:
    return Settings(
        google_api_key="test-key",
        gemini_model="gemini-2.5-flash",
        json_logs=False,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


@pytest.fixture
def fake_service(ready_settings: Settings) -> InvestigationService:
    graph = build_graph_from_structured(fake_structured_model())
    return InvestigationService(graph=graph, settings=ready_settings)


@pytest.fixture
def client(ready_settings: Settings, fake_service: InvestigationService) -> Iterator[TestClient]:
    app = create_app(settings=ready_settings, service=fake_service)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def unready_client() -> Iterator[TestClient]:
    settings = Settings(
        google_api_key=None,
        json_logs=False,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    app = create_app(settings=settings, service=None)
    with TestClient(app) as test_client:
        yield test_client
