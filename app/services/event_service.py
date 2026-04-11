"""
GrooveIQ – Event processing service.

Handles:
- Duplicate detection (same user+track+type within a short window)
- Noise filtering (play_end with negligible completion, etc.)
- Upsert of user record (last_seen)
- Persistence to listen_events table
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import ListenEvent, User
from app.models.schemas import EventCreate, EventResponse

logger = logging.getLogger(__name__)

# Dedup window: ignore identical (user, track, type) within this many seconds
_DEDUP_WINDOW_SECONDS = 2


async def process_event(
    session: AsyncSession,
    event: EventCreate,
) -> EventResponse:
    """
    Validate, filter, and persist a single event.
    Raises ValueError for events that should be rejected.
    """

    # ------------------------------------------------------------------
    # 1. Noise filtering
    # ------------------------------------------------------------------
    if event.event_type == "play_end":
        if event.value is not None and event.value < settings.MIN_PLAY_PERCENTAGE:
            # Accidental tap / autoplay that user immediately stopped
            logger.debug(
                "Dropping low-completion play_end",
                extra={"track_id": event.track_id, "value": event.value},
            )
            return EventResponse(accepted=0, rejected=1, errors=["play_end completion below threshold"])

    # ------------------------------------------------------------------
    # 2. Duplicate detection
    # ------------------------------------------------------------------
    # Compare against the *event's* timestamp, not the server clock.
    # This catches retries where the client re-sends the same event.
    event_ts = event.timestamp if event.timestamp else int(time.time())
    ts_lo = event_ts - _DEDUP_WINDOW_SECONDS
    ts_hi = event_ts + _DEDUP_WINDOW_SECONDS
    dup_q = (
        select(ListenEvent.id)
        .where(
            ListenEvent.user_id    == event.user_id,
            ListenEvent.track_id   == event.track_id,
            ListenEvent.event_type == event.event_type,
            ListenEvent.timestamp  >= ts_lo,
            ListenEvent.timestamp  <= ts_hi,
        )
        .limit(1)
    )
    dup_result = await session.execute(dup_q)
    if dup_result.scalar_one_or_none() is not None:
        logger.debug("Duplicate event dropped within dedup window")
        return EventResponse(accepted=1, rejected=0)   # silent accept (idempotent)

    # ------------------------------------------------------------------
    # 3. Ensure user exists
    # ------------------------------------------------------------------
    await _upsert_user(session, event.user_id, int(time.time()))

    # ------------------------------------------------------------------
    # 4. Persist
    # ------------------------------------------------------------------
    row = ListenEvent(
        user_id    = event.user_id,
        track_id   = event.track_id,
        event_type = event.event_type,
        value      = event.value,
        context    = event.context,
        client_id  = event.client_id,
        session_id = event.session_id,
        timestamp  = event.timestamp,
        # Rich signals
        surface           = event.surface,
        position          = event.position,
        request_id        = event.request_id,
        model_version     = event.model_version,
        session_position  = event.session_position,
        dwell_ms          = event.dwell_ms,
        pause_duration_ms = event.pause_duration_ms,
        num_seekfwd       = event.num_seekfwd,
        num_seekbk        = event.num_seekbk,
        shuffle           = event.shuffle,
        context_type      = event.context_type,
        context_id        = event.context_id,
        context_switch    = event.context_switch,
        reason_start      = event.reason_start,
        reason_end        = event.reason_end,
        device_id         = event.device_id,
        device_type       = event.device_type,
        # Local time context
        hour_of_day       = event.hour_of_day,
        day_of_week       = event.day_of_week,
        timezone          = event.timezone,
        # Audio output
        output_type          = event.output_type,
        output_device_name   = event.output_device_name,
        bluetooth_connected  = event.bluetooth_connected,
        # Location
        latitude          = event.latitude,
        longitude         = event.longitude,
        location_label    = event.location_label,
    )
    session.add(row)
    # Commit is handled by the get_session dependency on request close.

    # Fire radio session feedback hook (best-effort).
    # Updates the drift embedding in real-time when events arrive for active radio sessions.
    if event.context_type == "radio" and event.context_id:
        _radio_event_types = {"skip", "like", "dislike"}
        if event.event_type in _radio_event_types:
            try:
                from app.services.radio import record_feedback
                record_feedback(event.context_id, event.track_id, event.event_type)
            except Exception:
                logger.debug("Radio feedback hook error", exc_info=True)

    # Fire Last.fm scrobble/now-playing hook (best-effort, non-blocking).
    # Enqueue is a DB insert — the actual HTTP call happens in the background worker.
    if settings.lastfm_user_enabled and settings.LASTFM_SCROBBLE_ENABLED:
        try:
            from app.services.lastfm_scrobbler import on_event
            await on_event(session, event, row)
        except Exception:
            logger.debug("Last.fm scrobble hook error", exc_info=True)

    logger.debug(
        "Event accepted",
        extra={
            "user_id": event.user_id,
            "track_id": event.track_id,
            "event_type": event.event_type,
        },
    )

    return EventResponse(accepted=1, rejected=0)


async def _upsert_user(session: AsyncSession, user_id: str, now: int) -> None:
    """Create user record on first event, update last_seen otherwise."""
    result = await session.execute(
        select(User).where(User.user_id == user_id).limit(1)
    )
    user = result.scalar_one_or_none()
    if user is None:
        session.add(User(user_id=user_id, last_seen=now))
    else:
        await session.execute(
            update(User).where(User.user_id == user_id).values(last_seen=now)
        )
