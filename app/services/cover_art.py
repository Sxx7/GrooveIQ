"""
GrooveIQ -- Cover art resolver with persistent cache.

Last.fm stopped distributing real cover art in ~2020, so chart entries
that don't match the local library need a fallback source.  This module
provides a cached lookup via Spotify search (through spotdl-api or
Spotizerr) -- no extra API key or integration required beyond what the
download backend already has.

Cache key: normalised (artist, title).
Cache value: URL string, or None meaning "looked up, nothing found".
Cache storage: cover_art_cache table.

At render time the chart API prefers, in order:
    1. media server cover URL (for tracks matched to the local library)
    2. cached cover URL from download backend (this module)
    3. None

Once a downloaded track lands in the library and is synced, priority #1
takes over automatically.  The cached entry is intentionally left in
place as a resilience fallback if the media server is unreachable.
"""

from __future__ import annotations

import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import CoverArtCache
from app.services.spotdl import get_download_client

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Refresh negative cache entries after a week — Spotify's catalog may have
# added the track in the meantime.  Positive entries never expire; if an
# image URL goes stale the backend will return a new one on next manual rebuild.
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
    client=None,
) -> str | None:
    """Resolve an album cover URL for (artist, title), with persistent cache.

    Cache hit -> return immediately (positive *or* negative, with TTL).
    Cache miss -> query download backend, persist the result, return it.

    Pass an existing `client` to reuse connection pooling across many calls
    (e.g. during a chart build that resolves hundreds of tracks).  Without
    one, a temporary client is created and closed for a single lookup.

    The caller controls commit timing -- this function only stages changes
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
    if not settings.download_enabled:
        return None

    owns_client = client is None
    if owns_client:
        client = get_download_client()
        if client is None:
            return None

    url: str | None = None
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
    source = "spotdl" if settings.spotdl_enabled else "spotizerr"
    if cached is not None:
        cached.url = url
        cached.source = source
        cached.fetched_at = now
    else:
        session.add(CoverArtCache(
            artist_norm=artist_norm,
            title_norm=title_norm,
            url=url,
            source=source,
            fetched_at=now,
        ))

    return url


async def resolve_artist_image(
    session: AsyncSession,
    artist: str,
    client=None,
) -> str | None:
    """Resolve a portrait image URL for an artist, with persistent cache.

    Shares the cover_art_cache table with track cover art — artist entries
    use an empty title_norm to distinguish them from per-track rows.

    Tries the download backend's artist image resolution; falls back to
    top-track album art internally.
    """
    artist_norm = _normalize(artist)
    if not artist_norm:
        return None

    now = int(time.time())

    cached = (await session.execute(
        select(CoverArtCache).where(
            CoverArtCache.artist_norm == artist_norm,
            CoverArtCache.title_norm == "",
        )
    )).scalar_one_or_none()

    if cached is not None:
        if cached.url:
            return cached.url
        if now - cached.fetched_at < _NEGATIVE_TTL_SECONDS:
            return None

    if not settings.download_enabled:
        return None

    owns_client = client is None
    if owns_client:
        client = get_download_client()
        if client is None:
            return None

    url: str | None = None
    try:
        url = await client.resolve_artist_image(artist)
    except Exception as exc:
        logger.warning("Artist image lookup failed for %r: %s", artist, exc)
        return None
    finally:
        if owns_client:
            await client.close()

    source = "spotdl_artist" if settings.spotdl_enabled else "spotizerr_artist"
    if cached is not None:
        cached.url = url
        cached.source = source
        cached.fetched_at = now
    else:
        session.add(CoverArtCache(
            artist_norm=artist_norm,
            title_norm="",
            url=url,
            source=source,
            fetched_at=now,
        ))

    return url
