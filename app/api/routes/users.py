"""GrooveIQ – User management routes."""
from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, ListenSession, TrackFeatures, TrackInteraction, User
from app.models.schemas import OnboardingRequest, OnboardingResponse, UserCreate, UserResponse, UserUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolve_user(
    session: AsyncSession,
    user_id: str,
    api_key: str = "anonymous",
) -> User:
    """Look up a user by user_id string.  Raises 404 if not found.

    When ``API_KEY_USERS`` is configured, also verifies that the
    requesting API key is authorised to access this user (raises 403).
    """
    check_user_access(api_key, user_id)
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
    user = await _resolve_user(session, user_id, _key)
    response = {
        "uid": user.uid,
        "user_id": user.user_id,
        "display_name": user.display_name,
        "profile_updated_at": user.profile_updated_at,
        "taste_profile": user.taste_profile,
    }
    # Include Last.fm data if the user has a linked account
    if user.lastfm_username:
        response["lastfm"] = {
            "username": user.lastfm_username,
            "scrobbling_enabled": user.lastfm_session_key is not None,
            "synced_at": user.lastfm_synced_at,
            "profile": user.lastfm_cache,
        }
    return response


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

@router.post(
    "/users/{user_id}/onboarding",
    response_model=OnboardingResponse,
    summary="Submit onboarding preferences",
    description="""
Submit explicit user preferences for cold-start recommendation seeding.

Preferences are stored on the user and blended into the taste profile.
Subsequent calls overwrite previous onboarding data (full replace).
As real listening data accumulates, onboarding influence fades via decay.
""",
)
async def submit_onboarding(
    user_id: str,
    body: OnboardingRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    user = await _resolve_user(session, user_id, _key)

    import time as _time
    from sqlalchemy import or_

    prefs: dict = {}
    prefs_saved = 0
    matched_tracks = 0
    matched_artists = 0

    # --- Match favourite tracks against library ---
    if body.favourite_tracks:
        result = await session.execute(
            select(TrackFeatures.track_id).where(or_(
                TrackFeatures.track_id.in_(body.favourite_tracks),
                TrackFeatures.external_track_id.in_(body.favourite_tracks),
            ))
        )
        found = {r[0] for r in result.all()}
        prefs["favourite_tracks"] = list(found) if found else body.favourite_tracks
        matched_tracks = len(found)
        prefs_saved += 1

    # --- Match favourite artists against library ---
    if body.favourite_artists:
        # Case-insensitive substring match against artist column
        artist_ids: list[str] = []
        for artist_name in body.favourite_artists:
            result = await session.execute(
                select(TrackFeatures.track_id, TrackFeatures.artist)
                .where(func.lower(TrackFeatures.artist).contains(artist_name.lower()))
                .limit(1)
            )
            if result.first():
                matched_artists += 1
        prefs["favourite_artists"] = body.favourite_artists
        prefs_saved += 1

    # --- Store remaining preferences ---
    if body.favourite_genres:
        prefs["favourite_genres"] = body.favourite_genres
        prefs_saved += 1
    if body.mood_preferences:
        prefs["mood_preferences"] = body.mood_preferences
        prefs_saved += 1
    if body.listening_contexts:
        prefs["listening_contexts"] = body.listening_contexts
        prefs_saved += 1
    if body.device_types:
        prefs["device_types"] = body.device_types
        prefs_saved += 1
    if body.energy_preference is not None:
        prefs["energy_preference"] = body.energy_preference
        prefs_saved += 1
    if body.danceability_preference is not None:
        prefs["danceability_preference"] = body.danceability_preference
        prefs_saved += 1

    prefs["submitted_at"] = int(_time.time())

    user.onboarding_preferences = prefs
    await session.flush()

    # --- Trigger a taste profile seed if user has no interactions yet ---
    profile_seeded = False
    interaction_count = (await session.execute(
        select(func.count()).select_from(
            select(TrackInteraction.id)
            .where(TrackInteraction.user_id == user_id)
            .subquery()
        )
    )).scalar() or 0

    if interaction_count == 0:
        from app.services.taste_profile import build_seed_profile
        seed = await build_seed_profile(session, user)
        if seed:
            user.taste_profile = seed
            user.profile_updated_at = int(_time.time())
            profile_seeded = True

    await session.commit()

    return OnboardingResponse(
        user_id=user_id,
        preferences_saved=prefs_saved,
        matched_tracks=matched_tracks,
        matched_artists=matched_artists,
        profile_seeded=profile_seeded,
    )


@router.get(
    "/users/{user_id}/onboarding",
    summary="Get onboarding preferences",
)
async def get_onboarding(
    user_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns the user's stored onboarding preferences, or null if not submitted."""
    user = await _resolve_user(session, user_id, _key)
    return {
        "user_id": user_id,
        "onboarding_preferences": user.onboarding_preferences,
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
    await _resolve_user(session, user_id, _key)

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

    from sqlalchemy import or_
    q = (
        select(TrackInteraction, TrackFeatures)
        .outerjoin(TrackFeatures, or_(
            TrackInteraction.track_id == TrackFeatures.track_id,
            TrackInteraction.track_id == TrackFeatures.external_track_id,
        ))
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
                "title": tf.title if tf else None,
                "artist": tf.artist if tf else None,
                "album": tf.album if tf else None,
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
# Listening history
# ---------------------------------------------------------------------------

@router.get("/users/{user_id}/history", summary="Get user listening history")
async def get_user_history(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns a chronological list of every track the user started playing,
    enriched with track metadata and completion info from matching play_end events."""
    await _resolve_user(session, user_id, _key)

    from sqlalchemy import or_

    # Count total play_start events for this user
    count_q = select(func.count()).select_from(
        select(ListenEvent.id).where(
            ListenEvent.user_id == user_id,
            ListenEvent.event_type == "play_start",
        ).subquery()
    )
    total = (await session.execute(count_q)).scalar() or 0

    # Fetch play_start events, most recent first
    q = (
        select(ListenEvent)
        .where(
            ListenEvent.user_id == user_id,
            ListenEvent.event_type == "play_start",
        )
        .order_by(ListenEvent.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    starts = (await session.execute(q)).scalars().all()

    if not starts:
        return {"total": total, "history": []}

    # Collect track IDs and find matching play_end events
    track_ids = list({s.track_id for s in starts})

    # Fetch track metadata
    tf_q = select(TrackFeatures).where(or_(
        TrackFeatures.track_id.in_(track_ids),
        TrackFeatures.external_track_id.in_(track_ids),
    ))
    tf_rows = (await session.execute(tf_q)).scalars().all()
    tf_map = {}
    for tf in tf_rows:
        tf_map[tf.track_id] = tf
        if tf.external_track_id:
            tf_map[tf.external_track_id] = tf

    # For each play_start, find the closest subsequent play_end for the same
    # (user, track) to get completion/dwell info.
    # Strategy: match by session_id+track_id when available, fall back to
    # closest play_end after the play_start timestamp for the same track.
    session_ids = list({s.session_id for s in starts if s.session_id})
    end_by_session = {}  # (session_id, track_id) → play_end
    if session_ids:
        end_q = (
            select(ListenEvent)
            .where(
                ListenEvent.user_id == user_id,
                ListenEvent.event_type == "play_end",
                ListenEvent.session_id.in_(session_ids),
            )
        )
        ends = (await session.execute(end_q)).scalars().all()
        for e in ends:
            key = (e.session_id, e.track_id)
            if key not in end_by_session or e.timestamp > end_by_session[key].timestamp:
                end_by_session[key] = e

    # Fallback: fetch all play_end events for these tracks (for starts without session_id)
    # Build a map of track_id → list of play_end events sorted by timestamp
    end_by_track = {}  # track_id → [play_end events sorted by timestamp]
    needs_fallback = any(not s.session_id for s in starts)
    if needs_fallback:
        fb_q = (
            select(ListenEvent)
            .where(
                ListenEvent.user_id == user_id,
                ListenEvent.event_type == "play_end",
                ListenEvent.track_id.in_(track_ids),
            )
            .order_by(ListenEvent.timestamp)
        )
        fb_ends = (await session.execute(fb_q)).scalars().all()
        for e in fb_ends:
            end_by_track.setdefault(e.track_id, []).append(e)

    history = []
    for s in starts:
        tf = tf_map.get(s.track_id)
        # Prefer session-based match, fall back to closest play_end after this play_start
        end = None
        if s.session_id:
            end = end_by_session.get((s.session_id, s.track_id))
        if not end:
            candidates = end_by_track.get(s.track_id, [])
            # Find the first play_end after this play_start (list is sorted by timestamp)
            for c in candidates:
                if c.timestamp >= s.timestamp:
                    end = c
                    break

        entry = {
            "track_id": s.track_id,
            "timestamp": s.timestamp,
            "artist": tf.artist if tf else None,
            "title": tf.title if tf else None,
            "album": tf.album if tf else None,
            "duration": tf.duration if tf else None,
            "device_type": s.device_type,
            "output_type": s.output_type,
            "shuffle": s.shuffle,
            # Completion info from matching play_end
            "completion": round(end.value, 4) if end and end.value is not None else None,
            "dwell_ms": end.dwell_ms if end else None,
            "reason_end": end.reason_end if end else None,
        }
        history.append(entry)

    return {"total": total, "history": history}


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
    await _resolve_user(session, user_id, _key)

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
    from app.core.security import require_admin
    require_admin(_key)
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
    return await _resolve_user(session, user_id, _key)


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
        raise HTTPException(status_code=404, detail="Not found.")

    check_user_access(_key, user.user_id)

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

        from app.core.audit import audit_log
        audit_log("user_rename", api_key=_key, detail={
            "uid": uid,
            "old_user_id": old_user_id,
            "new_user_id": body.user_id,
        })

    return user
