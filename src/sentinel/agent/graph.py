"""The investigation graph.

Iteration 1 is deliberately a single node: given an alert, produce a structured
root-cause hypothesis. The graph shape exists so later iterations can add nodes
(retrieve runbook, plan remediation, request approval) without reworking the wiring.
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sentinel.agent.state import InvestigationState
from sentinel.contracts import Alert, RootCauseHypothesis

_SYSTEM_PROMPT = """\
You are a senior Site Reliability Engineer acting as an automated incident-response \
assistant. Given a production alert, produce a concise, evidence-based root-cause \
analysis.

Rules:
- Rank plausible causes by likelihood, most likely first.
- Assign each cause a calibrated confidence between 0 and 1.
- Cite the specific signals in the alert that support each cause.
- Recommend a single safe next diagnostic or mitigation step.
- Do not fabricate metrics or logs that are not present in the alert. If the evidence \
is thin, say so and lower your confidence accordingly."""


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


def _make_triage_node(
    structured_model: Runnable[Any, Any],
) -> Any:
    def triage_and_hypothesize(
        state: InvestigationState, config: RunnableConfig
    ) -> dict[str, RootCauseHypothesis]:
        alert = state["alert"]
        messages = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=_render_alert(alert)),
        ]
        hypothesis = structured_model.invoke(messages, config=config)
        return {"hypothesis": hypothesis}

    return triage_and_hypothesize


def build_graph_from_structured(
    structured_model: Runnable[Any, Any],
) -> CompiledStateGraph:
    """Compile the graph from a runnable that already emits a RootCauseHypothesis.

    Used directly by tests to inject a deterministic fake model.
    """
    builder = StateGraph(InvestigationState)
    builder.add_node("triage_and_hypothesize", _make_triage_node(structured_model))
    builder.add_edge(START, "triage_and_hypothesize")
    builder.add_edge("triage_and_hypothesize", END)
    return builder.compile()


def build_graph(model: BaseChatModel) -> CompiledStateGraph:
    """Compile the investigation graph, forcing structured output on the model."""
    structured_model = model.with_structured_output(RootCauseHypothesis)
    return build_graph_from_structured(structured_model)
