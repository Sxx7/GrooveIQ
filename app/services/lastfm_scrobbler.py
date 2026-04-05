"""
GrooveIQ – Last.fm scrobbler service.

Two responsibilities:
1. Event hook — called from event_service after persisting an event.
   Enqueues qualifying play_end events for scrobbling; fires now-playing
   on play_start (best-effort).
2. Background worker — processes the scrobble queue in batches.

Scrobble criteria (matching Last.fm rules):
- Track duration > 30 seconds
- Listened >= 50% of duration OR >= 240 seconds (4 min)
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ScrobbleQueue, TrackFeatures, User

logger = logging.getLogger(__name__)

_MIN_DURATION_S = 30
_MIN_LISTEN_MS = 240_000  # 4 minutes


async def on_event(
    session: AsyncSession,
    event,  # EventCreate
    persisted_event,  # ListenEvent
) -> None:
    """
    Hook called from event_service.process_event() after persisting.

    - play_start → fire now-playing (best-effort, no DB write)
    - play_end   → check scrobble criteria, enqueue if qualifying
    """
    if not settings.lastfm_user_enabled or not settings.LASTFM_SCROBBLE_ENABLED:
        return

    # Look up user's Last.fm session key
    user = (await session.execute(
        select(User).where(User.user_id == event.user_id)
    )).scalar_one_or_none()
    if not user or not user.lastfm_session_key:
        return

    # Look up track metadata (need artist + title for scrobbling)
    track = (await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == event.track_id)
    )).scalar_one_or_none()
    # Also try external_track_id
    if track is None:
        track = (await session.execute(
            select(TrackFeatures).where(TrackFeatures.external_track_id == event.track_id)
        )).scalar_one_or_none()
    if not track or not track.artist or not track.title:
        logger.debug("Scrobble skip: no artist/title for track %s", event.track_id)
        return

    if event.event_type == "play_start":
        await _fire_now_playing(user, track)

    elif event.event_type == "play_end":
        await _enqueue_scrobble(session, event, user, track)


async def _fire_now_playing(user, track) -> None:
    """Best-effort now-playing update. Failures are silently logged."""
    try:
        from app.services.lastfm_client import decrypt_session_key, get_lastfm_client

        sk = decrypt_session_key(user.lastfm_session_key)
        client = get_lastfm_client()
        duration_s = int(track.duration) if track.duration else None
        await client.update_now_playing(
            session_key=sk,
            artist=track.artist,
            track=track.title,
            album=track.album,
            duration=duration_s,
        )
    except Exception as e:
        logger.debug("Now-playing failed for user=%s: %s", user.user_id, e)


async def _enqueue_scrobble(session: AsyncSession, event, user, track) -> None:
    """Check scrobble criteria and add to queue if qualifying."""
    duration_s = track.duration  # float seconds from Essentia
    if duration_s is None and track.duration_ms:
        duration_s = track.duration_ms / 1000.0

    # Rule 1: track must be > 30 seconds
    if duration_s is not None and duration_s < _MIN_DURATION_S:
        return

    # Rule 2: must have listened >= 50% OR >= 4 minutes
    qualifies = False
    if event.value is not None and event.value >= 0.5:
        qualifies = True
    if event.dwell_ms is not None and event.dwell_ms >= _MIN_LISTEN_MS:
        qualifies = True
    # If we have both duration and dwell, check 50% precisely
    if (event.dwell_ms is not None and duration_s is not None
            and event.dwell_ms >= duration_s * 500):  # duration_s * 1000 * 0.5
        qualifies = True
    # Fallback: play_end events that passed the noise filter (MIN_PLAY_PERCENTAGE)
    # but carry no completion/dwell data — assume the client confirmed a valid listen.
    if not qualifies and event.value is None and event.dwell_ms is None:
        qualifies = True
    if not qualifies:
        return

    session.add(ScrobbleQueue(
        user_id=user.user_id,
        track_id=event.track_id,
        artist=track.artist,
        track_title=track.title,
        album=track.album,
        duration_s=int(duration_s) if duration_s else None,
        timestamp=event.timestamp,
    ))
    logger.debug("Scrobble enqueued: %s - %s for user %s", track.artist, track.title, user.user_id)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

async def process_scrobble_queue() -> dict:
    """
    Process pending scrobbles.  Groups by user, batches up to 50 per API call.
    Returns summary dict.
    """
    from app.services.lastfm_client import decrypt_session_key, get_lastfm_client, LastFmError

    processed = 0
    failed = 0

    async with AsyncSessionLocal() as session:
        # Fetch all pending items, grouped by user
        pending = (await session.execute(
            select(ScrobbleQueue)
            .where(ScrobbleQueue.status == "pending")
            .order_by(ScrobbleQueue.user_id, ScrobbleQueue.timestamp)
            .limit(500)
        )).scalars().all()

        if not pending:
            return {"processed": 0, "failed": 0}

        # Group by user
        by_user: dict[str, list] = {}
        for item in pending:
            by_user.setdefault(item.user_id, []).append(item)

        client = get_lastfm_client()

        users_to_refresh: list[User] = []

        for user_id, items in by_user.items():
            # Get session key for this user
            user = (await session.execute(
                select(User).where(User.user_id == user_id)
            )).scalar_one_or_none()
            if not user or not user.lastfm_session_key:
                # No session key — mark all as failed
                for item in items:
                    item.status = "failed"
                    item.last_error = "No Last.fm session key"
                    failed += len(items)
                continue

            try:
                sk = decrypt_session_key(user.lastfm_session_key)
            except Exception as e:
                for item in items:
                    item.status = "failed"
                    item.last_error = f"Decryption error: {e}"
                failed += len(items)
                continue

            user_sent = False
            # Process in batches of 50
            for batch_start in range(0, len(items), 50):
                batch = items[batch_start:batch_start + 50]
                tracks = [
                    {
                        "artist": item.artist,
                        "track": item.track_title,
                        "timestamp": item.timestamp,
                        "album": item.album,
                        "duration": item.duration_s,
                    }
                    for item in batch
                ]

                try:
                    await client.scrobble(session_key=sk, tracks=tracks)
                    for item in batch:
                        item.status = "sent"
                    processed += len(batch)
                    user_sent = True
                except LastFmError as e:
                    for item in batch:
                        item.attempts += 1
                        item.last_error = str(e)
                        if item.attempts >= 3:
                            item.status = "failed"
                            failed += 1
                except Exception as e:
                    for item in batch:
                        item.attempts += 1
                        item.last_error = str(e)
                        if item.attempts >= 3:
                            item.status = "failed"
                            failed += 1
                    logger.warning("Scrobble batch failed for user=%s: %s", user_id, e)

            if user_sent:
                users_to_refresh.append(user)

        await session.commit()

    # After committing scrobbles, refresh recent tracks for affected users
    if users_to_refresh:
        await _refresh_recent_tracks(users_to_refresh)

    # Clean up sent items older than 7 days
    cutoff = int(time.time()) - 7 * 86_400
    async with AsyncSessionLocal() as session:
        await session.execute(
            delete(ScrobbleQueue).where(
                ScrobbleQueue.status == "sent",
                ScrobbleQueue.created_at < cutoff,
            )
        )
        await session.commit()

    return {"processed": processed, "failed": failed}


async def _refresh_recent_tracks(users: list) -> None:
    """Fetch recent tracks from Last.fm for users who just had scrobbles sent."""
    from app.services.lastfm_client import get_lastfm_client

    client = get_lastfm_client()

    async with AsyncSessionLocal() as session:
        for user in users:
            if not user.lastfm_username:
                continue
            try:
                recent = await client.get_recent_tracks(user.lastfm_username, limit=50)
                user_row = (await session.execute(
                    select(User).where(User.user_id == user.user_id)
                )).scalar_one_or_none()
                if user_row:
                    cache = dict(user_row.lastfm_cache) if user_row.lastfm_cache else {}
                    cache["recent_tracks"] = recent
                    cache["recent_tracks_fetched_at"] = int(time.time())
                    user_row.lastfm_cache = cache
            except Exception as e:
                logger.debug("Recent tracks refresh failed for %s: %s", user.user_id, e)

        await session.commit()
