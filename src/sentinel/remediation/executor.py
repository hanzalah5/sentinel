"""Remediation execution.

The agent depends on the ``RemediationExecutor`` protocol, never on a concrete executor.
The only executor shipped here is a **simulated** one: it records the action and returns
success without touching any real infrastructure. Wiring in a real executor (kubectl,
cloud APIs, ...) is a single class behind this protocol — deliberately out of scope so
this project can never take a destructive action.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from sentinel.contracts import ExecutionOutcome, ExecutionResult, RemediationAction

logger = structlog.get_logger(__name__)


class RemediationExecutor(Protocol):
    """Anything that can carry out an approved remediation."""

    def execute(self, action: RemediationAction) -> ExecutionResult: ...


class SimulatedExecutor:
    """Dry-run executor: logs the action and reports success, touching nothing real."""

    def execute(self, action: RemediationAction) -> ExecutionResult:
        logger.info(
            "remediation.simulated",
            command=action.command,
            risk=action.risk.value,
        )
        return ExecutionResult(
            outcome=ExecutionOutcome.executed,
            detail=f"[simulated] {action.command}",
        )
