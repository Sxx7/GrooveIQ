"""
GrooveIQ -- Downloads API routes (Spotizerr proxy).

Proxies Spotizerr search/download/status so frontend apps only need
to configure GrooveIQ.  Persists download history in the DB.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import DownloadRequest
from app.models.schemas import (
    DownloadCreateRequest,
    DownloadResponse,
    DownloadStatusResponse,
)
from app.services.spotizerr import SpotizerrClient

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_client() -> SpotizerrClient:
    return SpotizerrClient(
        settings.SPOTIZERR_URL,
        settings.SPOTIZERR_USERNAME,
        settings.SPOTIZERR_PASSWORD,
    )


def _require_spotizerr() -> None:
    if not settings.spotizerr_enabled:
        raise HTTPException(
            status_code=503,
            detail="Spotizerr not configured. Set SPOTIZERR_URL in environment.",
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@router.get(
    "/downloads/search",
    summary="Search for tracks via Spotizerr",
)
async def search_tracks(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(10, ge=1, le=50),
    _key: str = Depends(require_api_key),
):
    """Proxy Spotizerr track search.  Returns results as Spotizerr sees them
    so the user can pick which track to download."""
    _require_spotizerr()
    client = _get_client()
    try:
        results = await client.search(q, limit=limit)
        return {"query": q, "limit": limit, "results": results}
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@router.post(
    "/downloads",
    summary="Download a track via Spotizerr",
    response_model=DownloadResponse,
)
async def create_download(
    body: DownloadCreateRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Trigger download of a specific Spotify track ID.  The user should
    first search via ``GET /v1/downloads/search`` and select a result."""
    _require_spotizerr()
    client = _get_client()
    try:
        dl_result = await client.download(body.spotify_id)
    except Exception as exc:
        logger.error("Spotizerr download error for %s: %s", body.spotify_id, exc)
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        await client.close()

    record = DownloadRequest(
        spotify_id=body.spotify_id,
        task_id=dl_result.get("task_id") or None,
        status=dl_result.get("status", "unknown"),
        track_title=body.track_title,
        artist_name=body.artist_name,
        album_name=body.album_name,
        cover_url=body.cover_url,
        requested_by=_key if _key != "anonymous" else None,
        error_message=dl_result.get("error"),
        updated_at=int(time.time()),
    )
    session.add(record)
    await session.flush()

    return DownloadResponse.model_validate(record)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get(
    "/downloads/status/{task_id}",
    summary="Check download progress",
    response_model=DownloadStatusResponse,
)
async def get_download_status(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Proxy Spotizerr task progress.  Also updates the DB record if found."""
    _require_spotizerr()
    client = _get_client()
    try:
        status_data = await client.get_status(task_id)
    finally:
        await client.close()

    # Opportunistically update DB record.
    result = await session.execute(
        select(DownloadRequest).where(DownloadRequest.task_id == task_id)
    )
    record = result.scalar_one_or_none()

    spotizerr_status = status_data.get("status", "unknown")
    if record and spotizerr_status != record.status:
        record.status = spotizerr_status
        record.updated_at = int(time.time())
        if status_data.get("error"):
            record.error_message = str(status_data["error"])

    return DownloadStatusResponse(
        task_id=task_id,
        status=spotizerr_status,
        progress=status_data.get("progress"),
        details=status_data,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@router.get(
    "/downloads",
    summary="List download history",
)
async def list_downloads(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns persisted download requests, newest first."""
    q = select(DownloadRequest).order_by(DownloadRequest.created_at.desc())
    if status:
        q = q.where(DownloadRequest.status == status)

    total = (await session.execute(
        select(func.count()).select_from(q.subquery())
    )).scalar() or 0

    records = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    return {
        "total": total,
        "downloads": [DownloadResponse.model_validate(r) for r in records],
    }
