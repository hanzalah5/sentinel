"""The eval harness: dataset integrity, scoring, runner, gate, report and CLI.

Everything here is keyless. A live run against Gemini happens in the Evals workflow.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from langchain_core.runnables import RunnableLambda

from sentinel.agent.graph import build_graph_from_structured
from sentinel.config import Settings
from sentinel.contracts import (
    Alert,
    AlertSeverity,
    CandidateCause,
    IncidentCategory,
    RootCauseHypothesis,
)
from sentinel.evals import __main__ as cli
from sentinel.evals.dataset import (
    CASES_PER_SCENARIO,
    SPLITS,
    EvalCase,
    load_split,
    read_cases,
    render_generated,
)
from sentinel.evals.gate import Baseline, Thresholds, check, load_baseline
from sentinel.evals.report import render_markdown
from sentinel.evals.runner import (
    EvalReport,
    Investigate,
    RequestPacer,
    current_git_sha,
    evaluate,
    graph_investigator,
    run_cases,
)
from sentinel.evals.scoring import (
    InvestigationOutcome,
    aggregate,
    classify_error,
    describe_errors,
    score_case,
)
from sentinel.ingestion.generator import SCENARIOS, TEMPLATES
from sentinel.rag.ingest import _load_runbooks
from sentinel.rag.runbooks import RunbookMatch

ROOT = Path(__file__).resolve().parents[1]
DATASETS = ROOT / "evals" / "datasets"
RUNBOOK_TITLES = {title for title, _, _ in _load_runbooks(ROOT / "runbooks")}


# --------------------------------------------------------------------------- helpers


def _alert(alert_id: str = "a-1") -> Alert:
    return Alert(
        alert_id=alert_id,
        title="t",
        description="d",
        severity=AlertSeverity.warning,
        service="svc",
        fired_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _case(
    case_id: str = "c-1",
    expected: IncidentCategory = IncidentCategory.db_pool_saturation,
    acceptable: list[IncidentCategory] | None = None,
    runbook: str | None = "High p99 latency / database connection pool saturation",
    source: str = "generated",
) -> EvalCase:
    return EvalCase.model_validate(
        {
            "case_id": case_id,
            "split": "dev",
            "source": source,
            "alert": _alert(case_id).model_dump(),
            "expected_category": expected,
            "acceptable_categories": acceptable or [],
            "expected_runbook": runbook,
        }
    )


def _hypothesis(
    *categories: IncidentCategory, cited: list[str] | None = None
) -> RootCauseHypothesis:
    return RootCauseHypothesis(
        summary="s",
        candidate_causes=[
            CandidateCause(cause=f"cause {i}", category=c, confidence=0.5)
            for i, c in enumerate(categories)
        ],
        recommended_next_step="n",
        runbook_references=cited or [],
    )


def _outcome(
    *categories: IncidentCategory, retrieved: list[str] | None = None
) -> InvestigationOutcome:
    return InvestigationOutcome(
        hypothesis=_hypothesis(*categories), retrieved_runbooks=retrieved or []
    )


def _oracle(cases: list[EvalCase]) -> Investigate:
    """A perfect system: answers each case with its expected category and runbook."""
    by_id = {case.alert.alert_id: case for case in cases}

    def investigate(alert: Alert) -> InvestigationOutcome:
        case = by_id[alert.alert_id]
        runbooks = [case.expected_runbook] if case.expected_runbook else []
        return InvestigationOutcome(
            hypothesis=_hypothesis(case.expected_category, cited=runbooks),
            retrieved_runbooks=runbooks,
        )

    return investigate


def _report(**metric_overrides: Any) -> EvalReport:
    results = [score_case(_case(), _outcome(IncidentCategory.db_pool_saturation), 1.0)]
    metrics = aggregate(results).model_copy(update=metric_overrides)
    return EvalReport(
        split="dev",
        model="fake",
        rag_enabled=True,
        git_sha="abc1234",
        started_at=datetime(2026, 7, 1, tzinfo=UTC),
        duration_seconds=1.0,
        metrics=metrics,
        results=results,
    )


_BASELINE = Baseline(
    splits={
        "dev": Thresholds(
            min_top1_accuracy=0.8,
            min_top3_accuracy=0.9,
            min_retrieval_hit_rate=0.8,
            max_error_rate=0.05,
        )
    }
)


# --------------------------------------------------------------------------- dataset


def test_generated_dataset_is_up_to_date() -> None:
    committed = (DATASETS / "generated.jsonl").read_text(encoding="utf-8")
    assert committed == render_generated(), "run: python -m sentinel.evals build"


def test_every_template_is_a_labelled_scenario() -> None:
    assert [s.template for s in SCENARIOS] == TEMPLATES
    assert {s.runbook_title for s in SCENARIOS} <= RUNBOOK_TITLES


@pytest.mark.parametrize("split", SPLITS)
def test_split_covers_every_scenario(split: str) -> None:
    generated = [c for c in load_split(DATASETS, split) if c.source == "generated"]  # type: ignore[arg-type]
    counts = Counter(c.expected_category for c in generated)
    assert counts == {s.category: CASES_PER_SCENARIO for s in SCENARIOS}


def test_dataset_labels_are_consistent() -> None:
    cases = [c for split in SPLITS for c in load_split(DATASETS, split)]
    ids = [c.case_id for c in cases]
    assert len(ids) == len(set(ids)), "case ids must be unique across splits"
    for case in cases:
        assert case.alert.alert_id == case.case_id
        assert case.expected_category not in case.acceptable_categories
        if case.expected_runbook is not None:
            assert case.expected_runbook in RUNBOOK_TITLES, case.case_id
        if case.source == "handwritten":
            assert case.notes, f"{case.case_id}: explain the label in notes"


def test_read_cases_reports_the_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(_case().model_dump_json() + "\n\n{not json}\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cases\.jsonl:3"):
        read_cases(path)


def test_read_cases_rejects_an_invalid_json_array(tmp_path: Path) -> None:
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{"case_id": "x"}]), encoding="utf-8")
    with pytest.raises(ValueError, match="invalid eval cases"):
        read_cases(path)


def test_accepts_expected_and_acceptable_categories() -> None:
    case = _case(acceptable=[IncidentCategory.bad_deploy])
    assert case.accepts(IncidentCategory.db_pool_saturation)
    assert case.accepts(IncidentCategory.bad_deploy)
    assert not case.accepts(IncidentCategory.other)


# --------------------------------------------------------------------------- scoring


def test_score_top1_top3_and_runbooks() -> None:
    runbook = "High p99 latency / database connection pool saturation"
    outcome = InvestigationOutcome(
        hypothesis=_hypothesis(
            IncidentCategory.bad_deploy,
            IncidentCategory.db_pool_saturation,
            cited=[runbook],
        ),
        retrieved_runbooks=[runbook, "Disk space nearly full"],
    )
    result = score_case(_case(), outcome, 1.23456)
    assert not result.top1_correct
    assert result.top3_correct
    assert result.retrieval_hit and result.citation_hit
    assert result.latency_seconds == 1.235


def test_score_counts_an_acceptable_category_as_correct() -> None:
    result = score_case(
        _case(acceptable=[IncidentCategory.bad_deploy]), _outcome(IncidentCategory.bad_deploy), 0.1
    )
    assert result.top1_correct


def test_score_only_considers_the_top_three_causes() -> None:
    outcome = _outcome(
        IncidentCategory.other,
        IncidentCategory.other,
        IncidentCategory.other,
        IncidentCategory.db_pool_saturation,
    )
    assert not score_case(_case(), outcome, 0.1).top3_correct


def test_score_failed_investigation_is_wrong() -> None:
    result = score_case(_case(), None, 0.5, error="boom")
    assert not result.top1_correct and not result.top3_correct
    assert result.retrieval_hit is False
    assert result.error == "boom"


def test_score_skips_runbooks_when_none_is_expected() -> None:
    result = score_case(_case(runbook=None), _outcome(IncidentCategory.db_pool_saturation), 0.1)
    assert result.retrieval_hit is None and result.citation_hit is None


def test_aggregate_metrics() -> None:
    runbook = "High p99 latency / database connection pool saturation"
    results = [
        score_case(
            _case("a"), _outcome(IncidentCategory.db_pool_saturation, retrieved=[runbook]), 1.0
        ),
        score_case(_case("b"), _outcome(IncidentCategory.other), 3.0),
        score_case(
            _case("c", IncidentCategory.disk_pressure, runbook=None, source="handwritten"),
            _outcome(IncidentCategory.disk_pressure),
            2.0,
        ),
        score_case(_case("d"), None, 9.0, error="timeout"),
    ]
    m = aggregate(results)
    assert (m.cases, m.errors, m.error_rate) == (4, 1, 0.25)
    assert m.top1_accuracy == 0.5
    assert m.retrieval_hit_rate == round(1 / 3, 4)  # case c expects no runbook
    assert m.latency_p50_seconds == 2.0  # errors are excluded from latency
    assert m.latency_p95_seconds == 3.0
    assert m.by_source["handwritten"].top1_accuracy == 1.0
    assert m.by_category["db_pool_saturation"].cases == 3


def test_aggregate_of_nothing() -> None:
    m = aggregate([])
    assert (m.cases, m.top1_accuracy, m.latency_p50_seconds) == (0, 0.0, 0.0)
    assert m.retrieval_hit_rate is None


# --------------------------------------------------------------------------- runner


def test_run_cases_with_a_perfect_system_scores_everything_correct() -> None:
    cases = load_split(DATASETS, "dev")
    results = run_cases(cases, _oracle(cases), concurrency=4)
    assert [r.case_id for r in results] == [c.case_id for c in cases]  # order preserved
    m = aggregate(results)
    assert (m.top1_accuracy, m.top3_accuracy, m.errors) == (1.0, 1.0, 0)
    assert m.retrieval_hit_rate == 1.0


def test_run_cases_records_failures_without_aborting() -> None:
    def explode(alert: Alert) -> InvestigationOutcome:
        raise RuntimeError("x" * 5000)

    results = run_cases([_case("a"), _case("b")], explode)
    assert all(r.error and r.error.startswith("RuntimeError: x") for r in results)
    assert all(len(r.error or "") == 2000 for r in results)


def test_graph_investigator_exposes_retrieved_runbooks() -> None:
    match = RunbookMatch(
        title="Disk space nearly full", source="disk-space.md", score=0.9, content="c"
    )

    class OneRunbook:
        def retrieve(self, query: str, k: int) -> list[RunbookMatch]:
            return [match]

    hypothesis = _hypothesis(IncidentCategory.disk_pressure)
    graph = build_graph_from_structured(RunnableLambda(lambda _m: hypothesis), OneRunbook())
    outcome = graph_investigator(graph)(_alert())
    assert outcome.retrieved_runbooks == ["Disk space nearly full"]
    assert outcome.hypothesis.candidate_causes[0].category is IncidentCategory.disk_pressure


def test_evaluate_builds_a_reproducible_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_SHA", "deadbeef")
    cases = [_case("a"), _case("b")]
    report = evaluate(cases, _oracle(cases), split="dev", model="fake", rag_enabled=True)
    assert report.git_sha == "deadbeef"
    assert report.metrics.top1_accuracy == 1.0
    assert len(report.results) == 2


def test_current_git_sha_without_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise OSError("git not installed")

    monkeypatch.setattr("subprocess.run", no_git)
    assert current_git_sha() is None


# --------------------------------------------------------------------------- gate


def test_gate_passes_a_good_run() -> None:
    assert (
        check(_report(top1_accuracy=0.9, top3_accuracy=0.95, retrieval_hit_rate=0.9), _BASELINE)
        == []
    )


def test_gate_reports_every_failed_threshold() -> None:
    failures = check(
        _report(top1_accuracy=0.5, top3_accuracy=0.6, error_rate=0.2, retrieval_hit_rate=0.1),
        _BASELINE,
    )
    assert len(failures) == 4
    assert failures[0].startswith("top-1 accuracy 50.0% is below 80.0%")


def test_gate_fails_when_retrieval_was_disabled() -> None:
    report = _report().model_copy(update={"rag_enabled": False})
    assert any("retrieval is gated" in f for f in check(report, _BASELINE))


def test_gate_fails_for_an_unknown_split() -> None:
    report = _report().model_copy(update={"split": "test"})
    assert check(report, _BASELINE) == ["no thresholds for split 'test' in the baseline"]


def test_load_baseline(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(_BASELINE.model_dump_json(), encoding="utf-8")
    assert load_baseline(path) == _BASELINE


# --------------------------------------------------------------------------- report


def test_report_lists_metrics_misses_and_gate_result() -> None:
    results = [
        score_case(_case("ok"), _outcome(IncidentCategory.db_pool_saturation), 1.0),
        score_case(_case("miss"), _outcome(IncidentCategory.other), 1.0),
        score_case(_case("err"), None, 1.0, error="Bad | thing\nhappened"),
    ]
    report = _report().model_copy(update={"results": results, "metrics": aggregate(results)})
    md = render_markdown(report, ["top-1 accuracy is low"])
    assert "| Top-1 category accuracy | 33.3% |" in md
    assert "| miss | `db_pool_saturation` | `other` |" in md
    assert "error: Bad \\| thing happened" in md
    assert "### Gate: FAILED" in md
    assert "### Gate: passed" in render_markdown(_report(), [])
    assert "Gate" not in render_markdown(_report())


# --------------------------------------------------------------------------- CLI


def _ready_settings() -> Settings:
    return Settings(google_api_key="test-key", json_logs=False, database_url=None)


def test_cli_build_writes_the_generated_dataset(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["build", "--dataset-dir", str(tmp_path)]) == 0
    assert (tmp_path / "generated.jsonl").read_text(encoding="utf-8") == render_generated()
    assert "wrote" in capsys.readouterr().out


def test_cli_run_writes_results_summary_and_passes_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = load_split(DATASETS, "dev")
    monkeypatch.setattr(cli, "get_settings", _ready_settings)
    monkeypatch.setattr(cli, "build_live_investigator", lambda settings: _oracle(cases))
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        Baseline(
            splits={"dev": Thresholds(min_top1_accuracy=0.9, min_top3_accuracy=0.9)}
        ).model_dump_json(),
        encoding="utf-8",
    )

    code = cli.main(
        [
            "run",
            "--split",
            "dev",
            "--dataset-dir",
            str(DATASETS),
            "--out",
            str(tmp_path / "out"),
            "--gate",
            str(baseline),
            "--limit",
            "5",
        ]
    )

    assert code == 0
    (result_file,) = (tmp_path / "out").glob("dev-*.json")
    saved = EvalReport.model_validate_json(result_file.read_text(encoding="utf-8"))
    assert saved.metrics.cases == 5
    assert "### Gate: passed" in summary.read_text(encoding="utf-8")


def test_cli_run_fails_when_the_gate_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def always_other(alert: Alert) -> InvestigationOutcome:
        return _outcome(IncidentCategory.other)

    monkeypatch.setattr(cli, "get_settings", _ready_settings)
    monkeypatch.setattr(cli, "build_live_investigator", lambda settings: always_other)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(_BASELINE.model_dump_json(), encoding="utf-8")

    code = cli.main(
        ["run", "--dataset-dir", str(DATASETS), "--out", str(tmp_path), "--gate", str(baseline)]
    )
    assert code == 1


def test_cli_run_needs_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(google_api_key=None, json_logs=False))
    assert cli.main(["run"]) == 2


def test_cli_run_without_a_baseline_still_fails_when_most_cases_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression: a run where every model call 404'd must not pass just because no
    baseline exists yet."""

    def model_gone(alert: Alert) -> InvestigationOutcome:
        raise RuntimeError("404 NOT_FOUND: model is no longer available")

    monkeypatch.setattr(cli, "get_settings", _ready_settings)
    monkeypatch.setattr(cli, "build_live_investigator", lambda settings: model_gone)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    code = cli.main(["run", "--dataset-dir", str(DATASETS), "--out", str(tmp_path), "--limit", "3"])

    assert code == 1
    assert "error rate 100.0% (3 other) is above the 10.0% limit" in capsys.readouterr().out


