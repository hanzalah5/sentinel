"""Typed state for the investigation graph."""

from __future__ import annotations

from typing import TypedDict

from sentinel.contracts import Alert, RootCauseHypothesis


class InvestigationState(TypedDict):
    """State threaded through the graph.

    ``alert`` is the input; ``hypothesis`` is produced by the triage node.
    """

    alert: Alert
    hypothesis: RootCauseHypothesis | None
