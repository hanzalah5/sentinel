"""The incident graph: investigate, plan a remediation, gate on human approval, execute.

Shape:  retrieve_runbooks -> triage_and_hypothesize -> plan_remediation
        -> request_approval --approved--> execute_remediation -> END
                            \\--rejected--------------------------> END

``request_approval`` calls LangGraph's ``interrupt()`` for actions that need sign-off;
compiled with a checkpointer, the paused state is durable and resumes on decision.
The investigation graph and ``/investigate`` are untouched — this is additive.
"""

from __future__ import annotations

from typing import Any, TypedDict

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from sentinel.agent.graph import (
    _make_retrieve_node,
    _make_triage_node,
    _render_alert,
    _render_runbooks,
)
from sentinel.contracts import (
    ActionRisk,
    Alert,
    ApprovalDecision,
    ExecutionResult,
    RemediationAction,
    RootCauseHypothesis,
)
from sentinel.rag.runbooks import NullRetriever, RunbookMatch, RunbookRetriever
from sentinel.remediation.executor import RemediationExecutor, SimulatedExecutor

logger = structlog.get_logger(__name__)


class IncidentState(TypedDict):
    """State threaded through the incident graph."""

    alert: Alert
    runbook_matches: list[RunbookMatch]
    hypothesis: RootCauseHypothesis | None
    remediation: RemediationAction | None
    decision: ApprovalDecision | None
    execution: ExecutionResult | None


_RISK_RANK = {ActionRisk.low: 0, ActionRisk.medium: 1, ActionRisk.high: 2}

_PLAN_SYSTEM_PROMPT = """\
You are a senior Site Reliability Engineer proposing a remediation for a production \
incident. Given the alert, the retrieved runbooks, and the root-cause analysis, propose \
exactly ONE concrete remediation action.

Rules:
- Prefer the safest action that credibly addresses the most likely cause; favour \
reversible mitigations (e.g. rollback, scale, restart) over irreversible ones.
- Provide the exact command or action to run.
- Classify the action's risk as low, medium, or high (destructive or wide-blast-radius \
actions are high).
- Give a short rationale tying the action to the evidence. Propose only one action."""


def _requires_approval(risk: ActionRisk, auto_approve_max_risk: str) -> bool:
    """A human must approve any action riskier than the auto-approve ceiling.

    An unknown ceiling (e.g. "none") means nothing auto-approves — every action
    requires human sign-off, the safest default for a tool that can touch infra.
    """
    try:
        ceiling = _RISK_RANK[ActionRisk(auto_approve_max_risk)]
    except ValueError:
        return True
    return _RISK_RANK[risk] > ceiling


def _build_plan_messages(
    alert: Alert, matches: list[RunbookMatch], hypothesis: RootCauseHypothesis
) -> list[BaseMessage]:
    causes = "\n".join(f"- [{c.confidence:.2f}] {c.cause}" for c in hypothesis.candidate_causes)
    analysis = f"Most likely explanation: {hypothesis.summary}\nRanked causes:\n{causes}"
    return [
        SystemMessage(content=_PLAN_SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"{_render_alert(alert)}\n\n{_render_runbooks(matches)}\n\n"
                f"Root-cause analysis:\n{analysis}"
            )
        ),
    ]


def _make_plan_node(structured_model: Runnable[Any, Any], auto_approve_max_risk: str) -> Any:
    def plan_remediation(
        state: IncidentState, config: RunnableConfig
    ) -> dict[str, RemediationAction]:
        hypothesis = state["hypothesis"]
        if hypothesis is None:
            raise ValueError("cannot plan a remediation without a hypothesis")
        action = structured_model.invoke(
            _build_plan_messages(state["alert"], state.get("runbook_matches", []), hypothesis),
            config=config,
        )
        if action is None:
            raise ValueError("remediation planner returned no action")
        # Approval requirement is policy, not the model's call to make.
        action = action.model_copy(
            update={"requires_approval": _requires_approval(action.risk, auto_approve_max_risk)}
        )
        return {"remediation": action}

    return plan_remediation


