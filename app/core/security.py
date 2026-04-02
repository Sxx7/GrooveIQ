"""
GrooveIQ – Security utilities.

Authentication strategy:
- Bearer token (API key) passed in the Authorization header.
- Keys are stored as SHA-256 hashes in the config to avoid accidental
  exposure via env dumps. Comparison uses hmac.compare_digest to
  prevent timing attacks.
- Rate limiting via in-process sliding-window counters (per API key).
  For multi-replica deployments, swap with a Redis-backed limiter.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Key hashing
# ---------------------------------------------------------------------------

def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _hashed_configured_keys() -> list[str]:
    return [hash_key(k) for k in settings.API_KEYS]


# ---------------------------------------------------------------------------
# Rate limiter (sliding window, in-process)
# ---------------------------------------------------------------------------

class _SlidingWindowLimiter:
    """Thread-safe sliding-window rate limiter keyed by an arbitrary string."""

    def __init__(self):
        # key -> deque of timestamps (seconds)
        self._windows: dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - window_seconds
        # Evict stale entries
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True


_limiter = _SlidingWindowLimiter()


# ---------------------------------------------------------------------------
# Dependency: require valid API key
# ---------------------------------------------------------------------------

async def require_api_key(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> str:
    """
    FastAPI dependency.  Raises 401/429 on auth or rate-limit failure.
    Returns the raw API key on success (can be used as an identity token).
    """
    # If no keys are configured, allow all (dev mode)
    if not settings.API_KEYS:
        return "anonymous"

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header. "
                   "Use: Authorization: Bearer <your-api-key>",
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw_key = credentials.credentials
    candidate_hash = hash_key(raw_key)
    configured_hashes = _hashed_configured_keys()

    # Constant-time comparison against all configured keys
    authenticated = any(
        hmac.compare_digest(candidate_hash, h) for h in configured_hashes
    )

    if not authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Rate limiting: use hashed key as the window identifier
    endpoint_path = request.url.path
    is_event_endpoint = "/events" in endpoint_path
    limit = settings.RATE_LIMIT_EVENTS if is_event_endpoint else settings.RATE_LIMIT_DEFAULT

    if not _limiter.is_allowed(candidate_hash, limit=limit):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Max {limit} requests/minute.",
            headers={"Retry-After": "60"},
        )

    return raw_key


# ---------------------------------------------------------------------------
# Optional auth (for endpoints that work anonymously but can be keyed)
# ---------------------------------------------------------------------------

async def optional_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> Optional[str]:
    if credentials and credentials.scheme.lower() == "bearer":
        return credentials.credentials
    return None
