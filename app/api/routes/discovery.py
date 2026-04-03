"""
GrooveIQ -- Discovery endpoints.

GET  /v1/discovery       — list discovery requests
POST /v1/discovery/run   — trigger discovery pipeline manually
GET  /v1/discovery/stats — summary stats for the dashboard
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import DiscoveryRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/discovery",
    summary="List discovery requests",
    description="Returns discovery requests with optional filtering by user_id and status.",
)
async def list_discovery_requests(
    user_id: str = Query(None),
    status: str = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    q = select(DiscoveryRequest).order_by(DiscoveryRequest.created_at.desc())
    count_q = select(func.count()).select_from(DiscoveryRequest)

    if user_id:
        q = q.where(DiscoveryRequest.user_id == user_id)
        count_q = count_q.where(DiscoveryRequest.user_id == user_id)
    if status:
        q = q.where(DiscoveryRequest.status == status)
        count_q = count_q.where(DiscoveryRequest.status == status)

    total = (await session.execute(count_q)).scalar() or 0
    rows = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    return {
        "total": total,
        "requests": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "artist_name": r.artist_name,
                "artist_mbid": r.artist_mbid,
                "source": r.source,
                "seed_artist": r.seed_artist,
                "seed_genre": r.seed_genre,
                "similarity_score": r.similarity_score,
                "status": r.status,
                "lidarr_artist_id": r.lidarr_artist_id,
                "error_message": r.error_message,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }


@router.post(
    "/discovery/run",
    summary="Trigger discovery pipeline",
    description="Starts the music discovery pipeline in the background.",
)
async def trigger_discovery(
    _key: str = Depends(require_api_key),
):
    if not settings.discovery_enabled:
        return {
            "status": "error",
            "message": "Discovery not configured. Set LASTFM_API_KEY, LIDARR_URL, and LIDARR_API_KEY.",
        }

    async def _run():
        try:
            from app.workers.scheduler import run_discovery_now
            await run_discovery_now()
        except Exception as exc:
            logger.error("Manual discovery run failed: %s", exc, exc_info=True)

    asyncio.create_task(_run())
    return {"status": "running", "message": "Discovery pipeline started."}


@router.get(
    "/discovery/stats",
    summary="Discovery statistics",
    description="Returns summary counts for the dashboard.",
)
async def discovery_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Count by status.
    status_rows = (await session.execute(
        select(
            DiscoveryRequest.status,
            func.count().label("cnt"),
        ).group_by(DiscoveryRequest.status)
    )).all()
    by_status = {row.status: row.cnt for row in status_rows}
    total = sum(by_status.values())

    # Today's count.
    today_start = int(time.time()) - (int(time.time()) % 86400)
    today_count = (await session.execute(
        select(func.count()).select_from(DiscoveryRequest)
        .where(DiscoveryRequest.created_at >= today_start)
    )).scalar() or 0

    return {
        "enabled": settings.discovery_enabled,
        "total": total,
        "by_status": {
            "pending": by_status.get("pending", 0),
            "sent": by_status.get("sent", 0),
            "in_lidarr": by_status.get("in_lidarr", 0),
            "failed": by_status.get("failed", 0),
        },
        "today_count": today_count,
        "daily_limit": settings.DISCOVERY_MAX_REQUESTS_PER_DAY,
    }
