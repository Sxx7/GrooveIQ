"""GrooveIQ – Playlist generation and management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import Playlist
from app.models.schemas import (
    PlaylistCreate,
    PlaylistDetailResponse,
    PlaylistResponse,
)
from app.services.playlist_service import (
    delete_playlist,
    generate_playlist,
    get_playlist_with_tracks,
)

router = APIRouter()


@router.post(
    "/playlists",
    response_model=PlaylistDetailResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new playlist",
)
async def create_playlist(
    body: PlaylistCreate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    try:
        playlist = await generate_playlist(
            session=session,
            name=body.name,
            strategy=body.strategy,
            seed_track_id=body.seed_track_id,
            params=body.params,
            max_tracks=body.max_tracks,
        )
        # Reload with tracks for response
        detail = await get_playlist_with_tracks(session, playlist.id)
        return detail
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
        raise HTTPException(status_code=404, detail=f"Playlist {playlist_id} not found")
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
    found = await delete_playlist(session, playlist_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"Playlist {playlist_id} not found")
