"""GrooveIQ – Dashboard statistics routes."""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, TrackFeatures, User, LibraryScanState

router = APIRouter()


@router.get("/stats", summary="Aggregate stats for the dashboard")
async def get_stats(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    now = int(time.time())
    day_ago = now - 86400
    hour_ago = now - 3600

    # Counts
    total_events = (await session.execute(select(func.count(ListenEvent.id)))).scalar() or 0
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    total_tracks = (await session.execute(select(func.count(TrackFeatures.id)))).scalar() or 0
    events_24h = (await session.execute(
        select(func.count(ListenEvent.id)).where(ListenEvent.timestamp >= day_ago)
    )).scalar() or 0
    events_1h = (await session.execute(
        select(func.count(ListenEvent.id)).where(ListenEvent.timestamp >= hour_ago)
    )).scalar() or 0

    # Event type breakdown (last 24h)
    type_rows = (await session.execute(
        select(ListenEvent.event_type, func.count(ListenEvent.id))
        .where(ListenEvent.timestamp >= day_ago)
        .group_by(ListenEvent.event_type)
        .order_by(func.count(ListenEvent.id).desc())
    )).all()
    event_types = {row[0]: row[1] for row in type_rows}

    # Top tracks (last 24h by event count)
    track_rows = (await session.execute(
        select(ListenEvent.track_id, func.count(ListenEvent.id).label("c"))
        .where(ListenEvent.timestamp >= day_ago)
        .group_by(ListenEvent.track_id)
        .order_by(func.count(ListenEvent.id).desc())
        .limit(10)
    )).all()
    top_tracks = [{"track_id": r[0], "events": r[1]} for r in track_rows]

    # Latest scan
    scan_row = (await session.execute(
        select(LibraryScanState).order_by(LibraryScanState.id.desc()).limit(1)
    )).scalar_one_or_none()
    latest_scan = None
    if scan_row:
        now_ts = int(time.time())
        processed = scan_row.files_analyzed + scan_row.files_failed + (scan_row.files_skipped or 0)
        percent = round(processed / scan_row.files_found * 100, 1) if scan_row.files_found > 0 else 0.0
        elapsed = (scan_row.scan_ended_at or now_ts) - scan_row.scan_started_at
        eta = None
        if scan_row.status == "running" and percent > 0:
            eta = int(elapsed / percent * (100 - percent))
        latest_scan = {
            "scan_id": scan_row.id,
            "status": scan_row.status,
            "files_found": scan_row.files_found,
            "files_analyzed": scan_row.files_analyzed,
            "files_skipped": scan_row.files_skipped or 0,
            "files_failed": scan_row.files_failed,
            "percent_complete": percent,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "current_file": scan_row.current_file,
            "started_at": scan_row.scan_started_at,
            "ended_at": scan_row.scan_ended_at,
        }

    return {
        "total_events": total_events,
        "total_users": total_users,
        "total_tracks_analyzed": total_tracks,
        "events_last_24h": events_24h,
        "events_last_1h": events_1h,
        "event_types_24h": event_types,
        "top_tracks_24h": top_tracks,
        "latest_scan": latest_scan,
    }
