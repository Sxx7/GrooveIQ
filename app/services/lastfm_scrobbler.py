"""
GrooveIQ – Last.fm scrobbler service.

Two responsibilities:
1. Event hook — called from event_service after persisting an event.
   Enqueues qualifying play_end events for scrobbling; fires now-playing
   on play_start (best-effort).
2. Background worker — processes the scrobble queue in batches.
3. Backfill — retroactively scrobble past play_end events that were missed.

Scrobble criteria (matching Last.fm rules):
- Track duration > 30 seconds
- Listened >= 50% of duration OR >= 240 seconds (4 min)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, ScrobbleQueue, TrackFeatures, User

logger = logging.getLogger(__name__)

_MIN_DURATION_S = 30
_MIN_LISTEN_MS = 240_000  # 4 minutes


# ---------------------------------------------------------------------------
# Track metadata resolution — multi-strategy lookup
# ---------------------------------------------------------------------------

@dataclass
class _TrackMeta:
    """Minimal track metadata needed for scrobbling."""
    artist: str
    title: str
    album: Optional[str] = None
    duration_s: Optional[float] = None


async def _resolve_track_meta(
    session: AsyncSession,
    track_id: str,
) -> Optional[_TrackMeta]:
    """
    Resolve artist/title for a track_id using multiple strategies:
      1. TrackFeatures.track_id (direct match after media server sync)
      2. TrackFeatures.external_track_id (pre-sync hash-based ID)
      3. Previously successful ScrobbleQueue entry for the same track_id

    Returns None if no metadata can be found.
    """
    # Strategy 1: direct track_id match
    track = (await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == track_id)
    )).scalar_one_or_none()

    # Strategy 2: external_track_id match
    if track is None:
        track = (await session.execute(
            select(TrackFeatures).where(TrackFeatures.external_track_id == track_id)
        )).scalar_one_or_none()

    if track and track.artist and track.title:
        duration_s = track.duration
        if duration_s is None and track.duration_ms:
            duration_s = track.duration_ms / 1000.0
        return _TrackMeta(
            artist=track.artist,
            title=track.title,
            album=track.album,
            duration_s=duration_s,
        )

    # Strategy 3: reuse metadata from a previous scrobble queue entry
    prev = (await session.execute(
        select(ScrobbleQueue.artist, ScrobbleQueue.track_title, ScrobbleQueue.album, ScrobbleQueue.duration_s)
        .where(ScrobbleQueue.track_id == track_id)
        .order_by(ScrobbleQueue.id.desc())
        .limit(1)
    )).first()
    if prev and prev.artist and prev.track_title:
        return _TrackMeta(
            artist=prev.artist,
            title=prev.track_title,
            album=prev.album,
            duration_s=float(prev.duration_s) if prev.duration_s else None,
        )

    return None


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

    # Resolve track metadata (multi-strategy lookup)
    meta = await _resolve_track_meta(session, event.track_id)
    if not meta:
        logger.warning("Scrobble skip: no metadata for track_id=%s (not in track_features, "
                        "run library sync?)", event.track_id)
        return

    if event.event_type == "play_start":
        await _fire_now_playing(user, meta)

    elif event.event_type == "play_end":
        # Use persisted event's timestamp (has DB default) not schema's (may be None)
        await _enqueue_scrobble(session, event, persisted_event, user, meta)


async def _fire_now_playing(user, meta: _TrackMeta) -> None:
    """Best-effort now-playing update. Failures are silently logged."""
    try:
        from app.services.lastfm_client import decrypt_session_key, get_lastfm_client

        sk = decrypt_session_key(user.lastfm_session_key)
        client = get_lastfm_client()
        duration = int(meta.duration_s) if meta.duration_s else None
        await client.update_now_playing(
            session_key=sk,
            artist=meta.artist,
            track=meta.title,
            album=meta.album,
            duration=duration,
        )
    except Exception as e:
        logger.debug("Now-playing failed for user=%s: %s", user.user_id, e)


async def _enqueue_scrobble(
    session: AsyncSession, event, persisted_event, user, meta: _TrackMeta,
) -> None:
    """Check scrobble criteria and add to queue if qualifying."""
    duration_s = meta.duration_s

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

    # Use the persisted event's timestamp (has DB default) — event.timestamp may be None
    ts = persisted_event.timestamp if persisted_event.timestamp else int(time.time())

    session.add(ScrobbleQueue(
        user_id=user.user_id,
        track_id=event.track_id,
        artist=meta.artist,
        track_title=meta.title,
        album=meta.album,
        duration_s=int(duration_s) if duration_s else None,
        timestamp=ts,
    ))
    logger.info("Scrobble enqueued: %s – %s for user %s", meta.artist, meta.title, user.user_id)


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
                logger.error("Session key decryption failed for user=%s: %s", user_id, e)
                for item in items:
                    item.status = "failed"
                    item.last_error = "Session key decryption failed"
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
                    logger.warning("Scrobble failed for user=%s: %s", user_id, e)
                    for item in batch:
                        item.attempts += 1
                        item.last_error = "Last.fm API error"
                        if item.attempts >= 3:
                            item.status = "failed"
                            failed += 1
                except Exception as e:
                    logger.warning("Scrobble batch failed for user=%s: %s", user_id, e)
                    for item in batch:
                        item.attempts += 1
                        item.last_error = "Scrobble submission failed"
                        if item.attempts >= 3:
                            item.status = "failed"
                            failed += 1

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


# ---------------------------------------------------------------------------
# Backfill — retroactively scrobble past play_end events
# ---------------------------------------------------------------------------

async def backfill_scrobbles(user_id: str) -> dict:
    """
    Scan past play_end events for a user and enqueue any that qualify
    for scrobbling but were missed (no matching ScrobbleQueue entry).

    Returns summary dict with enqueued/skipped/already_queued counts.
    """
    enqueued = 0
    skipped_no_meta = 0
    skipped_criteria = 0
    already_queued = 0

    async with AsyncSessionLocal() as session:
        # Verify user has Last.fm connected
        user = (await session.execute(
            select(User).where(User.user_id == user_id)
        )).scalar_one_or_none()
        if not user or not user.lastfm_session_key:
            return {"error": "User has no Last.fm session key"}

        # Get all play_end events for this user
        events = (await session.execute(
            select(ListenEvent)
            .where(
                ListenEvent.user_id == user_id,
                ListenEvent.event_type == "play_end",
            )
            .order_by(ListenEvent.timestamp)
        )).scalars().all()

        if not events:
            return {"enqueued": 0, "skipped_no_meta": 0, "skipped_criteria": 0,
                    "already_queued": 0, "total_play_ends": 0}

        # Get all track_ids already in the scrobble queue for this user
        existing = set(
            row[0] for row in (await session.execute(
                select(ScrobbleQueue.track_id, ScrobbleQueue.timestamp)
                .where(ScrobbleQueue.user_id == user_id)
            )).all()
        )
        # Build a set of (track_id, timestamp) for dedup
        existing_pairs = set(
            (row.track_id, row.timestamp) for row in (await session.execute(
                select(ScrobbleQueue.track_id, ScrobbleQueue.timestamp)
                .where(ScrobbleQueue.user_id == user_id)
            )).all()
        )

        for ev in events:
            ts = ev.timestamp or int(time.time())

            # Skip if already in queue
            if (ev.track_id, ts) in existing_pairs:
                already_queued += 1
                continue

            # Resolve metadata
            meta = await _resolve_track_meta(session, ev.track_id)
            if not meta:
                skipped_no_meta += 1
                continue

            duration_s = meta.duration_s

            # Check duration minimum
            if duration_s is not None and duration_s < _MIN_DURATION_S:
                skipped_criteria += 1
                continue

            # Check scrobble qualification criteria
            qualifies = False
            if ev.value is not None and ev.value >= 0.5:
                qualifies = True
            if ev.dwell_ms is not None and ev.dwell_ms >= _MIN_LISTEN_MS:
                qualifies = True
            if (ev.dwell_ms is not None and duration_s is not None
                    and ev.dwell_ms >= duration_s * 500):
                qualifies = True
            if not qualifies and ev.value is None and ev.dwell_ms is None:
                qualifies = True
            if not qualifies:
                skipped_criteria += 1
                continue

            session.add(ScrobbleQueue(
                user_id=user_id,
                track_id=ev.track_id,
                artist=meta.artist,
                track_title=meta.title,
                album=meta.album,
                duration_s=int(duration_s) if duration_s else None,
                timestamp=ts,
            ))
            enqueued += 1

        await session.commit()

    logger.info("Scrobble backfill for user=%s: enqueued=%d, skipped_no_meta=%d, "
                "skipped_criteria=%d, already_queued=%d, total_play_ends=%d",
                user_id, enqueued, skipped_no_meta, skipped_criteria,
                already_queued, len(events))

    return {
        "enqueued": enqueued,
        "skipped_no_meta": skipped_no_meta,
        "skipped_criteria": skipped_criteria,
        "already_queued": already_queued,
        "total_play_ends": len(events),
    }
