"""GrooveIQ – API call log routes (issue #79)."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_admin, require_api_key
from app.db.session import get_session
from app.services.api_call_log import get_call, get_stats, list_calls

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/users/{user_id}/api-calls",
    summary="List recent HTTP requests for one user",
    description=(
        "Paginated list of HTTP requests captured by the API logging middleware "
        "for the given user. Use `path_contains` and `include_events=false` to "
        "keep the table readable."
    ),
)
async def list_user_api_calls(
    user_id: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    method: str | None = Query(None, description="Filter to a single HTTP method (GET, POST, ...)"),
    path_contains: str | None = Query(None, description="Substring match on request path"),
    status: int | None = Query(None, ge=100, le=599, description="Exact status code"),
    include_events: bool = Query(True, description="Set false to hide POST /v1/events rows"),
    since_minutes: int | None = Query(None, ge=1, description="Only rows newer than this many minutes"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    check_user_access(_key, user_id)
    since = int(time.time()) - since_minutes * 60 if since_minutes else None
    rows, total = await list_calls(
        session,
        user_id=user_id,
        method=method,
        path_contains=path_contains,
        status=status,
        include_events=include_events,
        since=since,
        limit=limit,
        offset=offset,
    )
    return {
        "user_id": user_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get(
    "/api-calls/{call_id}",
    summary="Single API call detail (full body)",
    description="Returns the truncated request body, response summary, and metadata for one row.",
)
async def get_api_call(
    call_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    detail = await get_call(session, call_id)
    if not detail:
        raise HTTPException(status_code=404, detail="API call log entry not found.")
    if detail.get("user_id"):
        check_user_access(_key, detail["user_id"])
    return detail


@router.get(
    "/api-calls",
    summary="List recent HTTP requests across all users (admin)",
)
async def list_all_api_calls(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    method: str | None = None,
    path_contains: str | None = None,
    status: int | None = Query(None, ge=100, le=599),
    user_id: str | None = None,
    include_events: bool = True,
    since_minutes: int | None = Query(None, ge=1),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    since = int(time.time()) - since_minutes * 60 if since_minutes else None
    rows, total = await list_calls(
        session,
        user_id=user_id,
        method=method,
        path_contains=path_contains,
        status=status,
        include_events=include_events,
        since=since,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.get("/api-calls/stats/summary", summary="API call log stats (admin)")
async def get_api_call_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return await get_stats(session)
