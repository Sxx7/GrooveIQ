"""
GrooveIQ – Stale-while-revalidate (SWR) cache for recommendation "mixes".

Recommendation-mode requests (the discovery dial — see ``app/services/modes.py``)
are expensive to generate: candidate retrieval, ranking, reranking, feature
engineering.  A multi-shelf home view fires several ``?mode=...`` requests at
once and re-requests on every dial nudge, so the same ``(user, dial, context)``
mix is regenerated far more often than its inputs actually change.

This module is a small, generic in-memory cache with stale-while-revalidate
semantics:

* **fresh** (age ≤ ``fresh``) — served as-is, no work.
* **stale** (``fresh`` < age ≤ ``fresh + stale``) — the stale payload is served
  *immediately* and exactly **one** bounded background rebuild is scheduled per
  key; subsequent stale hits ride the in-flight rebuild rather than spawning
  their own.
* **miss** (no entry, or older than the stale grace) — the caller builds.

It is deliberately domain-agnostic: it stores opaque Python payloads keyed by an
opaque string and never imports the recommend pipeline.  The recommend route
composes the key (:func:`build_key`) from the request's ``model_version`` and
``config_version`` so a model retrain or a config change naturally lands on a
*different* key — old entries are simply never served again and age out.

Resource bounds (security checklist item 6):

* concurrent (background and :func:`get_or_build`) regenerations are capped by a
  semaphore sized from ``MIX_CACHE_MAX_CONCURRENT_REBUILDS``;
* at most one background rebuild runs per key at a time (the ``_inflight`` set);
* the store is capped at ``MIX_CACHE_MAX_ENTRIES`` (oldest-built evicted first).

All shared state is guarded by a single lock; background rebuilds swallow and
log their own exceptions so a transient failure never bubbles out of a request
or crashes the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# A zero-arg async callable that (re)builds a payload.  It must be
# self-contained — for background rebuilds it runs detached from any request,
# so it owns its own DB session / resources.
Builder = Callable[[], Awaitable[Any]]


@dataclass
class _Entry:
    payload: Any
    built_at: float  # time.monotonic() at build time


# ---------------------------------------------------------------------------
# Module singleton state (lock-guarded)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_store: dict[str, _Entry] = {}
_inflight: set[str] = set()  # keys with a background rebuild scheduled/running
_counters: dict[str, int] = {"hits": 0, "stale_serves": 0, "misses": 0, "rebuilds": 0, "evictions": 0}

# Semaphore is created lazily so it binds to the running loop, and recreated if
# the configured cap changes (e.g. a test monkeypatches settings).
_sem: asyncio.Semaphore | None = None
_sem_capacity: int = 0


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_capacity
    cap = max(1, int(settings.MIX_CACHE_MAX_CONCURRENT_REBUILDS))
    if _sem is None or _sem_capacity != cap:
        _sem = asyncio.Semaphore(cap)
        _sem_capacity = cap
    return _sem


# ---------------------------------------------------------------------------
# Key composition
# ---------------------------------------------------------------------------


def build_key(
    *,
    user_id: str,
    dial_bucket: str,
    context_bucket: str,
    limit: int,
    model_version: str,
    config_version: int,
) -> str:
    """Compose the canonical cache key for a recommendation mix.

    ``model_version`` and ``config_version`` are part of the key so a retrain or
    a config save lands on a fresh key — an old mix is never served against new
    model/config state.  ``limit`` is included because candidate over-fetch depth
    scales with it, so a 5-track and a 25-track request are genuinely different
    builds (and keeping it out would let a small build under-serve a large one).
    """
    parts = (
        "v1",
        str(user_id),
        str(dial_bucket),
        str(context_bucket),
        f"l{int(limit)}",
        f"m{model_version}",
        f"c{int(config_version)}",
    )
    return "|".join(parts)


# ---------------------------------------------------------------------------
# Core read / write
# ---------------------------------------------------------------------------


def _ttls(fresh_seconds: float | None, stale_seconds: float | None) -> tuple[float, float]:
    fresh = settings.MIX_CACHE_FRESH_SECONDS if fresh_seconds is None else fresh_seconds
    stale = settings.MIX_CACHE_STALE_SECONDS if stale_seconds is None else stale_seconds
    return float(fresh), float(stale)


def peek(
    key: str,
    *,
    fresh_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> tuple[Any | None, str]:
    """Classify the cached entry for ``key`` without building anything.

    Returns ``(payload, state)`` where ``state`` is ``"fresh"``, ``"stale"`` or
    ``"miss"``.  ``payload`` is ``None`` only for a miss.  A fully expired entry
    (older than the stale grace) is dropped and reported as a miss.

    Updates hit/stale/miss counters as a side effect (so callers using the
    lower-level read path still feed the stats).
    """
    fresh, stale = _ttls(fresh_seconds, stale_seconds)
    now = time.monotonic()
    with _lock:
        entry = _store.get(key)
        if entry is None:
            _counters["misses"] += 1
            return None, "miss"
        age = now - entry.built_at
        if age <= fresh:
            _counters["hits"] += 1
            return entry.payload, "fresh"
        if age <= fresh + stale:
            _counters["stale_serves"] += 1
            return entry.payload, "stale"
        # Fully expired — evict and treat as a miss.
        _counters["misses"] += 1
        del _store[key]
        return None, "miss"


def put(key: str, payload: Any, *, built_at: float | None = None) -> None:
    """Store ``payload`` under ``key``, stamping it built-now (or ``built_at``).

    ``built_at`` (a ``time.monotonic()`` value) is for tests that need to age an
    entry deterministically.  Enforces the entry cap by evicting the
    oldest-built entries.
    """
    stamp = time.monotonic() if built_at is None else built_at
    with _lock:
        _store[key] = _Entry(payload=payload, built_at=stamp)
        _evict_locked()


def _evict_locked() -> None:
    """Drop oldest-built entries until within ``MIX_CACHE_MAX_ENTRIES``. Caller holds the lock."""
    cap = max(1, int(settings.MIX_CACHE_MAX_ENTRIES))
    overflow = len(_store) - cap
    if overflow <= 0:
        return
    # Sort by build time ascending; the oldest `overflow` are evicted.
    oldest = sorted(_store.items(), key=lambda kv: kv[1].built_at)[:overflow]
    for k, _ in oldest:
        del _store[k]
    _counters["evictions"] += overflow


# ---------------------------------------------------------------------------
# Background rebuild (single-flight per key, semaphore-bounded)
# ---------------------------------------------------------------------------


def schedule_rebuild(key: str, builder: Builder) -> bool:
    """Schedule at most one background rebuild for ``key``.

    Returns ``True`` if this call started a rebuild, ``False`` if one was already
    in flight for the key or no event loop is running (nothing to schedule onto).
    The rebuild runs detached: it acquires the concurrency semaphore, awaits
    ``builder``, swaps the entry in, and clears the in-flight marker — swallowing
    and logging any exception so a failed rebuild keeps serving the stale entry.
    """
    with _lock:
        if key in _inflight:
            return False
        _inflight.add(key)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync context) — can't schedule background work.
        with _lock:
            _inflight.discard(key)
        return False
    loop.create_task(_rebuild(key, builder))
    return True


async def _rebuild(key: str, builder: Builder) -> None:
    try:
        async with _semaphore():
            payload = await builder()
        put(key, payload)
        with _lock:
            _counters["rebuilds"] += 1
    except Exception:  # pragma: no cover - defensive; rebuild failures are non-fatal
        logger.warning("mix_cache: background rebuild failed for a key", exc_info=True)
    finally:
        with _lock:
            _inflight.discard(key)


# ---------------------------------------------------------------------------
# Convenience: get-or-build (used by prewarm; the SWR primitive under test)
# ---------------------------------------------------------------------------


async def get_or_build(
    key: str,
    builder: Builder,
    *,
    fresh_seconds: float | None = None,
    stale_seconds: float | None = None,
) -> Any:
    """Return the cached payload for ``key``, building it if necessary.

    * fresh hit -> return immediately (``builder`` is **not** called);
    * stale hit -> return the stale payload immediately and schedule a single
      background rebuild (``builder`` runs once, in the background);
    * miss -> ``await builder()`` under the concurrency semaphore, store, return.

    ``builder`` must be self-contained (own its resources/session) because it may
    run detached in the background.
    """
    payload, state = peek(key, fresh_seconds=fresh_seconds, stale_seconds=stale_seconds)
    if state == "fresh":
        return payload
    if state == "stale":
        schedule_rebuild(key, builder)
        return payload
    # Miss: build in the foreground, bounded by the same concurrency cap.
    async with _semaphore():
        built = await builder()
    put(key, built)
    return built


# ---------------------------------------------------------------------------
# Lifecycle / observability
# ---------------------------------------------------------------------------


def clear() -> None:
    """Drop all entries and reset the in-flight / semaphore state.

    Called on config changes (immediate memory reclaim — the version key already
    prevents *serving* a stale-version mix) and between tests for isolation.
    In-flight background rebuilds may still complete and re-populate their key
    afterwards; that is harmless.
    """
    global _sem, _sem_capacity
    with _lock:
        _store.clear()
        _inflight.clear()
        _sem = None
        _sem_capacity = 0


def size() -> int:
    """Number of cached entries."""
    with _lock:
        return len(_store)


def stats() -> dict[str, Any]:
    """Snapshot of cache size, in-flight rebuilds, and hit/miss counters."""
    with _lock:
        return {
            "entries": len(_store),
            "inflight_rebuilds": len(_inflight),
            "max_entries": int(settings.MIX_CACHE_MAX_ENTRIES),
            **_counters,
        }
