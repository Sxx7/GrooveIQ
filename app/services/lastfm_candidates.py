"""
GrooveIQ – Last.fm similar-track candidate source.

Uses Last.fm's track.getSimilar API to provide collaborative filtering
from millions of users for free.  Especially valuable for small-scale
deployments (1-10 users) where local user-user CF has insufficient data.

Architecture:
  - A cached mapping (track_id → [(similar_track_id, score)]) is rebuilt
    periodically during the recommendation pipeline.
  - For each of the user's top tracks, Last.fm's similar tracks are
    fetched and matched back to the local library by artist+title.
  - The cache is stored in-memory as a module-level singleton.
  - At recommendation time, the cache is read without any API calls.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures, TrackInteraction, User

logger = logging.getLogger(__name__)

# Singleton cache: track_id → list of (similar_track_id, score).
_lock = threading.Lock()
_similar_cache: Dict[str, List[Tuple[str, float]]] = {}
_built_at: int = 0

# How many top tracks per user to query Last.fm for.
_TOP_TRACKS_PER_USER = 20
# Max similar tracks to fetch per seed from Last.fm.
_SIMILAR_PER_SEED = 30


def _normalize(s: str) -> str:
    """Lowercase, strip whitespace for fuzzy matching."""
    return s.strip().lower()


async def build_cache() -> Dict[str, Any]:
    """
    Rebuild the Last.fm similar-track cache for all users.

    For each user's top tracks that have artist+title metadata,
    queries Last.fm track.getSimilar and matches results back to
    the local library.

    Returns summary dict.
    """
    if not settings.LASTFM_API_KEY:
        return {"built": False, "reason": "lastfm_not_configured"}

    from app.services.lastfm_client import get_lastfm_client, LastFmError

    client = get_lastfm_client()
    new_cache: Dict[str, List[Tuple[str, float]]] = {}
    api_calls = 0
    matched = 0
    errors = 0

    async with AsyncSessionLocal() as session:
        # Build a lookup index: (normalised_artist, normalised_title) → track_id.
        lib_result = await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title)
            .where(
                TrackFeatures.artist.isnot(None),
                TrackFeatures.title.isnot(None),
            )
        )
        lib_rows = lib_result.all()

        if not lib_rows:
            logger.info("Last.fm candidates: no library tracks with artist+title metadata.")
            return {"built": False, "reason": "no_metadata", "library_tracks": 0}

        # Build lookup: (norm_artist, norm_title) → track_id.
        # If multiple tracks match the same key, first one wins.
        lib_lookup: Dict[Tuple[str, str], str] = {}
        for track_id, artist, title in lib_rows:
            key = (_normalize(artist), _normalize(title))
            if key not in lib_lookup:
                lib_lookup[key] = track_id

        logger.info(
            "Last.fm candidates: %d library tracks indexed for matching.",
            len(lib_lookup),
        )

        # Collect seed tracks: top tracks across all users.
        users = (await session.execute(
            select(User).where(User.is_active.is_(True))
        )).scalars().all()

        seed_tracks: Dict[str, Tuple[str, str]] = {}  # track_id → (artist, title)

        for user in users:
            profile = user.taste_profile
            if not profile:
                continue
            top_tracks = profile.get("top_tracks", [])
            for t in top_tracks[:_TOP_TRACKS_PER_USER]:
                tid = t.get("track_id")
                if tid and tid not in seed_tracks:
                    # Look up metadata from library.
                    feat_result = await session.execute(
                        select(TrackFeatures.artist, TrackFeatures.title)
                        .where(TrackFeatures.track_id == tid)
                    )
                    row = feat_result.first()
                    if row and row[0] and row[1]:
                        seed_tracks[tid] = (row[0], row[1])

        if not seed_tracks:
            logger.info("Last.fm candidates: no seed tracks with metadata.")
            return {"built": False, "reason": "no_seeds", "users": len(users)}

        logger.info(
            "Last.fm candidates: querying similar tracks for %d seeds.",
            len(seed_tracks),
        )

        # Query Last.fm for each seed and match back to library.
        for track_id, (artist, title) in seed_tracks.items():
            try:
                similar = await client.get_similar_tracks(
                    artist=artist, track=title, limit=_SIMILAR_PER_SEED,
                )
                api_calls += 1
            except LastFmError as e:
                if e.code == 6:
                    # Track not found on Last.fm — not an error.
                    continue
                logger.debug("Last.fm getSimilar failed for %s - %s: %s", artist, title, e)
                errors += 1
                continue
            except Exception as e:
                logger.debug("Last.fm getSimilar error for %s - %s: %s", artist, title, e)
                errors += 1
                continue

            matches: List[Tuple[str, float]] = []
            for sim_track in similar:
                sim_artist = sim_track.get("artist", {})
                if isinstance(sim_artist, dict):
                    sim_artist_name = sim_artist.get("name", "")
                else:
                    sim_artist_name = str(sim_artist)

                sim_title = sim_track.get("name", "")
                sim_match = float(sim_track.get("match", 0))

                if not sim_artist_name or not sim_title:
                    continue

                # Match against local library.
                key = (_normalize(sim_artist_name), _normalize(sim_title))
                local_tid = lib_lookup.get(key)
                if local_tid and local_tid != track_id:
                    matches.append((local_tid, sim_match))
                    matched += 1

            if matches:
                # Sort by score descending and deduplicate.
                seen: Set[str] = set()
                deduped: List[Tuple[str, float]] = []
                for tid, score in sorted(matches, key=lambda x: x[1], reverse=True):
                    if tid not in seen:
                        seen.add(tid)
                        deduped.append((tid, score))
                new_cache[track_id] = deduped

    # Atomic swap.
    with _lock:
        global _similar_cache, _built_at
        _similar_cache = new_cache
        _built_at = int(time.time())

    logger.info(
        "Last.fm candidates cache built: %d seeds cached, %d matches, "
        "%d API calls, %d errors.",
        len(new_cache), matched, api_calls, errors,
    )

    return {
        "built": True,
        "seeds_cached": len(new_cache),
        "total_matches": matched,
        "api_calls": api_calls,
        "errors": errors,
    }


def get_similar_for_track(
    track_id: str,
    k: int = 50,
    exclude_ids: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    """
    Get Last.fm similar tracks for a single seed track (from cache).

    Returns list of (track_id, score) sorted descending.
    """
    with _lock:
        cached = _similar_cache.get(track_id)

    if not cached:
        return []

    results: List[Tuple[str, float]] = []
    for tid, score in cached:
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, score))
        if len(results) >= k:
            break

    return results


def get_similar_for_user(
    top_track_ids: List[str],
    k: int = 100,
    exclude_ids: Optional[Set[str]] = None,
) -> List[Tuple[str, float]]:
    """
    Get Last.fm similar tracks across a user's top tracks (from cache).

    Merges results from all seed tracks, deduplicates, and returns
    the top k by score. Tracks appearing as similar to multiple seeds
    get their max score.
    """
    with _lock:
        cache = _similar_cache

    if not cache:
        return []

    # Merge: track_id → max score across all seeds.
    score_map: Dict[str, float] = {}
    input_set = set(top_track_ids)

    for seed_tid in top_track_ids:
        cached = cache.get(seed_tid)
        if not cached:
            continue
        for tid, score in cached:
            if tid in input_set:
                continue
            if exclude_ids and tid in exclude_ids:
                continue
            if tid not in score_map or score > score_map[tid]:
                score_map[tid] = score

    # Sort by score descending.
    results = sorted(score_map.items(), key=lambda x: x[1], reverse=True)
    return results[:k]


def is_ready() -> bool:
    """True if the cache has been built."""
    with _lock:
        return len(_similar_cache) > 0


def cache_size() -> int:
    """Number of seed tracks with cached similar tracks."""
    with _lock:
        return len(_similar_cache)


def cache_age() -> int:
    """Seconds since the cache was last built. 0 if never built."""
    with _lock:
        if _built_at == 0:
            return 0
        return int(time.time()) - _built_at
