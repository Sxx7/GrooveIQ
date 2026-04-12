"""
GrooveIQ – Event ingestion routes.

POST /v1/events       – ingest a single event
POST /v1/events/batch – ingest up to 50 events
GET  /v1/events       – query events (admin/debug)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, check_user_event_rate, require_admin, require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent
from app.models.schemas import EventBatch, EventCreate, EventResponse, ListenEventRead
from app.services.event_service import process_event

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# POST /v1/events  – single event
# ---------------------------------------------------------------------------

@router.post(
    "/events",
    response_model=EventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a single listen event",
    description="""
Send a behavioral event from your music player.

**Event types and their `value` payload:**

| event_type   | value meaning              | example  |
|--------------|----------------------------|----------|
| play_end     | completion ratio (0–1)     | 0.94     |
| skip         | elapsed seconds at skip    | 12.4     |
| pause/resume | elapsed seconds            | 45.0     |
| rating       | star rating (1–5)          | 4        |
| seek_back    | seconds jumped backward    | 15.0     |
| seek_forward | seconds jumped forward     | 30.0     |
| volume_up/dn | new volume level (0–100)   | 80       |
| like/dislike | (no value needed)          | null     |
| reco_impression | (no value needed)     | null     |

For `reco_impression` events, the key fields are `request_id`, `surface`,
`position`, and `model_version` — not `value`.

All events also accept optional rich signal fields: `surface`, `position`,
`request_id`, `model_version`, `session_position`, `dwell_ms`,
`pause_duration_ms`, `num_seekfwd`, `num_seekbk`, `shuffle`,
`context_type`, `context_id`, `context_switch`, `reason_start`,
`reason_end`, `device_id`, `device_type`.

The server returns **202 Accepted** immediately. Events are processed
asynchronously in the background.
""",
)
async def ingest_event(
    event: EventCreate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    check_user_access(_key, event.user_id)
    check_user_event_rate(event.user_id)
    result = await process_event(session, event)
    return result


# ---------------------------------------------------------------------------
# POST /v1/events/batch  – up to N events
# ---------------------------------------------------------------------------

@router.post(
    "/events/batch",
    response_model=EventResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a batch of events (up to 50)",
    description="""
Send multiple events in a single HTTP request.  Recommended for clients
that buffer events locally and flush periodically (e.g. every 30 s).

Maximum batch size is configurable via `EVENT_BATCH_MAX` (default: 50).

Each event is validated independently. The response reports how many
were accepted vs rejected, with per-event error messages for rejected ones.
""",
)
async def ingest_event_batch(
    batch: EventBatch,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Count events per user so we can rate-limit accurately.
    user_event_counts: dict[str, int] = {}
    for event in batch.events:
        user_event_counts[event.user_id] = user_event_counts.get(event.user_id, 0) + 1

    # Check authorization and rate limit for each user with their full
    # batch count (not just 1 hit per unique user).
    for uid, count in user_event_counts.items():
        check_user_access(_key, uid)
        check_user_event_rate(uid, count=count)

    accepted = 0
    rejected = 0
    errors: list[str] = []

    for i, event in enumerate(batch.events):
        try:
            await process_event(session, event)
            accepted += 1
        except Exception as e:
            rejected += 1
            errors.append(f"Event[{i}]: rejected")
            logger.warning("Batch event rejected", extra={"error": str(e), "index": i, "track_id": event.track_id})

    return EventResponse(accepted=accepted, rejected=rejected, errors=errors)


# ---------------------------------------------------------------------------
# GET /v1/events  – query (debug / admin)
# ---------------------------------------------------------------------------

@router.get(
    "/events",
    response_model=list[ListenEventRead],
    summary="Query stored events (admin)",
    description="Returns raw events for a user/track. Useful for debugging.",
)
async def query_events(
    user_id:      str | None = Query(None, max_length=128),
    track_id:     str | None = Query(None, max_length=128),
    event_type:   str | None = Query(None, max_length=32),
    device_id:    str | None = Query(None, max_length=128),
    context_type: str | None = Query(None, max_length=32),
    request_id:   str | None = Query(None, max_length=128),
    limit:        int = Query(50, ge=1, le=500),
    offset:       int = Query(0, ge=0),
    session:      AsyncSession = Depends(get_session),
    _key:         str = Depends(require_api_key),
):
    # Per-user auth: if a user_id filter is provided, verify the key
    # has access. If no user_id is provided, require admin privileges
    # (querying across all users is an admin-only operation).
    if user_id:
        check_user_access(_key, user_id)
    else:
        require_admin(_key)

    q = select(ListenEvent).order_by(ListenEvent.timestamp.desc())
    if user_id:
        q = q.where(ListenEvent.user_id == user_id)
    if track_id:
        q = q.where(ListenEvent.track_id == track_id)
    if event_type:
        q = q.where(ListenEvent.event_type == event_type)
    if device_id:
        q = q.where(ListenEvent.device_id == device_id)
    if context_type:
        q = q.where(ListenEvent.context_type == context_type)
    if request_id:
        q = q.where(ListenEvent.request_id == request_id)
    q = q.limit(limit).offset(offset)

    result = await session.execute(q)
    rows = result.scalars().all()
    return [ListenEventRead.model_validate(r) for r in rows]
