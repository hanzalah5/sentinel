"""The investigation graph.

Iteration 3 shape: ``retrieve_runbooks`` -> ``triage_and_hypothesize``. Retrieval grounds
the hypothesis in curated runbooks; it degrades to an empty context (and the agent still
answers) when RAG is disabled or a lookup fails.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.agent.state import InvestigationState
from sentinel.contracts import Alert, RootCauseHypothesis
from sentinel.rag.runbooks import NullRetriever, RunbookMatch, RunbookRetriever

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """\
You are a senior Site Reliability Engineer acting as an automated incident-response \
assistant. You are given a production alert and a set of potentially relevant runbooks. \
Produce a concise, evidence-based root-cause analysis.

Rules:
- Ground your reasoning in the provided runbooks where they apply; prefer their guidance \
over guesses.
- List the exact titles of the runbooks you actually relied on in `runbook_references`.
- Rank plausible causes by likelihood, most likely first.
- Assign each cause a calibrated confidence between 0 and 1.
- Cite the specific signals in the alert (and runbook guidance) that support each cause.
- Recommend a single safe next diagnostic or mitigation step.
- Do not fabricate metrics or logs that are not present in the alert. If the runbooks or \
evidence are thin, say so and lower your confidence accordingly."""


def _render_alert(alert: Alert) -> str:
    labels = "\n".join(f"  - {k}: {v}" for k, v in alert.labels.items()) or "  (none)"
    return (
        f"Alert ID: {alert.alert_id}\n"
        f"Title: {alert.title}\n"
        f"Severity: {alert.severity.value}\n"
        f"Service: {alert.service or 'unknown'}\n"
        f"Source: {alert.source}\n"
        f"Fired at: {alert.fired_at.isoformat()}\n"
        f"Description:\n{alert.description}\n"
        f"Labels:\n{labels}"
    )


def _render_runbooks(matches: list[RunbookMatch]) -> str:
    if not matches:
        return (
            "Runbooks:\n  (No runbooks were retrieved. Reason from first principles and "
            "lower your confidence accordingly.)"
        )
    blocks = [f"### {m.title} (source: {m.source})\n{m.content}" for m in matches]
    return (
        "Relevant runbooks (cite the titles you rely on in `runbook_references`):\n\n"
        + "\n\n".join(blocks)
    )


def build_investigation_messages(alert: Alert, matches: list[RunbookMatch]) -> list[BaseMessage]:
    """Assemble the chat messages for the triage step (public for testability)."""
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=f"{_render_alert(alert)}\n\n{_render_runbooks(matches)}"),
    ]


def _make_retrieve_node(retriever: RunbookRetriever, top_k: int) -> Any:
    def retrieve_runbooks(
        state: InvestigationState, config: RunnableConfig
    ) -> dict[str, list[RunbookMatch]]:
        alert = state["alert"]
        query = f"{alert.title}\n{alert.description}"
        try:
            matches = retriever.retrieve(query, top_k)
        except Exception as exc:  # a retrieval blip must not fail the investigation
            logger.warning("rag.retrieve_failed", alert_id=alert.alert_id, error=str(exc))
            matches = []
        return {"runbook_matches": matches}

    return retrieve_runbooks


def _make_triage_node(structured_model: Runnable[Any, Any]) -> Any:
    def triage_and_hypothesize(
        state: InvestigationState, config: RunnableConfig
    ) -> dict[str, RootCauseHypothesis]:
        messages = build_investigation_messages(state["alert"], state.get("runbook_matches", []))
        hypothesis = structured_model.invoke(messages, config=config)
        if hypothesis is None:
            # Defensive: a structured-output parser can yield None on an empty/blocked
            # completion; fail loudly here rather than with an opaque AttributeError later.
            raise ValueError("structured model returned no hypothesis")
        return {"hypothesis": hypothesis}

    return triage_and_hypothesize


def build_graph_from_structured(
    structured_model: Runnable[Any, Any],
    retriever: RunbookRetriever | None = None,
    *,
    top_k: int = 3,
) -> CompiledStateGraph:
    """Compile the graph from a runbook retriever and a structured-output runnable.

    Used directly by tests to inject deterministic fakes. ``retriever`` defaults to a
    no-op so callers that don't care about RAG keep working.
    """
    retriever = retriever or NullRetriever()
    builder = StateGraph(InvestigationState)
    builder.add_node("retrieve_runbooks", _make_retrieve_node(retriever, top_k))
    builder.add_node("triage_and_hypothesize", _make_triage_node(structured_model))
    builder.add_edge(START, "retrieve_runbooks")
    builder.add_edge("retrieve_runbooks", "triage_and_hypothesize")
    builder.add_edge("triage_and_hypothesize", END)
    return builder.compile()


def build_graph(
    model: BaseChatModel, retriever: RunbookRetriever, *, top_k: int = 3
) -> CompiledStateGraph:
    """Compile the investigation graph, forcing structured output on the model."""
    structured_model = model.with_structured_output(RootCauseHypothesis)
    return build_graph_from_structured(structured_model, retriever, top_k=top_k)
