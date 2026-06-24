"""GrooveIQ – Playlist generation and management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, hash_key, require_admin, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import Playlist
from app.models.schemas import (
    PlaylistCreate,
    PlaylistDetailResponse,
    PlaylistResponse,
)
from app.services.playlist_service import (
    PlaylistServiceUnavailableError,
    compute_cache_key,
    delete_playlist,
    generate_playlist,
    get_playlist_with_tracks,
    utc_day_bucket,
)

router = APIRouter()


@router.post(
    "/playlists",
    response_model=PlaylistDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new playlist (idempotent within a UTC day)",
)
async def create_playlist(
    body: PlaylistCreate,
    response: Response,
    refresh: bool = Query(
        False,
        description="Bypass the daily idempotency cache and force a fresh generation.",
    ),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Optional personalization: validate + authorize the user before it can
    # influence selection or the (now per-user) idempotency cache. Same rules
    # as /recommend; both are no-ops/soft unless configured.
    if body.user_id:
        validate_user_id(body.user_id)
        check_user_access(_key, body.user_id)

    # Daily idempotency: same caller + same params + same UTC day → return the
    # already-generated playlist instead of creating a duplicate row. See #89.
    key_hash = hash_key(_key)
    cache_key = compute_cache_key(
        created_by=key_hash,
        strategy=body.strategy,
        seed_track_id=body.seed_track_id,
        params=body.params,
        max_tracks=body.max_tracks,
        bucket_date=utc_day_bucket(),
        user_id=body.user_id,
    )

    if not refresh:
        hit = await session.execute(
            select(Playlist.id).where(Playlist.cache_key == cache_key).order_by(Playlist.id.desc()).limit(1)
        )
        existing_id = hit.scalar_one_or_none()
        if existing_id is not None:
            response.status_code = status.HTTP_200_OK
            return await get_playlist_with_tracks(session, existing_id)

    try:
        playlist = await generate_playlist(
            session=session,
            name=body.name,
            strategy=body.strategy,
            seed_track_id=body.seed_track_id,
            params=body.params,
            max_tracks=body.max_tracks,
            user_id=body.user_id,
        )
        # Record which API key created this playlist + the daily cache key.
        playlist.created_by = key_hash
        playlist.cache_key = cache_key
        await session.flush()
        # Reload with tracks for response
        detail = await get_playlist_with_tracks(session, playlist.id)
        return detail
    except PlaylistServiceUnavailableError as e:
        # Strategy-side reasons that aren't the caller's fault — CLAP not
        # installed, audio backfill still running, model failed to load, etc.
        # Use 503 so clients can retry / fall back instead of treating it
        # like bad input. See issue #91 follow-up.
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        # Genuine bad input (missing prompt, unknown energy curve, etc.).
        raise HTTPException(status_code=400, detail=str(e) or "Invalid playlist parameters.")


@router.get(
    "/playlists",
    response_model=list[PlaylistResponse],
    summary="List all playlists",
)
async def list_playlists(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    strategy: str = Query(None),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    q = select(Playlist).order_by(Playlist.created_at.desc())
    if strategy:
        q = q.where(Playlist.strategy == strategy)
    q = q.offset(offset).limit(limit)

    result = await session.execute(q)
    return result.scalars().all()


@router.get(
    "/playlists/{playlist_id}",
    response_model=PlaylistDetailResponse,
    summary="Get playlist with track details",
)
async def get_playlist(
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    detail = await get_playlist_with_tracks(session, playlist_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Not found.")
    return detail


@router.delete(
    "/playlists/{playlist_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a playlist",
)
async def remove_playlist(
    playlist_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Check ownership: only the creator or an admin can delete.
    result = await session.execute(select(Playlist.created_by).where(Playlist.id == playlist_id))
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Playlist not found.")
    if row and row != hash_key(_key):
        # Not the creator — require admin.
        require_admin(_key)

    found = await delete_playlist(session, playlist_id)
    if not found:
        raise HTTPException(status_code=404, detail="Playlist not found.")

    from app.core.audit import audit_log

    audit_log("playlist_delete", api_key=_key, detail={"playlist_id": playlist_id})
