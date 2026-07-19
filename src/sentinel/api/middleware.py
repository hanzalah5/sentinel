"""Correlation-ID middleware.

Assigns every request a correlation ID (honouring an inbound ``X-Correlation-ID``
header if present), stores it on ``request.state`` and in the logging context, and
echoes it back on the response so callers can correlate their logs with ours.
"""

from __future__ import annotations

import re

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sentinel.logging import new_correlation_id, set_correlation_id

CORRELATION_ID_HEADER = "X-Correlation-ID"
# Accept only a bounded, safe token from clients; otherwise mint our own. This stops a
# client from injecting multi-KB or forged/colliding ids into our logs and traces.
_VALID_CORRELATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        cid = (
            incoming
            if incoming and _VALID_CORRELATION_ID.fullmatch(incoming)
            else new_correlation_id()
        )
        set_correlation_id(cid)
        request.state.correlation_id = cid
        structlog.contextvars.bind_contextvars(correlation_id=cid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[CORRELATION_ID_HEADER] = cid
        return response