def _coerce_decision(resumed: Any) -> ApprovalDecision:
    """Coerce a resume payload into a decision, failing closed.

    Anything that is not an explicit, valid approval becomes ``rejected``: a
    remediation must never execute without a clear yes.
    """
    if isinstance(resumed, ApprovalDecision):
        return resumed
    raw = resumed.get("decision") if isinstance(resumed, dict) else resumed
    if isinstance(raw, ApprovalDecision):
        return raw
    if isinstance(raw, str):
        try:
            return ApprovalDecision(raw)
        except ValueError:
            pass
    logger.warning("approval.malformed_resume", resumed=repr(resumed))
    return ApprovalDecision.rejected


def request_approval(state: IncidentState, config: RunnableConfig) -> dict[str, ApprovalDecision]:
    action = state["remediation"]
    if action is None:
        raise ValueError("no remediation to approve")
    if not action.requires_approval:
        logger.info("approval.auto", command=action.command, risk=action.risk.value)
        return {"decision": ApprovalDecision.approved}
    hypothesis = state["hypothesis"]
    resumed = interrupt(
        {
            "kind": "approval_request",
            "action": action.model_dump(mode="json"),
            "hypothesis_summary": hypothesis.summary if hypothesis else None,
        }
    )
    decision = _coerce_decision(resumed)
    logger.info("approval.decided", decision=decision.value, command=action.command)
    return {"decision": decision}


def _make_execute_node(executor: RemediationExecutor) -> Any:
    def execute_remediation(
        state: IncidentState, config: RunnableConfig
    ) -> dict[str, ExecutionResult]:
        action = state["remediation"]
        if action is None:
            raise ValueError("no remediation to execute")
        result = executor.execute(action)
        logger.info("remediation.execution", outcome=result.outcome.value)
        return {"execution": result}

    return execute_remediation


def _route_after_approval(state: IncidentState) -> str:
    return "execute_remediation" if state["decision"] == ApprovalDecision.approved else END


def build_incident_graph_from_parts(
    hypothesis_model: Runnable[Any, Any],
    remediation_model: Runnable[Any, Any],
    retriever: RunbookRetriever | None = None,
    executor: RemediationExecutor | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    *,
    top_k: int = 3,
    auto_approve_max_risk: str = "none",
) -> CompiledStateGraph:
    """Compile the incident graph from injected parts (used directly by tests)."""
    retriever = retriever or NullRetriever()
    executor = executor or SimulatedExecutor()

    builder = StateGraph(IncidentState)
    builder.add_node("retrieve_runbooks", _make_retrieve_node(retriever, top_k))
    builder.add_node("triage_and_hypothesize", _make_triage_node(hypothesis_model))
    builder.add_node("plan_remediation", _make_plan_node(remediation_model, auto_approve_max_risk))
    builder.add_node("request_approval", request_approval)
    builder.add_node("execute_remediation", _make_execute_node(executor))

    builder.add_edge(START, "retrieve_runbooks")
    builder.add_edge("retrieve_runbooks", "triage_and_hypothesize")
    builder.add_edge("triage_and_hypothesize", "plan_remediation")
    builder.add_edge("plan_remediation", "request_approval")
    builder.add_conditional_edges(
        "request_approval",
        _route_after_approval,
        {"execute_remediation": "execute_remediation", END: END},
    )
    builder.add_edge("execute_remediation", END)
    return builder.compile(checkpointer=checkpointer)


def build_incident_graph(
    model: BaseChatModel,
    retriever: RunbookRetriever,
    executor: RemediationExecutor,
    checkpointer: BaseCheckpointSaver[Any],
    *,
    top_k: int = 3,
    auto_approve_max_risk: str = "none",
) -> CompiledStateGraph:
    """Compile the incident graph, forcing structured output on the model per node."""
    return build_incident_graph_from_parts(
        model.with_structured_output(RootCauseHypothesis),
        model.with_structured_output(RemediationAction),
        retriever,
        executor,
        checkpointer,
        top_k=top_k,
        auto_approve_max_risk=auto_approve_max_risk,
    )
