"""
GrooveIQ -- Fill Library pipeline.

Queries AcousticBrainz Lookup for tracks matching each user's taste profile,
groups results by album, and sends the best-matching albums to Lidarr for
download.  Unlike the discovery pipeline (which adds whole artist
discographies), this targets specific albums containing taste-matched tracks.

Flow:
  User taste profile
    -> AB Lookup POST /v1/search (closest strategy)
    -> Filter by distance threshold
    -> Group by mb_album_id
    -> Rank: most matched tracks first, then lowest avg distance
    -> Dedup vs library + Lidarr + previous runs
    -> Add artist (unmonitored) + monitor & search specific album via Lidarr
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import FillLibraryRequest, TrackFeatures, User
from app.services.ab_lookup import AcousticBrainzClient
from app.services.discovery import LidarrClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artist name normalisation (same logic as discovery.py)
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_artist(name: str) -> str:
    n = name.lower().strip()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# Album grouping helpers
# ---------------------------------------------------------------------------


def _group_by_album(
    tracks: list[dict],
    max_distance: float,
    library_artists: set[str],
) -> list[dict[str, Any]]:
    """Group AB search results by album, filter, and rank.

    Returns a list of album dicts sorted by (matched_tracks DESC, avg_distance ASC).
    """
    albums: dict[str, dict[str, Any]] = {}  # mb_album_id -> album info

    for t in tracks:
        distance = t.get("distance")
        mb_album_id = t.get("mb_album_id")
        mb_artist_id = t.get("mb_artist_id")
        artist = t.get("artist")

        # Skip tracks that can't be targeted in Lidarr
        if not mb_album_id or not mb_artist_id:
            continue
        # Skip tracks above distance threshold
        if distance is not None and distance > max_distance:
            continue
        # Skip artists already in library
        if artist and _normalize_artist(artist) in library_artists:
            continue

        if mb_album_id not in albums:
            albums[mb_album_id] = {
                "album_mbid": mb_album_id,
                "album_name": t.get("album"),
                "artist_name": artist or "Unknown",
                "artist_mbid": mb_artist_id,
                "distances": [],
            }

        albums[mb_album_id]["distances"].append(distance or 0.0)

    # Compute stats and rank
    result = []
    for album in albums.values():
        dists = album.pop("distances")
        album["matched_tracks"] = len(dists)
        album["avg_distance"] = sum(dists) / len(dists)
        album["best_distance"] = min(dists)
        result.append(album)

    # Rank: most matched tracks first, then lowest avg distance
    result.sort(key=lambda a: (-a["matched_tracks"], a["avg_distance"]))
    return result


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def run_fill_library(max_albums: int | None = None) -> dict[str, Any]:
    """Run the Fill Library pipeline.

    For each user with a taste profile, queries AB Lookup for matching tracks,
    groups by album, and sends top albums to Lidarr for download.

    Args:
        max_albums: Override for FILL_LIBRARY_MAX_ALBUMS config.

    Returns:
        Summary dict with counts and status.
    """
    if not settings.fill_library_enabled:
        return {
            "status": "not_configured",
            "message": "Set FILL_LIBRARY_ENABLED, AB_LOOKUP_URL, LIDARR_URL, LIDARR_API_KEY",
        }

    album_limit = max_albums or settings.FILL_LIBRARY_MAX_ALBUMS
    max_distance = settings.FILL_LIBRARY_MAX_DISTANCE

    summary: dict[str, Any] = {
        "status": "completed",
        "users_processed": 0,
        "albums_queued": 0,
        "albums_skipped": 0,
        "tracks_matched": 0,
        "tracks_no_album": 0,
        "errors": 0,
    }

    ab_client = AcousticBrainzClient(settings.AB_LOOKUP_URL)
    lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)

    try:
        # Health-check AB Lookup
        health = await ab_client.health_check()
        if health.get("status") != "ready":
            logger.info("Fill Library: AB Lookup not ready (%s), skipping", health.get("status"))
            return {**summary, "status": "ab_not_ready"}

        async with AsyncSessionLocal() as db:
            # Build dedup sets
            library_artists_raw = (
                await db.execute(select(TrackFeatures.artist).where(TrackFeatures.artist.isnot(None)).distinct())
            ).all()
            library_artists = {_normalize_artist(r[0]) for r in library_artists_raw}
            logger.info("Fill Library: %d library artists for dedup", len(library_artists))

            try:
                lidarr_album_ids = await lidarr.get_existing_album_foreign_ids()
                lidarr_artist_ids = await lidarr.get_existing_artist_mbids()
                logger.info(
                    "Fill Library: %d Lidarr albums, %d Lidarr artists for dedup",
                    len(lidarr_album_ids),
                    len(lidarr_artist_ids),
                )
            except Exception as exc:
                logger.error("Fill Library: failed to fetch Lidarr data: %s", exc)
                return {**summary, "status": "error", "error": f"lidarr_fetch_failed: {exc}"}

            # Previous fill-library album MBIDs (avoid re-adding)
            prev_album_mbids = set()
            rows = (
                await db.execute(
                    select(FillLibraryRequest.album_mbid).where(FillLibraryRequest.status.notin_(["failed"]))
                )
            ).all()
            for (mbid,) in rows:
                if mbid:
                    prev_album_mbids.add(mbid)
            logger.info("Fill Library: %d previously requested albums", len(prev_album_mbids))

            # Collect candidate albums across all users
            all_albums: dict[str, dict[str, Any]] = {}  # mb_album_id -> album info + user_id

            users = (await db.execute(select(User))).scalars().all()
            for user in users:
                profile = user.taste_profile
                if not profile or not profile.get("audio_preferences"):
                    continue

                try:
                    results = await ab_client.search(profile, limit=settings.FILL_LIBRARY_QUERY_LIMIT)
                except Exception as exc:
                    logger.warning("Fill Library: AB query failed for user %s: %s", user.user_id, exc)
                    summary["errors"] += 1
                    continue

                if not results:
                    continue

                # Count tracks without album for summary
                for t in results:
                    if not t.get("mb_album_id"):
                        summary["tracks_no_album"] += 1

                summary["tracks_matched"] += len(results)
                summary["users_processed"] += 1

                albums = _group_by_album(results, max_distance, library_artists)

                for album in albums:
                    mbid = album["album_mbid"]
                    if mbid not in all_albums or album["matched_tracks"] > all_albums[mbid]["matched_tracks"]:
                        album["user_id"] = user.user_id
                        all_albums[mbid] = album

            # Dedup against Lidarr and previous runs
            candidates = []
            for album in all_albums.values():
                mbid = album["album_mbid"]
                if mbid in lidarr_album_ids:
                    summary["albums_skipped"] += 1
                    continue
                if mbid in prev_album_mbids:
                    summary["albums_skipped"] += 1
                    continue
                candidates.append(album)

            # Re-sort merged candidates and take top N
            candidates.sort(key=lambda a: (-a["matched_tracks"], a["avg_distance"]))
            candidates = candidates[:album_limit]

            logger.info(
                "Fill Library: %d candidate albums after dedup (limit %d)",
                len(candidates),
                album_limit,
            )

            # Process each album through Lidarr
            for album in candidates:
                await _process_album(album, lidarr, lidarr_artist_ids, db, summary)
                await db.commit()

    except Exception as exc:
        logger.error("Fill Library pipeline error: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = str(exc)
    finally:
        await ab_client.close()
        await lidarr.close()

    logger.info("Fill Library finished: %s", summary)
    return summary


async def _process_album(
    album: dict[str, Any],
    lidarr: LidarrClient,
    lidarr_artist_ids: set[str],
    db: AsyncSession,
    summary: dict[str, Any],
) -> None:
    """Add a single album to Lidarr: ensure artist exists, monitor album, trigger search."""
    artist_mbid = album["artist_mbid"]
    album_mbid = album["album_mbid"]
    artist_name = album["artist_name"]
    status = "pending"
    lidarr_artist_id = None
    lidarr_album_id = None
    error_msg = None

    try:
        # Step 1: Ensure artist exists in Lidarr (unmonitored)
        if artist_mbid not in lidarr_artist_ids:
            try:
                lookup = await lidarr.lookup_artist(mbid=artist_mbid)
                if not lookup:
                    lookup = await lidarr.lookup_artist(name=artist_name)
                if not lookup or not lookup.get("foreignArtistId"):
                    status = "skipped"
                    error_msg = "Artist not found in Lidarr lookup"
                    _record(db, album, status, lidarr_artist_id, lidarr_album_id, error_msg)
                    summary["albums_skipped"] += 1
                    return

                foreign_artist_id = lookup["foreignArtistId"]
                try:
                    result = await lidarr.add_artist_unmonitored(foreign_artist_id, artist_name)
                    lidarr_artist_id = result.get("id")
                    lidarr_artist_ids.add(foreign_artist_id)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code == 409:
                        lidarr_artist_ids.add(foreign_artist_id)
                    else:
                        raise
            except Exception as exc:
                status = "failed"
                error_msg = f"Artist add failed: {exc}"
                _record(db, album, status, lidarr_artist_id, lidarr_album_id, error_msg)
                summary["errors"] += 1
                logger.warning("Fill Library: artist add failed for %s: %s", artist_name, exc)
                return

        status = "artist_added"

        # Step 2: Look up the album in Lidarr
        try:
            album_lookup = await lidarr.lookup_album(album_mbid)
            if not album_lookup or not album_lookup.get("id"):
                status = "skipped"
                error_msg = "Album not found in Lidarr lookup"
                _record(db, album, status, lidarr_artist_id, lidarr_album_id, error_msg)
                summary["albums_skipped"] += 1
                return
            lidarr_album_id = album_lookup["id"]
        except Exception as exc:
            status = "failed"
            error_msg = f"Album lookup failed: {exc}"
            _record(db, album, status, lidarr_artist_id, lidarr_album_id, error_msg)
            summary["errors"] += 1
            logger.warning("Fill Library: album lookup failed for %s: %s", album_mbid, exc)
            return

        # Step 3: Monitor and trigger search
        try:
            await lidarr.monitor_album([lidarr_album_id])
            status = "album_monitored"
            await lidarr.search_album([lidarr_album_id])
            status = "sent"
            summary["albums_queued"] += 1
            logger.info(
                "Fill Library: queued %s - %s (distance=%.3f, %d matched tracks)",
                artist_name,
                album.get("album_name") or album_mbid,
                album["avg_distance"],
                album["matched_tracks"],
            )
        except Exception as exc:
            status = "failed"
            error_msg = f"Album monitor/search failed: {exc}"
            summary["errors"] += 1
            logger.warning(
                "Fill Library: album monitor/search failed for %s: %s",
                album_mbid,
                exc,
            )

    except Exception as exc:
        status = "failed"
        error_msg = str(exc)
        summary["errors"] += 1
        logger.error("Fill Library: unexpected error for %s: %s", album_mbid, exc, exc_info=True)

    _record(db, album, status, lidarr_artist_id, lidarr_album_id, error_msg)


def _record(
    db: AsyncSession,
    album: dict[str, Any],
    status: str,
    lidarr_artist_id: int | None,
    lidarr_album_id: int | None,
    error_message: str | None,
) -> None:
    """Add a FillLibraryRequest row."""
    db.add(
        FillLibraryRequest(
            user_id=album.get("user_id", ""),
            artist_name=album["artist_name"],
            artist_mbid=album.get("artist_mbid"),
            album_name=album.get("album_name"),
            album_mbid=album["album_mbid"],
            matched_tracks=album["matched_tracks"],
            avg_distance=album.get("avg_distance"),
            best_distance=album.get("best_distance"),
            status=status,
            lidarr_artist_id=lidarr_artist_id,
            lidarr_album_id=lidarr_album_id,
            error_message=error_message,
        )
    )
