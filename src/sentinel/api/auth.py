"""Bearer API-key authentication.

Keys come from ``SENTINEL_API_KEYS`` (``name:key`` pairs). Each request must send
``Authorization: Bearer <key>``; the matching key's *name* becomes the caller's identity,
recorded on approval decisions. Keys are held only as SHA-256 digests in memory and
compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, identified by the name of their API key."""

    name: str


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class ApiKeyAuthenticator:
    """Maps a presented bearer token to a ``Principal``, or rejects it."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._digests = [(name, _digest(key)) for name, key in keys.items()]

    @property
    def configured(self) -> bool:
        return bool(self._digests)

    def authenticate(self, token: str) -> Principal | None:
        presented = _digest(token)
        match: str | None = None
        # Compare against every key (no early exit) so timing doesn't reveal which,
        # if any, matched.
        for name, digest in self._digests:
            if hmac.compare_digest(presented, digest) and match is None:
                match = name
        return Principal(match) if match is not None else None


_bearer = HTTPBearer(
    auto_error=False,
    description="An API key from SENTINEL_API_KEYS, sent as `Authorization: Bearer <key>`.",
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def require_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    """FastAPI dependency: the authenticated caller, or 401/503."""
    authenticator: ApiKeyAuthenticator = request.app.state.authenticator
    if not authenticator.configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication is not configured; set SENTINEL_API_KEYS",
        )
    if credentials is None:
        raise _unauthorized("missing bearer token")
    principal = authenticator.authenticate(credentials.credentials)
    if principal is None:
        raise _unauthorized("invalid API key")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
