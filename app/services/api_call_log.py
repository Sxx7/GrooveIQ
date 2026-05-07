"""
GrooveIQ – API call log service.

Persists every /v1/* HTTP request captured by the logging middleware so the
frontend's API traffic can be browsed per-user from the dashboard for
debugging. Mirrors the reco_audit pattern: writes use their own session
(fire-and-forget from the caller), reads stream through the request session.

Body redaction and size caps live here so middleware stays small.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ApiCallLog, User

logger = logging.getLogger(__name__)

# Background batch-writer state. Each /v1/* request writes one row to
# api_call_logs; under dashboard polling that's ~1.5 commits/sec, all
# fighting the same SQLite write lock as the library scanner. We collect
# rows in an in-process queue and flush in batches to keep the writer
# count down to one and the commit count down by ~50–100×.
_log_queue: asyncio.Queue[ApiCallLog] | None = None
_writer_task: asyncio.Task | None = None
_FLUSH_INTERVAL_SECONDS = 1.0
_MAX_QUEUE_SIZE = 5000   # If the flusher falls this far behind, drop new rows.
_MAX_BATCH_SIZE = 500    # Bound the per-flush transaction.


# Substrings (case-insensitive) of JSON keys whose values must never be persisted.
_REDACT_KEYS = {
    "password",
    "api_key",
    "apikey",
    "token",
    "session_key",
    "secret",
    "authorization",
    "client_secret",
    "encryption_key",
}

# Surface labels for paths we never log even when API_LOG_ENABLED=true. Long-poll
# / SSE / health routes would dominate the table without adding signal.
_SKIP_PATH_PREFIXES = (
    "/health",
    "/v1/pipeline/stream",
    "/static/",
    "/dashboard",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/favicon",
    "/v1/api-calls",  # don't log the log-viewing endpoint itself
)


def should_log_path(path: str) -> bool:
    """Return False for endpoints we deliberately skip."""
    if not path.startswith("/v1/") and path not in {"/", ""}:
        return False
    for prefix in _SKIP_PATH_PREFIXES:
        if path.startswith(prefix):
            return False
    # /v1/users/{id}/api-calls is the dashboard's own log-viewer endpoint;
    # logging it would create a "poll == new row" feedback loop.
    if path.endswith("/api-calls") or "/api-calls/" in path:
        return False
    return not (path in ("/v1/events", "/v1/events/batch") and not settings.API_LOG_INCLUDE_EVENTS)


def redact(value: Any) -> Any:
    """Recursively replace values for keys that look secret-like."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and any(s in k.lower() for s in _REDACT_KEYS):
                out[k] = "***redacted***"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def _truncate_str(s: str, max_bytes: int) -> str:
    encoded = s.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return s
    # Truncate to max_bytes and add a marker; decode safely on a char boundary.
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "…[truncated]"


def truncate_body(body_bytes: bytes | None, content_type: str | None) -> Any:
    """Decode + JSON-parse if possible, else return a string preview. Always size-capped."""
    if not body_bytes:
        return None
    max_bytes = max(256, settings.API_LOG_MAX_BODY_BYTES)
    if len(body_bytes) > max_bytes:
        head = body_bytes[:max_bytes]
        truncated = True
    else:
        head = body_bytes
        truncated = False

    if content_type and "application/json" in content_type.lower():
        try:
            parsed = json.loads(head.decode("utf-8", errors="replace"))
            return redact(_summarize_response(parsed))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # fall through to string preview

    try:
        text = head.decode("utf-8", errors="replace")
    except Exception:
        text = repr(head)
    if truncated:
        text = text + "…[truncated]"
    return _truncate_str(text, max_bytes)


def _summarize_response(data: Any) -> Any:
    """For list-shaped data, keep first N entries with a small set of keys."""
    if isinstance(data, list):
        cap = max(5, settings.API_LOG_MAX_LIST_ITEMS)
        if len(data) <= cap:
            return data
        head = data[:cap]
        return {
            "__truncated_list__": True,
            "items_total": len(data),
            "items_shown": cap,
            "items": head,
        }
    if isinstance(data, dict):
        # If a top-level "tracks" / "items" / "candidates" key holds a long list,
        # truncate it but keep the surrounding metadata. Otherwise return as-is.
        out = {}
        for k, v in data.items():
            if isinstance(v, list):
                out[k] = _summarize_response(v)
            else:
                out[k] = v
        return out
    return data


# ---------------------------------------------------------------------------
# Caller identity (issue #81)
# ---------------------------------------------------------------------------


