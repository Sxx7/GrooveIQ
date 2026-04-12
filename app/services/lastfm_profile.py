"""
GrooveIQ – Last.fm profile pull service.

Periodically fetches Last.fm profile data for linked users and caches it
in the user.lastfm_cache JSON column.

Cached data includes: user info, top artists (multiple periods),
top tracks, loved tracks, and genre tags from top artists.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import User

logger = logging.getLogger(__name__)

# Periods to fetch for top artists/tracks
_PERIODS = ("7day", "1month", "overall")


async def refresh_lastfm_profiles() -> dict:
    """
    Refresh Last.fm cached data for all linked users.

    Only refreshes users whose lastfm_synced_at is older than
    LASTFM_REFRESH_HOURS or null.
    """
    from app.services.lastfm_client import LastFmError, get_lastfm_client

    refreshed = 0
    errors = 0
    cutoff = int(time.time()) - settings.LASTFM_REFRESH_HOURS * 3600

    async with AsyncSessionLocal() as session:
        users = (
            (
                await session.execute(
                    select(User).where(
                        User.lastfm_username.isnot(None),
                        User.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )

        client = get_lastfm_client()

        for user in users:
            if user.lastfm_synced_at and user.lastfm_synced_at > cutoff:
                continue

            try:
                cache = await _fetch_user_profile(client, user.lastfm_username)
                user.lastfm_cache = cache
                user.lastfm_synced_at = int(time.time())
                refreshed += 1
            except LastFmError as e:
                logger.warning(
                    "Last.fm profile fetch failed for %s: %s",
                    user.lastfm_username,
                    e,
                )
                errors += 1
            except Exception as e:
                logger.error(
                    "Unexpected error fetching Last.fm profile for %s: %s",
                    user.lastfm_username,
                    e,
                )
                errors += 1

        await session.commit()

    return {"refreshed": refreshed, "errors": errors, "total_linked": len(users)}


async def refresh_single_user(session: AsyncSession, user: User) -> dict:
    """Refresh Last.fm data for a single user. Used by the connect endpoint."""
    from app.services.lastfm_client import get_lastfm_client

    if not user.lastfm_username:
        return {}

    client = get_lastfm_client()
    cache = await _fetch_user_profile(client, user.lastfm_username)
    user.lastfm_cache = cache
    user.lastfm_synced_at = int(time.time())
    return cache


async def _fetch_user_profile(client, username: str) -> dict:
    """Fetch and assemble a complete Last.fm profile for one user."""
    profile: dict = {}

    # User info (playcount, country, registered, etc.)
    profile["user_info"] = await client.get_user_info(username)

    # Top artists by period
    profile["top_artists"] = {}
    for period in _PERIODS:
        profile["top_artists"][period] = await client.get_top_artists(
            username,
            period=period,
            limit=50,
        )

    # Top tracks by period
    profile["top_tracks"] = {}
    for period in _PERIODS:
        profile["top_tracks"][period] = await client.get_top_tracks(
            username,
            period=period,
            limit=50,
        )

    # Recent tracks
    profile["recent_tracks"] = await client.get_recent_tracks(username, limit=50)

    # Loved tracks
    profile["loved_tracks"] = await client.get_loved_tracks(username, limit=50)

    # Genre tags from top 10 overall artists
    top_10 = profile["top_artists"].get("overall", [])[:10]
    genre_counts: dict[str, int] = {}
    for artist_data in top_10:
        artist_name = artist_data.get("name", "")
        if not artist_name:
            continue
        try:
            tags = await client.get_artist_tags(artist_name, limit=5)
            for tag in tags:
                name = tag.get("name", "").lower()
                count = int(tag.get("count", 0))
                if name:
                    genre_counts[name] = genre_counts.get(name, 0) + count
        except Exception:
            pass  # non-critical

    # Sort genres by aggregate count
    profile["genres"] = dict(sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:30])

    profile["fetched_at"] = int(time.time())
    return profile
