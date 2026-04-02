"""
GrooveIQ – Event ingestion routes.

POST /v1/events       – ingest a single event
POST /v1/events/batch – ingest up to 50 events
GET  /v1/events       – query events (admin/debug)
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, User
from app.models.schemas import EventBatch, EventCreate, EventResponse
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

The server returns **202 Accepted** immediately. Events are processed
asynchronously in the background.
""",
)
async def ingest_event(
    event: EventCreate,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
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
    accepted = 0
    rejected = 0
    errors: list[str] = []

    for i, event in enumerate(batch.events):
        try:
            await process_event(session, event)
            accepted += 1
        except Exception as e:
            rejected += 1
            errors.append(f"Event[{i}] ({event.track_id}): {e}")
            logger.warning("Batch event rejected", extra={"error": str(e), "index": i})

    return EventResponse(accepted=accepted, rejected=rejected, errors=errors)


# ---------------------------------------------------------------------------
# GET /v1/events  – query (debug / admin)
# ---------------------------------------------------------------------------

@router.get(
    "/events",
    summary="Query stored events (admin)",
    description="Returns raw events for a user/track. Useful for debugging.",
)
async def query_events(
    user_id:   Optional[str] = Query(None),
    track_id:  Optional[str] = Query(None),
    limit:     int = Query(50, ge=1, le=500),
    offset:    int = Query(0, ge=0),
    session:   AsyncSession = Depends(get_session),
    _key:      str = Depends(require_api_key),
):
    q = select(ListenEvent).order_by(ListenEvent.timestamp.desc())
    if user_id:
        q = q.where(ListenEvent.user_id == user_id)
    if track_id:
        q = q.where(ListenEvent.track_id == track_id)
    q = q.limit(limit).offset(offset)

    result = await session.execute(q)
    rows = result.scalars().all()
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "track_id": r.track_id,
            "event_type": r.event_type,
            "value": r.value,
            "context": r.context,
            "timestamp": r.timestamp,
            "session_id": r.session_id,
        }
        for r in rows
    ]
