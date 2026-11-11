"""Score one investigation against its label, and aggregate scores into metrics.

Scoring is exact: a case is correct when the predicted ``category`` of the top-ranked
cause is accepted by the case. No LLM judge is involved, so a run is cheap and two runs
of the same outputs always score the same.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from sentinel.contracts import IncidentCategory, RootCauseHypothesis
from sentinel.evals.dataset import EvalCase

TOP_K = 3

ErrorKind = Literal["rate_limited", "timeout", "other"]

_RATE_LIMITED = re.compile(r"\b429\b|RESOURCE_EXHAUSTED|quota", re.IGNORECASE)
_TIMEOUT = re.compile(r"\b504\b|DEADLINE_EXCEEDED|timed? ?out", re.IGNORECASE)


def classify_error(error: str) -> ErrorKind:
    """Bucket a failure so API quota or latency problems can't pass for model quality."""
    if _RATE_LIMITED.search(error):
        return "rate_limited"
    if _TIMEOUT.search(error):
        return "timeout"
    return "other"


@dataclass(frozen=True)
class InvestigationOutcome:
    """What the system under test produced for one alert."""

    hypothesis: RootCauseHypothesis
    retrieved_runbooks: list[str]


class CaseResult(BaseModel):
    """The scored result of one case."""

    case_id: str
    source: str
    expected_category: IncidentCategory
    acceptable_categories: list[IncidentCategory]
    predicted_categories: list[IncidentCategory]
    top1_correct: bool
    top3_correct: bool
    expected_runbook: str | None
    retrieved_runbooks: list[str]
    cited_runbooks: list[str]
    retrieval_hit: bool | None
    citation_hit: bool | None
    latency_seconds: float
    error: str | None = None
    error_kind: ErrorKind | None = None


def score_case(
    case: EvalCase,
    outcome: InvestigationOutcome | None,
    latency_seconds: float,
    error: str | None = None,
) -> CaseResult:
    """Score one case. A failed investigation (``outcome is None``) scores as wrong."""
    if outcome is None:
        predicted: list[IncidentCategory] = []
        retrieved: list[str] = []
        cited: list[str] = []
    else:
        predicted = [cause.category for cause in outcome.hypothesis.candidate_causes]
        retrieved = list(outcome.retrieved_runbooks)
        cited = list(outcome.hypothesis.runbook_references)

    expected_runbook = case.expected_runbook
    return CaseResult(
        case_id=case.case_id,
        source=case.source,
        expected_category=case.expected_category,
        acceptable_categories=case.acceptable_categories,
        predicted_categories=predicted,
        top1_correct=bool(predicted) and case.accepts(predicted[0]),
        top3_correct=any(case.accepts(category) for category in predicted[:TOP_K]),
        expected_runbook=expected_runbook,
        retrieved_runbooks=retrieved,
        cited_runbooks=cited,
        retrieval_hit=None if expected_runbook is None else expected_runbook in retrieved,
        citation_hit=None if expected_runbook is None else expected_runbook in cited,
        latency_seconds=round(latency_seconds, 3),
        error=error,
        error_kind=classify_error(error) if error else None,
    )


class GroupMetrics(BaseModel):
    """Accuracy for a subset of cases."""

    cases: int
    top1_accuracy: float


class Metrics(BaseModel):
    """Aggregate metrics for a run."""

    cases: int
    errors: int
    error_rate: float
    errors_by_kind: dict[str, int]
    top1_accuracy: float
    top3_accuracy: float
    retrieval_hit_rate: float | None
    citation_rate: float | None
    latency_p50_seconds: float
    latency_p95_seconds: float
    by_category: dict[str, GroupMetrics]
    by_source: dict[str, GroupMetrics]


def describe_errors(metrics: Metrics) -> str:
    """E.g. ``"18 rate_limited, 2 timeout"``; empty when there were no errors."""
    return ", ".join(f"{count} {kind}" for kind, count in metrics.errors_by_kind.items())


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def _optional_rate(flags: list[bool | None]) -> float | None:
    scored = [flag for flag in flags if flag is not None]
    return _rate(sum(scored), len(scored)) if scored else None


def _grouped(results: list[CaseResult], key: str) -> dict[str, GroupMetrics]:
    groups: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        groups[str(getattr(result, key))].append(result)
    return {
        name: GroupMetrics(
            cases=len(members),
            top1_accuracy=_rate(sum(r.top1_correct for r in members), len(members)),
        )
        for name, members in sorted(groups.items())
    }


def aggregate(results: list[CaseResult]) -> Metrics:
    """Roll case results up into run-level metrics."""
    total = len(results)
    errors = sum(result.error is not None for result in results)
    latencies = [r.latency_seconds for r in results if r.error is None]
    return Metrics(
        cases=total,
        errors=errors,
        error_rate=_rate(errors, total),
        errors_by_kind=dict(sorted(Counter(r.error_kind for r in results if r.error_kind).items())),
        top1_accuracy=_rate(sum(r.top1_correct for r in results), total),
        top3_accuracy=_rate(sum(r.top3_correct for r in results), total),
        retrieval_hit_rate=_optional_rate([r.retrieval_hit for r in results]),
        citation_rate=_optional_rate([r.citation_hit for r in results]),
        latency_p50_seconds=_percentile(latencies, 0.5),
        latency_p95_seconds=_percentile(latencies, 0.95),
        by_category=_grouped(results, "expected_category"),
        by_source=_grouped(results, "source"),
    )
