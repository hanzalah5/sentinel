"""Runbook-grounding behaviour of the graph, with fake retrievers (no DB needed)."""

from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.runnables import RunnableLambda

from sentinel.agent.graph import (
    build_graph_from_structured,
    build_investigation_messages,
)
from sentinel.config import Settings
from sentinel.contracts import Alert, AlertSeverity, RootCauseHypothesis
from sentinel.rag.runbooks import NullRetriever, RunbookMatch
from sentinel.rag.store import (
    PgVectorRunbookRetriever,
    _vector_literal,
    build_retriever,
)

_HYPOTHESIS = RootCauseHypothesis(summary="s", candidate_causes=[], recommended_next_step="n")


def _alert() -> Alert:
    return Alert(
        alert_id="a1",
        title="High latency",
        description="p99 latency spiked",
        severity=AlertSeverity.critical,
        service="checkout-api",
        fired_at=datetime.now(UTC),
    )


class FakeRetriever:
    def __init__(self, matches: list[RunbookMatch]) -> None:
        self._matches = matches

    def retrieve(self, query: str, k: int) -> list[RunbookMatch]:
        return self._matches


class BoomRetriever:
    def retrieve(self, query: str, k: int) -> list[RunbookMatch]:
        raise RuntimeError("db down")


def test_hypothesis_runbook_references_defaults_to_empty() -> None:
    assert _HYPOTHESIS.runbook_references == []


def test_build_messages_includes_runbook_text() -> None:
    match = RunbookMatch(
        title="DB pool saturation",
        source="high-latency.md",
        score=0.9,
        content="raise the pool size",
    )
    human = build_investigation_messages(_alert(), [match])[-1].content
    assert isinstance(human, str)
    assert "DB pool saturation" in human
    assert "raise the pool size" in human


def test_build_messages_notes_absence_of_runbooks() -> None:
    human = build_investigation_messages(_alert(), [])[-1].content
    assert isinstance(human, str)
    assert "no runbooks" in human.lower()


def test_graph_threads_runbook_matches_into_state() -> None:
    matches = [RunbookMatch(title="DB pool", source="x.md", score=0.9, content="...")]
    graph = build_graph_from_structured(
        RunnableLambda(lambda _messages: _HYPOTHESIS), FakeRetriever(matches)
    )

    result = graph.invoke({"alert": _alert()})

    assert [m.title for m in result["runbook_matches"]] == ["DB pool"]
    assert isinstance(result["hypothesis"], RootCauseHypothesis)


def test_graph_degrades_when_retrieval_fails() -> None:
    graph = build_graph_from_structured(
        RunnableLambda(lambda _messages: _HYPOTHESIS), BoomRetriever()
    )

    result = graph.invoke({"alert": _alert()})

    assert result["runbook_matches"] == []  # retrieval error swallowed
    assert isinstance(result["hypothesis"], RootCauseHypothesis)  # still completes


def test_graph_runs_with_null_retriever() -> None:
    graph = build_graph_from_structured(
        RunnableLambda(lambda _messages: _HYPOTHESIS), NullRetriever()
    )

    result = graph.invoke({"alert": _alert()})

    assert result["runbook_matches"] == []


def test_vector_literal_format() -> None:
    # The exact literal shape is load-bearing: it is interpolated as %s::vector.
    assert _vector_literal([0.1, 0.2, 0.3]) == "[0.1,0.2,0.3]"
    assert _vector_literal([]) == "[]"
    assert _vector_literal([1]) == "[1.0]"


def test_build_retriever_disabled_returns_null() -> None:
    retriever = build_retriever(Settings(database_url=None, google_api_key=None))
    assert isinstance(retriever, NullRetriever)


def test_build_retriever_enabled_returns_pgvector() -> None:
    # Constructing the pgvector retriever must not touch the database.
    retriever = build_retriever(Settings(database_url="postgresql://x/y", google_api_key="k"))
    assert isinstance(retriever, PgVectorRunbookRetriever)
