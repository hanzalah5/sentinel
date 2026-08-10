"""Durable human-in-the-loop incident flow (keyless: fake model + MemorySaver)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from sentinel.agent.incident_graph import (
    _coerce_decision,
    _requires_approval,
    build_incident_graph_from_parts,
)
from sentinel.api.app import create_app
from sentinel.config import Settings
from sentinel.contracts import (
    ActionRisk,
    Alert,
    AlertSeverity,
    ApprovalDecision,
    ExecutionOutcome,
    ExecutionResult,
    IncidentStatus,
    RemediationAction,
    RootCauseHypothesis,
)
from sentinel.incident import IncidentService
from sentinel.rag.runbooks import NullRetriever

_HYPOTHESIS = RootCauseHypothesis(
    summary="Post-deploy regression in checkout-api",
    candidate_causes=[],
    recommended_next_step="roll back",
)
_HIGH_RISK = RemediationAction(
    summary="Roll back the bad deploy",
    command="kubectl rollout undo deployment/checkout-api",
    risk=ActionRisk.high,
    rationale="5xx began right after v2.3.0",
)
_LOW_RISK = RemediationAction(
    summary="Bounce one pod",
    command="kubectl delete pod checkout-api-abc123",
    risk=ActionRisk.low,
    rationale="single stuck pod",
)


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, action: RemediationAction) -> ExecutionResult:
        self.calls.append(action.command)
        return ExecutionResult(outcome=ExecutionOutcome.executed, detail="ok")


def _fixed(obj: Any) -> Runnable[Any, Any]:
    return RunnableLambda(lambda _messages: obj)


def _service(
    action: RemediationAction,
    executor: RecordingExecutor | None = None,
    *,
    auto_approve_max_risk: str = "low",
    checkpointer: MemorySaver | None = None,
) -> tuple[IncidentService, RecordingExecutor]:
    executor = executor or RecordingExecutor()
    graph = build_incident_graph_from_parts(
        _fixed(_HYPOTHESIS),
        _fixed(action),
        NullRetriever(),
        executor,
        checkpointer or MemorySaver(),
        auto_approve_max_risk=auto_approve_max_risk,
    )
    settings = Settings(
        google_api_key=None,
        json_logs=False,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    return IncidentService(graph=graph, settings=settings), executor


def _alert(alert_id: str) -> Alert:
    return Alert(
        alert_id=alert_id,
        title="Elevated 5xx on checkout-api",
        description="5xx rose to 12% after deploy v2.3.0",
        severity=AlertSeverity.critical,
        service="checkout-api",
        fired_at=datetime.now(UTC),
    )


def test_high_risk_pauses_for_approval_then_executes() -> None:
    service, executor = _service(_HIGH_RISK)

    view = service.start(_alert("i-1"), thread_id="t1")
    assert view.status == IncidentStatus.awaiting_approval
    assert view.remediation is not None and view.remediation.requires_approval is True
    assert view.execution is None
    assert executor.calls == []  # nothing runs before approval

    final = service.decide("t1", ApprovalDecision.approved)
    assert final is not None
    assert final.status == IncidentStatus.executed
    assert final.execution is not None
    assert final.execution.outcome == ExecutionOutcome.executed
    assert executor.calls == [_HIGH_RISK.command]


def test_rejection_skips_execution() -> None:
    service, executor = _service(_HIGH_RISK)
    service.start(_alert("i-2"), thread_id="t2")

    final = service.decide("t2", ApprovalDecision.rejected)

    assert final is not None
    assert final.status == IncidentStatus.rejected
    assert final.execution is None
    assert executor.calls == []


def test_low_risk_auto_executes_without_interrupt() -> None:
    service, executor = _service(_LOW_RISK)

    view = service.start(_alert("i-3"), thread_id="t3")

    assert view.status == IncidentStatus.executed
    assert view.remediation is not None and view.remediation.requires_approval is False
    assert executor.calls == [_LOW_RISK.command]


def test_get_reflects_state_and_unknown_is_none() -> None:
    service, _ = _service(_HIGH_RISK)
    assert service.get("ghost") is None

    service.start(_alert("i-4"), thread_id="t4")
    view = service.get("t4")
    assert view is not None and view.status == IncidentStatus.awaiting_approval


def test_decide_unknown_thread_returns_none() -> None:
    service, _ = _service(_HIGH_RISK)
    assert service.decide("ghost", ApprovalDecision.approved) is None


# --- Safety invariants of the shipped default policy ---


def test_production_default_requires_approval_even_for_low_risk() -> None:
    """The shipped default ("none") must gate *every* action, including low risk."""
    service, executor = _service(_LOW_RISK, auto_approve_max_risk="none")

    view = service.start(_alert("i-5"), thread_id="t5")

    assert view.status == IncidentStatus.awaiting_approval
    assert view.remediation is not None and view.remediation.requires_approval is True
    assert executor.calls == []


@pytest.mark.parametrize("risk", [ActionRisk.low, ActionRisk.medium, ActionRisk.high])
def test_requires_approval_under_none_and_unknown_ceilings(risk: ActionRisk) -> None:
    # "none" (the shipped default) and any unrecognised ceiling must fail closed.
    assert _requires_approval(risk, "none") is True
    assert _requires_approval(risk, "typo-ceiling") is True


def test_requires_approval_respects_an_explicit_ceiling() -> None:
    assert _requires_approval(ActionRisk.low, "low") is False
    assert _requires_approval(ActionRisk.medium, "low") is True
    assert _requires_approval(ActionRisk.high, "medium") is True


@pytest.mark.parametrize(
    "resumed",
    [{}, {"decision": None}, {"decision": "approve"}, "yes", None, 42],
)
def test_coerce_decision_fails_closed(resumed: Any) -> None:
    """Anything that isn't an explicit, valid approval must become 'rejected'."""
    assert _coerce_decision(resumed) == ApprovalDecision.rejected


