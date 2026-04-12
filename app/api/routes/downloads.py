"""
GrooveIQ -- Downloads API routes (Spotizerr proxy).

Proxies Spotizerr search/download/status so frontend apps only need
to configure GrooveIQ.  Persists download history in the DB.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_key, require_api_key
from app.db.session import get_session
from app.models.db import DownloadRequest
from app.models.schemas import (
    DownloadCreateRequest,
    DownloadResponse,
    DownloadStatusResponse,
)
from app.services.spotdl import get_download_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_client():
    """Return the configured download client (spotdl-api or Spotizerr)."""
    client = get_download_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="No download backend configured. Set SPOTDL_API_URL or SPOTIZERR_URL.",
        )
    return client


def _require_download_backend() -> None:
    if not settings.download_enabled:
        raise HTTPException(
            status_code=503,
            detail="No download backend configured. Set SPOTDL_API_URL or SPOTIZERR_URL.",
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
    """Proxy Spotizerr track search.  Flattens raw Spotify objects into a
    simple format the mobile client can consume directly."""
    _require_download_backend()
    client = _get_client()
    try:
        raw = await client.search(q, limit=limit)
        results = [_flatten_track(t) for t in raw]
        return results
    finally:
        await client.close()


def _flatten_track(track: dict) -> dict:
    """Convert a raw Spotify track object into a flat search result."""
    artists = track.get("artists") or []
    artist_name = artists[0]["name"] if artists else ""

    album = track.get("album") or {}
    album_name = album.get("name")

    images = album.get("images") or []
    # Prefer the 300px image, fall back to first available.
    image_url = None
    for img in images:
        if img.get("width") == 300 or img.get("height") == 300:
            image_url = img["url"]
            break
    if not image_url and images:
        image_url = images[0]["url"]

    return {
        "spotify_id": track.get("id", ""),
        "title": track.get("name", ""),
        "artist": artist_name,
        "album": album_name,
        "type": track.get("type", "track"),
        "image_url": image_url,
    }


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
    _require_download_backend()
    client = _get_client()
    try:
        dl_result = await client.download(body.spotify_id)
    except Exception as exc:
        logger.error("Download service error for %s: %s", body.spotify_id, exc)
        raise HTTPException(status_code=502, detail="Download service temporarily unavailable")
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
        requested_by=hash_key(_key)[:16] if _key != "anonymous" else None,
        error_message=dl_result.get("error"),
        updated_at=int(time.time()),
    )
    session.add(record)
    await session.flush()

    # Spawn a background watcher that polls Spotizerr until the download
    # reaches a terminal state, then triggers the media server + GrooveIQ
    # library scan so the new file becomes instantly playable.  Only
    # in-flight downloads are worth watching — "error" results are already
    # final and "duplicate" shares a task_id with whichever watcher owns
    # the original request (start_watcher is idempotent).
    if record.task_id and record.status not in ("error", "unknown"):
        from app.services.download_watcher import start_watcher

        await start_watcher(record.task_id)

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
    _require_download_backend()
    client = _get_client()
    try:
        status_data = await client.get_status(task_id)
    finally:
        await client.close()

    # Opportunistically update DB record.
    result = await session.execute(select(DownloadRequest).where(DownloadRequest.task_id == task_id))
    record = result.scalar_one_or_none()

    # Flattened shape from SpotizerrClient.get_status():
    # {"status", "progress", "error", "raw"}
    spotizerr_status = status_data.get("status", "unknown")
    # Map Spotizerr terminal states onto our DB statuses so the history
    # table shows "completed" / "error" rather than Spotizerr's internal
    # enum strings.
    db_status = spotizerr_status
    if spotizerr_status in ("complete", "done"):
        db_status = "completed"
    if record and db_status != record.status:
        record.status = db_status
        record.updated_at = int(time.time())
        if status_data.get("error"):
            record.error_message = str(status_data["error"])[:1024]

    return DownloadStatusResponse(
        task_id=task_id,
        status=spotizerr_status,
        progress=status_data.get("progress"),
        details=status_data.get("raw") or status_data,
    )


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@router.get(
    "/downloads",
    summary="List download history",
)
async def list_downloads(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns persisted download requests, newest first."""
    q = select(DownloadRequest).order_by(DownloadRequest.created_at.desc())
    if status:
        q = q.where(DownloadRequest.status == status)

    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0

    records = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    return {
        "total": total,
        "downloads": [DownloadResponse.model_validate(r) for r in records],
    }
