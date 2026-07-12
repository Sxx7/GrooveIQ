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
import re
import threading
import time
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures, TrackInteraction, User

logger = logging.getLogger(__name__)

# Singleton cache: track_id → list of (similar_track_id, score).
_lock = threading.Lock()
_similar_cache: dict[str, list[tuple[str, float]]] = {}
_built_at: int = 0

# How many top tracks per user to query Last.fm for.
_TOP_TRACKS_PER_USER = 20

# Also seed the cache from up to this many of the user's most-played tracks (the
# realistic radio-seed universe), so borrowed-CF actually covers radio seeds and
# not just the top tracks. See docs/RECO_ALGORITHM_AUDIT.md §8.5.
_MAX_LIBRARY_SEEDS = 500
# Max similar tracks to fetch per seed from Last.fm.
_SIMILAR_PER_SEED = 30


def _normalize(s: str) -> str:
    """Lowercase, strip whitespace for fuzzy matching."""
    return s.strip().lower()


# Version/remix/feat keywords that mark a title suffix as a *variant* of a base
# track rather than a distinct song. Used to collapse "Song (Alex Skrindo Remix)"
# / "Song - Radio Edit" / "Song feat. X" down to "Song" so a remix-heavy library
# still matches Last.fm's base-title getSimilar results (and vice versa). Exact
# matching is always tried first; base matching is only a fallback, so a false
# collapse costs at most a slightly-wrong *version* of the right song.
_VERSION_KW = (
    r"remix|mix|edit|version|live|acoustic|unplugged|remaster(?:ed)?|instrumental|"
    r"extended|bootleg|rework|vip|dub|reprise|mono|demo|session|feat\.?|ft\.?|featuring"
)
_TRAILING_GROUP_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")
_VERSION_KW_RE = re.compile(rf"\b(?:{_VERSION_KW})\b", re.IGNORECASE)
_FEAT_SEG_RE = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\b.*$", re.IGNORECASE)
_DASH_VERSION_RE = re.compile(rf"\s+[-–—]\s+.*\b(?:{_VERSION_KW})\b.*$", re.IGNORECASE)


def _base_title(title: str) -> str:
    """Strip remix/feat/version markers to a base title for fuzzy matching.

    Conservative: a trailing ``(...)`` / ``[...]`` group is removed only when it
    contains a version/feat keyword, so legitimately-distinct parentheticals
    (e.g. "Clair de Lune (Suite bergamasque)") are preserved. Returns lowercased.
    """
    t = title.strip()
    changed = True
    while changed:
        changed = False
        m = _TRAILING_GROUP_RE.search(t)
        if m and _VERSION_KW_RE.search(m.group(0)):
            t = t[: m.start()].strip()
            changed = True
    t = _FEAT_SEG_RE.sub("", t).strip()
    t = _DASH_VERSION_RE.sub("", t).strip()
    return t.lower()


def _match_to_library(
    artist: str,
    title: str,
    lib_lookup: dict[tuple[str, str], str],
    lib_base_lookup: dict[tuple[str, str], str],
) -> tuple[str | None, bool]:
    """Resolve (artist, title) to a library track_id: exact first, then base title.

    Returns ``(track_id_or_None, is_fuzzy)`` — ``is_fuzzy`` is True when the match
    came from the base-title fallback rather than an exact match.
    """
    na = _normalize(artist)
    tid = lib_lookup.get((na, _normalize(title)))
    if tid:
        return tid, False
    tid = lib_base_lookup.get((na, _base_title(title)))
    return tid, tid is not None


