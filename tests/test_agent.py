"""The investigation graph in isolation."""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableLambda

from sentinel.agent.graph import build_graph_from_structured
from sentinel.contracts import Alert, AlertSeverity, RootCauseHypothesis


def _alert() -> Alert:
    return Alert(
        alert_id="a1",
        title="High latency",
        description="p99 latency spiked.",
        severity=AlertSeverity.critical,
        service="checkout-api",
        fired_at=datetime.now(UTC),
    )


def test_graph_produces_structured_hypothesis() -> None:
    fixed = RootCauseHypothesis(
        summary="summary",
        candidate_causes=[],
        recommended_next_step="next step",
    )
    graph = build_graph_from_structured(RunnableLambda(lambda _messages: fixed))

    result = graph.invoke({"alert": _alert()})

    assert isinstance(result["hypothesis"], RootCauseHypothesis)
    assert result["hypothesis"].summary == "summary"
