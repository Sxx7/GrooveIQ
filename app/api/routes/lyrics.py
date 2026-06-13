"""
GrooveIQ — lyrics drain control + telemetry routes.

Operator surface for the lyrics acquisition drain (``app/services/lyrics_drain.py``):
stats for the dashboard, a queue listing, a manual tick, and per-row / bulk
state controls. The per-track *display* endpoint lives in ``tracks.py``
(``GET /v1/tracks/{id}/lyrics``); this router is the ``/v1/lyrics/*`` admin side.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, require_api_key
from app.db.session import get_session
from app.services import lyrics_drain

router = APIRouter()


@router.get("/lyrics/stats", summary="Lyrics drain stats (queue counts, coverage, capacity, ETA)")
async def get_lyrics_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return await lyrics_drain.get_stats(session)


@router.get("/lyrics/requests", summary="List lyrics drain queue rows")
async def list_lyrics_requests(
    status: str | None = Query(None, description="Filter by queue status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    rows = await lyrics_drain.list_requests(session, status=status, limit=limit, offset=offset)
    return {"requests": rows, "limit": limit, "offset": offset}


@router.post("/lyrics/run", summary="Run one lyrics drain tick now (admin)")
async def run_lyrics_now(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    return await lyrics_drain.run_lyrics_tick(session)


@router.post("/lyrics/requests/{request_id}/retry", summary="Re-queue a lyrics row (admin)")
async def retry_lyrics_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lyrics_drain.retry_request(session, request_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found.")
    return {"status": "queued", "id": request_id}


@router.post("/lyrics/requests/{request_id}/skip", summary="Permanently skip a lyrics row (admin)")
async def skip_lyrics_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lyrics_drain.skip_request(session, request_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found.")
    return {"status": "permanently_skipped", "id": request_id}


@router.delete("/lyrics/requests/{request_id}", summary="Forget a lyrics row (admin)")
async def delete_lyrics_request(
    request_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    ok = await lyrics_drain.delete_request(session, request_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found.")
    return {"status": "deleted", "id": request_id}


@router.post("/lyrics/requests/reset", summary="Bulk re-queue lyrics rows by scope (admin)")
async def reset_lyrics_requests(
    body: dict = Body(..., examples=[{"scope": "no_lyrics"}]),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    require_admin(_key)
    scope = (body or {}).get("scope")
    try:
        count = await lyrics_drain.reset_state(session, scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"reset": count, "scope": scope}
