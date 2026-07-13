"""GrooveIQ — Unified in-app notification feed (Activity sheet).

Projects the generic notification outbox (``notification_deliveries`` joined to
``notification_events``) into the per-user Activity feed the iOS app renders as
one scrollable, typed list: followed-artist releases + recommendations (album /
playlist / mix). Unlike the push path — which forwards only a whitelisted subset
of ``data`` keys (see ``_ROUTE_SOURCE_KEYS`` in notification_dispatch) — the feed
returns each event's FULL captured ``data`` JSON, so rich rows (cover art,
deep-link ids) render from a single read.

Read-state lives on ``notification_deliveries.seen_at`` (NULL = unseen);
``unseen_count`` drives the bell badge and ``POST .../seen`` stamps it. This is
the superset-and-successor of the release-only ``/feed`` route (feed.py), which
reads the separate ``release_events`` tables — the feed reads the outbox instead.

Deliveries are shown regardless of ``dispatch_state`` (a budget-``suppressed`` or
still-``pending`` delivery is a legitimate feed item — a pull surface that is
independent of, and can run ahead of, the push).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Body, Depends, Path, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import check_user_access, require_api_key
from app.core.user_id import validate_user_id
from app.db.session import get_session
from app.models.db import NotificationDelivery, NotificationEvent

logger = logging.getLogger(__name__)
router = APIRouter()

# The Activity feed surfaces "here's something new for you" notifications:
# followed-artist releases + recommendations (album / playlist / mix). The
# transactional download_completed / newly_added events route to Library (not the
# Activity sheet — see UIStore.applyPushRoute on the client), so they're excluded
# from this feed and its unseen badge. Extend this tuple to surface more types.
_ACTIVITY_EVENT_TYPES = ("new_release", "recommendation")


@router.get("/users/{user_id}/notifications", summary="Unified in-app notification feed")
async def get_notifications(
    user_id: str = Path(..., min_length=1, max_length=128),
    limit: int = Query(30, ge=1, le=100),
    before: int | None = Query(None, description="created_at cursor; returns rows strictly older"),
    unseen_only: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    # Retention window: never surface notifications older than the horizon. The
    # nightly prune deletes them too; this filter keeps the boundary exact between
    # runs (and honours retention even if the prune is disabled). 0 = keep forever.
    retention_days = settings.NOTIFICATION_RETENTION_DAYS
    cutoff = int(time.time()) - retention_days * 86_400 if retention_days > 0 else None

    q = (
        select(NotificationDelivery, NotificationEvent)
        .join(NotificationEvent, NotificationEvent.id == NotificationDelivery.event_id)
        .where(
            NotificationDelivery.user_id == user_id,
            NotificationDelivery.event_type.in_(_ACTIVITY_EVENT_TYPES),
        )
        .order_by(NotificationEvent.created_at.desc(), NotificationDelivery.id.desc())
    )
    if cutoff is not None:
        q = q.where(NotificationEvent.created_at >= cutoff)
    if before is not None:
        q = q.where(NotificationEvent.created_at < before)
    if unseen_only:
        q = q.where(NotificationDelivery.seen_at.is_(None))

    rows = (await session.execute(q.limit(limit + 1))).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        {
            "id": d.id,  # delivery id — the handle POST /seen and the client use
            "event_type": d.event_type,
            "title": e.title,
            "body": e.body,
            "data": e.data or {},  # full captured payload: cover_url + deep-link ids
            "created_at": e.created_at,
            "seen_at": d.seen_at,
        }
        for d, e in rows
    ]
    next_before = items[-1]["created_at"] if (has_more and items) else None

    # unseen badge count — same retention window, on the delivery's own created_at
    # (== the event's, set together at emit) so it needs no join.
    unseen_where = [
        NotificationDelivery.user_id == user_id,
        NotificationDelivery.event_type.in_(_ACTIVITY_EVENT_TYPES),
        NotificationDelivery.seen_at.is_(None),
    ]
    if cutoff is not None:
        unseen_where.append(NotificationDelivery.created_at >= cutoff)
    unseen_count = (
        await session.scalar(
            select(func.count()).select_from(NotificationDelivery).where(*unseen_where)
        )
    ) or 0

    return {"items": items, "next_before": next_before, "unseen_count": unseen_count}


@router.post("/users/{user_id}/notifications/seen", summary="Mark notifications seen")
async def mark_notifications_seen(
    user_id: str = Path(..., min_length=1, max_length=128),
    body: dict = Body(...),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # 200 + JSON body (not 204): the iOS Alamofire client decodes an empty struct
    # from a body but throws invalidEmptyResponse on a true empty 204. Body is
    # `{ "ids": [...] }` (specific deliveries) or `{ "all": true }`.
    validate_user_id(user_id)
    check_user_access(_key, user_id)

    now = int(time.time())
    stmt = update(NotificationDelivery).where(NotificationDelivery.user_id == user_id)
    if body.get("all") is True:
        # Clear the badge: only the activity types the feed shows, only the unseen.
        stmt = stmt.where(
            NotificationDelivery.event_type.in_(_ACTIVITY_EVENT_TYPES),
            NotificationDelivery.seen_at.is_(None),
        )
    else:
        ids = body.get("ids") or []
        if not ids:
            return {"status": "ok", "updated": 0}
        stmt = stmt.where(NotificationDelivery.id.in_(ids))

    res = await session.execute(stmt.values(seen_at=now))
    await session.commit()
    return {"status": "ok", "updated": max(0, res.rowcount or 0)}
