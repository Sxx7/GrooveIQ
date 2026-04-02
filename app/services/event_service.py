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
    cutoff = int(time.time()) - _DEDUP_WINDOW_SECONDS
    dup_q = (
        select(ListenEvent.id)
        .where(
            ListenEvent.user_id    == event.user_id,
            ListenEvent.track_id   == event.track_id,
            ListenEvent.event_type == event.event_type,
            ListenEvent.timestamp  >= cutoff,
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
    )
    session.add(row)
    # Commit is handled by the get_session dependency on request close.

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
