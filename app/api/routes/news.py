"""GrooveIQ -- Personalized music news feed API routes."""

from __future__ import annotations

import logging

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.config import settings
from app.core.security import require_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/news/{user_id}",
    summary="Personalized music news feed",
    description=(
        "Returns a personalized music news feed sourced from Reddit, "
        "scored and ranked based on the user's taste profile."
    ),
)
async def get_news(
    user_id: str = Path(..., min_length=1, max_length=128),
    limit: int = Query(25, ge=1, le=100),
    tag: Optional[str] = Query(None, max_length=50, description="Filter by tag: FRESH, NEWS, DISCUSSION"),
    subreddit: Optional[str] = Query(None, max_length=100, description="Filter to a specific subreddit"),
    _key: str = Depends(require_api_key),
):
    if not settings.news_enabled:
        raise HTTPException(
            status_code=503,
            detail="News feed is not enabled. Set NEWS_ENABLED=true in your .env file.",
        )

    from app.db.session import AsyncSessionLocal
    from app.models.db import User
    from app.services.reddit_news import get_cache_age_minutes, get_personalized_feed
    from sqlalchemy import select

    # Load user's taste profile
    taste_profile = None
    async with AsyncSessionLocal() as session:
        row = (await session.execute(
            select(User.taste_profile).where(User.user_id == user_id)
        )).scalar_one_or_none()
        if row is not None:
            taste_profile = row

    cache_age = get_cache_age_minutes()
    cache_stale = cache_age > (settings.NEWS_INTERVAL_MINUTES * 2)

    items = get_personalized_feed(
        taste_profile=taste_profile,
        limit=limit,
        tag_filter=tag,
        subreddit_filter=subreddit,
    )

    return {
        "user_id": user_id,
        "total": len(items),
        "cache_age_minutes": round(cache_age, 1) if cache_age != float("inf") else None,
        "cache_stale": cache_stale,
        "items": items,
    }
