"""Langfuse tracing wiring (Langfuse Python SDK v3).

Tracing is optional and degrades gracefully: if keys are missing or Langfuse cannot be
initialised, the app runs normally without traces.
"""

from __future__ import annotations

import structlog
from langchain_core.callbacks import BaseCallbackHandler

from sentinel.config import Settings

logger = structlog.get_logger(__name__)

_configured = False


def configure_langfuse(settings: Settings) -> None:
    """Initialise the process-wide Langfuse client from settings (idempotent)."""
    global _configured
    if _configured or not settings.langfuse_enabled:
        return
    try:
        from langfuse import Langfuse

        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
        _configured = True
        logger.info("langfuse.configured", host=settings.langfuse_host)
    except Exception as exc:  # pragma: no cover - defensive, tracing is optional
        logger.warning("langfuse.configure_failed", error=str(exc))


def get_langfuse_handler(settings: Settings) -> BaseCallbackHandler | None:
    """Return a LangChain callback handler for Langfuse, or None if disabled."""
    if not settings.langfuse_enabled:
        return None
    try:
        from langfuse.langchain import CallbackHandler

        return CallbackHandler()
    except Exception as exc:  # pragma: no cover - defensive, tracing is optional
        logger.warning("langfuse.handler_failed", error=str(exc))
        return None
