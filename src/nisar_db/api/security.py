"""API keys, scopes and rate limits.

A key arrives as an ``X-API-Key`` header, an ``Authorization: Bearer`` header,
or the ``nisar_db_key`` cookie that ``/api/v1/session`` sets, so a browser can
open a private viewer once and keep using it. Local mode asks for none.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, Request

from nisar_db.api.settings import ApiKey, Settings, hash_key

COOKIE = "nisar_db_key"


@dataclass(frozen=True)
class Caller:
    """Who is calling: a key's name, or ``local`` / ``anonymous``."""

    name: str
    key: ApiKey | None = None

    def allows(self, scope: str) -> bool:
        """Return whether the caller may use ``scope``."""
        return self.key is None or self.key.allows(scope)


def presented_key(request: Request) -> str | None:
    """Return the key a request carries, from header, bearer token or cookie."""
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key or request.cookies.get(COOKIE)


def match_key(settings: Settings, key: str | None) -> ApiKey | None:
    """Return the configured key ``key`` hashes to, compared in constant time."""
    if not key:
        return None
    digest = hash_key(key)
    for k in settings.keys:
        if hmac.compare_digest(k.sha256, digest):
            return k
    return None


def caller_for(request: Request, scope: str) -> Caller:
    """Identify the caller and check it may use ``scope``.

    Raises
    ------
    HTTPException
        401 without a valid key where one is needed, 403 for a key that lacks
        the scope.

    """
    settings: Settings = request.app.state.settings
    if not settings.shared:
        return Caller("local")
    key = match_key(settings, presented_key(request))
    if scope == "read" and not settings.private_read:
        return Caller(key.name if key else "anonymous", key)
    if key is None:
        raise HTTPException(
            401, "an API key is needed (X-API-Key header or Bearer token)"
        )
    if not key.allows(scope):
        raise HTTPException(403, f"the key {key.name!r} lacks the {scope!r} scope")
    return Caller(key.name, key)


def require(scope: str):
    """Return a FastAPI dependency that resolves to the checked ``Caller``."""

    def dependency(request: Request) -> Caller:
        return caller_for(request, scope)

    return dependency


class RateLimiter:
    """Sliding one-minute window of requests per client."""

    def __init__(self, per_minute: int) -> None:
        """Allow ``per_minute`` requests per client (0: no limit)."""
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, client: str) -> None:
        """Count one request for ``client``.

        Raises
        ------
        HTTPException
            429 once the client is over its limit, with ``Retry-After``.

        """
        if self.per_minute <= 0:
            return
        now = time.monotonic()
        with self._lock:
            hits = self._hits[client]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                retry = int(60 - (now - hits[0])) + 1
                raise HTTPException(
                    429,
                    "rate limit reached; try again shortly",
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)


def limited(scope: str):
    """Return a dependency that checks ``scope`` and then the rate limit."""

    def dependency(request: Request) -> Caller:
        caller = caller_for(request, scope)
        client = (
            caller.name
            if caller.key
            else (request.client.host if request.client else "?")
        )
        request.app.state.limiter.check(client)
        return caller

    return dependency


#: Dependencies the routes share (a call in a default would run per route).
READ = require("read")
JOBS = require("jobs")
LIMITED_READ = limited("read")
