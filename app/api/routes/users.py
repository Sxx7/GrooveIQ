"""GrooveIQ – User management routes."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, ListenSession, TrackFeatures, TrackInteraction, User
from app.models.schemas import UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_user(session: AsyncSession, user_id: str) -> User:
    """Look up a user by user_id string.  Raises 404 if not found."""
    result = await session.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


# ---------------------------------------------------------------------------
# Taste profile
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/profile", summary="Get user taste profile")
async def get_user_profile(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns the computed taste profile for a user (audio prefs, mood, behaviour stats)."""
    user = await _resolve_user(session, user_id)
    return {
        "uid": user.uid,
        "user_id": user.user_id,
        "display_name": user.display_name,
        "profile_updated_at": user.profile_updated_at,
        "taste_profile": user.taste_profile,
    }


# ---------------------------------------------------------------------------
# Track interactions
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/interactions", summary="Get user track interactions")
async def get_user_interactions(
    user_id: str,
    sort_by: str = Query("satisfaction_score", pattern="^(satisfaction_score|play_count|last_played_at|skip_count)$"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns per-track interaction data for a user (satisfaction scores, play/skip counts, etc.)."""
    await _resolve_user(session, user_id)

    sort_col = {
        "satisfaction_score": TrackInteraction.satisfaction_score,
        "play_count": TrackInteraction.play_count,
        "last_played_at": TrackInteraction.last_played_at,
        "skip_count": TrackInteraction.skip_count,
    }[sort_by]
    from sqlalchemy import asc, desc as sa_desc
    order = sa_desc(sort_col) if sort_dir == "desc" else asc(sort_col)

    count_q = select(func.count()).select_from(
        select(TrackInteraction.id).where(TrackInteraction.user_id == user_id).subquery()
    )
    total = (await session.execute(count_q)).scalar() or 0

    q = (
        select(TrackInteraction, TrackFeatures)
        .outerjoin(TrackFeatures, TrackInteraction.track_id == TrackFeatures.track_id)
        .where(TrackInteraction.user_id == user_id)
        .order_by(order)
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(q)).all()

    return {
        "total": total,
        "interactions": [
            {
                "track_id": ti.track_id,
                "satisfaction_score": round(ti.satisfaction_score, 4) if ti.satisfaction_score is not None else None,
                "play_count": ti.play_count,
                "skip_count": ti.skip_count,
                "like_count": ti.like_count,
                "dislike_count": ti.dislike_count,
                "repeat_count": ti.repeat_count,
                "avg_completion": round(ti.avg_completion, 3) if ti.avg_completion is not None else None,
                "first_played_at": ti.first_played_at,
                "last_played_at": ti.last_played_at,
                # Track metadata (if analyzed)
                "file_path": tf.file_path if tf else None,
                "bpm": tf.bpm if tf else None,
                "key": tf.key if tf else None,
                "mode": tf.mode if tf else None,
                "energy": tf.energy if tf else None,
                "mood_tags": tf.mood_tags if tf else None,
                "duration": tf.duration if tf else None,
            }
            for ti, tf in rows
        ],
    }


# ---------------------------------------------------------------------------
# Listening sessions
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/sessions", summary="Get user listening sessions")
async def get_user_sessions(
    user_id: str,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns materialised listening sessions for a user, most recent first."""
    await _resolve_user(session, user_id)

    count_q = select(func.count()).select_from(
        select(ListenSession.id).where(ListenSession.user_id == user_id).subquery()
    )
    total = (await session.execute(count_q)).scalar() or 0

    q = (
        select(ListenSession)
        .where(ListenSession.user_id == user_id)
        .order_by(ListenSession.started_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(q)).scalars().all()

    return {
        "total": total,
        "sessions": [
            {
                "id": s.id,
                "session_key": s.session_key,
                "started_at": s.started_at,
                "ended_at": s.ended_at,
                "duration_s": s.duration_s,
                "track_count": s.track_count,
                "play_count": s.play_count,
                "skip_count": s.skip_count,
                "like_count": s.like_count,
                "skip_rate": round(s.skip_rate, 3) if s.skip_rate is not None else None,
                "avg_completion": round(s.avg_completion, 3) if s.avg_completion is not None else None,
                "dominant_context_type": s.dominant_context_type,
                "dominant_device_type": s.dominant_device_type,
                "hour_of_day": s.hour_of_day,
                "day_of_week": s.day_of_week,
            }
            for s in rows
        ],
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@router.get("/users", summary="List all users")
async def list_users(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    q = (
        select(
            User,
            func.count(ListenEvent.id).label("event_count"),
        )
        .outerjoin(ListenEvent, User.user_id == ListenEvent.user_id)
        .group_by(User.id)
        .order_by(User.last_seen.desc().nullslast())
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(q)
    rows = result.all()
    return [
        {
            "uid": user.uid,
            "user_id": user.user_id,
            "display_name": user.display_name,
            "created_at": user.created_at,
            "last_seen": user.last_seen,
            "event_count": count,
        }
        for user, count in rows
    ]


@router.post("/users", response_model=UserResponse, status_code=201, summary="Register a user")
async def create_user(
    body: UserCreate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(select(User).where(User.user_id == body.user_id))
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="User already exists.")
    user = User(user_id=body.user_id, display_name=body.display_name)
    session.add(user)
    # Flush to get the auto-increment id assigned before response serialization.
    await session.flush()
    return user


@router.get("/users/{user_id}", response_model=UserResponse, summary="Get a user")
async def get_user(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    return await _resolve_user(session, user_id)


@router.patch(
    "/users/{uid}",
    response_model=UserResponse,
    summary="Update a user (rename / change display name)",
    description="""
Update a user's mutable fields by their stable numeric UID.

If `user_id` (the username) is changed, the new value is cascaded
to all related tables: listen_events, listen_sessions, and track_interactions.
The numeric UID never changes.
""",
)
async def update_user(
    uid: int,
    body: UserUpdate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    result = await session.execute(select(User).where(User.id == uid))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail=f"User with uid={uid} not found.")

    old_user_id = user.user_id

    # Update display_name if provided.
    if body.display_name is not None:
        user.display_name = body.display_name

    # Rename user_id with cascade.
    if body.user_id is not None and body.user_id != old_user_id:
        # Check uniqueness of the new user_id.
        conflict = await session.execute(
            select(User.id).where(User.user_id == body.user_id)
        )
        if conflict.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=409,
                detail=f"user_id '{body.user_id}' is already taken.",
            )

        # Cascade to all tables that store user_id as a string column.
        await session.execute(
            update(ListenEvent)
            .where(ListenEvent.user_id == old_user_id)
            .values(user_id=body.user_id)
        )
        await session.execute(
            update(ListenSession)
            .where(ListenSession.user_id == old_user_id)
            .values(user_id=body.user_id)
        )
        await session.execute(
            update(TrackInteraction)
            .where(TrackInteraction.user_id == old_user_id)
            .values(user_id=body.user_id)
        )

        user.user_id = body.user_id
        logger.info(
            "User renamed",
            extra={"uid": uid, "old_user_id": old_user_id, "new_user_id": body.user_id},
        )

    return user
