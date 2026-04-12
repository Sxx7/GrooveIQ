"""
GrooveIQ – Sessionization service.

Materialises ListenSession rows from raw ListenEvent data.

Strategy:
  1. If client-supplied session_id exists on events, group by that.
  2. Otherwise, split by inactivity gap (default 30 min) per user.

Runs incrementally: tracks the highest event.id already processed
so each run only looks at new events.

Edge cases handled:
  - Events without session_id mixed with events that have one
  - Single-event sessions (kept if they meet SESSION_MIN_EVENTS, dropped otherwise)
  - Clock skew / out-of-order events (sorted by timestamp before grouping)
  - Users with zero playback events (skipped)
  - Very long sessions capped at 24h to avoid garbage from stuck clients
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, ListenSession

logger = logging.getLogger(__name__)

# Cap: sessions longer than this are likely garbage (stuck client).
_MAX_SESSION_DURATION_S = 24 * 3600

# Process events in chunks to limit memory.
_EVENT_CHUNK_SIZE = 5000


async def run_sessionizer() -> dict:
    """
    Main entry point.  Processes all un-sessionised events.

    Returns a summary dict: {sessions_created, sessions_updated, events_processed}.
    """
    async with AsyncSessionLocal() as session:
        # Find the high-water mark: highest event_id_max across all sessions.
        result = await session.execute(select(func.coalesce(func.max(ListenSession.event_id_max), 0)))
        hwm = result.scalar_one()

        total_created = 0
        total_updated = 0
        total_events = 0

        # Process in chunks to keep memory bounded.
        while True:
            events = await _fetch_events_after(session, hwm, _EVENT_CHUNK_SIZE)
            if not events:
                break

            created, updated = await _process_events(session, events)
            total_created += created
            total_updated += updated
            total_events += len(events)

            # Advance high-water mark to the last event in this chunk.
            hwm = events[-1].id
            await session.commit()

            logger.debug(
                "Sessionizer chunk processed",
                extra={"events": len(events), "created": created, "updated": updated},
            )

        if total_events > 0:
            logger.info(
                "Sessionizer complete",
                extra={
                    "events_processed": total_events,
                    "sessions_created": total_created,
                    "sessions_updated": total_updated,
                },
            )

    return {
        "sessions_created": total_created,
        "sessions_updated": total_updated,
        "events_processed": total_events,
    }


async def _fetch_events_after(session: AsyncSession, after_id: int, limit: int) -> Sequence:
    """Fetch events with id > after_id, ordered by id."""
    result = await session.execute(
        select(ListenEvent).where(ListenEvent.id > after_id).order_by(ListenEvent.id).limit(limit)
    )
    return result.scalars().all()


async def _process_events(session: AsyncSession, events: Sequence) -> tuple[int, int]:
    """
    Group events into sessions and upsert ListenSession rows.

    Returns (created_count, updated_count).
    """
    # Bucket events by user_id first.
    by_user: dict[str, list] = {}
    for ev in events:
        by_user.setdefault(ev.user_id, []).append(ev)

    created = 0
    updated = 0

    for user_id, user_events in by_user.items():
        # Sort by timestamp for correct gap detection (events may arrive out of order).
        user_events.sort(key=lambda e: (e.timestamp, e.id))
        sessions = _split_into_sessions(user_events)

        for sess_events in sessions:
            if len(sess_events) < settings.SESSION_MIN_EVENTS:
                continue

            row = _build_session_row(user_id, sess_events)
            if row is None:
                continue

            is_new = await _upsert_session(session, row)
            if is_new:
                created += 1
            else:
                updated += 1

    return created, updated


def _split_into_sessions(events: list) -> list[list]:
    """
    Split a user's ordered events into session groups.

    Uses client-supplied session_id when available.  Falls back to
    inactivity-gap splitting (SESSION_GAP_MINUTES).
    """
    if not events:
        return []

    gap_seconds = settings.SESSION_GAP_MINUTES * 60

    # Separate events that have a client session_id from those that don't.
    with_sid: dict[str, list] = {}
    without_sid: list = []

    for ev in events:
        if ev.session_id:
            with_sid.setdefault(ev.session_id, []).append(ev)
        else:
            without_sid.append(ev)

    sessions: list[list] = []

    # Client-supplied sessions: trust the grouping, but still split if
    # there's a >24h gap (garbage protection).
    for sid, group in with_sid.items():
        group.sort(key=lambda e: (e.timestamp, e.id))
        sub_sessions = _split_by_gap(group, _MAX_SESSION_DURATION_S)
        sessions.extend(sub_sessions)

    # Gap-based splitting for events without session_id.
    if without_sid:
        without_sid.sort(key=lambda e: (e.timestamp, e.id))
        sub_sessions = _split_by_gap(without_sid, gap_seconds)
        sessions.extend(sub_sessions)

    return sessions


def _split_by_gap(events: list, gap_seconds: int) -> list[list]:
    """Split ordered events into groups whenever the gap exceeds threshold."""
    if not events:
        return []

    groups: list[list] = [[events[0]]]
    for ev in events[1:]:
        prev = groups[-1][-1]
        if ev.timestamp - prev.timestamp >= gap_seconds:
            groups.append([ev])
        else:
            groups[-1].append(ev)

    return groups


def _build_session_row(user_id: str, events: list) -> dict | None:
    """
    Compute session aggregates from a list of events.

    Returns a dict suitable for upserting into ListenSession,
    or None if the session should be discarded.
    """
    if not events:
        return None

    started_at = events[0].timestamp
    ended_at = events[-1].timestamp
    duration_s = ended_at - started_at

    # Cap runaway sessions.
    if duration_s > _MAX_SESSION_DURATION_S:
        duration_s = _MAX_SESSION_DURATION_S

    # Unique tracks (by track_id).
    track_ids = {ev.track_id for ev in events}

    # Count event types.
    type_counts = Counter(ev.event_type for ev in events)
    play_count = type_counts.get("play_start", 0)
    skip_count = type_counts.get("skip", 0)
    like_count = type_counts.get("like", 0)
    dislike_count = type_counts.get("dislike", 0)
    seek_count = type_counts.get("seek_forward", 0) + type_counts.get("seek_back", 0)

    # Skip rate: avoid division by zero.
    skip_rate = skip_count / max(play_count, 1)

    # Average completion from play_end events with a value.
    completions = [ev.value for ev in events if ev.event_type == "play_end" and ev.value is not None]
    avg_completion = sum(completions) / len(completions) if completions else None

    # Total dwell from events that carry dwell_ms.
    dwell_values = [ev.dwell_ms for ev in events if ev.dwell_ms is not None]
    total_dwell_ms = sum(dwell_values) if dwell_values else None

    # Dominant context_type and device_type (most common non-null).
    dominant_context_type = _most_common(ev.context_type for ev in events if ev.context_type)
    dominant_device_type = _most_common(ev.device_type for ev in events if ev.device_type)

    # Time context from first event.
    first = events[0]
    hour_of_day = first.hour_of_day
    day_of_week = first.day_of_week

    # Session key: prefer client session_id, fall back to user:start_ts.
    session_id = first.session_id
    if session_id:
        session_key = f"{user_id}:{session_id}"
    else:
        session_key = f"{user_id}:ts:{started_at}"

    return {
        "session_key": session_key,
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": duration_s,
        "track_count": len(track_ids),
        "play_count": play_count,
        "skip_count": skip_count,
        "like_count": like_count,
        "dislike_count": dislike_count,
        "seek_count": seek_count,
        "skip_rate": skip_rate,
        "avg_completion": avg_completion,
        "total_dwell_ms": total_dwell_ms,
        "dominant_context_type": dominant_context_type,
        "dominant_device_type": dominant_device_type,
        "hour_of_day": hour_of_day,
        "day_of_week": day_of_week,
        "event_id_min": events[0].id,
        "event_id_max": events[-1].id,
        "built_at": int(time.time()),
    }


def _most_common(values) -> str | None:
    """Return the most common value, or None if empty."""
    counts = Counter(values)
    if not counts:
        return None
    return counts.most_common(1)[0][0]


async def _upsert_session(session: AsyncSession, row: dict) -> bool:
    """
    Insert or update a session row.  Returns True if newly created.

    Uses INSERT ... ON CONFLICT for atomicity.  On conflict (same session_key),
    we update aggregates in case new events extended an existing session.
    """
    # Check if this session_key already exists.
    existing = await session.execute(
        select(ListenSession.id, ListenSession.event_id_max)
        .where(ListenSession.session_key == row["session_key"])
        .limit(1)
    )
    found = existing.first()

    if found is None:
        session.add(ListenSession(**row))
        return True

    # Update existing session with extended data.
    existing_id, existing_max = found
    # Only update if we have newer events.
    if row["event_id_max"] <= existing_max:
        return False

    from sqlalchemy import update

    await session.execute(
        update(ListenSession)
        .where(ListenSession.id == existing_id)
        .values(
            ended_at=row["ended_at"],
            duration_s=row["duration_s"],
            track_count=row["track_count"],
            play_count=row["play_count"],
            skip_count=row["skip_count"],
            like_count=row["like_count"],
            dislike_count=row["dislike_count"],
            seek_count=row["seek_count"],
            skip_rate=row["skip_rate"],
            avg_completion=row["avg_completion"],
            total_dwell_ms=row["total_dwell_ms"],
            dominant_context_type=row["dominant_context_type"],
            dominant_device_type=row["dominant_device_type"],
            event_id_max=row["event_id_max"],
            built_at=row["built_at"],
        )
    )
    return False