async def build_cache() -> dict[str, Any]:
    """
    Rebuild the Last.fm similar-track cache for all users.

    For each user's top tracks that have artist+title metadata,
    queries Last.fm track.getSimilar and matches results back to
    the local library.

    Returns summary dict.
    """
    if not settings.LASTFM_API_KEY:
        return {"built": False, "reason": "lastfm_not_configured"}

    from app.services.lastfm_client import LastFmError, get_lastfm_client

    client = get_lastfm_client()
    new_cache: dict[str, list[tuple[str, float]]] = {}
    api_calls = 0
    matched = 0
    fuzzy_matches = 0
    base_seed_retries = 0
    errors = 0

    async with AsyncSessionLocal() as session:
        # Build a lookup index: (normalised_artist, normalised_title) → track_id.
        lib_result = await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title).where(
                TrackFeatures.artist.isnot(None),
                TrackFeatures.title.isnot(None),
            )
        )
        lib_rows = lib_result.all()

        if not lib_rows:
            logger.info("Last.fm candidates: no library tracks with artist+title metadata.")
            return {"built": False, "reason": "no_metadata", "library_tracks": 0}

        # Build two lookups keyed by track: exact (norm_artist, norm_title) and a
        # base-title fallback (norm_artist, base_title) that collapses remix/feat/
        # version variants. First writer wins in each. The base index lets a remix-
        # heavy library match Last.fm's base-title getSimilar results (and vice versa).
        lib_lookup: dict[tuple[str, str], str] = {}
        lib_base_lookup: dict[tuple[str, str], str] = {}
        for track_id, artist, title in lib_rows:
            na = _normalize(artist)
            key = (na, _normalize(title))
            if key not in lib_lookup:
                lib_lookup[key] = track_id
            bkey = (na, _base_title(title))
            if bkey not in lib_base_lookup:
                lib_base_lookup[bkey] = track_id

        logger.info(
            "Last.fm candidates: %d library tracks indexed for matching (%d base-title keys).",
            len(lib_lookup),
            len(lib_base_lookup),
        )

        # Collect seed tracks: top tracks across all users.
        users = (await session.execute(select(User).where(User.is_active.is_(True)))).scalars().all()

        seed_tracks: dict[str, tuple[str, str]] = {}  # track_id → (artist, title)

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
                        select(TrackFeatures.artist, TrackFeatures.title).where(TrackFeatures.track_id == tid)
                    )
                    row = feat_result.first()
                    if row and row[0] and row[1]:
                        seed_tracks[tid] = (row[0], row[1])

        # Extend coverage beyond top-20: also seed from the most-played tracks (the
        # realistic radio-seed universe). Without this the borrowed-CF cache only
        # covers top tracks and radio seeds almost never hit it. Capped + batched.
        played_result = await session.execute(
            select(TrackInteraction.track_id)
            .where(TrackInteraction.play_count > 0)
            .order_by(TrackInteraction.play_count.desc())
            .limit(_MAX_LIBRARY_SEEDS)
        )
        played_ids = [tid for (tid,) in played_result.all() if tid not in seed_tracks]
        if played_ids:
            meta_rows = await session.execute(
                select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title).where(
                    TrackFeatures.track_id.in_(played_ids)
                )
            )
            for tid, artist, title in meta_rows.all():
                if artist and title and tid not in seed_tracks:
                    seed_tracks[tid] = (artist, title)

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
                    artist=artist,
                    track=title,
                    limit=_SIMILAR_PER_SEED,
                )
                api_calls += 1
            except LastFmError as e:
                if e.code != 6:
                    logger.debug("Last.fm getSimilar failed for %s - %s: %s", artist, title, e)
                    errors += 1
                    continue
                similar = []  # code 6 = not found → fall through to a base-title retry
            except Exception as e:
                logger.debug("Last.fm getSimilar error for %s - %s: %s", artist, title, e)
                errors += 1
                continue

            # Remix/feat titles Last.fm often doesn't index verbatim (e.g. "Broken
            # Angel (Alex Skrindo Remix)"): retry once with the base title so the
            # seed still contributes candidates. Bounded — fires only on empty/miss.
            if not similar:
                base = _base_title(title)
                if base and base != _normalize(title):
                    try:
                        similar = await client.get_similar_tracks(
                            artist=artist,
                            track=base,
                            limit=_SIMILAR_PER_SEED,
                        )
                        api_calls += 1
                        base_seed_retries += 1
                    except LastFmError as e:
                        if e.code != 6:
                            errors += 1
                        similar = []
                    except Exception:
                        errors += 1
                        similar = []
            if not similar:
                continue

            matches: list[tuple[str, float]] = []
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

                # Match against local library: exact key first, then base title.
                local_tid, is_fuzzy = _match_to_library(
                    sim_artist_name, sim_title, lib_lookup, lib_base_lookup
                )
                if local_tid and local_tid != track_id:
                    matches.append((local_tid, sim_match))
                    matched += 1
                    if is_fuzzy:
                        fuzzy_matches += 1

            if matches:
                # Sort by score descending and deduplicate.
                seen: set[str] = set()
                deduped: list[tuple[str, float]] = []
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
        "Last.fm candidates cache built: %d seeds cached, %d matches (%d fuzzy), "
        "%d API calls (%d base-title retries), %d errors.",
        len(new_cache),
        matched,
        fuzzy_matches,
        api_calls,
        base_seed_retries,
        errors,
    )

    return {
        "built": True,
        "seeds_cached": len(new_cache),
        "total_matches": matched,
        "fuzzy_matches": fuzzy_matches,
        "base_seed_retries": base_seed_retries,
        "api_calls": api_calls,
        "errors": errors,
    }


def get_similar_for_track(
    track_id: str,
    k: int = 50,
    exclude_ids: set[str] | None = None,
) -> list[tuple[str, float]]:
    """
    Get Last.fm similar tracks for a single seed track (from cache).

    Returns list of (track_id, score) sorted descending.
    """
    with _lock:
        cached = _similar_cache.get(track_id)

    if not cached:
        return []

    results: list[tuple[str, float]] = []
    for tid, score in cached:
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, score))
        if len(results) >= k:
            break

    return results


def get_similar_for_user(
    top_track_ids: list[str],
    k: int = 100,
    exclude_ids: set[str] | None = None,
) -> list[tuple[str, float]]:
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
    score_map: dict[str, float] = {}
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
