"""Incident lifecycle service over a durable, interruptible graph.

Each incident is a LangGraph *thread* (keyed by ``thread_id``). ``start`` runs until the
graph either finishes or pauses at the approval interrupt; ``decide`` resumes a paused
thread with the human's decision; ``get`` reports current durable state. All three read
the authoritative state back from the checkpointer via ``get_state`` (``next`` is set
while paused), so status is correct regardless of how invoke surfaces interrupts.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from sentinel.config import Settings
from sentinel.contracts import (
    Alert,
    ApprovalDecision,
    ExecutionOutcome,
    IncidentStatus,
    IncidentView,
)
from sentinel.logging import set_correlation_id

logger = structlog.get_logger(__name__)


class IncidentService:
    """Drives the incident graph and packages its state as an ``IncidentView``."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        settings: Settings,
        langfuse_handler: BaseCallbackHandler | None = None,
    ) -> None:
        self._graph = graph
        self._settings = settings
        self._langfuse_handler = langfuse_handler

    def _config(self, thread_id: str) -> RunnableConfig:
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "run_name": "incident",
            "metadata": {
                "correlation_id": thread_id,
                "langfuse_tags": ["sentinel", "iteration-4", "incident"],
            },
        }
        if self._langfuse_handler is not None:
            config["callbacks"] = [self._langfuse_handler]
        return config

    def start(self, alert: Alert, thread_id: str | None = None) -> IncidentView:
        """Open an incident; run until it completes or pauses for approval."""
        tid = thread_id or uuid.uuid4().hex
        set_correlation_id(tid)
        logger.info("incident.start", thread_id=tid, alert_id=alert.alert_id)
        self._graph.invoke({"alert": alert}, config=self._config(tid))
        view = self._view_from_state(tid)
        if view is None:  # pragma: no cover - state is written by the run above
            raise RuntimeError("incident state missing immediately after start")
        return view

    def decide(self, thread_id: str, decision: ApprovalDecision) -> IncidentView | None:
        """Resume a paused incident with a decision. None if the thread is unknown."""
        set_correlation_id(thread_id)
        snapshot = self._graph.get_state(self._config(thread_id))
        if not snapshot.values:
            return None
        if snapshot.next:  # still paused: resume it
            logger.info("incident.decide", thread_id=thread_id, decision=decision.value)
            self._graph.invoke(
                Command(resume={"decision": decision.value}),
                config=self._config(thread_id),
            )
        return self._view_from_state(thread_id)

    def get(self, thread_id: str) -> IncidentView | None:
        """Report the current durable state of an incident. None if unknown."""
        return self._view_from_state(thread_id)

    def _view_from_state(self, thread_id: str) -> IncidentView | None:
        snapshot = self._graph.get_state(self._config(thread_id))
        if not snapshot.values:
            return None
        return self._view(thread_id, snapshot.values, awaiting=bool(snapshot.next))

    def _view(self, thread_id: str, values: dict[str, Any], awaiting: bool) -> IncidentView:
        alert = values.get("alert")
        decision = values.get("decision")
        execution = values.get("execution")

        if awaiting:
            status = IncidentStatus.awaiting_approval
        elif decision == ApprovalDecision.rejected:
            status = IncidentStatus.rejected
        elif execution is not None and execution.outcome == ExecutionOutcome.failed:
            status = IncidentStatus.failed
        elif execution is not None:
            status = IncidentStatus.executed
        else:  # pragma: no cover - defensive; a settled incident always has one of these
            status = IncidentStatus.awaiting_approval

        return IncidentView(
            thread_id=thread_id,
            status=status,
            alert_id=alert.alert_id if alert is not None else "unknown",
            hypothesis=values.get("hypothesis"),
            remediation=values.get("remediation"),
            decision=decision,
            execution=execution,
        )
