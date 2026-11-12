"""The regression gate: compare a run's metrics with committed thresholds.

``evals/baseline.json`` holds one set of thresholds per split. Thresholds are set from a
measured run minus a small margin, so normal model variance passes but a real
regression fails the build.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sentinel.evals.runner import EvalReport
from sentinel.evals.scoring import describe_errors


class Thresholds(BaseModel):
    """Minimum acceptable metrics for one split."""

    min_top1_accuracy: float = Field(ge=0.0, le=1.0)
    min_top3_accuracy: float = Field(ge=0.0, le=1.0)
    min_retrieval_hit_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    max_error_rate: float = Field(default=0.0, ge=0.0, le=1.0)


class Baseline(BaseModel):
    """The committed baseline file."""

    note: str = ""
    splits: dict[str, Thresholds]


def load_baseline(path: Path) -> Baseline:
    return Baseline.model_validate_json(path.read_text(encoding="utf-8"))


def check(report: EvalReport, baseline: Baseline) -> list[str]:
    """Return one human-readable message per failed threshold (empty means pass)."""
    thresholds = baseline.splits.get(report.split)
    if thresholds is None:
        return [f"no thresholds for split {report.split!r} in the baseline"]

    m = report.metrics
    failures: list[str] = []
    if m.top1_accuracy < thresholds.min_top1_accuracy:
        failures.append(
            f"top-1 accuracy {m.top1_accuracy:.1%} is below {thresholds.min_top1_accuracy:.1%}"
        )
    if m.top3_accuracy < thresholds.min_top3_accuracy:
        failures.append(
            f"top-3 accuracy {m.top3_accuracy:.1%} is below {thresholds.min_top3_accuracy:.1%}"
        )
    if m.error_rate > thresholds.max_error_rate:
        failures.append(
            f"error rate {m.error_rate:.1%} ({describe_errors(m)}) is above "
            f"{thresholds.max_error_rate:.1%}"
        )
    if thresholds.min_retrieval_hit_rate is not None:
        if not report.rag_enabled:
            failures.append("retrieval is gated but was disabled for this run (no DATABASE_URL)")
        elif (
            m.retrieval_hit_rate is not None
            and m.retrieval_hit_rate < thresholds.min_retrieval_hit_rate
        ):
            failures.append(
                f"retrieval hit rate {m.retrieval_hit_rate:.1%} is below "
                f"{thresholds.min_retrieval_hit_rate:.1%}"
            )
    return failures
