"""GrooveIQ -- Personalized music news feed API routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.core.config import settings
from app.core.security import check_user_access, require_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "/news/refresh",
    summary="Refresh news feed cache",
    description="Manually trigger a refresh of the Reddit news cache. Runs in background.",
    status_code=202,
)
async def refresh_news(
    _key: str = Depends(require_api_key),
):
    if not settings.news_enabled:
        raise HTTPException(
            status_code=503,
            detail="News feed is not enabled. Set NEWS_ENABLED=true in your .env file.",
        )

    import asyncio

    from app.services.reddit_news import refresh_cache

    asyncio.create_task(refresh_cache())
    return {"status": "refresh_started"}


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
    tag: str | None = Query(None, max_length=50, description="Filter by tag: FRESH, NEWS, DISCUSSION"),
    subreddit: str | None = Query(None, max_length=100, description="Filter to a specific subreddit"),
    _key: str = Depends(require_api_key),
):
    check_user_access(_key, user_id)
    if not settings.news_enabled:
        raise HTTPException(
            status_code=503,
            detail="News feed is not enabled. Set NEWS_ENABLED=true in your .env file.",
        )

    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.db import TrackFeatures, TrackInteraction, User
    from app.services.reddit_news import get_cache_age_minutes, get_personalized_feed

    # Load user's taste profile + top artists/genres from library
    taste_profile = None
    library_artists: set[str] = set()
    library_genres: set[str] = set()
    async with AsyncSessionLocal() as session:
        row = (await session.execute(select(User.taste_profile).where(User.user_id == user_id))).scalar_one_or_none()
        if row is not None:
            taste_profile = row

        # Fetch artist and genre names for user's top interacted tracks
        top_tracks_q = (
            select(TrackFeatures.artist, TrackFeatures.genre)
            .join(TrackInteraction, TrackInteraction.track_id == TrackFeatures.track_id)
            .where(TrackInteraction.user_id == user_id)
            .where(TrackInteraction.satisfaction_score > 0)
            .order_by(TrackInteraction.satisfaction_score.desc())
            .limit(200)
        )
        rows = (await session.execute(top_tracks_q)).all()
        for artist, genre in rows:
            if artist:
                library_artists.add(artist.strip().lower())
            if genre:
                for g in genre.split(","):
                    g = g.strip().lower()
                    if g:
                        library_genres.add(g)

    cache_age = get_cache_age_minutes()
    cache_stale = cache_age > (settings.NEWS_INTERVAL_MINUTES * 2)

    items = get_personalized_feed(
        taste_profile=taste_profile,
        limit=limit,
        tag_filter=tag,
        subreddit_filter=subreddit,
        extra_artists=library_artists,
        extra_genres=library_genres,
    )

    return {
        "user_id": user_id,
        "total": len(items),
        "cache_age_minutes": round(cache_age, 1) if cache_age != float("inf") else None,
        "cache_stale": cache_stale,
        "items": items,
    }
