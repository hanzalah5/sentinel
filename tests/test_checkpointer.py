"""Checkpoint serialization: incidents survive a restart as real types, and only known
types are ever revived from a checkpoint.

The Postgres tests run when SENTINEL_TEST_DATABASE_URL points at a disposable database
(CI provides one); otherwise they are skipped.
"""

from __future__ import annotations

import os
import typing
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.event_hooks import SerdeEvent, register_serde_event_listener
from pydantic import BaseModel

from sentinel.agent.incident_graph import IncidentState, build_incident_graph_from_parts
from sentinel.checkpointer import build_checkpointer, build_serializer, checkpoint_allowlist
from sentinel.config import Settings
from sentinel.contracts import (
    ActionRisk,
    Alert,
    ApprovalDecision,
    CandidateCause,
    ExecutionOutcome,
    ExecutionResult,
    IncidentCategory,
    IncidentStatus,
    RemediationAction,
    RootCauseHypothesis,
)
from sentinel.incident import IncidentService
from sentinel.rag.runbooks import NullRetriever, RunbookMatch
from sentinel.remediation.executor import SimulatedExecutor

TEST_DATABASE_URL = os.environ.get("SENTINEL_TEST_DATABASE_URL")
needs_postgres = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="set SENTINEL_TEST_DATABASE_URL to run Postgres tests"
)

_HYPOTHESIS = RootCauseHypothesis(
    summary="Connection pool exhausted after the deploy.",
    candidate_causes=[
        CandidateCause(
            cause="pool exhausted", category=IncidentCategory.db_pool_saturation, confidence=0.7
        )
    ],
    recommended_next_step="Roll back.",
    runbook_references=["High p99 latency / database connection pool saturation"],
)
_ACTION = RemediationAction(
    summary="Roll back checkout-api",
    command="kubectl rollout undo deployment/checkout-api",
    risk=ActionRisk.high,
    rationale="The regression started with the deploy.",
)
_STATE: dict[str, Any] = {
    "alert": Alert(
        alert_id="cp-1",
        title="High p99 latency",
        description="p99 up",
        fired_at=datetime(2026, 7, 1, tzinfo=UTC),
    ),
    "runbook_matches": [RunbookMatch(title="T", source="t.md", score=0.9, content="c")],
    "hypothesis": _HYPOTHESIS,
    "remediation": _ACTION,
    "decision": ApprovalDecision.approved,
    "decided_by": "oncall-alice",
    "decision_note": "approved in #incident",
    "execution": ExecutionResult(outcome=ExecutionOutcome.executed, detail="ok"),
}


@pytest.fixture
def serde_events() -> Iterator[list[SerdeEvent]]:
    """Every serializer event (unregistered / blocked types). Unlike LangGraph's log
    warnings, events are not de-duplicated per process, so tests can rely on them."""
    events: list[SerdeEvent] = []
    unregister = register_serde_event_listener(events.append)
    try:
        yield events
    finally:
        unregister()


def _state_types(tp: Any, found: set[type]) -> None:
    """Collect every Pydantic model and Enum reachable from a type annotation."""
    origin = typing.get_origin(tp)
    if origin is not None:
        for arg in typing.get_args(tp):
            _state_types(arg, found)
        return
    if isinstance(tp, type) and tp not in found:
        if issubclass(tp, Enum):
            found.add(tp)
        elif issubclass(tp, BaseModel):
            found.add(tp)
            for field in tp.model_fields.values():
                _state_types(field.annotation, found)


def _service(checkpointer: BaseCheckpointSaver[Any]) -> IncidentService:
    graph = build_incident_graph_from_parts(
        RunnableLambda(lambda _m: _HYPOTHESIS),
        RunnableLambda(lambda _m: _ACTION),
        NullRetriever(),
        SimulatedExecutor(),
        checkpointer,
    )
    return IncidentService(graph=graph, settings=Settings(json_logs=False))


# --------------------------------------------------------------------------- serializer


def test_allowlist_covers_every_type_incident_state_can_hold() -> None:
    found: set[type] = set()
    for hint in typing.get_type_hints(IncidentState).values():
        _state_types(hint, found)
    allowed = set(checkpoint_allowlist())
    missing = {(t.__module__, t.__name__) for t in found} - allowed
    assert not missing, f"add these modules to _STATE_TYPE_MODULES: {sorted(missing)}"
    assert ("sentinel.contracts", "RemediationAction") in allowed  # sanity: the walk found types


def test_state_round_trips_as_real_types_without_warnings(serde_events: list[SerdeEvent]) -> None:
    serde = build_serializer()
    for key, value in _STATE.items():
        revived = serde.loads_typed(serde.dumps_typed(value))
        assert revived == value, key
        assert type(revived) is type(value), key
    assert serde_events == []


def test_unknown_types_are_not_instantiated(serde_events: list[SerdeEvent]) -> None:
    class Planted(BaseModel):
        payload: str

    serde = build_serializer()
    revived = serde.loads_typed(serde.dumps_typed(Planted(payload="x")))
    assert not isinstance(revived, Planted)
    assert revived == {"payload": "x"}  # left as plain data
    assert [e["kind"] for e in serde_events] == ["msgpack_blocked"]


# --------------------------------------------------------------------------- savers


def test_in_memory_saver_uses_the_strict_serializer(serde_events: list[SerdeEvent]) -> None:
    saver = build_checkpointer(Settings(database_url=None, json_logs=False))
    assert isinstance(saver, MemorySaver)
    first, second = _service(saver), _service(saver)

    assert first.start(_STATE["alert"], thread_id="m-1").status == IncidentStatus.awaiting_approval
    view = second.decide("m-1", ApprovalDecision.approved, decided_by="oncall-alice", note="ok")

    assert view is not None and view.status == IncidentStatus.executed
    assert view.decided_by == "oncall-alice"
    assert serde_events == []


@needs_postgres
def test_paused_incident_survives_a_restart_on_postgres(serde_events: list[SerdeEvent]) -> None:
    """Pause with one checkpointer, then resume with a brand-new one (a process restart)."""
    settings = Settings(database_url=TEST_DATABASE_URL, json_logs=False)
    thread_id = uuid.uuid4().hex

    before = build_checkpointer(settings)
    assert not isinstance(before, MemorySaver), "expected the Postgres saver"
    started = _service(before).start(_STATE["alert"], thread_id=thread_id)
    assert started.status == IncidentStatus.awaiting_approval

    after = build_checkpointer(settings)  # fresh pool, fresh saver: nothing shared in memory
    view = _service(after).decide(
        thread_id, ApprovalDecision.approved, decided_by="oncall-alice", note="restart test"
    )

    assert view is not None and view.status == IncidentStatus.executed
    assert isinstance(view.remediation, RemediationAction)
    assert view.remediation == _ACTION
    assert (view.decided_by, view.decision_note) == ("oncall-alice", "restart test")
    assert serde_events == []