# --------------------------------------------------------------------------- free-tier support

_QUOTA_429 = (
    "ChatGoogleGenerativeAIError: Error calling model 'gemini-3.8-flash' (RESOURCE_EXHAUSTED): "
    "429 RESOURCE_EXHAUSTED. You exceeded your current quota"
)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (_QUOTA_429, "rate_limited"),
        ("504 DEADLINE_EXCEEDED. Deadline expired before operation could complete.", "timeout"),
        ("ReadTimeout: timed out", "timeout"),
        ("404 NOT_FOUND: model is no longer available", "other"),
    ],
)
def test_classify_error(error: str, kind: str) -> None:
    assert classify_error(error) == kind


def test_errors_are_counted_by_kind() -> None:
    results = [
        score_case(_case("a"), None, 1.0, error=_QUOTA_429),
        score_case(_case("b"), None, 1.0, error=_QUOTA_429),
        score_case(_case("c"), None, 1.0, error="504 DEADLINE_EXCEEDED"),
        score_case(_case("d"), _outcome(IncidentCategory.db_pool_saturation), 1.0),
    ]
    m = aggregate(results)
    assert results[0].error_kind == "rate_limited" and results[3].error_kind is None
    assert m.errors_by_kind == {"rate_limited": 2, "timeout": 1}
    assert describe_errors(m) == "2 rate_limited, 1 timeout"


