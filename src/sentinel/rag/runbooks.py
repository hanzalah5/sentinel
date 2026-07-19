"""Runbook retrieval contracts.

The agent depends on the ``RunbookRetriever`` protocol, not on Postgres. That keeps the
graph testable (inject a fake) and lets retrieval degrade to a no-op when RAG is disabled.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class RunbookMatch(BaseModel):
    """A runbook retrieved as relevant to an alert."""

    title: str = Field(description="Human-readable runbook title.")
    source: str = Field(description="Origin of the runbook, e.g. the filename.")
    score: float = Field(description="Similarity score in [0, 1]; higher is closer.")
    content: str = Field(description="The runbook text.")


class RunbookRetriever(Protocol):
    """Anything that can return runbooks relevant to a query."""

    def retrieve(self, query: str, k: int) -> list[RunbookMatch]: ...


class NullRetriever:
    """Used when RAG is disabled: always returns no matches."""

    def retrieve(self, query: str, k: int) -> list[RunbookMatch]:
        return []
