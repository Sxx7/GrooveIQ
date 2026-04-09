"""
GrooveIQ -- Cover art resolver with persistent cache.

Last.fm stopped distributing real cover art in ~2020, so chart entries
that don't match the local library need a fallback source.  This module
provides a cached lookup via Spotizerr's Spotify search — no extra API
key or integration required beyond what Spotizerr already has.

Cache key: normalised (artist, title).
Cache value: URL string, or None meaning "looked up, nothing found".
Cache storage: cover_art_cache table.

At render time the chart API prefers, in order:
    1. media server cover URL (for tracks matched to the local library)
    2. cached Spotizerr URL (this module)
    3. None

Once a downloaded track lands in the library and is synced, priority #1
takes over automatically.  The cached entry is intentionally left in
place as a resilience fallback if the media server is unreachable.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import CoverArtCache
from app.services.spotizerr import SpotizerrClient

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Refresh negative cache entries after a week — Spotify's catalog may have
# added the track in the meantime.  Positive entries never expire; if an
# image URL goes stale Spotizerr will return a new one on next manual rebuild.
_NEGATIVE_TTL_SECONDS = 7 * 24 * 3600


def _normalize(s: str) -> str:
    """Match the normalisation used by charts.py / spotizerr.py."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


async def resolve_cover_art(
    session: AsyncSession,
    artist: str,
    title: str,
    client: Optional[SpotizerrClient] = None,
) -> Optional[str]:
    """Resolve an album cover URL for (artist, title), with persistent cache.

    Cache hit → return immediately (positive *or* negative, with TTL).
    Cache miss → query Spotizerr, persist the result, return it.

    Pass an existing `client` to reuse connection pooling across many calls
    (e.g. during a chart build that resolves hundreds of tracks).  Without
    one, a temporary client is created and closed for a single lookup.

    The caller controls commit timing — this function only stages changes
    on the provided session.
    """
    artist_norm = _normalize(artist)
    title_norm = _normalize(title)
    if not artist_norm or not title_norm:
        return None

    now = int(time.time())

    # -- Cache lookup -------------------------------------------------------
    cached = (await session.execute(
        select(CoverArtCache).where(
            CoverArtCache.artist_norm == artist_norm,
            CoverArtCache.title_norm == title_norm,
        )
    )).scalar_one_or_none()

    if cached is not None:
        if cached.url:
            return cached.url
        # Negative hit: honour TTL before re-querying upstream.
        if now - cached.fetched_at < _NEGATIVE_TTL_SECONDS:
            return None

    # -- Upstream lookup ----------------------------------------------------
    if not settings.spotizerr_enabled:
        return None

    owns_client = client is None
    if owns_client:
        client = SpotizerrClient(
            settings.SPOTIZERR_URL,
            settings.SPOTIZERR_USERNAME,
            settings.SPOTIZERR_PASSWORD,
        )

    url: Optional[str] = None
    try:
        url = await client.resolve_cover_art(artist, title)
    except Exception as exc:
        logger.warning("Cover art lookup failed for %r/%r: %s", artist, title, exc)
        # Don't persist on transient errors — let the next build retry.
        return None
    finally:
        if owns_client:
            await client.close()

    # -- Persist (positive or negative) -------------------------------------
    if cached is not None:
        cached.url = url
        cached.source = "spotizerr"
        cached.fetched_at = now
    else:
        session.add(CoverArtCache(
            artist_norm=artist_norm,
            title_norm=title_norm,
            url=url,
            source="spotizerr",
            fetched_at=now,
        ))

    return url
