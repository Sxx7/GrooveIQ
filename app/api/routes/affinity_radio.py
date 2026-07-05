"""
GrooveIQ – Affinity radio endpoints.

A pure-similarity sibling of ``/radio``: stateful sessions that seed from a track,
artist, or playlist and stream the sonically **nearest library tracks to the fixed
seed** — no feedback drift, no ranker, no diversity reranking. Each ``/next`` walks
one step further out through the seed's neighbourhood, excluding everything already
served this session plus the user's disliked tracks and (by default) everything
they've already heard.

Use this for "I just want more of exactly this, and surface tracks I haven't heard."

Endpoints:
  POST   /v1/affinity/start          — create a session, return the first batch
  GET    /v1/affinity/{id}/next      — fetch the next batch (spirals outward)
  DELETE /v1/affinity/{id}           — stop a session
  GET    /v1/affinity                — list active sessions
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_admin, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import Playlist, TrackFeatures, User
from app.models.schemas import (
    AffinityNextResponse,
    AffinitySessionResponse,
    AffinityStartRequest,
    AffinityStartResponse,
    AffinityTrackItem,
)
from app.services import affinity_radio as affinity_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/affinity/start",
    summary="Start an affinity (pure-similarity) radio session",
    description="""
Start a pure-similarity radio session seeded from a track, artist, or playlist.

Unlike `/radio`, this streams the sonically nearest library tracks to the fixed
seed with no feedback drift, no ranking model, and no diversity reranking — just
cosine order. Each `/next` spirals one step further out through the seed's
neighbourhood without repeating.

By default (`unheard_only=true`) already-played tracks are excluded, so every
result is both close to the seed and new to you. Disliked tracks are always
excluded.

Returns the session ID and the first batch of tracks.
""",
    status_code=201,
)
async def start_affinity(
    body: AffinityStartRequest,
    db: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(body.user_id)
    check_user_access(_key, body.user_id)

    # Verify user exists.
    result = await db.execute(select(User.user_id).where(User.user_id == body.user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Validate the seed (mirrors /radio's seed resolution accepting internal or
    # media-server IDs for track seeds).
    if body.seed_type == "track":
        result = await db.execute(
            select(TrackFeatures.track_id).where(
                or_(
                    TrackFeatures.track_id == body.seed_value,
                    TrackFeatures.media_server_id == body.seed_value,
                )
            )
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Seed track not found.")

    elif body.seed_type == "artist":
        result = await db.execute(
            select(TrackFeatures.track_id).where(TrackFeatures.artist.ilike(f"%{body.seed_value}%")).limit(1)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="No tracks found for this artist.")

    elif body.seed_type == "playlist":
        try:
            pl_id = int(body.seed_value)
        except ValueError:
            raise HTTPException(status_code=400, detail="Playlist seed_value must be a numeric ID.")
        result = await db.execute(select(Playlist.id).where(Playlist.id == pl_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Playlist not found.")

    session = await affinity_service.create_affinity_session(
        user_id=body.user_id,
        seed_type=body.seed_type,
        seed_value=body.seed_value,
        db=db,
        unheard_only=body.unheard_only,
    )

    if session.seed_embedding is None:
        affinity_service.remove_session(session.session_id)
        raise HTTPException(
            status_code=422,
            detail="Could not compute seed embedding. Ensure the seed has analyzed tracks with embeddings.",
        )

    tracks = await affinity_service.get_next_tracks(session.session_id, body.count, db)
    if not tracks:
        affinity_service.remove_session(session.session_id)
        raise HTTPException(
            status_code=422,
            detail="No similar tracks available for this seed. The library may need more analyzed tracks.",
        )

    return AffinityStartResponse(
        session_id=session.session_id,
        seed_type=session.seed_type,
        seed_value=session.seed_value,
        seed_display_name=session.seed_display_name,
        unheard_only=session.unheard_only,
        exhausted=len(tracks) < body.count,
        tracks=[AffinityTrackItem(**t) for t in tracks],
    )


@router.get(
    "/affinity/{session_id}/next",
    summary="Get the next batch of affinity tracks",
    description="""
Fetch the next batch for an active affinity session. Each call returns the nearest
library tracks to the fixed seed that haven't been served yet this session (and,
by default, that you haven't already heard). Fewer than `count` results — or the
`exhausted` flag — means the reachable neighbourhood is used up.
""",
)
async def affinity_next(
    session_id: str,
    count: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    s = affinity_service.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Affinity session not found or expired.")

    check_user_access(_key, s.user_id)

    tracks = await affinity_service.get_next_tracks(session_id, count, db)
    if tracks is None:
        raise HTTPException(status_code=404, detail="Affinity session not found or expired.")

    return AffinityNextResponse(
        session_id=session_id,
        total_served=s.total_served,
        exhausted=len(tracks) < count,
        tracks=[AffinityTrackItem(**t) for t in tracks],
    )


@router.delete(
    "/affinity/{session_id}",
    summary="Stop an affinity session",
)
async def stop_affinity(
    session_id: str,
    _key: str = Depends(require_api_key),
):
    s = affinity_service.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Affinity session not found or expired.")

    check_user_access(_key, s.user_id)

    affinity_service.remove_session(session_id)
    return {"status": "stopped", "session_id": session_id}


@router.get(
    "/affinity",
    summary="List active affinity sessions",
)
async def list_affinity_sessions(
    user_id: str = Query(None, description="Filter by user"),
    _key: str = Depends(require_api_key),
):
    if user_id:
        validate_user_id(user_id)
        check_user_access(_key, user_id)
    else:
        # Listing across users requires admin privileges.
        require_admin(_key)

    sessions = affinity_service.list_sessions(user_id=user_id)
    return {
        "active_sessions": len(sessions),
        "sessions": [AffinitySessionResponse(**s) for s in sessions],
    }