# Order matters: CLI patterns before browser, since some libraries (e.g.
# python-requests) include "Mozilla" in their UA. Mobile is checked before
# browser too because some mobile webviews still send a Mozilla UA.
_CLI_PATTERNS = (
    "curl/",
    "wget/",
    "httpie/",
    "postmanruntime",
    "go-http",
    "python-requests",
    "python-urllib",
    "okhttp/",
    "java/",
    "axios/",
    "node-fetch",
    "got (https",
    "insomnia",
    "thunderclient",
    "rest-client",
)

_MOBILE_PATTERNS = (
    "iphone",
    "ipad",
    "ipod",
    "android",
    "darwin/",
    "mobile",
    "okhttp",  # also a mobile signal — already caught above as cli though
    "cfnetwork",
)

_BROWSER_PATTERNS = (
    "mozilla/",
    "chrome/",
    "safari/",
    "firefox/",
    "edge/",
    "edg/",
    "webkit/",
    "trident/",
)


def classify_user_agent(ua: str | None) -> str:
    """Classify a User-Agent string into browser / mobile / cli / other."""
    if not ua or not ua.strip():
        return "other"
    ua_lower = ua.lower()
    if any(p in ua_lower for p in _CLI_PATTERNS):
        return "cli"
    if any(p in ua_lower for p in _MOBILE_PATTERNS):
        return "mobile"
    if any(p in ua_lower for p in _BROWSER_PATTERNS):
        return "browser"
    return "other"


def parse_client_ip(forwarded_for: str | None, fallback: str | None) -> str | None:
    """Pick the client IP. ``X-Forwarded-For`` is a comma-separated chain
    where the leftmost entry is the original client; trust it when set,
    fall back to the direct peer address otherwise.
    """
    if forwarded_for:
        first = forwarded_for.split(",", 1)[0].strip()
        if first:
            return first[:64]
    return (fallback or "")[:64] or None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def start_log_writer() -> None:
    """Launch the background flusher. Call from app startup (lifespan)."""
    global _log_queue, _writer_task
    if _writer_task is not None and not _writer_task.done():
        return
    _log_queue = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
    _writer_task = asyncio.create_task(_flush_loop(), name="api_call_log_flusher")


async def stop_log_writer() -> None:
    """Cancel the flusher and drain remaining rows. Call from shutdown."""
    global _writer_task
    if _writer_task is None:
        return
    _writer_task.cancel()
    try:
        await _writer_task
    except (asyncio.CancelledError, Exception):
        pass
    # Final drain after cancellation so in-flight rows aren't silently lost.
    await _flush_batch()
    _writer_task = None


async def _flush_loop() -> None:
    """Drain the queue every _FLUSH_INTERVAL_SECONDS until cancelled.

    Drains *before* sleeping so a row enqueued immediately after the
    writer starts doesn't have to wait a full interval to be persisted.
    """
    while True:
        try:
            await _flush_batch()
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover — loop must keep running
            logger.exception("api_call_log flush loop error (continuing)")


