"""Run labelled cases through the investigation graph and collect a scored report."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel

from sentinel.contracts import Alert
from sentinel.evals.dataset import EvalCase
from sentinel.evals.scoring import (
    CaseResult,
    InvestigationOutcome,
    Metrics,
    aggregate,
    score_case,
)

Investigate = Callable[[Alert], InvestigationOutcome]

# Long enough to keep the provider's error details (e.g. which quota was exceeded).
_MAX_ERROR_CHARS = 2000


class RequestPacer:
    """Spaces case starts evenly to stay under a provider's requests-per-minute limit.

    Thread-safe: concurrent workers queue on the same schedule. ``per_minute=None``
    disables pacing.
    """

    def __init__(
        self,
        per_minute: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if per_minute is not None and per_minute <= 0:
            raise ValueError("per_minute must be positive")
        self._interval = 60.0 / per_minute if per_minute else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait(self) -> None:
        """Block until the next start slot is free."""
        if not self._interval:
            return
        with self._lock:
            delay = self._next_start - self._clock()
            if delay > 0:
                self._sleep(delay)
            self._next_start = max(self._clock(), self._next_start) + self._interval


class EvalReport(BaseModel):
    """Everything needed to reproduce and compare a run."""

    split: str
    model: str
    rag_enabled: bool
    git_sha: str | None
    started_at: datetime
    duration_seconds: float
    metrics: Metrics
    results: list[CaseResult]


def graph_investigator(graph: CompiledStateGraph) -> Investigate:
    """Adapt the compiled investigation graph to the harness.

    Calls the graph directly (not the API service) so the retrieved runbooks are visible
    and retrieval can be scored separately from the model's answer.
    """

    def investigate(alert: Alert) -> InvestigationOutcome:
        state = graph.invoke({"alert": alert})
        hypothesis = state.get("hypothesis")
        if hypothesis is None:
            raise ValueError("the graph produced no hypothesis")
        matches = state.get("runbook_matches", [])
        return InvestigationOutcome(
            hypothesis=hypothesis,
            retrieved_runbooks=[match.title for match in matches],
        )

    return investigate


def run_cases(
    cases: list[EvalCase],
    investigate: Investigate,
    *,
    concurrency: int = 2,
    pacer: RequestPacer | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> list[CaseResult]:
    """Investigate every case (in parallel) and score it. Results keep the input order.

    A case that raises is recorded as an error and scored as wrong; it never aborts the
    run, so one flaky call cannot hide the rest of the results.
    """

    def run_one(case: EvalCase) -> CaseResult:
        if pacer is not None:
            pacer.wait()  # pacing time is not counted as latency
        start = clock()
        try:
            outcome = investigate(case.alert)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]
            return score_case(case, None, clock() - start, error=error)
        return score_case(case, outcome, clock() - start)

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return list(pool.map(run_one, cases))


def current_git_sha() -> str | None:
    """The commit under evaluation: ``GITHUB_SHA`` in CI, else the local HEAD."""
    sha = os.environ.get("GITHUB_SHA")
    if sha:
        return sha
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def evaluate(
    cases: list[EvalCase],
    investigate: Investigate,
    *,
    split: str,
    model: str,
    rag_enabled: bool,
    concurrency: int = 2,
    requests_per_minute: float | None = None,
) -> EvalReport:
    """Run, score and aggregate ``cases`` into a report."""
    started_at = datetime.now(UTC)
    start = time.perf_counter()
    results = run_cases(
        cases,
        investigate,
        concurrency=concurrency,
        pacer=RequestPacer(requests_per_minute),
    )
    return EvalReport(
        split=split,
        model=model,
        rag_enabled=rag_enabled,
        git_sha=current_git_sha(),
        started_at=started_at,
        duration_seconds=round(time.perf_counter() - start, 2),
        metrics=aggregate(results),
        results=results,
    )
