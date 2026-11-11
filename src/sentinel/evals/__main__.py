"""Eval CLI.

Usage:
    python -m sentinel.evals build                       # rebuild generated.jsonl
    python -m sentinel.evals run --split dev             # live run (needs GOOGLE_API_KEY)
    python -m sentinel.evals run --split dev --gate evals/baseline.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sentinel.config import Settings, get_settings
from sentinel.evals.dataset import SPLITS, Split, load_split, write_generated
from sentinel.evals.gate import check, load_baseline
from sentinel.evals.report import render_markdown
from sentinel.evals.runner import Investigate, evaluate, graph_investigator
from sentinel.evals.scoring import describe_errors
from sentinel.logging import configure_logging

DEFAULT_DATASET_DIR = Path("evals/datasets")


def build_live_investigator(settings: Settings) -> Investigate:
    """The real system under test: Gemini plus pgvector retrieval when configured."""
    from sentinel.agent.graph import build_graph
    from sentinel.llm import get_chat_model
    from sentinel.rag.store import build_retriever

    graph = build_graph(
        get_chat_model(settings), build_retriever(settings), top_k=settings.runbook_top_k
    )
    return graph_investigator(graph)


def _cmd_build(args: argparse.Namespace) -> int:
    path = write_generated(args.dataset_dir)
    print(f"wrote {path}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.json_logs)
    if not settings.llm_configured:
        print("GOOGLE_API_KEY is not configured; a live eval needs it.", file=sys.stderr)
        return 2

    split: Split = args.split
    cases = load_split(args.dataset_dir, split)
    if args.limit:
        cases = cases[: args.limit]

    report = evaluate(
        cases,
        build_live_investigator(settings),
        split=split,
        model=settings.gemini_model,
        rag_enabled=settings.rag_enabled,
        concurrency=args.concurrency,
        requests_per_minute=args.rpm or None,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.strftime("%Y%m%dT%H%M%SZ")
    result_path = args.out / f"{split}-{stamp}.json"
    result_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")

    if args.gate:
        failures = check(report, load_baseline(args.gate))
    else:
        # No baseline yet: still refuse a run that mostly errored. Its scores say
        # nothing about the agent, and a "passing" job would hide that.
        failures = []
        if report.metrics.error_rate > args.max_error_rate:
            failures.append(
                f"error rate {report.metrics.error_rate:.1%} "
                f"({describe_errors(report.metrics)}) is above the "
                f"{args.max_error_rate:.1%} limit; scores from this run are not meaningful"
            )
    markdown = render_markdown(report, failures)
    print(markdown)
    print(f"Full results: {result_path}")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(markdown)
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sentinel.evals", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="rebuild generated.jsonl from the alert generator")
    build.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    build.set_defaults(handler=_cmd_build)

    run = sub.add_parser("run", help="run a split against the live model and score it")
    run.add_argument("--split", choices=SPLITS, default="dev")
    run.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATASET_DIR)
    run.add_argument("--out", type=Path, default=Path("eval-results"))
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument(
        "--rpm",
        type=float,
        default=0,
        help="pace case starts to this many per minute (0 = no pacing); for rate-limited keys",
    )
    run.add_argument("--limit", type=int, default=0, help="only run the first N cases")
    run.add_argument("--gate", type=Path, default=None, help="baseline file to gate against")
    run.add_argument(
        "--max-error-rate",
        type=float,
        default=0.1,
        help="fail the run above this error rate when no baseline is given (default 0.1)",
    )
    run.set_defaults(handler=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code: int = args.handler(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
