"""GrooveIQ -- New-release feed routes (P1).

Serves the per-user "New releases" feed: notifications (eligible) joined to
release_events that are actually available (``available_at IS NOT NULL``),
newest-available first. Read-state is stamped via ``/feed/seen``.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Body, Depends, Path, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import ReleaseEvent, UserReleaseNotification

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/users/{user_id}/feed", summary="New-release feed for followed artists")
async def get_feed(
    user_id: str = Path(..., min_length=1, max_length=128),
    limit: int = Query(30, ge=1, le=100),
    before: int | None = Query(None, description="available_at cursor; returns rows strictly older"),
    unseen_only: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    q = (
        select(UserReleaseNotification, ReleaseEvent)
        .join(ReleaseEvent, ReleaseEvent.id == UserReleaseNotification.release_event_id)
        .where(
            UserReleaseNotification.user_id == user_id,
            UserReleaseNotification.eligible.is_(True),
            ReleaseEvent.available_at.isnot(None),
        )
        .order_by(ReleaseEvent.available_at.desc())
    )
    if before is not None:
        q = q.where(ReleaseEvent.available_at < before)
    if unseen_only:
        q = q.where(UserReleaseNotification.seen_at.is_(None))

    rows = (await session.execute(q.limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "notification_id": n.id,
            "release_event_id": e.id,
            "artist_name": e.artist_name,
            "artist_mbid": e.artist_mbid,
            "album_title": e.album_title,
            "kind": e.kind,
            "cover_url": e.cover_url,
            "first_release_date": e.first_release_date,
            "available_at": e.available_at,
            "seen_at": n.seen_at,
        }
        for n, e in rows
    ]
    next_before = items[-1]["available_at"] if (has_more and items) else None

    unseen_count = (
        await session.scalar(
            select(func.count())
            .select_from(UserReleaseNotification)
            .join(ReleaseEvent, ReleaseEvent.id == UserReleaseNotification.release_event_id)
            .where(
                UserReleaseNotification.user_id == user_id,
                UserReleaseNotification.eligible.is_(True),
                UserReleaseNotification.seen_at.is_(None),
                ReleaseEvent.available_at.isnot(None),
            )
        )
    ) or 0

    return {"items": items, "next_before": next_before, "unseen_count": unseen_count}


@router.post("/users/{user_id}/feed/seen", summary="Mark feed items seen")
async def mark_feed_seen(
    user_id: str = Path(..., min_length=1, max_length=128),
    body: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # 200 + JSON body (not 204): the iOS Alamofire client decodes an empty
    # struct from a body but throws `invalidEmptyResponse` on a true empty 204.
    # Body is `{ "notification_ids": [...] }` or `{ "all": true }`.
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    now = int(time.time())
    stmt = update(UserReleaseNotification).where(UserReleaseNotification.user_id == user_id)
    if body.get("all") is True:
        stmt = stmt.where(UserReleaseNotification.seen_at.is_(None))
    else:
        ids = body.get("notification_ids") or []
        if not ids:
            return {"status": "ok", "updated": 0}
        stmt = stmt.where(UserReleaseNotification.id.in_(ids))

    res = await session.execute(stmt.values(seen_at=now))
    await session.commit()
    return {"status": "ok", "updated": max(0, res.rowcount or 0)}