async def _flush_batch() -> None:
    """Drain up to _MAX_BATCH_SIZE rows and commit them in one transaction."""
    if _log_queue is None or _log_queue.empty():
        return
    rows: list[ApiCallLog] = []
    while len(rows) < _MAX_BATCH_SIZE:
        try:
            rows.append(_log_queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if not rows:
        return
    try:
        async with AsyncSessionLocal() as session:
            session.add_all(rows)
            await session.commit()
    except Exception as e:
        logger.warning(
            "api_call_log batch flush failed (%d rows dropped): %s", len(rows), e
        )


async def write_log(
    *,
    method: str,
    path: str,
    route_template: str | None,
    query_string: str | None,
    request_body: Any,
    status_code: int,
    duration_ms: int,
    user_id: str | None,
    request_id: str | None,
    response_summary: Any,
    response_size_bytes: int | None,
    error: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Enqueue one HTTP-call row for the background batch writer.

    Returns immediately. Errors (queue full, model construction failure)
    are swallowed — these are debug logs and must never affect request
    handling. The actual SQLite commit happens in `_flush_batch`.
    """
    if not settings.API_LOG_ENABLED:
        return
    if _log_queue is None:
        # Writer not started — silently drop. Happens in tests that don't
        # call start_log_writer(); production startup always calls it.
        return
    try:
        row = ApiCallLog(
            created_at=int(time.time()),
            user_id=user_id,
            request_id=request_id,
            method=method,
            path=path[:512],
            route_template=(route_template or "")[:512] or None,
            query_string=(query_string or "")[:4096] or None,
            request_body=request_body,
            status_code=status_code,
            duration_ms=duration_ms,
            response_summary=response_summary,
            response_size_bytes=response_size_bytes,
            error=(error or "")[:4096] or None,
            client_ip=client_ip,
            user_agent=(user_agent or "")[:512] or None,
            source_class=classify_user_agent(user_agent),
        )
        _log_queue.put_nowait(row)
    except asyncio.QueueFull:
        logger.debug("api_call_log queue full; dropping row")
    except Exception as e:  # pragma: no cover — logging shouldn't crash anything
        logger.warning("api_call_log write failed: %s", e)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_calls(
    session: AsyncSession,
    *,
    user_id: str | None = None,
    method: str | None = None,
    path_contains: str | None = None,
    status: int | None = None,
    include_events: bool = True,
    since: int | None = None,
    source: str | None = None,
    client_ip_contains: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total) — rows are paginated summaries (no body)."""
    q = select(ApiCallLog)
    count_q = select(func.count(ApiCallLog.id))

    conds = []
    if user_id:
        # PATCH /v1/users/{uid} captures user_id=NULL because the route's path
        # param is the numeric uid, not the user_id string. Resolve uid via a
        # User lookup and OR-match path so those rows surface under the
        # per-user view too. Issue #81 follow-up.
        # The User model exposes `uid` as a property over the `id` column;
        # the SQL column is `User.id`.
        uid = (await session.execute(select(User.id).where(User.user_id == user_id))).scalar_one_or_none()
        if uid is not None:
            uid_path = f"/v1/users/{uid}"
            conds.append(
                or_(
                    ApiCallLog.user_id == user_id,
                    ApiCallLog.path == uid_path,
                    ApiCallLog.path.like(f"{uid_path}/%"),
                )
            )
        else:
            conds.append(ApiCallLog.user_id == user_id)
    if method:
        conds.append(ApiCallLog.method == method.upper())
    if path_contains:
        conds.append(ApiCallLog.path.contains(path_contains))
    if status is not None:
        conds.append(ApiCallLog.status_code == status)
    if since is not None:
        conds.append(ApiCallLog.created_at >= since)
    if not include_events:
        conds.append(ApiCallLog.path != "/v1/events")
        conds.append(ApiCallLog.path != "/v1/events/batch")
    if source:
        conds.append(ApiCallLog.source_class == source)
    if client_ip_contains:
        conds.append(ApiCallLog.client_ip.contains(client_ip_contains))

    for c in conds:
        q = q.where(c)
        count_q = count_q.where(c)

    q = q.order_by(ApiCallLog.created_at.desc()).limit(limit).offset(offset)

    rows = (await session.execute(q)).scalars().all()
    total = (await session.execute(count_q)).scalar_one()

    return [_row_to_summary(r) for r in rows], int(total or 0)


async def get_call(session: AsyncSession, call_id: int) -> dict[str, Any] | None:
    row = (await session.execute(select(ApiCallLog).where(ApiCallLog.id == call_id))).scalar_one_or_none()
    if not row:
        return None
    return _row_to_detail(row)


async def get_stats(session: AsyncSession) -> dict[str, Any]:
    total = int((await session.execute(select(func.count(ApiCallLog.id)))).scalar_one() or 0)
    oldest = (await session.execute(select(func.min(ApiCallLog.created_at)))).scalar_one()
    return {
        "enabled": settings.API_LOG_ENABLED,
        "include_events": settings.API_LOG_INCLUDE_EVENTS,
        "retention_days": settings.API_LOG_RETENTION_DAYS,
        "total_rows": total,
        "oldest_at": int(oldest) if oldest else None,
    }


async def purge_old(session: AsyncSession, retention_days: int) -> int:
    """Delete rows older than the cutoff; returns number of rows removed."""
    cutoff = int(time.time()) - max(1, retention_days) * 86400
    result = await session.execute(delete(ApiCallLog).where(ApiCallLog.created_at < cutoff))
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# Row serialisation
# ---------------------------------------------------------------------------


def _row_to_summary(r: ApiCallLog) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at,
        "user_id": r.user_id,
        "method": r.method,
        "path": r.path,
        "status_code": r.status_code,
        "duration_ms": r.duration_ms,
        "is_error": r.status_code >= 400,
        "request_id": r.request_id,
        "client_ip": r.client_ip,
        "source_class": r.source_class,
    }


def _row_to_detail(r: ApiCallLog) -> dict[str, Any]:
    return {
        "id": r.id,
        "created_at": r.created_at,
        "user_id": r.user_id,
        "method": r.method,
        "path": r.path,
        "route_template": r.route_template,
        "query_string": r.query_string,
        "request_body": r.request_body,
        "status_code": r.status_code,
        "duration_ms": r.duration_ms,
        "response_summary": r.response_summary,
        "response_size_bytes": r.response_size_bytes,
        "error": r.error,
        "request_id": r.request_id,
        "client_ip": r.client_ip,
        "user_agent": r.user_agent,
        "source_class": r.source_class,
    }
