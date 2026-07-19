"""Application service: runs an investigation and packages a traceable result."""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from sentinel.config import Settings
from sentinel.contracts import Alert, InvestigationResult
from sentinel.logging import get_correlation_id, new_correlation_id, set_correlation_id

logger = structlog.get_logger(__name__)


class InvestigationService:
    """Wires the compiled graph, settings, and (optional) Langfuse handler together."""

    def __init__(
        self,
        graph: CompiledStateGraph,
        settings: Settings,
        langfuse_handler: BaseCallbackHandler | None = None,
    ) -> None:
        self._graph = graph
        self._settings = settings
        self._langfuse_handler = langfuse_handler

    def investigate(self, alert: Alert, correlation_id: str | None = None) -> InvestigationResult:
        """Run the agent over an alert and return a structured, traceable result."""
        cid = correlation_id or get_correlation_id() or new_correlation_id()
        # Bind for callers that don't set it themselves (e.g. the ingestion worker),
        # so every log line from this investigation carries the same id as the trace.
        set_correlation_id(cid)

        config: RunnableConfig = {
            "run_name": "incident-investigation",
            "metadata": {
                "correlation_id": cid,
                "alert_id": alert.alert_id,
                "service": alert.service,
                "severity": alert.severity.value,
                "langfuse_tags": ["sentinel", "iteration-1", alert.severity.value],
            },
        }
        if self._langfuse_handler is not None:
            config["callbacks"] = [self._langfuse_handler]

        logger.info(
            "investigation.start",
            alert_id=alert.alert_id,
            severity=alert.severity.value,
            service=alert.service,
        )
        state = self._graph.invoke({"alert": alert}, config=config)
        hypothesis = state["hypothesis"]
        logger.info(
            "investigation.complete",
            alert_id=alert.alert_id,
            candidate_causes=len(hypothesis.candidate_causes),
        )

        return InvestigationResult(
            alert_id=alert.alert_id,
            correlation_id=cid,
            model=self._settings.gemini_model,
            hypothesis=hypothesis,
            created_at=datetime.now(UTC),
        )
