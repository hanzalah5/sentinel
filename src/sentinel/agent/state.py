"""Typed state for the investigation graph."""

from __future__ import annotations

from typing import TypedDict

from sentinel.contracts import Alert, RootCauseHypothesis
from sentinel.rag.runbooks import RunbookMatch


class InvestigationState(TypedDict):
    """State threaded through the graph.

    ``alert`` is the input; ``retrieve_runbooks`` fills ``runbook_matches``; and
    ``triage_and_hypothesize`` produces the ``hypothesis``.
    """

    alert: Alert
    runbook_matches: list[RunbookMatch]
    hypothesis: RootCauseHypothesis | None
