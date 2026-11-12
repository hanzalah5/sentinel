"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status

from sentinel.agent.graph import build_graph
from sentinel.agent.incident_graph import build_incident_graph
from sentinel.api.auth import ApiKeyAuthenticator, CurrentPrincipal, require_principal
from sentinel.api.middleware import CorrelationIdMiddleware
from sentinel.checkpointer import build_checkpointer
from sentinel.config import Settings, get_settings
from sentinel.contracts import (
    Alert,
    DecisionRequest,
    HealthStatus,
    IncidentView,
    InvestigationResult,
)
from sentinel.incident import IncidentService
from sentinel.llm import get_chat_model
from sentinel.logging import configure_logging
from sentinel.observability import configure_langfuse, get_langfuse_handler
from sentinel.rag.store import build_retriever
from sentinel.remediation.executor import SimulatedExecutor
from sentinel.service import InvestigationService

logger = structlog.get_logger(__name__)


def build_service(settings: Settings) -> InvestigationService:
    """Assemble the real (Gemini-backed) investigation service."""
    model = get_chat_model(settings)
    retriever = build_retriever(settings)
    graph = build_graph(model, retriever, top_k=settings.runbook_top_k)
    handler = get_langfuse_handler(settings)
    return InvestigationService(graph=graph, settings=settings, langfuse_handler=handler)


def build_incident_service(settings: Settings) -> IncidentService:
    """Assemble the incident service: incident graph + durable checkpointer + executor."""
    model = get_chat_model(settings)
    graph = build_incident_graph(
        model,
        build_retriever(settings),
        SimulatedExecutor(),
        build_checkpointer(settings),
        top_k=settings.runbook_top_k,
        auto_approve_max_risk=settings.hitl_auto_approve_max_risk,
    )
    handler = get_langfuse_handler(settings)
    return IncidentService(graph=graph, settings=settings, langfuse_handler=handler)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.json_logs)
    configure_langfuse(settings)

    # If a service was injected (e.g. in tests) keep it; otherwise build the real one
    # when the LLM is configured. A missing key leaves the service unavailable rather
    # than crashing the process, so /healthz stays green for orchestrators.
    if app.state.service is None and settings.llm_configured:
        try:
            app.state.service = build_service(settings)
            logger.info("service.ready", model=settings.gemini_model)
        except Exception as exc:  # pragma: no cover - defensive startup guard
            logger.error("service.init_failed", error=str(exc))
    if app.state.service is None:
        reason = (
            "GOOGLE_API_KEY not configured"
            if not settings.llm_configured
            else "service initialization failed"
        )
        logger.warning("service.unavailable", reason=reason)

    if app.state.incident_service is None and settings.llm_configured:
        try:
            app.state.incident_service = build_incident_service(settings)
            logger.info("incident_service.ready")
        except Exception as exc:  # pragma: no cover - defensive startup guard
            logger.error("incident_service.init_failed", error=str(exc))

    yield


def get_service(request: Request) -> InvestigationService:
    service: InvestigationService | None = request.app.state.service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation service unavailable; set GOOGLE_API_KEY.",
        )
    return service


def get_incident_service(request: Request) -> IncidentService:
    service: IncidentService | None = request.app.state.incident_service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Incident service unavailable; set GOOGLE_API_KEY.",
        )
    return service


def create_app(
    settings: Settings | None = None,
    service: InvestigationService | None = None,
    incident_service: IncidentService | None = None,
) -> FastAPI:
    """Create the FastAPI app. Inject services to bypass real-LLM startup (tests)."""
    settings = settings or get_settings()

    app = FastAPI(
        title="Sentinel",
        version="0.1.0",
        summary="Agentic incident-response and root-cause copilot.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.incident_service = incident_service
    app.state.authenticator = ApiKeyAuthenticator(settings.api_keys())
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/healthz", response_model=HealthStatus, tags=["ops"])
    async def healthz() -> HealthStatus:
        """Liveness: the process is up. Always 200 while the server is running."""
        return HealthStatus(status="ok", detail="alive")

    @app.get("/readyz", response_model=HealthStatus, tags=["ops"])
    async def readyz(request: Request) -> HealthStatus:
        """Readiness: the agent is wired, callers can authenticate, investigations work."""
        if not request.app.state.authenticator.configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="not ready: SENTINEL_API_KEYS not configured",
            )
        if request.app.state.service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="not ready: investigation service unavailable",
            )
        return HealthStatus(status="ok", detail="ready")

    @app.post(
        "/investigate",
        response_model=InvestigationResult,
        tags=["agent"],
        dependencies=[Depends(require_principal)],
    )
    def investigate(
        alert: Alert,
        request: Request,
        service: Annotated[InvestigationService, Depends(get_service)],
    ) -> InvestigationResult:
        """Investigate an alert and return a structured root-cause hypothesis."""
        correlation_id = getattr(request.state, "correlation_id", None)
        return service.investigate(alert, correlation_id=correlation_id)

    @app.post(
        "/incidents",
        response_model=IncidentView,
        tags=["incident"],
        dependencies=[Depends(require_principal)],
    )
    def create_incident(
        alert: Alert,
        service: Annotated[IncidentService, Depends(get_incident_service)],
    ) -> IncidentView:
        """Open an incident: investigate, plan a remediation, and pause for approval."""
        return service.start(alert)

    @app.post(
        "/incidents/{thread_id}/decision",
        response_model=IncidentView,
        tags=["incident"],
    )
    def decide_incident(
        principal: CurrentPrincipal,
        thread_id: str,
        body: DecisionRequest,
        service: Annotated[IncidentService, Depends(get_incident_service)],
    ) -> IncidentView:
        """Approve or reject a paused incident's proposed remediation.

        The caller's identity and note are stored on the incident as its audit trail.
        """
        view = service.decide(thread_id, body.decision, decided_by=principal.name, note=body.note)
        if view is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown incident")
        return view

    @app.get(
        "/incidents/{thread_id}",
        response_model=IncidentView,
        tags=["incident"],
        dependencies=[Depends(require_principal)],
    )
    def get_incident(
        thread_id: str,
        service: Annotated[IncidentService, Depends(get_incident_service)],
    ) -> IncidentView:
        """Report the current durable state of an incident."""
        view = service.get(thread_id)
        if view is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown incident")
        return view

    return app
