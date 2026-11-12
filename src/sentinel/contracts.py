"""Typed domain contracts.

These Pydantic models are the backbone of the system: the ``Alert`` is the validated
input, and ``RootCauseHypothesis`` is the schema the LLM is *forced* to fill via
structured output, so we never parse hopeful free-text JSON.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AlertSeverity(StrEnum):
    """Severity of an incoming alert."""

    info = "info"
    warning = "warning"
    critical = "critical"


class Alert(BaseModel):
    """A production alert to be investigated."""

    alert_id: str = Field(description="Unique identifier of the alert.")
    title: str = Field(description="Short human-readable alert title.")
    description: str = Field(
        description="Detailed alert text, including any metrics or log excerpts."
    )
    severity: AlertSeverity = Field(default=AlertSeverity.warning, description="Alert severity.")
    service: str | None = Field(default=None, description="Name of the affected service, if known.")
    source: str = Field(
        default="unknown",
        description="Monitoring system that produced the alert, e.g. prometheus.",
    )
    fired_at: datetime = Field(description="Timestamp when the alert fired (UTC).")
    labels: dict[str, str] = Field(
        default_factory=dict, description="Key/value labels attached to the alert."
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "alert_id": "alert-1024",
                    "title": "High p99 latency on checkout-api",
                    "description": (
                        "p99 latency rose from 180ms to 2.4s over 5 minutes. "
                        "Error rate steady. DB connection pool saturation warnings "
                        "in checkout-api logs."
                    ),
                    "severity": "critical",
                    "service": "checkout-api",
                    "source": "prometheus",
                    "fired_at": "2026-07-12T09:31:00Z",
                    "labels": {"region": "us-east-1", "deployment": "v3.14.2"},
                }
            ]
        }
    }


class IncidentCategory(StrEnum):
    """Coarse root-cause mechanism.

    The free-text ``cause`` stays the explanation; the category makes answers
    comparable, so root-cause accuracy can be scored exactly (see ``sentinel.evals``).
    """

    db_pool_saturation = "db_pool_saturation"
    memory_leak_oom = "memory_leak_oom"
    bad_deploy = "bad_deploy"
    disk_pressure = "disk_pressure"
    startup_crash = "startup_crash"
    cert_expiry = "cert_expiry"
    dependency_failure = "dependency_failure"
    other = "other"


class CandidateCause(BaseModel):
    """A single ranked, evidence-backed candidate root cause."""

    cause: str = Field(description="A specific, plausible root cause.")
    category: IncidentCategory = Field(
        description=(
            "The mechanism behind this cause. Choose the most specific one: "
            "db_pool_saturation (database connection pool exhausted or connections held "
            "too long); memory_leak_oom (memory growth, memory pressure or OOM kills); "
            "disk_pressure (a disk or volume filling up); startup_crash (the process "
            "fails or is killed while starting); cert_expiry (a TLS certificate expired, "
            "expiring or not yet rotated everywhere); dependency_failure (an upstream or "
            "downstream service failing or slow); bad_deploy (a regression shipped in a "
            "recent deploy or config change that no category above describes better); "
            "other (none of these)."
        )
    )
    confidence: float = Field(ge=0.0, le=1.0, description="Calibrated confidence between 0 and 1.")
    evidence: list[str] = Field(
        default_factory=list,
        description="Concrete signals from the alert that support this cause.",
    )


class RootCauseHypothesis(BaseModel):
    """The agent's structured root-cause analysis for an alert."""

    summary: str = Field(description="One-paragraph summary of the most likely explanation.")
    candidate_causes: list[CandidateCause] = Field(
        description="Ranked list of plausible causes, most likely first."
    )
    recommended_next_step: str = Field(
        description="A single safe next diagnostic or mitigation action."
    )
    runbook_references: list[str] = Field(
        default_factory=list,
        description="Titles of the runbooks the analysis relied on, if any.",
    )


class InvestigationResult(BaseModel):
    """The full, traceable result returned to the caller."""

    alert_id: str
    correlation_id: str
    model: str
    hypothesis: RootCauseHypothesis
    created_at: datetime


class HealthStatus(BaseModel):
    """Response body for the liveness/readiness endpoints."""

    status: str
    detail: str | None = None


class ActionRisk(StrEnum):
    """How risky/destructive a proposed remediation is."""

    low = "low"
    medium = "medium"
    high = "high"


class RemediationAction(BaseModel):
    """A single concrete remediation the agent proposes for an incident."""

    summary: str = Field(description="One-line description of the proposed remediation.")
    command: str = Field(
        description=(
            "The concrete action or command to run, e.g. "
            "'kubectl rollout undo deployment/checkout-api'."
        )
    )
    risk: ActionRisk = Field(description="How risky/destructive the action is.")
    rationale: str = Field(description="Why this action addresses the most likely root cause.")
    requires_approval: bool = Field(
        default=True,
        description="Whether a human must approve before this action executes.",
    )


class ApprovalDecision(StrEnum):
    """A human's decision on a proposed remediation."""

    approved = "approved"
    rejected = "rejected"


class ExecutionOutcome(StrEnum):
    """What happened to a remediation once the decision was made."""

    executed = "executed"
    skipped = "skipped"
    failed = "failed"


class ExecutionResult(BaseModel):
    """The result of (not) executing a remediation."""

    outcome: ExecutionOutcome
    detail: str


class IncidentStatus(StrEnum):
    """Lifecycle status of an incident."""

    awaiting_approval = "awaiting_approval"
    executed = "executed"
    rejected = "rejected"
    failed = "failed"


class IncidentView(BaseModel):
    """The externally visible state of an incident (API response)."""

    thread_id: str
    status: IncidentStatus
    alert_id: str
    hypothesis: RootCauseHypothesis | None = None
    remediation: RemediationAction | None = None
    decision: ApprovalDecision | None = None
    decided_by: str | None = Field(
        default=None,
        description="Who made the decision: an API key name, or 'policy:auto-approve'.",
    )
    decision_note: str | None = None
    execution: ExecutionResult | None = None


class DecisionRequest(BaseModel):
    """Request body for approving or rejecting a proposed remediation."""

    decision: ApprovalDecision
    note: str | None = None
