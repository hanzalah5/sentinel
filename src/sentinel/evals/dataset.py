"""Labelled evaluation cases.

Two sources live under ``evals/datasets/``:

- ``generated.jsonl`` is built by ``python -m sentinel.evals build`` from the synthetic
  alert generator. Each generator template is one scenario with a known category and
  runbook, so these labels are correct by construction.
- ``handwritten.json`` holds harder, hand-labelled cases with misleading or mixed
  signals. It is a readable JSON array so labels are easy to review.

Every case belongs to one split: ``dev`` is used while changing prompts or retrieval,
``test`` is held out and only run for reporting.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, TypeAdapter

from sentinel.contracts import Alert, IncidentCategory
from sentinel.ingestion.generator import REGIONS, SCENARIOS, SERVICES

Split = Literal["dev", "test"]
SPLITS: tuple[Split, ...] = ("dev", "test")

GENERATED_FILE = "generated.jsonl"
HANDWRITTEN_FILE = "handwritten.json"

# Fixed seeds keep the generated set reproducible; different seeds keep the splits apart.
_SPLIT_SEEDS: dict[Split, int] = {"dev": 1001, "test": 2002}
CASES_PER_SCENARIO = 3
_EPOCH = datetime(2026, 7, 1, tzinfo=UTC)


class EvalCase(BaseModel):
    """One alert with the answer a good investigation should reach."""

    case_id: str
    split: Split
    source: Literal["generated", "handwritten"]
    alert: Alert
    expected_category: IncidentCategory
    acceptable_categories: list[IncidentCategory] = Field(
        default_factory=list,
        description="Other categories that also count as correct, for genuinely ambiguous cases.",
    )
    expected_runbook: str | None = Field(
        default=None,
        description="Title of the runbook retrieval should surface; None when none applies.",
    )
    notes: str = ""

    def accepts(self, category: IncidentCategory) -> bool:
        """True if ``category`` is a correct answer for this case."""
        return category == self.expected_category or category in self.acceptable_categories


def build_generated_cases(split: Split, per_scenario: int = CASES_PER_SCENARIO) -> list[EvalCase]:
    """Deterministically build labelled cases for ``split`` from the generator scenarios."""
    rng = random.Random(_SPLIT_SEEDS[split])
    cases: list[EvalCase] = []
    for scenario in SCENARIOS:
        for i in range(per_scenario):
            service = rng.choice(SERVICES)
            title, description, severity = scenario.template(rng, service)
            alert = Alert(
                alert_id=f"eval-{split}-{scenario.name}-{i + 1}",
                title=title,
                description=description,
                severity=severity,
                service=service,
                source="synthetic-generator",
                fired_at=_EPOCH + timedelta(minutes=len(cases)),
                labels={"region": rng.choice(REGIONS)},
            )
            cases.append(
                EvalCase(
                    case_id=alert.alert_id,
                    split=split,
                    source="generated",
                    alert=alert,
                    expected_category=scenario.category,
                    expected_runbook=scenario.runbook_title,
                )
            )
    return cases


def render_generated() -> str:
    """The exact contents ``generated.jsonl`` should have (one case per line)."""
    cases = [case for split in SPLITS for case in build_generated_cases(split)]
    return "".join(case.model_dump_json() + "\n" for case in cases)


def write_generated(dataset_dir: Path) -> Path:
    """(Re)build ``generated.jsonl`` under ``dataset_dir``."""
    path = dataset_dir / GENERATED_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_generated(), encoding="utf-8")
    return path


_CASE_LIST = TypeAdapter(list[EvalCase])


def read_cases(path: Path) -> list[EvalCase]:
    """Read cases from a ``.jsonl`` (one per line) or ``.json`` (array) file."""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        try:
            return _CASE_LIST.validate_python(json.loads(text))
        except ValueError as exc:
            raise ValueError(f"{path}: invalid eval cases: {exc}") from exc
    cases: list[EvalCase] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            cases.append(EvalCase.model_validate_json(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: invalid eval case: {exc}") from exc
    return cases


def load_split(dataset_dir: Path, split: Split) -> list[EvalCase]:
    """All cases for ``split``: generated first, then handwritten."""
    cases: list[EvalCase] = []
    for name in (GENERATED_FILE, HANDWRITTEN_FILE):
        cases.extend(case for case in read_cases(dataset_dir / name) if case.split == split)
    return cases