def test_coerce_decision_accepts_valid_decisions() -> None:
    assert _coerce_decision({"decision": "approved"}) == ApprovalDecision.approved
    assert _coerce_decision("approved") == ApprovalDecision.approved
    assert _coerce_decision(ApprovalDecision.approved) == ApprovalDecision.approved
    assert _coerce_decision({"decision": "rejected"}) == ApprovalDecision.rejected


def test_malformed_resume_does_not_execute() -> None:
    """An end-to-end resume without a clear approval must not run the remediation."""
    checkpointer = MemorySaver()
    service, executor = _service(_HIGH_RISK, checkpointer=checkpointer)
    service.start(_alert("i-6"), thread_id="t6")

    # Resume the paused graph with a payload that carries no decision at all.
    service._graph.invoke(
        Command(resume={"note": "clicked the wrong button"}),
        config={"configurable": {"thread_id": "t6"}},
    )

    view = service.get("t6")
    assert view is not None
    assert view.status == IncidentStatus.rejected
    assert executor.calls == []


def test_paused_incident_resumes_through_a_new_service_instance() -> None:
    """Durability: the pause lives in the checkpointer, not the service object."""
    checkpointer = MemorySaver()
    starter, executor = _service(_HIGH_RISK, checkpointer=checkpointer)
    view = starter.start(_alert("i-7"), thread_id="t7")
    assert view.status == IncidentStatus.awaiting_approval

    # A *different* service instance (as after a restart) sharing the same store.
    resumer, _ = _service(_HIGH_RISK, executor, checkpointer=checkpointer)
    final = resumer.decide("t7", ApprovalDecision.approved)

    assert final is not None
    assert final.status == IncidentStatus.executed
    assert executor.calls == [_HIGH_RISK.command]


# --- API level ---

ALERT_PAYLOAD: dict[str, Any] = {
    "alert_id": "api-1",
    "title": "Elevated 5xx on checkout-api",
    "description": "5xx rose to 12% after deploy v2.3.0",
    "severity": "critical",
    "service": "checkout-api",
    "source": "prometheus",
    "fired_at": "2026-07-13T09:31:00Z",
}


@pytest.fixture
def incident_client() -> Iterator[TestClient]:
    graph = build_incident_graph_from_parts(
        _fixed(_HYPOTHESIS),
        _fixed(_HIGH_RISK),
        NullRetriever(),
        RecordingExecutor(),
        MemorySaver(),
        auto_approve_max_risk="low",
    )
    settings = Settings(
        google_api_key=None,
        json_logs=False,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    service = IncidentService(graph=graph, settings=settings)
    app = create_app(settings=settings, service=None, incident_service=service)
    with TestClient(app) as test_client:
        yield test_client


def test_incident_api_pause_then_approve(incident_client: TestClient) -> None:
    created = incident_client.post("/incidents", json=ALERT_PAYLOAD)
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "awaiting_approval"
    assert body["remediation"]["risk"] == "high"
    thread_id = body["thread_id"]

    fetched = incident_client.get(f"/incidents/{thread_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "awaiting_approval"

    decided = incident_client.post(
        f"/incidents/{thread_id}/decision", json={"decision": "approved"}
    )
    assert decided.status_code == 200
    body = decided.json()
    assert body["status"] == "executed"
    assert body["execution"]["outcome"] == "executed"


def test_incident_api_decision_unknown_is_404(incident_client: TestClient) -> None:
    response = incident_client.post("/incidents/ghost/decision", json={"decision": "approved"})
    assert response.status_code == 404


def test_incidents_503_when_service_unavailable() -> None:
    settings = Settings(
        google_api_key=None,
        json_logs=False,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    app = create_app(settings=settings, service=None, incident_service=None)
    with TestClient(app) as client:
        response = client.post("/incidents", json=ALERT_PAYLOAD)
        assert response.status_code == 503
