"""Correlation-ID middleware.

Assigns every request a correlation ID (honouring an inbound ``X-Correlation-ID``
header if present), stores it on ``request.state`` and in the logging context, and
echoes it back on the response so callers can correlate their logs with ours.
"""

from __future__ import annotations

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from sentinel.logging import new_correlation_id, set_correlation_id

CORRELATION_ID_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(CORRELATION_ID_HEADER)
        cid = incoming or new_correlation_id()
        set_correlation_id(cid)
        request.state.correlation_id = cid
        structlog.contextvars.bind_contextvars(correlation_id=cid)
        try:
            response = await call_next(request)
        finally:
            structlog.contextvars.clear_contextvars()
        response.headers[CORRELATION_ID_HEADER] = cid
        return response
