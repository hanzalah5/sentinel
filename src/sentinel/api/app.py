"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status

from sentinel.agent.graph import build_graph
from sentinel.api.middleware import CorrelationIdMiddleware
from sentinel.config import Settings, get_settings
from sentinel.contracts import Alert, HealthStatus, InvestigationResult
from sentinel.llm import get_chat_model
from sentinel.logging import configure_logging
from sentinel.observability import configure_langfuse, get_langfuse_handler
from sentinel.rag.store import build_retriever
from sentinel.service import InvestigationService

logger = structlog.get_logger(__name__)


def build_service(settings: Settings) -> InvestigationService:
    """Assemble the real (Gemini-backed) investigation service."""
    model = get_chat_model(settings)
    retriever = build_retriever(settings)
    graph = build_graph(model, retriever, top_k=settings.runbook_top_k)
    handler = get_langfuse_handler(settings)
    return InvestigationService(graph=graph, settings=settings, langfuse_handler=handler)


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

    yield


def get_service(request: Request) -> InvestigationService:
    service: InvestigationService | None = request.app.state.service
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Investigation service unavailable; set GOOGLE_API_KEY.",
        )
    return service


def create_app(
    settings: Settings | None = None,
    service: InvestigationService | None = None,
) -> FastAPI:
    """Create the FastAPI app. Inject ``service`` to bypass real-LLM startup (tests)."""
    settings = settings or get_settings()

    app = FastAPI(
        title="Sentinel",
        version="0.1.0",
        summary="Agentic incident-response and root-cause copilot.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/healthz", response_model=HealthStatus, tags=["ops"])
    async def healthz() -> HealthStatus:
        """Liveness: the process is up. Always 200 while the server is running."""
        return HealthStatus(status="ok", detail="alive")

    @app.get("/readyz", response_model=HealthStatus, tags=["ops"])
    async def readyz(request: Request) -> HealthStatus:
        """Readiness: the agent is wired and can serve investigations."""
        if request.app.state.service is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="not ready: investigation service unavailable",
            )
        return HealthStatus(status="ok", detail="ready")

    @app.post("/investigate", response_model=InvestigationResult, tags=["agent"])
    def investigate(
        alert: Alert,
        request: Request,
        service: Annotated[InvestigationService, Depends(get_service)],
    ) -> InvestigationResult:
        """Investigate an alert and return a structured root-cause hypothesis."""
        correlation_id = getattr(request.state, "correlation_id", None)
        return service.investigate(alert, correlation_id=correlation_id)

    return app
