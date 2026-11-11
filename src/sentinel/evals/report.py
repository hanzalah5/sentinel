"""Render an eval report as Markdown (for the terminal and the GitHub job summary)."""

from __future__ import annotations

from sentinel.evals.runner import EvalReport


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def render_markdown(report: EvalReport, failures: list[str] | None = None) -> str:
    """A compact summary, per-category accuracy, and every missed case."""
    m = report.metrics
    lines = [
        f"## Sentinel eval: `{report.split}` split",
        "",
        f"Model `{report.model}` · retrieval {'on' if report.rag_enabled else 'off'} · "
        f"commit `{(report.git_sha or 'unknown')[:7]}` · {m.cases} cases in "
        f"{report.duration_seconds:.0f} s",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Top-1 category accuracy | {_pct(m.top1_accuracy)} |",
        f"| Top-3 category accuracy | {_pct(m.top3_accuracy)} |",
        f"| Runbook retrieval hit rate | {_pct(m.retrieval_hit_rate)} |",
        f"| Expected runbook cited | {_pct(m.citation_rate)} |",
        f"| Errors | {m.errors} ({_pct(m.error_rate)}) |",
        f"| Latency p50 / p95 | {m.latency_p50_seconds:.1f} s / {m.latency_p95_seconds:.1f} s |",
        "",
        "| Case source | Cases | Top-1 |",
        "|---|---|---|",
    ]
    lines += [
        f"| {name} | {group.cases} | {_pct(group.top1_accuracy)} |"
        for name, group in m.by_source.items()
    ]
    lines += ["", "| Expected category | Cases | Top-1 |", "|---|---|---|"]
    lines += [
        f"| `{name}` | {group.cases} | {_pct(group.top1_accuracy)} |"
        for name, group in m.by_category.items()
    ]

    missed = [r for r in report.results if not r.top1_correct]
    if missed:
        lines += ["", "### Missed cases", "", "| Case | Expected | Predicted (ranked) |"]
        lines += ["|---|---|---|"]
        for r in missed:
            error = (r.error or "").replace("|", "\\|").replace("\n", " ")
            predicted = ", ".join(f"`{c.value}`" for c in r.predicted_categories) or (
                f"error: {error}" if error else "(none)"
            )
            lines.append(f"| {r.case_id} | `{r.expected_category.value}` | {predicted} |")

    if failures is not None:
        lines += [""]
        if failures:
            lines += ["### Gate: FAILED", ""] + [f"- {failure}" for failure in failures]
        else:
            lines += ["### Gate: passed"]
    return "\n".join(lines) + "\n"
