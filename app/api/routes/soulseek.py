"""
GrooveIQ -- Soulseek (slskd) download API routes.

Separate from the Spotify-ID-based /v1/downloads endpoints because
Soulseek uses text search and file-level selection instead of Spotify
track IDs.
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
    DownloadResponse,
    SoulseekDownloadRequest,
    SoulseekSearchResult,
)
from app.services.slskd import get_slskd_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _require_slskd() -> None:
    if not settings.slskd_enabled:
        raise HTTPException(
            status_code=503,
            detail="Soulseek (slskd) not configured. Set SLSKD_URL, SLSKD_API_KEY, and SLSKD_ENABLED=true.",
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get(
    "/soulseek/search",
    summary="Search Soulseek for tracks",
    response_model=list[SoulseekSearchResult],
)
async def search_soulseek(
    q: str = Query(..., min_length=1, description="Search query (e.g. 'Radiohead Creep')"),
    limit: int = Query(20, ge=1, le=100),
    _key: str = Depends(require_api_key),
):
    """Search the Soulseek network via slskd and return ranked file results."""
    _require_slskd()
    client = get_slskd_client()
    try:
        results = await client.search(q, limit=limit)
        return [SoulseekSearchResult(**r) for r in results]
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@router.post(
    "/soulseek/download",
    summary="Download a file from Soulseek",
    response_model=DownloadResponse,
    status_code=201,
)
async def create_soulseek_download(
    body: SoulseekDownloadRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Queue a specific file for download from a Soulseek peer.

    Use ``GET /v1/soulseek/search`` first to find files, then pass the
    chosen result's ``username``, ``filename``, and ``size`` here.
    """
    _require_slskd()
    client = get_slskd_client()
    try:
        dl_result = await client.download(body.username, body.filename, body.size)
    except Exception as exc:
        logger.error("slskd download error: %s", exc)
        raise HTTPException(status_code=502, detail="slskd service temporarily unavailable")
    finally:
        await client.close()

    if dl_result.get("state") == "error":
        error_msg = dl_result.get("error", "Unknown error")
        record = DownloadRequest(
            source="soulseek",
            status="error",
            slskd_username=body.username,
            slskd_filename=body.filename,
            track_title=body.track_title,
            artist_name=body.artist_name,
            album_name=body.album_name,
            requested_by=hash_key(_key)[:16] if _key != "anonymous" else None,
            error_message=error_msg[:1024],
            updated_at=int(time.time()),
        )
        session.add(record)
        await session.flush()
        return DownloadResponse.model_validate(record)

    record = DownloadRequest(
        source="soulseek",
        status="queued",
        slskd_username=body.username,
        slskd_filename=body.filename,
        track_title=body.track_title,
        artist_name=body.artist_name,
        album_name=body.album_name,
        requested_by=hash_key(_key)[:16] if _key != "anonymous" else None,
        updated_at=int(time.time()),
    )
    session.add(record)
    await session.flush()

    # Spawn watcher to poll slskd until the transfer completes.
    from app.services.slskd_watcher import start_watcher

    await start_watcher(record.id)

    return DownloadResponse.model_validate(record)


# ---------------------------------------------------------------------------
# Transfer status
# ---------------------------------------------------------------------------


@router.get(
    "/soulseek/downloads/{username}/{transfer_id}",
    summary="Check Soulseek transfer status",
)
async def get_soulseek_transfer(
    username: str,
    transfer_id: str,
    _key: str = Depends(require_api_key),
):
    """Get real-time status of a Soulseek download transfer from slskd."""
    _require_slskd()
    client = get_slskd_client()
    try:
        transfer = await client.get_transfer(username, transfer_id)
    finally:
        await client.close()

    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@router.delete(
    "/soulseek/downloads/{username}/{transfer_id}",
    summary="Cancel a Soulseek download",
    status_code=204,
)
async def cancel_soulseek_transfer(
    username: str,
    transfer_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Cancel an in-progress Soulseek download."""
    _require_slskd()
    client = get_slskd_client()
    try:
        ok = await client.cancel_transfer(username, transfer_id)
    finally:
        await client.close()

    if not ok:
        raise HTTPException(status_code=404, detail="Transfer not found or already completed")

    # Update DB record if we have one.
    result = await session.execute(
        select(DownloadRequest).where(
            DownloadRequest.source == "soulseek",
            DownloadRequest.slskd_username == username,
            DownloadRequest.slskd_transfer_id == transfer_id,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        record.status = "error"
        record.error_message = "Cancelled by user"
        record.updated_at = int(time.time())


# ---------------------------------------------------------------------------
# List downloads
# ---------------------------------------------------------------------------


@router.get(
    "/soulseek/downloads",
    summary="List Soulseek download history",
)
async def list_soulseek_downloads(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns persisted Soulseek download requests, newest first."""
    q = select(DownloadRequest).where(DownloadRequest.source == "soulseek").order_by(DownloadRequest.created_at.desc())
    if status:
        q = q.where(DownloadRequest.status == status)

    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    records = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    return {
        "total": total,
        "downloads": [DownloadResponse.model_validate(r) for r in records],
    }
