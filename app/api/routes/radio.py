"""
GrooveIQ – Radio endpoints.

Stateful radio sessions that seed from a track, artist, or playlist
and adapt in real-time based on in-session feedback.

Feedback flows through the normal event ingestion pipeline (POST /v1/events)
with context_type=radio and context_id=session_id. The event_service hooks
into radio.record_feedback() automatically for skip/like/dislike events.

Endpoints:
  POST /v1/radio/start          — create a radio session, return first batch
  GET  /v1/radio/{id}/next      — fetch next batch of tracks
  DELETE /v1/radio/{id}         — stop a radio session
  GET  /v1/radio                — list active radio sessions
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, Playlist, TrackFeatures, User  # noqa: F401 (Playlist used in validation)
from app.models.schemas import (
    RadioNextResponse,
    RadioSessionResponse,
    RadioStartRequest,
    RadioStartResponse,
    RadioTrackItem,
)
from app.services import radio as radio_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_model_version() -> str:
    from app.services.ranker import get_model_version
    return get_model_version() or "radio-v1"


@router.post(
    "/radio/start",
    summary="Start a radio session",
    description="""
Start an adaptive radio session seeded from a track, artist, or playlist.

The radio generates an infinite stream of recommendations that adapts
in real-time based on skip/like/dislike feedback within the session.

Returns the session ID and the first batch of tracks.
""",
    status_code=201,
)
async def start_radio(
    body: RadioStartRequest,
    db: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    check_user_access(_key, body.user_id)

    # Verify user exists.
    result = await db.execute(
        select(User.user_id).where(User.user_id == body.user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="User not found.")

    # Validate seed based on type.
    if body.seed_type == "track":
        result = await db.execute(
            select(TrackFeatures.track_id)
            .where(TrackFeatures.track_id == body.seed_value)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Seed track not found.")

    elif body.seed_type == "artist":
        result = await db.execute(
            select(TrackFeatures.track_id)
            .where(TrackFeatures.artist.ilike(f"%{body.seed_value}%"))
            .limit(1)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="No tracks found for this artist.")

    elif body.seed_type == "playlist":
        try:
            pl_id = int(body.seed_value)
        except ValueError:
            raise HTTPException(status_code=400, detail="Playlist seed_value must be a numeric ID.")
        result = await db.execute(
            select(Playlist.id).where(Playlist.id == pl_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Playlist not found.")

    # Create the radio session.
    session = await radio_service.create_radio_session(
        user_id=body.user_id,
        seed_type=body.seed_type,
        seed_value=body.seed_value,
        db=db,
        device_type=body.device_type,
        output_type=body.output_type,
        location_label=body.location_label,
        hour_of_day=body.hour_of_day,
        day_of_week=body.day_of_week,
    )

    if session.seed_embedding is None:
        # Clean up the session we just created
        radio_service.remove_session(session.session_id)
        raise HTTPException(
            status_code=422,
            detail="Could not compute seed embedding. Ensure the seed has analyzed tracks with embeddings.",
        )

    # Generate first batch.
    tracks = await radio_service.get_next_tracks(session.session_id, body.count, db)

    if not tracks:
        radio_service.remove_session(session.session_id)
        raise HTTPException(
            status_code=422,
            detail="No candidates available for this seed. The library may need more analyzed tracks.",
        )

    # Log reco_impression events for the first batch.
    model_version = _get_model_version()
    request_id = str(uuid.uuid4())
    now = int(time.time())
    for t in tracks:
        db.add(ListenEvent(
            user_id=body.user_id,
            track_id=t["track_id"],
            event_type="reco_impression",
            surface="radio",
            position=t["position"],
            request_id=request_id,
            model_version=model_version,
            context_type="radio",
            context_id=session.session_id,
            device_type=body.device_type,
            output_type=body.output_type,
            location_label=body.location_label,
            hour_of_day=body.hour_of_day,
            day_of_week=body.day_of_week,
            timestamp=now,
        ))
    await db.commit()

    return RadioStartResponse(
        session_id=session.session_id,
        seed_type=session.seed_type,
        seed_value=session.seed_value,
        seed_display_name=session.seed_display_name,
        tracks=[RadioTrackItem(**t) for t in tracks],
    )


@router.get(
    "/radio/{session_id}/next",
    summary="Get next batch of radio tracks",
    description="""
Fetch the next batch of tracks for an active radio session.

Each call generates fresh candidates influenced by any feedback
sent since the previous batch. Context params can be updated
to reflect changing conditions (e.g. switching from headphones to speaker).
""",
)
async def radio_next(
    session_id: str,
    count: int = Query(10, ge=1, le=50),
    # Updatable context
    device_type: str = Query(None),
    output_type: str = Query(None),
    location_label: str = Query(None),
    hour_of_day: int = Query(None, ge=0, le=23),
    day_of_week: int = Query(None, ge=1, le=7),
    db: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    s = radio_service.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Radio session not found or expired.")

    check_user_access(_key, s.user_id)

    # Update context if provided.
    if device_type is not None:
        s.device_type = device_type
    if output_type is not None:
        s.output_type = output_type
    if location_label is not None:
        s.location_label = location_label
    if hour_of_day is not None:
        s.hour_of_day = hour_of_day
    if day_of_week is not None:
        s.day_of_week = day_of_week

    tracks = await radio_service.get_next_tracks(session_id, count, db)
    if tracks is None:
        raise HTTPException(status_code=404, detail="Radio session not found or expired.")

    # Log impressions.
    model_version = _get_model_version()
    request_id = str(uuid.uuid4())
    now = int(time.time())
    for t in tracks:
        db.add(ListenEvent(
            user_id=s.user_id,
            track_id=t["track_id"],
            event_type="reco_impression",
            surface="radio",
            position=t["position"],
            request_id=request_id,
            model_version=model_version,
            context_type="radio",
            context_id=session_id,
            device_type=s.device_type,
            output_type=s.output_type,
            location_label=s.location_label,
            hour_of_day=s.hour_of_day,
            day_of_week=s.day_of_week,
            timestamp=now,
        ))
    await db.commit()

    return RadioNextResponse(
        session_id=session_id,
        total_served=s.total_served,
        tracks=[RadioTrackItem(**t) for t in tracks],
    )


@router.delete(
    "/radio/{session_id}",
    summary="Stop a radio session",
)
async def stop_radio(
    session_id: str,
    _key: str = Depends(require_api_key),
):
    s = radio_service.get_session(session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="Radio session not found or expired.")

    check_user_access(_key, s.user_id)

    radio_service.remove_session(session_id)
    return {"status": "stopped", "session_id": session_id}


@router.get(
    "/radio",
    summary="List active radio sessions",
)
async def list_radio_sessions(
    user_id: str = Query(None, description="Filter by user"),
    _key: str = Depends(require_api_key),
):
    if user_id:
        check_user_access(_key, user_id)

    sessions = radio_service.list_sessions(user_id=user_id)
    return {
        "active_sessions": len(sessions),
        "sessions": [RadioSessionResponse(**s) for s in sessions],
    }