def test_report_flags_rate_limiting_and_keeps_errors_short() -> None:
    results = [score_case(_case("a"), None, 1.0, error=_QUOTA_429 + " x" * 500)]
    report = _report().model_copy(update={"results": results, "metrics": aggregate(results)})
    md = render_markdown(report)
    assert "1 case(s) were rate-limited by the API" in md
    assert "| Errors | 1 (100.0%): 1 rate_limited |" in md
    assert "x " * 100 not in md  # only a preview in the table; the JSON keeps the full text


class _FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def test_pacer_spaces_starts_evenly() -> None:
    t = _FakeTime()
    pacer = RequestPacer(30, clock=t.clock, sleep=t.sleep)  # one start every 2 s
    for _ in range(3):
        pacer.wait()
    assert t.sleeps == [2.0, 2.0]  # the first start is immediate


def test_pacer_does_not_wait_when_work_is_slower_than_the_pace() -> None:
    t = _FakeTime()
    pacer = RequestPacer(60, clock=t.clock, sleep=t.sleep)
    pacer.wait()
    t.now += 5  # the case itself took longer than the 1 s interval
    pacer.wait()
    assert t.sleeps == []


def test_pacer_disabled_and_validated() -> None:
    t = _FakeTime()
    pacer = RequestPacer(None, clock=t.clock, sleep=t.sleep)
    for _ in range(5):
        pacer.wait()
    assert t.sleeps == []
    with pytest.raises(ValueError):
        RequestPacer(0)


def test_run_cases_waits_on_the_pacer_before_each_case() -> None:
    waits: list[int] = []

    class CountingPacer(RequestPacer):
        def wait(self) -> None:
            waits.append(1)

    cases = [_case("a"), _case("b"), _case("c")]
    run_cases(cases, _oracle(cases), concurrency=1, pacer=CountingPacer(None))
    assert len(waits) == 3


def test_cli_run_accepts_a_pace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cases = load_split(DATASETS, "dev")
    monkeypatch.setattr(cli, "get_settings", _ready_settings)
    monkeypatch.setattr(cli, "build_live_investigator", lambda settings: _oracle(cases))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    argv = ["run", "--dataset-dir", str(DATASETS), "--out", str(tmp_path), "--limit", "2"]
    assert cli.main([*argv, "--concurrency", "1", "--rpm", "6000"]) == 0
