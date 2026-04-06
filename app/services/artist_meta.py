"""
GrooveIQ -- Artist metadata service.

Combines Last.fm API data (artist.getInfo, artist.getTopTags,
artist.getTopTracks) with local library matching to provide rich
artist metadata through the API.

Results are cached in-memory with a 1-hour TTL.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)
_CACHE_TTL = 3600  # 1 hour


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# In-memory cache: normalized_artist_name -> (timestamp, result_dict)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    with _lock:
        entry = _cache.get(key)
        if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
            return entry[1]
    return None


def _cache_set(key: str, value: Dict[str, Any]) -> None:
    with _lock:
        _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Library lookups
# ---------------------------------------------------------------------------

async def _build_track_lookup(
    session: AsyncSession,
) -> Dict[Tuple[str, str], str]:
    """Build (normalized_artist, normalized_title) -> track_id lookup."""
    rows = (await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title)
        .where(TrackFeatures.artist.isnot(None), TrackFeatures.title.isnot(None))
    )).all()
    lookup: Dict[Tuple[str, str], str] = {}
    for track_id, artist, title in rows:
        key = (_normalize(artist), _normalize(title))
        if key not in lookup:
            lookup[key] = track_id
    return lookup


async def _build_artist_lookup(
    session: AsyncSession,
) -> Dict[str, List[str]]:
    """Build normalized_artist -> [track_id, ...] lookup."""
    rows = (await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.artist)
        .where(TrackFeatures.artist.isnot(None))
    )).all()
    lookup: Dict[str, List[str]] = {}
    for track_id, artist in rows:
        norm = _normalize(artist)
        lookup.setdefault(norm, []).append(track_id)
    return lookup


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

def _pick_image_url(images: list) -> Optional[str]:
    """Pick the best image URL from Last.fm's image array.

    Prefers extralarge (300x300). Falls back to large, then any non-empty.
    Returns None if all URLs are empty (common since ~2020).
    """
    if not images:
        return None
    by_size = {img.get("size", ""): img.get("#text", "") for img in images}
    for preferred in ("extralarge", "large", "mega", "medium", "small"):
        url = by_size.get(preferred, "")
        if url:
            return url
    return None


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

async def get_artist_meta(name: str) -> Optional[Dict[str, Any]]:
    """
    Fetch rich artist metadata combining Last.fm data with local library info.

    Returns None if Last.fm returns no data for the artist.
    """
    if not settings.LASTFM_API_KEY:
        return None

    # Check cache first.
    cache_key = _normalize(name)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    from app.services.lastfm_client import get_lastfm_client, LastFmError

    client = get_lastfm_client()

    # Fetch all three Last.fm endpoints concurrently.
    try:
        info_task = client.get_artist_info(name)
        tags_task = client.get_artist_tags(name)
        top_tracks_task = client.get_artist_top_tracks(name, limit=10)
        info, tags, top_tracks = await asyncio.gather(
            info_task, tags_task, top_tracks_task,
            return_exceptions=True,
        )
    except Exception as exc:
        logger.warning("Last.fm artist meta fetch failed for %r: %s", name, exc)
        return None

    # If artist.getInfo failed, we have no data.
    if isinstance(info, Exception):
        if isinstance(info, LastFmError) and info.code == 6:
            return None  # Artist not found
        logger.warning("artist.getInfo failed for %r: %s", name, info)
        return None

    if not info or not info.get("name"):
        return None

    # Tags — graceful fallback if the call failed.
    if isinstance(tags, Exception):
        logger.debug("artist.getTopTags failed for %r: %s", name, tags)
        tags = []

    # Top tracks — graceful fallback.
    if isinstance(top_tracks, Exception):
        logger.debug("artist.getTopTracks failed for %r: %s", name, top_tracks)
        top_tracks = []

    # Build library lookups.
    async with AsyncSessionLocal() as session:
        track_lookup = await _build_track_lookup(session)
        artist_lookup = await _build_artist_lookup(session)

    norm_artist = _normalize(info.get("name", name))
    library_tracks = artist_lookup.get(norm_artist, [])

    # Parse bio.
    bio_data = info.get("bio", {})
    bio_summary = bio_data.get("summary", "")
    bio_full = bio_data.get("content", "")

    # Parse similar artists.
    raw_similar = info.get("similar", {}).get("artist", [])
    if isinstance(raw_similar, dict):
        raw_similar = [raw_similar]
    similar = []
    for sa in raw_similar:
        sa_name = sa.get("name", "")
        if not sa_name:
            continue
        similar.append({
            "name": sa_name,
            "match": float(sa.get("match", 0)) if sa.get("match") else None,
            "in_library": _normalize(sa_name) in artist_lookup,
        })

    # Parse top tracks with library matching.
    top_tracks_out = []
    for t in top_tracks:
        title = t.get("name", "")
        if not title:
            continue
        playcount = int(t.get("playcount", 0))
        matched_id = track_lookup.get((_normalize(info.get("name", name)), _normalize(title)))
        top_tracks_out.append({
            "title": title,
            "playcount": playcount,
            "in_library": matched_id is not None,
            "matched_track_id": matched_id,
        })

    # Parse top albums from artist.getInfo (if present).
    # Note: artist.getInfo doesn't include top albums directly.
    # We skip the extra API call for artist.getTopAlbums to stay within
    # reasonable rate limits. If needed later, add it.

    # Parse stats.
    stats_data = info.get("stats", {})
    listeners = int(stats_data.get("listeners", 0)) if stats_data.get("listeners") else None
    playcount = int(stats_data.get("playcount", 0)) if stats_data.get("playcount") else None

    # Parse tags.
    tag_names = []
    # Prefer the dedicated getTopTags result (more complete).
    if tags and isinstance(tags, list):
        tag_names = [t.get("name", "") for t in tags if t.get("name")]
    else:
        # Fall back to tags from getInfo.
        info_tags = info.get("tags", {}).get("tag", [])
        if isinstance(info_tags, dict):
            info_tags = [info_tags]
        tag_names = [t.get("name", "") for t in info_tags if t.get("name")]

    result: Dict[str, Any] = {
        "name": info.get("name", name),
        "mbid": info.get("mbid") or None,
        "bio": bio_summary or None,
        "bio_full": bio_full or None,
        "image_url": _pick_image_url(info.get("image", [])),
        "tags": tag_names,
        "listeners": listeners,
        "playcount": playcount,
        "similar": similar,
        "top_tracks": top_tracks_out,
        "in_library": len(library_tracks) > 0,
        "library_track_count": len(library_tracks),
    }

    _cache_set(cache_key, result)
    return result
