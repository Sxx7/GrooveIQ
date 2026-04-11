"""
GrooveIQ – Security utilities.

Authentication strategy:
- Bearer token (API key) passed in the Authorization header.
- Keys are stored as SHA-256 hashes in the config to avoid accidental
  exposure via env dumps. Comparison uses hmac.compare_digest to
  prevent timing attacks.
- Rate limiting via pluggable backend: Redis (shared across workers)
  when REDIS_URL is configured, otherwise in-process sliding-window
  counters.
- Optional per-user authorization: when API_KEY_USERS is configured,
  each API key is bound to specific user_id(s).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)

# ---------------------------------------------------------------------------
# Key hashing
# ---------------------------------------------------------------------------

def hash_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw API key."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


# Pre-compute at import time — settings are immutable after startup.
# A frozen tuple avoids re-hashing on every request and keeps the
# collection size constant (no timing side-channel on key count).
_HASHED_CONFIGURED_KEYS: tuple[str, ...] = tuple(
    hash_key(k) for k in settings.api_keys_list
)


# ---------------------------------------------------------------------------
# Rate limiter interface + implementations
# ---------------------------------------------------------------------------

class RateLimiter(ABC):
    """Abstract rate limiter interface."""

    @abstractmethod
    def is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        ...

    def is_batch_allowed(self, key: str, count: int, limit: int, window_seconds: int = 60) -> bool:
        """Check whether *count* new entries fit within the rate limit window.

        Default implementation calls is_allowed() in a loop — subclasses
        may override for atomicity.
        """
        for _ in range(count):
            if not self.is_allowed(key, limit, window_seconds):
                return False
        return True


class InMemoryLimiter(RateLimiter):
    """Sliding-window rate limiter using in-process deques.

    Suitable for single-process deployments.  Each worker process
    maintains its own independent counters — in multi-worker mode the
    effective limit is multiplied by the number of workers.
    """

    def __init__(self):
        self._windows: dict[str, deque] = defaultdict(deque)

    def is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    def is_batch_allowed(self, key: str, count: int, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        window = self._windows[key]
        cutoff = now - window_seconds
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) + count > limit:
            return False
        for _ in range(count):
            window.append(now)
        return True


class RedisLimiter(RateLimiter):
    """Sliding-window rate limiter backed by Redis.

    Uses a sorted set per key with scores = timestamp.  Atomic and
    shared across all workers/replicas.
    """

    def __init__(self, redis_url: str):
        self._fallback = InMemoryLimiter()
        try:
            import redis
            self._r = redis.from_url(redis_url, decode_responses=True)
            # Verify connectivity.
            self._r.ping()
            logger.info("Redis rate limiter connected: %s", redis_url.split("@")[-1])
        except Exception as e:
            logger.warning(
                "Redis rate limiter unavailable (%s), falling back to in-memory", e
            )
            self._r = None

    def is_allowed(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        if self._r is None:
            return self._fallback.is_allowed(key, limit, window_seconds)
        try:
            return self._check_redis(key, limit, window_seconds)
        except Exception:
            # Redis is down — degrade gracefully to in-memory limiter
            # (fail-closed: rate limits are still enforced, just per-process).
            logger.warning("Redis rate limiter error, falling back to in-memory")
            return self._fallback.is_allowed(key, limit, window_seconds)

    def _check_redis(self, key: str, limit: int, window_seconds: int) -> bool:
        import redis as _redis

        now = time.time()
        cutoff = now - window_seconds
        rkey = f"grooveiq:rl:{key}"

        pipe = self._r.pipeline(True)
        pipe.zremrangebyscore(rkey, "-inf", cutoff)
        pipe.zcard(rkey)
        pipe.zadd(rkey, {f"{now}": now})
        pipe.expire(rkey, window_seconds + 1)
        results = pipe.execute()

        current_count = results[1]
        if current_count >= limit:
            # Remove the entry we just added.
            self._r.zrem(rkey, f"{now}")
            return False
        return True


def _create_limiter() -> RateLimiter:
    """Create the appropriate rate limiter based on configuration."""
    if settings.REDIS_URL:
        return RedisLimiter(settings.REDIS_URL)
    return InMemoryLimiter()


_limiter: RateLimiter = _create_limiter()

# Secondary limiter keyed on user_id to prevent event-flood attacks
# targeting a single user through a shared API key.
_user_event_limiter: RateLimiter = _limiter  # shares the same backend

# Max events per user per minute (single + batch combined).
_USER_EVENT_LIMIT = 600


# ---------------------------------------------------------------------------
# Per-user authorization (optional)
# ---------------------------------------------------------------------------

def _parse_key_user_bindings() -> dict[str, set[str]]:
    """Parse API_KEY_USERS into a mapping of key hash -> allowed user_ids.

    Format: ``key1:user1,user2;key2:user3``
    When empty, all keys can access all users (legacy behaviour).
    """
    raw = settings.API_KEY_USERS
    if not raw:
        return {}
    bindings: dict[str, set[str]] = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if ":" not in entry:
            continue
        key_part, users_part = entry.split(":", 1)
        key_part = key_part.strip()
        if not key_part:
            continue
        key_hash = hash_key(key_part)
        user_ids = {u.strip() for u in users_part.split(",") if u.strip()}
        bindings[key_hash] = user_ids
    return bindings


_key_user_bindings: dict[str, set[str]] = {}


def _init_key_user_bindings() -> None:
    """Initialize key-user bindings (called once at module load)."""
    global _key_user_bindings
    _key_user_bindings = _parse_key_user_bindings()
    if _key_user_bindings:
        logger.info(
            "Per-user authorization enabled: %d key(s) with user bindings",
            len(_key_user_bindings),
        )


_init_key_user_bindings()


def check_user_access(api_key: str, user_id: str) -> None:
    """Verify that the given API key is allowed to access the given user.

    No-op when API_KEY_USERS is not configured (all keys can access all users).
    Raises HTTPException(403) when the key is bound to specific users and
    ``user_id`` is not among them.
    """
    if not _key_user_bindings:
        return  # no bindings configured — allow all
    key_hash = hash_key(api_key)
    allowed = _key_user_bindings.get(key_hash)
    if allowed is None:
        # Key not in bindings — it's an unrestricted key.
        return
    if user_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This API key does not have access to this user.",
        )


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
    # Only skip auth when explicitly opted in via DISABLE_AUTH=true
    if settings.DISABLE_AUTH and not settings.api_keys_list:
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
    configured_hashes = _HASHED_CONFIGURED_KEYS

    # Constant-time comparison against all configured keys (no short-circuit)
    authenticated = False
    for h in configured_hashes:
        authenticated |= hmac.compare_digest(candidate_hash, h)

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
# Per-user event rate limiting
# ---------------------------------------------------------------------------

def check_user_event_rate(user_id: str, count: int = 1) -> None:
    """Enforce per-user rate limit on event ingestion.

    Raises HTTPException(429) if the user exceeds the event rate limit.
    Called from event ingestion routes after authentication.

    *count* is the number of events being ingested in this request
    (default 1 for single-event ingestion, >1 for batches).
    """
    rkey = f"user_event:{user_id}"
    if count <= 1:
        allowed = _user_event_limiter.is_allowed(rkey, limit=_USER_EVENT_LIMIT)
    else:
        allowed = _user_event_limiter.is_batch_allowed(
            rkey, count=count, limit=_USER_EVENT_LIMIT,
        )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Event rate limit exceeded for user. Max {_USER_EVENT_LIMIT}/minute.",
            headers={"Retry-After": "60"},
        )


# ---------------------------------------------------------------------------
# Optional auth (for endpoints that work anonymously but can be keyed)
# ---------------------------------------------------------------------------

def _parse_admin_keys() -> set[str]:
    """Parse ADMIN_API_KEYS into a set of key hashes.

    When empty, *all* keys are treated as admins (backwards-compatible
    for single-user / small-deployment setups).
    """
    raw = getattr(settings, "ADMIN_API_KEYS", "")
    if not raw:
        return set()
    return {hash_key(k.strip()) for k in raw.split(",") if k.strip()}


_admin_key_hashes: set[str] = set()


def _init_admin_keys() -> None:
    global _admin_key_hashes
    _admin_key_hashes = _parse_admin_keys()
    if _admin_key_hashes:
        logger.info(
            "Admin key enforcement enabled: %d admin key(s) configured",
            len(_admin_key_hashes),
        )


_init_admin_keys()


def require_admin(api_key: str) -> None:
    """Verify that the given API key has admin privileges.

    No-op when ADMIN_API_KEYS is not configured (all keys are admins).
    Raises HTTPException(403) when the key is not in the admin set.
    """
    if not _admin_key_hashes:
        return  # no admin keys configured — all keys are admin
    if hash_key(api_key) not in _admin_key_hashes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This operation requires an admin API key.",
        )
