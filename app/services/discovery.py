"""
GrooveIQ -- Music discovery pipeline.

Discovers new artists via Last.fm (similar artists + genre-based lookup)
and sends them to Lidarr for automatic download.  Runs per-user based
on taste profiles and listening history.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import DiscoveryRequest, TrackFeatures, TrackInteraction, User
from app.services.ab_lookup import AcousticBrainzClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Artist name normalisation (for dedup when mbid is unavailable)
# ---------------------------------------------------------------------------

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_artist(name: str) -> str:
    """Lowercase, strip 'the ', collapse whitespace, remove punctuation."""
    n = name.lower().strip()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# Last.fm client
# ---------------------------------------------------------------------------


class LastFmClient:
    BASE_URL = "https://ws.audioscrobbler.com/2.0/"
    MIN_REQUEST_GAP = 0.2  # 200ms between requests (5 req/s limit)

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=15.0, verify=True)

    async def close(self):
        await self._client.aclose()

    async def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.MIN_REQUEST_GAP:
            await asyncio.sleep(self.MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    async def _get(self, params: dict) -> dict:
        params.update({"api_key": self._api_key, "format": "json"})
        await self._throttle()
        resp = await self._client.get(self.BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_similar_artists(self, artist: str, limit: int = 20) -> list[dict[str, Any]]:
        """Return similar artists from Last.fm."""
        try:
            data = await self._get(
                {
                    "method": "artist.getSimilar",
                    "artist": artist,
                    "limit": limit,
                    "autocorrect": 1,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Last.fm artist.getSimilar failed for %r: %s", artist, exc)
            return []

        artists_raw = data.get("similarartists", {}).get("artist", [])
        if isinstance(artists_raw, dict):
            artists_raw = [artists_raw]

        results = []
        for a in artists_raw:
            results.append(
                {
                    "name": a.get("name", ""),
                    "mbid": a.get("mbid") or None,
                    "match": float(a.get("match", 0)),
                }
            )
        return results

    async def get_top_artists_for_tag(self, tag: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return top artists for a genre/tag from Last.fm."""
        try:
            data = await self._get(
                {
                    "method": "tag.getTopArtists",
                    "tag": tag,
                    "limit": limit,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("Last.fm tag.getTopArtists failed for %r: %s", tag, exc)
            return []

        artists_raw = data.get("topartists", {}).get("artist", [])
        if isinstance(artists_raw, dict):
            artists_raw = [artists_raw]

        results = []
        for a in artists_raw:
            results.append(
                {
                    "name": a.get("name", ""),
                    "mbid": a.get("mbid") or None,
                }
            )
        return results


# ---------------------------------------------------------------------------
# Lidarr client
# ---------------------------------------------------------------------------


class LidarrClient:
    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=30.0,
            verify=True,
            headers={"X-Api-Key": api_key},
        )

    async def close(self):
        await self._client.aclose()

    async def get_existing_artist_mbids(self) -> set[str]:
        """Return the set of MusicBrainz IDs already in Lidarr."""
        resp = await self._client.get(f"{self._base_url}/api/v1/artist")
        resp.raise_for_status()
        return {a["foreignArtistId"] for a in resp.json() if a.get("foreignArtistId")}

    async def lookup_artist(self, *, name: str | None = None, mbid: str | None = None) -> dict[str, Any] | None:
        """Search Lidarr for an artist by mbid (preferred) or name."""
        if mbid:
            term = f"lidarr:{mbid}"
        elif name:
            term = name
        else:
            return None

        resp = await self._client.get(
            f"{self._base_url}/api/v1/artist/lookup",
            params={"term": term},
        )
        resp.raise_for_status()
        results = resp.json()
        return results[0] if results else None

    async def add_artist(self, foreign_artist_id: str, artist_name: str) -> dict[str, Any]:
        """Add an artist to Lidarr."""
        body = {
            "artistName": artist_name,
            "foreignArtistId": foreign_artist_id,
            "qualityProfileId": settings.LIDARR_QUALITY_PROFILE_ID,
            "metadataProfileId": settings.LIDARR_METADATA_PROFILE_ID,
            "rootFolderPath": settings.LIDARR_ROOT_FOLDER,
            "monitored": True,
            "addOptions": {
                "monitor": "all",
                "searchForMissingAlbums": True,
            },
        }
        resp = await self._client.post(
            f"{self._base_url}/api/v1/artist",
            json=body,
        )
        resp.raise_for_status()
        return resp.json()

    # --- Fill Library extensions (album-level control) ---

    async def add_artist_unmonitored(self, foreign_artist_id: str, artist_name: str) -> dict[str, Any]:
        """Add an artist with no albums monitored (for targeted album downloads)."""
        body = {
            "artistName": artist_name,
            "foreignArtistId": foreign_artist_id,
            "qualityProfileId": settings.LIDARR_QUALITY_PROFILE_ID,
            "metadataProfileId": settings.LIDARR_METADATA_PROFILE_ID,
            "rootFolderPath": settings.LIDARR_ROOT_FOLDER,
            "monitored": True,
            "addOptions": {
                "monitor": "none",
                "searchForMissingAlbums": False,
            },
        }
        resp = await self._client.post(f"{self._base_url}/api/v1/artist", json=body)
        resp.raise_for_status()
        return resp.json()

    async def get_existing_album_foreign_ids(self) -> set[str]:
        """Return the set of MusicBrainz release group IDs already in Lidarr."""
        resp = await self._client.get(f"{self._base_url}/api/v1/album")
        resp.raise_for_status()
        return {a["foreignAlbumId"] for a in resp.json() if a.get("foreignAlbumId")}

    async def lookup_album(self, mb_release_group_id: str) -> dict[str, Any] | None:
        """Look up an album by MusicBrainz release group ID."""
        resp = await self._client.get(
            f"{self._base_url}/api/v1/album/lookup",
            params={"term": f"lidarr:{mb_release_group_id}"},
        )
        resp.raise_for_status()
        results = resp.json()
        return results[0] if results else None

    async def monitor_album(self, album_ids: list[int]) -> None:
        """Set specific albums to monitored."""
        resp = await self._client.put(
            f"{self._base_url}/api/v1/album/monitor",
            json={"albumIds": album_ids, "monitored": True},
        )
        resp.raise_for_status()

    async def search_album(self, album_ids: list[int]) -> None:
        """Trigger Lidarr to search and download specific albums."""
        resp = await self._client.post(
            f"{self._base_url}/api/v1/command",
            json={"name": "AlbumSearch", "albumIds": album_ids},
        )
        resp.raise_for_status()

    # --- Backfill-engine extensions (drain wanted/missing through streamrip) ---

    async def get_missing_albums(
        self,
        page_size: int = 100,
        monitored: bool = True,
        sort_key: str = "albums.releaseDate",
        sort_direction: str = "descending",
    ) -> list[dict[str, Any]]:
        """Return all rows from /api/v1/wanted/missing, paginating as needed.

        Each row carries the Lidarr album id, title, artist sub-object
        (with foreignArtistId), foreignAlbumId (MB release group), and
        releaseDate. ``sort_key`` is one of Lidarr's documented keys
        (``albums.title``, ``albums.releaseDate``, ``artists.sortName``,
        ``albums.added``); ``sort_direction`` is ``ascending`` or
        ``descending``.
        """
        return await self._fetch_wanted_pages("/api/v1/wanted/missing", page_size, monitored, sort_key, sort_direction)

    async def get_cutoff_unmet_albums(
        self,
        page_size: int = 100,
        monitored: bool = True,
        sort_key: str = "albums.releaseDate",
        sort_direction: str = "descending",
    ) -> list[dict[str, Any]]:
        """Return all rows from /api/v1/wanted/cutoff (quality-upgrade queue)."""
        return await self._fetch_wanted_pages("/api/v1/wanted/cutoff", page_size, monitored, sort_key, sort_direction)

    async def _fetch_wanted_pages(
        self,
        path: str,
        page_size: int,
        monitored: bool,
        sort_key: str,
        sort_direction: str,
    ) -> list[dict[str, Any]]:
        """Walk Lidarr pagination until totalRecords is exhausted."""
        records: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = await self._client.get(
                f"{self._base_url}{path}",
                params={
                    "page": page,
                    "pageSize": page_size,
                    "sortKey": sort_key,
                    "sortDirection": sort_direction,
                    "monitored": "true" if monitored else "false",
                    "includeArtist": "true",
                },
            )
            resp.raise_for_status()
            body = resp.json()
            page_records = body.get("records") if isinstance(body, dict) else None
            if not page_records:
                break
            records.extend(page_records)
            total = body.get("totalRecords") or len(records)
            if len(records) >= total or len(page_records) < page_size:
                break
            page += 1
        return records

    async def trigger_downloaded_scan(self, path: str | None = None) -> None:
        """POST /api/v1/command DownloadedAlbumsScan to import dropped files.

        ``path`` should be the directory streamrip writes into, as visible to
        the Lidarr container. Lidarr will pick the file up, match it to the
        monitored album, and move it into the artist library.
        """
        body: dict[str, Any] = {"name": "DownloadedAlbumsScan"}
        if path:
            body["path"] = path
        resp = await self._client.post(f"{self._base_url}/api/v1/command", json=body)
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Discovery pipeline
# ---------------------------------------------------------------------------


def _extract_seed_artists(taste_profile: dict, limit: int = 10) -> list[str]:
    """Get the most-listened artist names from a taste profile."""
    top_tracks = taste_profile.get("top_tracks", [])
    # Count artist frequency (weighted by position — earlier = higher score)
    artist_scores: dict[str, float] = {}
    for i, t in enumerate(top_tracks):
        artist = t.get("artist")
        if artist:
            artist_scores[artist] = artist_scores.get(artist, 0) + 1.0 / (i + 1)
    sorted_artists = sorted(artist_scores, key=artist_scores.get, reverse=True)
    return sorted_artists[:limit]


def _extract_seed_genres(taste_profile: dict, track_genres: list[str], limit: int = 5) -> list[str]:
    """Get the most common genres from the user's library tracks."""
    genre_counts: dict[str, int] = {}
    for genre_str in track_genres:
        for g in genre_str.split(","):
            g = g.strip().lower()
            if g:
                genre_counts[g] = genre_counts.get(g, 0) + 1
    sorted_genres = sorted(genre_counts, key=genre_counts.get, reverse=True)
    return sorted_genres[:limit]


async def _get_user_track_genres(user_id: str, session: AsyncSession) -> list[str]:
    """Get genre strings for tracks the user has interacted with."""
    result = await session.execute(
        select(TrackFeatures.genre)
        .join(TrackInteraction, TrackInteraction.track_id == TrackFeatures.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackFeatures.genre.isnot(None),
        )
        .order_by(TrackInteraction.satisfaction_score.desc())
        .limit(100)
    )
    return [row[0] for row in result.all()]


async def run_discovery_pipeline() -> dict[str, Any]:
    """
    Main entry point.  For each user with a taste profile:
    1. Extract seed artists + genres
    2. Query Last.fm for similar / genre-based artists
    3. Filter out known artists
    4. Send new discoveries to Lidarr
    5. Record in discovery_requests table
    """
    from app.services.download_chain import lidarr_enabled_in_chain

    if not settings.discovery_enabled:
        logger.warning("Discovery pipeline skipped: not configured (set LASTFM_API_KEY, LIDARR_URL, LIDARR_API_KEY)")
        return {"status": "skipped", "reason": "not_configured"}

    # Allow operators to disable discovery from the routing GUI without
    # tearing down the underlying Lidarr config.
    if not lidarr_enabled_in_chain("bulk_album"):
        logger.info("Discovery pipeline skipped: Lidarr disabled in bulk_album routing chain")
        return {"status": "skipped", "reason": "lidarr_disabled_in_routing"}

    summary = {
        "users_processed": 0,
        "artists_discovered": 0,
        "artists_sent_to_lidarr": 0,
        "errors": 0,
        "status": "completed",
    }

    lastfm = LastFmClient(settings.LASTFM_API_KEY)
    lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)

    try:
        async with AsyncSessionLocal() as session:
            # Daily budget check.
            today_start = int(time.time()) - (int(time.time()) % 86400)
            today_count = (
                await session.execute(
                    select(func.count()).select_from(DiscoveryRequest).where(DiscoveryRequest.created_at >= today_start)
                )
            ).scalar() or 0
            remaining_budget = settings.DISCOVERY_MAX_REQUESTS_PER_DAY - today_count
            logger.info("Discovery budget: %d/%d remaining", remaining_budget, settings.DISCOVERY_MAX_REQUESTS_PER_DAY)

            if remaining_budget <= 0:
                logger.info(
                    "Discovery daily limit reached (%d/%d)", today_count, settings.DISCOVERY_MAX_REQUESTS_PER_DAY
                )
                return {"status": "daily_limit_reached", **summary}

            # Build dedup sets.
            library_artists_raw = (
                await session.execute(select(TrackFeatures.artist).where(TrackFeatures.artist.isnot(None)).distinct())
            ).all()
            library_artists_norm = {_normalize_artist(r[0]) for r in library_artists_raw}
            logger.info("Discovery dedup: %d library artists", len(library_artists_norm))

            try:
                lidarr_mbids = await lidarr.get_existing_artist_mbids()
                logger.info("Discovery dedup: %d artists already in Lidarr", len(lidarr_mbids))
            except Exception as exc:
                logger.error("Failed to fetch Lidarr artists: %s", exc, exc_info=True)
                return {"status": "error", "reason": f"lidarr_fetch_failed: {exc}", **summary}

            already_requested = set()
            already_requested_names = set()
            rows = (
                await session.execute(
                    select(DiscoveryRequest.artist_mbid, DiscoveryRequest.artist_name).where(
                        DiscoveryRequest.status != "failed"
                    )
                )
            ).all()
            for mbid, name in rows:
                if mbid:
                    already_requested.add(mbid)
                already_requested_names.add(_normalize_artist(name))

            # Process each user.
            users = (await session.execute(select(User))).scalars().all()
            logger.info("Discovery: found %d users to process", len(users))

            for user in users:
                if remaining_budget <= 0:
                    break

                try:
                    profile = user.taste_profile
                    seed_artists: list[str] = []
                    seed_genres: list[str] = []

                    if profile and profile.get("top_tracks"):
                        # User has a taste profile — extract seeds from it.
                        seed_artists = _extract_seed_artists(profile)
                        track_genres = await _get_user_track_genres(user.user_id, session)
                        seed_genres = _extract_seed_genres(profile, track_genres)

                    if not seed_artists and not seed_genres:
                        # Fallback: seed from the library's existing artists + genres.
                        lib_artists = (
                            await session.execute(
                                select(TrackFeatures.artist)
                                .where(TrackFeatures.artist.isnot(None))
                                .group_by(TrackFeatures.artist)
                                .order_by(func.count().desc())
                                .limit(10)
                            )
                        ).all()
                        seed_artists = [r[0] for r in lib_artists]

                        lib_genres = (
                            await session.execute(
                                select(TrackFeatures.genre)
                                .where(TrackFeatures.genre.isnot(None))
                                .group_by(TrackFeatures.genre)
                                .order_by(func.count().desc())
                                .limit(20)
                            )
                        ).all()
                        all_genres: list[str] = []
                        for r in lib_genres:
                            for g in r[0].split(","):
                                g = g.strip().lower()
                                if g and g not in all_genres:
                                    all_genres.append(g)
                        seed_genres = all_genres[:5]

                    if not seed_artists and not seed_genres:
                        logger.info("Discovery for user %s: no seeds found (empty library?), skipping", user.user_id)
                        continue

                    logger.info(
                        "Discovery for user %s: %d seed artists %r, %d seed genres %r",
                        user.user_id,
                        len(seed_artists),
                        seed_artists[:3],
                        len(seed_genres),
                        seed_genres[:3],
                    )

                    # Collect candidates from Last.fm.
                    candidates: list[tuple[dict[str, Any], str, str | None, str | None]] = []
                    # (artist_info, source, seed_artist, seed_genre)

                    for artist_name in seed_artists:
                        similar = await lastfm.get_similar_artists(
                            artist_name,
                            limit=settings.DISCOVERY_SIMILAR_LIMIT,
                        )
                        for a in similar:
                            candidates.append((a, "lastfm_similar", artist_name, None))

                    for genre in seed_genres:
                        top = await lastfm.get_top_artists_for_tag(genre, limit=50)
                        for a in top:
                            candidates.append((a, "lastfm_genre", None, genre))

                    # Deduplicate and filter.
                    seen_in_batch: set[str] = set()
                    for artist_info, source, seed_a, seed_g in candidates:
                        if remaining_budget <= 0:
                            break

                        name = artist_info.get("name", "").strip()
                        mbid = artist_info.get("mbid")
                        if not name:
                            continue

                        norm = _normalize_artist(name)

                        # Skip if already known.
                        if norm in seen_in_batch:
                            continue
                        seen_in_batch.add(norm)

                        if norm in library_artists_norm:
                            continue
                        if norm in already_requested_names:
                            continue
                        if mbid and mbid in already_requested:
                            continue
                        if mbid and mbid in lidarr_mbids:
                            continue

                        # Look up in Lidarr to get the foreignArtistId.
                        try:
                            lookup = await lidarr.lookup_artist(mbid=mbid, name=name)
                        except Exception as exc:
                            logger.warning("Lidarr lookup failed for %r: %s", name, exc)
                            summary["errors"] += 1
                            continue

                        if not lookup:
                            logger.debug("Lidarr lookup returned no results for %r", name)
                            continue

                        foreign_id = lookup.get("foreignArtistId")
                        if not foreign_id:
                            continue

                        # Skip if already in Lidarr by foreignArtistId.
                        if foreign_id in lidarr_mbids:
                            continue

                        # Add to Lidarr.
                        status = "pending"
                        lidarr_id = None
                        error_msg = None
                        try:
                            result = await lidarr.add_artist(foreign_id, name)
                            lidarr_id = result.get("id")
                            status = "sent"
                            lidarr_mbids.add(foreign_id)  # prevent re-adding in same run
                            summary["artists_sent_to_lidarr"] += 1
                            logger.info("Added artist to Lidarr: %s (mbid=%s)", name, foreign_id)
                        except httpx.HTTPStatusError as exc:
                            if exc.response.status_code == 409:
                                # Already exists in Lidarr.
                                status = "in_lidarr"
                                lidarr_mbids.add(foreign_id)
                            else:
                                status = "failed"
                                error_msg = f"HTTP {exc.response.status_code}"
                                summary["errors"] += 1
                                logger.warning(
                                    "Lidarr add failed for %r: HTTP %d: %s",
                                    name,
                                    exc.response.status_code,
                                    exc.response.text[:200],
                                )
                        except Exception as exc:
                            status = "failed"
                            error_msg = "Internal error"
                            summary["errors"] += 1
                            logger.warning("Lidarr add failed for %r: %s", name, exc)

                        # Record in DB.
                        session.add(
                            DiscoveryRequest(
                                user_id=user.user_id,
                                artist_name=name,
                                artist_mbid=mbid or foreign_id,
                                source=source,
                                seed_artist=seed_a,
                                seed_genre=seed_g,
                                similarity_score=artist_info.get("match"),
                                status=status,
                                lidarr_artist_id=lidarr_id,
                                error_message=error_msg,
                            )
                        )
                        already_requested_names.add(norm)
                        if mbid:
                            already_requested.add(mbid)
                        summary["artists_discovered"] += 1
                        remaining_budget -= 1

                    summary["users_processed"] += 1
                    await session.commit()

                except Exception as exc:
                    logger.error("Discovery failed for user %s: %s", user.user_id, exc, exc_info=True)
                    summary["errors"] += 1

    except Exception as exc:
        logger.error("Discovery pipeline error: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = str(exc)
    finally:
        await lastfm.close()
        await lidarr.close()

    # --- AcousticBrainz discovery (optional, runs alongside Last.fm) ---
    if settings.ab_lookup_enabled:
        try:
            ab_results = await _run_ab_discovery(session=None)
            summary["ab_tracks_discovered"] = ab_results.get("tracks_discovered", 0)
            summary["ab_artists_sent"] = ab_results.get("artists_sent_to_lidarr", 0)
        except Exception as exc:
            logger.error("AcousticBrainz discovery failed: %s", exc, exc_info=True)
            summary["ab_error"] = str(exc)

    logger.info("Discovery pipeline finished: %s", summary)
    return summary


async def _run_ab_discovery(session: AsyncSession | None = None) -> dict[str, Any]:
    """Discover tracks via the AcousticBrainz Lookup container.

    For each user with a taste profile, query AB Lookup for matching tracks
    not in the local library, then send new artists to Lidarr or tracks
    to spotdl-api for download.
    """
    summary: dict[str, Any] = {
        "users_processed": 0,
        "tracks_discovered": 0,
        "artists_sent_to_lidarr": 0,
        "errors": 0,
    }

    ab_client = AcousticBrainzClient(settings.AB_LOOKUP_URL)
    lidarr: LidarrClient | None = None
    if settings.LIDARR_URL and settings.LIDARR_API_KEY:
        lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)

    try:
        # Check AB Lookup is ready
        health = await ab_client.health_check()
        if health.get("status") != "ready":
            logger.info("AcousticBrainz Lookup not ready (%s), skipping", health.get("status"))
            return summary

        async with AsyncSessionLocal() as db:
            # Get library artists for dedup
            library_artists_raw = (
                await db.execute(select(TrackFeatures.artist).where(TrackFeatures.artist.isnot(None)).distinct())
            ).all()
            library_artists_norm = {_normalize_artist(r[0]) for r in library_artists_raw}

            lidarr_mbids: set[str] = set()
            if lidarr:
                try:
                    lidarr_mbids = await lidarr.get_existing_artist_mbids()
                except Exception as exc:
                    logger.warning("Failed to fetch Lidarr artists for AB discovery: %s", exc)

            users = (await db.execute(select(User))).scalars().all()

            for user in users:
                profile = user.taste_profile
                if not profile or not profile.get("audio_preferences"):
                    continue

                try:
                    results = await ab_client.search(profile, limit=settings.AB_DISCOVERY_LIMIT)
                    if not results:
                        continue

                    # Filter out artists already in library
                    new_artists: dict[str, dict[str, Any]] = {}
                    for track in results:
                        artist = track.get("artist")
                        if not artist:
                            continue
                        norm = _normalize_artist(artist)
                        if norm in library_artists_norm:
                            continue
                        mb_artist_id = track.get("mb_artist_id")
                        if mb_artist_id and mb_artist_id in lidarr_mbids:
                            continue
                        if norm not in new_artists:
                            new_artists[norm] = {
                                "name": artist,
                                "mb_artist_id": mb_artist_id,
                                "sample_track": track.get("title"),
                                "mbid": track.get("mbid"),
                            }

                    # Send to Lidarr by artist MBID
                    for norm, info in new_artists.items():
                        mb_artist_id = info.get("mb_artist_id")
                        name = info["name"]

                        if lidarr and mb_artist_id:
                            try:
                                lookup = await lidarr.lookup_artist(mbid=mb_artist_id)
                                if lookup:
                                    foreign_id = lookup.get("foreignArtistId")
                                    if foreign_id and foreign_id not in lidarr_mbids:
                                        await lidarr.add_artist(foreign_id, name)
                                        lidarr_mbids.add(foreign_id)
                                        summary["artists_sent_to_lidarr"] += 1
                                        logger.info(
                                            "AB discovery: added %s to Lidarr (mbid=%s)",
                                            name,
                                            foreign_id,
                                        )
                            except httpx.HTTPStatusError as exc:
                                if exc.response.status_code != 409:
                                    logger.warning(
                                        "AB discovery: Lidarr add failed for %s: %s",
                                        name,
                                        exc,
                                    )
                                    summary["errors"] += 1
                            except Exception as exc:
                                logger.warning("AB discovery: Lidarr add failed for %s: %s", name, exc)
                                summary["errors"] += 1

                        # Record discovery
                        db.add(
                            DiscoveryRequest(
                                user_id=user.user_id,
                                artist_name=name,
                                artist_mbid=mb_artist_id,
                                source="acousticbrainz",
                                status="sent" if mb_artist_id else "pending",
                            )
                        )
                        summary["tracks_discovered"] += 1
                        library_artists_norm.add(norm)

                    summary["users_processed"] += 1
                    await db.commit()

                except Exception as exc:
                    logger.error(
                        "AB discovery failed for user %s: %s",
                        user.user_id,
                        exc,
                        exc_info=True,
                    )
                    summary["errors"] += 1

    finally:
        await ab_client.close()
        if lidarr:
            await lidarr.close()

    logger.info("AcousticBrainz discovery finished: %s", summary)
    return summary
