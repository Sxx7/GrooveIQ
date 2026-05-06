"""
GrooveIQ – HTTP request/response logging middleware.

Captures every /v1/* request and persists a summary row to api_call_logs
so the dashboard's User Diagnostics tab can show what the frontend posted
and what GrooveIQ returned. See app/services/api_call_log.py for the
write/read service layer (redaction, truncation, persistence, purge).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from fastapi import Request, Response

from app.core.config import settings
from app.services.api_call_log import (
    parse_client_ip,
    should_log_path,
    truncate_body,
    write_log,
)

logger = logging.getLogger(__name__)


def _route_template(request: Request) -> str | None:
    """Return the FastAPI route path template once routing has matched."""
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return route.path
    return None


def _redact_query(query_string: str | None) -> str | None:
    if not query_string:
        return None
    parts = []
    for kv in query_string.split("&"):
        if "=" in kv:
            k, _ = kv.split("=", 1)
            if any(s in k.lower() for s in ("password", "token", "secret", "api_key")):
                parts.append(f"{k}=***redacted***")
            else:
                parts.append(kv)
        else:
            parts.append(kv)
    return "&".join(parts)


def _extract_user_id(request: Request, parsed_body: Any) -> str | None:
    """Try path -> body -> query string in that order."""
    path_params = request.scope.get("path_params") or {}
    if path_params.get("user_id"):
        return str(path_params["user_id"])
    if isinstance(parsed_body, dict):
        body_user = parsed_body.get("user_id")
        if body_user:
            return str(body_user)
    user = request.query_params.get("user_id") or request.query_params.get("user")
    if user:
        return str(user)
    return None


def _is_streamed_content(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return "text/event-stream" in ct or "multipart/" in ct or "application/octet-stream" in ct


async def api_call_logging_middleware(request: Request, call_next):
    """Buffer request + response, persist a row to api_call_logs.

    Fire-and-forget: the DB write runs as a background task so client-visible
    latency only includes the in-memory buffering (single-digit ms in practice).
    """
    if not settings.API_LOG_ENABLED or not should_log_path(request.url.path):
        return await call_next(request)

    t0 = time.monotonic()

    # Caller identity (issue #81) — captured up-front so all three log paths
    # below carry it: success, streaming, and exception.
    client_ip = parse_client_ip(
        request.headers.get("x-forwarded-for"),
        request.client.host if request.client else None,
    )
    user_agent = request.headers.get("user-agent")

    # Buffer request body so downstream Pydantic parsing can re-read it.
    # Starlette caches on Request._body after first call to body() — FastAPI
    # uses the same accessor under the hood, so this is transparent.
    request_body_bytes: bytes = b""
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            request_body_bytes = await request.body()
        except Exception:
            request_body_bytes = b""

    request_body_summary = None
    if request_body_bytes:
        request_body_summary = truncate_body(request_body_bytes, request.headers.get("content-type"))

    response: Response | None = None
    error_msg: str | None = None
    try:
        response = await call_next(request)
    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        duration_ms = int((time.monotonic() - t0) * 1000)
        # Best effort — log a 500 row, then re-raise so framework error handlers run.
        asyncio.create_task(
            write_log(
                method=request.method,
                path=request.url.path,
                route_template=_route_template(request),
                query_string=_redact_query(str(request.url.query) or None),
                request_body=request_body_summary,
                status_code=500,
                duration_ms=duration_ms,
                user_id=_extract_user_id(
                    request,
                    request_body_summary if isinstance(request_body_summary, dict) else None,
                ),
                request_id=request.headers.get("x-request-id"),
                response_summary=None,
                response_size_bytes=None,
                error=error_msg,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        raise

    # Don't buffer streaming or binary responses — the path-skip list already
    # covers /v1/pipeline/stream, but new SSE / file routes shouldn't break us.
    content_type = response.headers.get("content-type", "")
    if _is_streamed_content(content_type):
        duration_ms = int((time.monotonic() - t0) * 1000)
        asyncio.create_task(
            write_log(
                method=request.method,
                path=request.url.path,
                route_template=_route_template(request),
                query_string=_redact_query(str(request.url.query) or None),
                request_body=request_body_summary,
                status_code=response.status_code,
                duration_ms=duration_ms,
                user_id=_extract_user_id(
                    request,
                    request_body_summary if isinstance(request_body_summary, dict) else None,
                ),
                request_id=request.headers.get("x-request-id"),
                response_summary={"_skipped": "streaming response"},
                response_size_bytes=None,
                error=None,
                client_ip=client_ip,
                user_agent=user_agent,
            )
        )
        return response

    # BaseHTTPMiddleware wraps the route response so the body is exposed as an
    # async iterator — drain it, then return a fresh Response with the same
    # bytes so the client still gets the data.
    body_chunks: list[bytes] = []
    try:
        async for chunk in response.body_iterator:
            body_chunks.append(chunk)
    except Exception as exc:
        # If the body fails mid-stream, log what we have and let the exception
        # propagate so the client sees a broken response (vs a fake 200).
        logger.warning("api_call_logging: response body iteration failed: %s", exc)
        raise
    response_bytes = b"".join(body_chunks)

    headers = dict(response.headers)
    # Framework will recompute Content-Length for the new body.
    headers.pop("content-length", None)

    new_response = Response(
        content=response_bytes,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
    )

    duration_ms = int((time.monotonic() - t0) * 1000)
    response_summary = truncate_body(response_bytes, content_type)
    user_id = _extract_user_id(
        request,
        request_body_summary if isinstance(request_body_summary, dict) else None,
    )

    asyncio.create_task(
        write_log(
            method=request.method,
            path=request.url.path,
            route_template=_route_template(request),
            query_string=_redact_query(str(request.url.query) or None),
            request_body=request_body_summary,
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_id=user_id,
            request_id=request.headers.get("x-request-id"),
            response_summary=response_summary,
            response_size_bytes=len(response_bytes),
            error=None,
            client_ip=client_ip,
            user_agent=user_agent,
        )
    )

    return new_response


__all__ = ["api_call_logging_middleware"]
