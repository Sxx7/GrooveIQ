"""
GrooveIQ -- Charts service.

Fetches global and genre-based charts from Last.fm, matches tracks/artists
to the local library, optionally sends missing artists to Lidarr for download,
and persists chart snapshots for the API.

Charts are rebuilt periodically (default: every 24h) by the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ChartEntry, DiscoveryRequest, TrackFeatures

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Last.fm returns this hash as a generic placeholder for all tracks/artists
# since ~2020.  It's a grey music note — useless as a real image.
_LASTFM_PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"

# DownloadRequest statuses that mean "already have it, or it's in flight" — used
# to skip re-queuing a chart track we've already fetched (issue #64). A prior
# *failure* is deliberately absent, so failed tracks stay eligible for retry on
# the next daily build.
_DL_ACTIVE_OR_DONE = ("completed", "queued", "downloading", "duplicate", "processing")


def _pick_image_url(images: list) -> str | None:
    """Pick the best image URL from Last.fm's image array.

    Prefers extralarge (300x300). Falls back through smaller sizes.
    Returns None if all URLs are empty or are the generic placeholder.
    """
    if not images:
        return None
    by_size = {img.get("size", ""): img.get("#text", "") for img in images if isinstance(img, dict)}
    for preferred in ("extralarge", "large", "mega", "medium", "small"):
        url = by_size.get(preferred, "")
        if url:
            if _LASTFM_PLACEHOLDER_HASH in url:
                return None  # generic placeholder, not a real image
            return url
    return None


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


# Last.fm chart titles routinely append a featured artist after a literal "+"
# ("Stateside + Zara Larsson") and library tags often use "(feat./ft./featuring X)"
# parentheticals — both refer to the same recording. Strip these suffixes
# before matching so the cross-reference doesn't false-negative.
#
# Patterns we strip (case-insensitive, anchored to a separator on the left):
#   "<title> + <name>"
#   "<title> (feat. <name>)" / "(ft. <name>)" / "(featuring <name>)"
#   "<title> (with <name>)"
#   "<title> [feat. <name>]" / "[ft. ...]" / "[featuring ...]"
_FEATURE_RE = re.compile(
    r"\s*(?:"
    # "+ Zara Larsson" / "+ Foo & Bar" — require ≥2 tokens after "+" so we
    # don't strip legitimate single-word suffixes ("Up + Down").
    r"\+\s+\S+\s+\S+.*"
    r"|\((?:feat|ft|featuring|with)\.?\s+[^)]+\)"  # "(feat. X)" / "(ft. X)" / "(with X)"
    r"|\[(?:feat|ft|featuring|with)\.?\s+[^\]]+\]"  # "[feat. X]"
    r")\s*$",
    re.IGNORECASE,
)


def _strip_features(title: str) -> str:
    """Remove a trailing featured-artist clause from a track title.

    Returns the input unchanged when no feature suffix is present, so callers
    can safely use both the original and stripped forms as alternate lookup
    keys.
    """
    if not title:
        return title
    return _FEATURE_RE.sub("", title).rstrip()


def _snapshot_date(now: int) -> str:
    """UTC calendar date ('YYYY-MM-DD') for a build timestamp (issue #75).

    Derived from the shared ``now`` so every chart in one build run lands on the
    same snapshot date. UTC (not local) to stay consistent with the scheduler's
    UTC cron jobs and the epoch-based ``fetched_at``.
    """
    return time.strftime("%Y-%m-%d", time.gmtime(now))


# ---------------------------------------------------------------------------
# Last.fm chart API
# ---------------------------------------------------------------------------


class _ChartClient:
    """Thin wrapper around Last.fm chart/geo/tag endpoints."""

    BASE_URL = "https://ws.audioscrobbler.com/2.0/"
    MIN_REQUEST_GAP = 0.2

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=15.0, verify=True)

    async def close(self):
        await self._client.aclose()

    async def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.MIN_REQUEST_GAP:
            import asyncio

            await asyncio.sleep(self.MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    async def _get(self, params: dict) -> dict:
        params.update({"api_key": self._api_key, "format": "json"})
        await self._throttle()
        resp = await self._client.get(self.BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_top_tracks(self, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Global top tracks (chart.getTopTracks)."""
        try:
            data = await self._get(
                {
                    "method": "chart.getTopTracks",
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("chart.getTopTracks failed: %s", exc)
            return []
        tracks = data.get("tracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_top_artists(self, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Global top artists (chart.getTopArtists)."""
        try:
            data = await self._get(
                {
                    "method": "chart.getTopArtists",
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("chart.getTopArtists failed: %s", exc)
            return []
        artists = data.get("artists", {}).get("artist", [])
        return artists if isinstance(artists, list) else [artists]

    async def get_geo_top_tracks(self, country: str, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Top tracks by country (geo.getTopTracks)."""
        try:
            data = await self._get(
                {
                    "method": "geo.getTopTracks",
                    "country": country,
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("geo.getTopTracks failed for %r: %s", country, exc)
            return []
        tracks = data.get("tracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_tag_top_tracks(self, tag: str, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Top tracks by genre tag (tag.getTopTracks)."""
        try:
            data = await self._get(
                {
                    "method": "tag.getTopTracks",
                    "tag": tag,
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("tag.getTopTracks failed for %r: %s", tag, exc)
            return []
        # NB: tag.getTopTracks JSON root is "tracks" (the XML element is
        # <toptracks>, but Last.fm's JSON serialisation renames it), unlike
        # tag.getTopArtists ("topartists") / tag.getTopAlbums ("albums").
        tracks = data.get("tracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_tag_top_artists(self, tag: str, limit: int = 100) -> list[dict[str, Any]]:
        """Top artists by genre tag (tag.getTopArtists)."""
        try:
            data = await self._get(
                {
                    "method": "tag.getTopArtists",
                    "tag": tag,
                    "limit": limit,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("tag.getTopArtists failed for %r: %s", tag, exc)
            return []
        artists = data.get("topartists", {}).get("artist", [])
        return artists if isinstance(artists, list) else [artists]

    async def get_geo_top_artists(self, country: str, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Top artists by country (geo.getTopArtists)."""
        try:
            data = await self._get(
                {
                    "method": "geo.getTopArtists",
                    "country": country,
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("geo.getTopArtists failed for %r: %s", country, exc)
            return []
        artists = data.get("topartists", {}).get("artist", [])
        return artists if isinstance(artists, list) else [artists]

    async def get_tag_top_albums(self, tag: str, limit: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Top albums by genre tag (tag.getTopAlbums).

        Last.fm exposes album charts only per-tag — there is no global or geo
        album chart — so album scopes are always ``tag:<genre>``.
        """
        try:
            data = await self._get(
                {
                    "method": "tag.getTopAlbums",
                    "tag": tag,
                    "limit": limit,
                    "page": page,
                }
            )
        except httpx.HTTPStatusError as exc:
            logger.warning("tag.getTopAlbums failed for %r: %s", tag, exc)
            return []
        albums = data.get("albums", {}).get("album", [])
        return albums if isinstance(albums, list) else [albums]


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------


async def _build_library_lookup(session: AsyncSession) -> dict[tuple[str, str], str]:
    """Build (normalized_artist, normalized_title) -> track_id lookup.

    Registers both the canonical title and the feature-stripped title when
    they differ, so chart rows with "+ <featured>" or "(feat. …)" suffixes
    match library tracks that lack them — and vice versa.
    """
    rows = (
        await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title).where(
                TrackFeatures.artist.isnot(None), TrackFeatures.title.isnot(None)
            )
        )
    ).all()
    lookup: dict[tuple[str, str], str] = {}
    for track_id, artist, title in rows:
        artist_norm = _normalize(artist)
        title_norm = _normalize(title)
        lookup[(artist_norm, title_norm)] = track_id
        stripped = _normalize(_strip_features(title))
        if stripped and stripped != title_norm:
            # ``setdefault`` so the canonical key wins when two library rows
            # collapse to the same stripped form (e.g. a track and its
            # "feat. …" remix variant).
            lookup.setdefault((artist_norm, stripped), track_id)
    return lookup


async def _build_artist_lookup(session: AsyncSession) -> dict[str, list[str]]:
    """Build normalized_artist -> [track_id, ...] lookup."""
    rows = (
        await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist).where(TrackFeatures.artist.isnot(None))
        )
    ).all()
    lookup: dict[str, list[str]] = {}
    for track_id, artist in rows:
        norm = _normalize(artist)
        lookup.setdefault(norm, []).append(track_id)
    return lookup


async def _build_album_lookup(session: AsyncSession) -> dict[tuple[str, str], list[str]]:
    """Build (normalized_artist, normalized_album) -> [track_id, ...] lookup.

    Prefers ``album_artist`` over ``artist`` for the key when present, since a
    chart album's credited artist is the album artist (matters for compilations
    and "feat." tracks). Powers library matching for genre album charts.
    """
    rows = (
        await session.execute(
            select(
                TrackFeatures.track_id,
                TrackFeatures.artist,
                TrackFeatures.album_artist,
                TrackFeatures.album,
            ).where(TrackFeatures.album.isnot(None))
        )
    ).all()
    lookup: dict[tuple[str, str], list[str]] = {}
    for track_id, artist, album_artist, album in rows:
        credited = album_artist or artist
        if not credited or not album:
            continue
        key = (_normalize(credited), _normalize(album))
        lookup.setdefault(key, []).append(track_id)
    return lookup


# ---------------------------------------------------------------------------
# Lidarr integration (reuse discovery's LidarrClient)
# ---------------------------------------------------------------------------


async def _send_artists_to_lidarr(
    artist_names_mbids: list[tuple[str, str | None]],
    max_adds: int = 50,
) -> dict[str, int]:
    """Send chart artists not in library to Lidarr for download."""
    if not settings.discovery_enabled:
        return {"skipped": len(artist_names_mbids), "reason": "lidarr_not_configured"}

    from app.services.discovery import LidarrClient

    lidarr = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
    stats = {"sent": 0, "already_in_lidarr": 0, "lookup_failed": 0, "errors": 0}

    try:
        existing_mbids = await lidarr.get_existing_artist_mbids()

        async with AsyncSessionLocal() as session:
            already_requested = set()
            rows = (
                await session.execute(select(DiscoveryRequest.artist_mbid).where(DiscoveryRequest.status != "failed"))
            ).all()
            already_requested = {r[0] for r in rows if r[0]}

        added = 0
        for name, mbid in artist_names_mbids:
            if added >= max_adds:
                break
            if mbid and (mbid in existing_mbids or mbid in already_requested):
                stats["already_in_lidarr"] += 1
                continue

            try:
                lookup = await lidarr.lookup_artist(mbid=mbid, name=name)
            except Exception:
                stats["lookup_failed"] += 1
                continue

            if not lookup:
                stats["lookup_failed"] += 1
                continue

            foreign_id = lookup.get("foreignArtistId")
            if not foreign_id or foreign_id in existing_mbids:
                stats["already_in_lidarr"] += 1
                continue

            try:
                await lidarr.add_artist(foreign_id, name)
                existing_mbids.add(foreign_id)
                stats["sent"] += 1
                added += 1
                logger.info("Charts: added artist to Lidarr: %s", name)

                async with AsyncSessionLocal() as session:
                    session.add(
                        DiscoveryRequest(
                            user_id="__charts__",
                            artist_name=name,
                            artist_mbid=mbid or foreign_id,
                            source="chart",
                            status="sent",
                        )
                    )
                    await session.commit()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    stats["already_in_lidarr"] += 1
                else:
                    stats["errors"] += 1
            except Exception:
                stats["errors"] += 1
    finally:
        await lidarr.close()

    return stats


# ---------------------------------------------------------------------------
# Cascade auto-download (individual chart-track downloads, issue #64)
# ---------------------------------------------------------------------------


async def _send_tracks_via_cascade(
    tracks: list[tuple[str, str]],
    max_adds: int = 20,
) -> dict[str, int]:
    """Auto-download unmatched chart tracks through the download cascade (issue #64).

    Tracks are fetched on a **fast lane** (:func:`build_fast_lane_chain` — spotdl/
    YouTube first, streamrip last) so they never queue behind the Lidarr backfill's
    streamrip lock. Candidates arrive in chart-position order, so the ``max_adds``
    cap naturally prioritises the highest-ranked not-in-library tracks.

    Tracks already downloaded or in flight (per ``DownloadRequest`` history) are
    skipped so a daily rebuild doesn't re-queue the same chart-toppers. Each queued
    download persists a ``download_requests`` row (visible in ``GET /v1/downloads``)
    and spawns the appropriate watcher so completion triggers the media-server
    refresh + GrooveIQ library scan.
    """
    from app.models.db import DownloadRequest
    from app.services.download_chain import (
        TrackRef,
        build_fast_lane_chain,
        try_download_chain,
    )
    from app.services.download_dispatch import persist_cascade, spawn_watcher

    if not settings.download_enabled:
        logger.warning("Charts: no download backend configured, skipping track downloads")
        return {"sent": 0, "not_found": 0, "duplicate": 0, "errors": 0, "already_have": 0}

    stats = {"sent": 0, "not_found": 0, "duplicate": 0, "errors": 0, "already_have": 0}

    # Deduplicate by normalised artist+title (within this run).
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for artist, title in tracks:
        key = (_normalize(artist), _normalize(title))
        if key not in seen and key[0] and key[1]:
            seen.add(key)
            unique.append((artist, title))

    # Skip tracks we've already downloaded or that are in flight, so a daily
    # rebuild doesn't re-queue the same chart-toppers every day. Only
    # success/in-flight statuses count — a prior failure stays eligible for retry.
    already: set[tuple[str, str]] = set()
    try:
        async with AsyncSessionLocal() as hist_session:
            rows = (
                await hist_session.execute(
                    select(DownloadRequest.artist_name, DownloadRequest.track_title).where(
                        DownloadRequest.status.in_(_DL_ACTIVE_OR_DONE)
                    )
                )
            ).all()
        for a, t in rows:
            if a and t:
                already.add((_normalize(a), _normalize(t)))
    except Exception as exc:
        logger.warning("Charts: could not load download history for dedup: %s", exc)

    # Compute the fast-lane chain once (cheap, in-memory routing config).
    fast_lane = build_fast_lane_chain()

    sent = 0
    for artist, title in unique:
        if sent >= max_adds:
            break

        if (_normalize(artist), _normalize(title)) in already:
            stats["already_have"] += 1
            continue

        track_ref = TrackRef(artist=artist, title=title)
        try:
            cascade = await try_download_chain(track_ref, chain_override=fast_lane)
        except Exception as exc:
            logger.error("Charts cascade failed for %s — %s: %s", artist, title, exc)
            stats["errors"] += 1
            continue

        try:
            async with AsyncSessionLocal() as dl_session:
                record = await persist_cascade(
                    session=dl_session,
                    cascade=cascade,
                    track_title=title,
                    artist_name=artist,
                    requested_by="__charts__",
                )
                await dl_session.commit()
                if cascade.success:
                    try:
                        await spawn_watcher(record, cascade)
                    except Exception as exc:
                        logger.warning("Charts: watcher spawn failed for %s — %s: %s", artist, title, exc)
        except Exception as exc:
            logger.warning("Charts: could not persist download for %s — %s: %s", artist, title, exc)
            stats["errors"] += 1
            continue

        if not cascade.success:
            # No backend matched/succeeded — bucket between "not_found" (every
            # attempt was a clean skip / no-match) and "errors" (any real failure).
            had_real_failure = any(att.status not in ("skipped",) for att in cascade.attempts)
            if had_real_failure:
                stats["errors"] += 1
            else:
                stats["not_found"] += 1
            continue

        if cascade.final_status == "duplicate":
            stats["duplicate"] += 1
        else:
            stats["sent"] += 1
            sent += 1
            logger.info("Charts: queued %s — %s via %s", artist, title, cascade.final_backend)

    return stats


# Back-compat alias — old name retained so external callers (if any) keep working.
_send_tracks_to_spotizerr = _send_tracks_via_cascade


# ---------------------------------------------------------------------------
# Chart build pipeline
# ---------------------------------------------------------------------------


async def build_charts() -> dict[str, Any]:
    """
    Main entry point. Fetches charts from Last.fm, matches to library,
    optionally sends missing artists to Lidarr, and stores chart entries.
    """
    if not settings.LASTFM_API_KEY:
        logger.warning("Charts build skipped: LASTFM_API_KEY not configured")
        return {"status": "skipped", "reason": "no_lastfm_api_key"}

    client = _ChartClient(settings.LASTFM_API_KEY)
    summary: dict[str, Any] = {
        "status": "completed",
        "charts_built": 0,
        "total_entries": 0,
        "library_matches": 0,
        "cover_art_resolved": 0,
        "artists_sent_to_lidarr": 0,
        "tracks_sent_to_spotizerr": 0,
        "errors": 0,
    }

    now = int(time.time())

    # Long-lived Spotizerr client used to fall back for cover art on
    # unmatched entries when Last.fm returns its placeholder image.
    # Shared across all chart builds in this run so connection pooling
    # and auth token caching are reused.
    from app.services.spotdl import get_download_client

    cover_client = None
    if settings.download_enabled:
        cover_client = get_download_client()

    try:
        async with AsyncSessionLocal() as session:
            track_lookup = await _build_library_lookup(session)
            artist_lookup = await _build_artist_lookup(session)
            album_lookup = await _build_album_lookup(session)

            # Collect candidates for external download services.
            lidarr_candidates: list[tuple[str, str | None]] = []
            spotizerr_candidates: list[tuple[str, str]] = []  # (artist, title)

            # --- 1. Global top tracks ---
            await _build_track_chart(
                client,
                session,
                track_lookup,
                artist_lookup,
                lidarr_candidates,
                spotizerr_candidates,
                chart_type="top_tracks",
                scope="global",
                fetch_fn=client.get_top_tracks,
                limit=settings.CHARTS_TOP_LIMIT,
                now=now,
                summary=summary,
                cover_client=cover_client,
            )

            # --- 2. Global top artists ---
            await _build_artist_chart(
                client,
                session,
                artist_lookup,
                lidarr_candidates,
                chart_type="top_artists",
                scope="global",
                fetch_fn=client.get_top_artists,
                limit=settings.CHARTS_TOP_LIMIT,
                now=now,
                summary=summary,
            )

            # --- 3. Genre/tag charts ---
            for tag in settings.charts_tags_list:
                await _build_track_chart(
                    client,
                    session,
                    track_lookup,
                    artist_lookup,
                    lidarr_candidates,
                    spotizerr_candidates,
                    chart_type="top_tracks",
                    scope=f"tag:{tag}",
                    fetch_fn=lambda lim=100, pg=1, t=tag: client.get_tag_top_tracks(t, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                    cover_client=cover_client,
                )
                await _build_artist_chart(
                    client,
                    session,
                    artist_lookup,
                    lidarr_candidates,
                    chart_type="top_artists",
                    scope=f"tag:{tag}",
                    fetch_fn=lambda lim=100, t=tag: client.get_tag_top_artists(t, lim),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                )
                # Genre album chart (Last.fm exposes albums only per-tag).
                await _build_album_chart(
                    client,
                    session,
                    album_lookup,
                    chart_type="top_albums",
                    scope=f"tag:{tag}",
                    fetch_fn=lambda lim=100, pg=1, t=tag: client.get_tag_top_albums(t, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                    cover_client=cover_client,
                )

            # --- 4. Country charts (tracks + artists) ---
            for country in settings.charts_countries_list:
                await _build_track_chart(
                    client,
                    session,
                    track_lookup,
                    artist_lookup,
                    lidarr_candidates,
                    spotizerr_candidates,
                    chart_type="top_tracks",
                    scope=f"geo:{country}",
                    fetch_fn=lambda lim=100, pg=1, c=country: client.get_geo_top_tracks(c, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                    cover_client=cover_client,
                )
                await _build_artist_chart(
                    client,
                    session,
                    artist_lookup,
                    lidarr_candidates,
                    chart_type="top_artists",
                    scope=f"geo:{country}",
                    fetch_fn=lambda lim=100, pg=1, c=country: client.get_geo_top_artists(c, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                )

            await session.commit()

        # Send missing artists to Lidarr (deduplicated).
        if lidarr_candidates and settings.CHARTS_LIDARR_AUTO_ADD:
            seen: set[str] = set()
            unique: list[tuple[str, str | None]] = []
            for name, mbid in lidarr_candidates:
                norm = _normalize(name)
                if norm not in seen:
                    seen.add(norm)
                    unique.append((name, mbid))
            lidarr_result = await _send_artists_to_lidarr(unique, max_adds=settings.CHARTS_LIDARR_MAX_ADDS)
            summary["artists_sent_to_lidarr"] = lidarr_result.get("sent", 0)
            summary["lidarr_detail"] = lidarr_result

        # Auto-download the top not-in-library chart tracks through the download
        # cascade (issue #64) — gated on any backend being configured, not the
        # legacy Spotizerr-only path. Fast lane: spotdl first, streamrip last.
        if spotizerr_candidates and settings.charts_autodownload_enabled and settings.download_enabled:
            autodl_result = await _send_tracks_via_cascade(
                spotizerr_candidates,
                max_adds=settings.CHARTS_AUTODOWNLOAD_TOP_N,
            )
            summary["tracks_autodownloaded"] = autodl_result.get("sent", 0)
            summary["tracks_sent_to_spotizerr"] = autodl_result.get("sent", 0)  # back-compat key
            summary["autodownload_detail"] = autodl_result

    except Exception as exc:
        logger.error("Charts build failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = str(exc)
    finally:
        await client.close()
        if cover_client is not None:
            await cover_client.close()

    logger.info("Charts build finished: %s", summary)
    return summary


async def _build_track_chart(
    client: _ChartClient,
    session: AsyncSession,
    track_lookup: dict[tuple[str, str], str],
    artist_lookup: dict[str, list[str]],
    lidarr_candidates: list[tuple[str, str | None]],
    spotizerr_candidates: list[tuple[str, str]],
    *,
    chart_type: str,
    scope: str,
    fetch_fn,
    limit: int,
    now: int,
    summary: dict[str, Any],
    cover_client=None,
) -> None:
    """Fetch a track chart, match to library, persist entries."""
    try:
        raw_tracks = await fetch_fn(limit)
    except Exception as exc:
        logger.warning("Failed to fetch chart %s/%s: %s", chart_type, scope, exc)
        summary["errors"] += 1
        return

    if not raw_tracks:
        return

    # Import once per chart build, not once per track.
    from app.services.cover_art import resolve_cover_art as _resolve_cover_art

    # Delete only *today's* rows for this chart, not the whole history (issue
    # #75). This keeps prior days' snapshots intact while making a same-day
    # rebuild idempotent (it replaces today's rows rather than duplicating them).
    snapshot_date = _snapshot_date(now)
    await session.execute(
        delete(ChartEntry).where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
            ChartEntry.snapshot_date == snapshot_date,
        )
    )

    for i, track in enumerate(raw_tracks[:limit]):
        artist_name = ""
        title = ""
        mbid = None
        playcount = 0
        listeners = 0

        if isinstance(track.get("artist"), dict):
            artist_name = track["artist"].get("name", "")
            mbid = track["artist"].get("mbid") or None
        elif isinstance(track.get("artist"), str):
            artist_name = track["artist"]

        title = track.get("name", "")
        playcount = int(track.get("playcount", 0))
        listeners = int(track.get("listeners", 0))
        image_url = _pick_image_url(track.get("image", []))

        # Match to library. Try the canonical title first; fall back to the
        # feature-stripped variant so "Stateside + Zara Larsson" matches
        # the library's "Stateside".
        matched_track_id = None
        artist_norm = _normalize(artist_name)
        title_norm = _normalize(title)
        if artist_norm and title_norm:
            matched_track_id = track_lookup.get((artist_norm, title_norm))
            if not matched_track_id:
                stripped = _normalize(_strip_features(title))
                if stripped and stripped != title_norm:
                    matched_track_id = track_lookup.get((artist_norm, stripped))

        # Fallback cover art: Last.fm dropped the placeholder filter above and
        # we have no URL. Resolve via spotdl-api for *every* such entry — even
        # matched ones — because TrackFeatures.media_server_id may point at a
        # song Navidrome no longer has (returns "Artwork not found" XML), and
        # the frontend uses image_url as the fallback when library.cover_url
        # 404s.
        if image_url is None and cover_client is not None and artist_name and title:
            resolved = await _resolve_cover_art(
                session,
                artist_name,
                title,
                client=cover_client,
            )
            if resolved:
                image_url = resolved
                summary["cover_art_resolved"] += 1

        # Collect for download services.
        if not matched_track_id and artist_name and title:
            norm_artist = _normalize(artist_name)
            if norm_artist not in artist_lookup:
                lidarr_candidates.append((artist_name, mbid))
            spotizerr_candidates.append((artist_name, title))

        session.add(
            ChartEntry(
                chart_type=chart_type,
                scope=scope,
                position=i,
                track_title=title,
                artist_name=artist_name,
                artist_mbid=mbid,
                playcount=playcount,
                listeners=listeners,
                image_url=image_url,
                matched_track_id=matched_track_id,
                in_library=matched_track_id is not None,
                fetched_at=now,
                snapshot_date=snapshot_date,
            )
        )
        summary["total_entries"] += 1
        if matched_track_id:
            summary["library_matches"] += 1

    summary["charts_built"] += 1


async def _build_artist_chart(
    client: _ChartClient,
    session: AsyncSession,
    artist_lookup: dict[str, list[str]],
    lidarr_candidates: list[tuple[str, str | None]],
    *,
    chart_type: str,
    scope: str,
    fetch_fn,
    limit: int,
    now: int,
    summary: dict[str, Any],
) -> None:
    """Fetch an artist chart, match to library, persist entries."""
    try:
        raw_artists = await fetch_fn(limit)
    except Exception as exc:
        logger.warning("Failed to fetch chart %s/%s: %s", chart_type, scope, exc)
        summary["errors"] += 1
        return

    if not raw_artists:
        return

    # Per-day delete (issue #75) — see _build_track_chart for rationale.
    snapshot_date = _snapshot_date(now)
    await session.execute(
        delete(ChartEntry).where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
            ChartEntry.snapshot_date == snapshot_date,
        )
    )

    for i, artist in enumerate(raw_artists[:limit]):
        name = artist.get("name", "")
        mbid = artist.get("mbid") or None
        playcount = int(artist.get("playcount", 0))
        listeners = int(artist.get("listeners", 0))
        image_url = _pick_image_url(artist.get("image", []))

        # Check if we have any tracks by this artist.
        norm = _normalize(name)
        matched_tracks = artist_lookup.get(norm, [])
        matched_track_id = matched_tracks[0] if matched_tracks else None

        if not matched_tracks and name:
            lidarr_candidates.append((name, mbid))

        session.add(
            ChartEntry(
                chart_type=chart_type,
                scope=scope,
                position=i,
                artist_name=name,
                artist_mbid=mbid,
                playcount=playcount,
                listeners=listeners,
                image_url=image_url,
                matched_track_id=matched_track_id,
                in_library=len(matched_tracks) > 0,
                library_track_count=len(matched_tracks),
                fetched_at=now,
                snapshot_date=snapshot_date,
            )
        )
        summary["total_entries"] += 1
        if matched_tracks:
            summary["library_matches"] += 1

    summary["charts_built"] += 1


async def _build_album_chart(
    client: _ChartClient,
    session: AsyncSession,
    album_lookup: dict[tuple[str, str], list[str]],
    *,
    chart_type: str,
    scope: str,
    fetch_fn,
    limit: int,
    now: int,
    summary: dict[str, Any],
    cover_client=None,
) -> None:
    """Fetch a genre album chart, match to library, persist entries.

    Album charts are **library-only** this pass: entries are matched against
    owned tracks (by album artist + album title) and cover art is resolved, but
    no per-row acquisition is queued (unlike track charts, which auto-download).
    """
    try:
        raw_albums = await fetch_fn(limit)
    except Exception as exc:
        logger.warning("Failed to fetch chart %s/%s: %s", chart_type, scope, exc)
        summary["errors"] += 1
        return

    if not raw_albums:
        return

    from app.services.cover_art import resolve_cover_art as _resolve_cover_art

    # Per-day delete (issue #75) — see _build_track_chart for rationale.
    snapshot_date = _snapshot_date(now)
    await session.execute(
        delete(ChartEntry).where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
            ChartEntry.snapshot_date == snapshot_date,
        )
    )

    for i, album in enumerate(raw_albums[:limit]):
        artist_name = ""
        mbid = None
        if isinstance(album.get("artist"), dict):
            artist_name = album["artist"].get("name", "")
            mbid = album["artist"].get("mbid") or None
        elif isinstance(album.get("artist"), str):
            artist_name = album["artist"]

        album_name = album.get("name", "")
        # tag.getTopAlbums carries no listeners and only sometimes a playcount.
        playcount = int(album.get("playcount", 0) or 0)
        image_url = _pick_image_url(album.get("image", []))

        # Match (album artist, album title) to owned library tracks.
        owned: list[str] = []
        artist_norm = _normalize(artist_name)
        album_norm = _normalize(album_name)
        if artist_norm and album_norm:
            owned = album_lookup.get((artist_norm, album_norm), [])
        matched_track_id = owned[0] if owned else None

        # Cover-art fallback via spotdl-api (Last.fm dropped real images ~2020).
        if image_url is None and cover_client is not None and artist_name and album_name:
            resolved = await _resolve_cover_art(
                session,
                artist_name,
                album_name,
                client=cover_client,
            )
            if resolved:
                image_url = resolved
                summary["cover_art_resolved"] += 1

        session.add(
            ChartEntry(
                chart_type=chart_type,
                scope=scope,
                position=i,
                album_name=album_name,
                artist_name=artist_name,
                artist_mbid=mbid,
                playcount=playcount,
                listeners=0,
                image_url=image_url,
                matched_track_id=matched_track_id,
                in_library=len(owned) > 0,
                library_track_count=len(owned),
                fetched_at=now,
                snapshot_date=snapshot_date,
            )
        )
        summary["total_entries"] += 1
        if owned:
            summary["library_matches"] += 1

    summary["charts_built"] += 1


# ---------------------------------------------------------------------------
# On-demand single-chart build (live country/genre picker)
# ---------------------------------------------------------------------------

# Per-scope locks so two concurrent "view Japan's chart" requests don't both
# fire a Last.fm fetch + delete/insert race for the same snapshot.
_single_build_locks: dict[str, asyncio.Lock] = {}


def _resolve_track_fetch(client: _ChartClient, scope: str):
    """Return a ``fetch_fn(limit)`` for a track chart scope, or None if invalid."""
    if scope == "global":
        return client.get_top_tracks
    if scope.startswith("tag:"):
        tag = scope[4:]
        return lambda lim=100, pg=1: client.get_tag_top_tracks(tag, lim, pg)
    if scope.startswith("geo:"):
        country = scope[4:]
        return lambda lim=100, pg=1: client.get_geo_top_tracks(country, lim, pg)
    return None


def _resolve_artist_fetch(client: _ChartClient, scope: str):
    """Return a ``fetch_fn(limit)`` for an artist chart scope, or None if invalid."""
    if scope == "global":
        return client.get_top_artists
    if scope.startswith("tag:"):
        tag = scope[4:]
        return lambda lim=100: client.get_tag_top_artists(tag, lim)
    if scope.startswith("geo:"):
        country = scope[4:]
        return lambda lim=100, pg=1: client.get_geo_top_artists(country, lim, pg)
    return None


async def build_single_chart(chart_type: str, scope: str, *, limit: int | None = None) -> dict[str, Any]:
    """Build + persist ONE chart (chart_type/scope) on demand, matched to library.

    Powers the live country/genre picker: when the UI requests a scope the
    scheduled daily build didn't cover, we fetch just that chart from Last.fm,
    match it, and write today's snapshot — so it then reads back through the
    normal ``GET /v1/charts/{type}`` path with full trend/snapshot support and
    starts accruing history from first view.

    **Cover art is deliberately NOT resolved here.** On a box with a download
    backend, ``_resolve_cover_art`` fires one streamrip/spotdl *search per
    unmatched track*, which would turn an interactive "show me Germany's top
    tracks" click into a multi-minute wait (and hammer the download sidecar).
    Instead, covers come from (a) matched library tracks at serve time, (b) the
    ``cover_art_cache``, and (c) the nightly :func:`build_charts` cron, which
    does resolve them. Track charts thus render immediately; unmatched entries
    show the Last.fm image or a placeholder until the cron backfills.

    Unlike :func:`build_charts`, this does **not** queue downloads or Lidarr
    adds — merely viewing a chart must not trigger acquisition. Idempotent per
    (chart_type, scope, UTC-day): the underlying ``_build_*_chart`` deletes
    today's rows for the scope first, so a repeat call just refreshes them.
    """
    if not settings.LASTFM_API_KEY:
        return {"status": "skipped", "reason": "no_lastfm_api_key"}
    if chart_type not in ("top_tracks", "top_artists", "top_albums"):
        return {"status": "error", "reason": "bad_chart_type"}
    if chart_type == "top_albums" and not scope.startswith("tag:"):
        # Last.fm only exposes album charts per-tag (no global/geo album chart).
        return {"status": "error", "reason": "albums_require_tag_scope"}

    lock = _single_build_locks.setdefault(f"{chart_type}|{scope}", asyncio.Lock())
    async with lock:
        client = _ChartClient(settings.LASTFM_API_KEY)
        summary: dict[str, Any] = {
            "status": "completed",
            "charts_built": 0,
            "total_entries": 0,
            "library_matches": 0,
            "cover_art_resolved": 0,
            "errors": 0,
        }
        now = int(time.time())
        _lim = limit or settings.CHARTS_TOP_LIMIT

        # No cover client on the interactive path — see docstring. Covers come
        # from library matches (serve time), the cache, and the nightly cron.
        cover_client = None

        try:
            async with AsyncSessionLocal() as session:
                if chart_type == "top_tracks":
                    fetch_fn = _resolve_track_fetch(client, scope)
                    if fetch_fn is None:
                        return {"status": "error", "reason": "bad_scope"}
                    track_lookup = await _build_library_lookup(session)
                    artist_lookup = await _build_artist_lookup(session)
                    # Throwaway candidate lists: on-demand views never dispatch.
                    await _build_track_chart(
                        client,
                        session,
                        track_lookup,
                        artist_lookup,
                        [],
                        [],
                        chart_type="top_tracks",
                        scope=scope,
                        fetch_fn=fetch_fn,
                        limit=_lim,
                        now=now,
                        summary=summary,
                        cover_client=cover_client,
                    )
                elif chart_type == "top_artists":
                    fetch_fn = _resolve_artist_fetch(client, scope)
                    if fetch_fn is None:
                        return {"status": "error", "reason": "bad_scope"}
                    artist_lookup = await _build_artist_lookup(session)
                    await _build_artist_chart(
                        client,
                        session,
                        artist_lookup,
                        [],
                        chart_type="top_artists",
                        scope=scope,
                        fetch_fn=fetch_fn,
                        limit=_lim,
                        now=now,
                        summary=summary,
                    )
                else:  # top_albums (tag scope, validated above)
                    tag = scope[4:]
                    album_lookup = await _build_album_lookup(session)
                    await _build_album_chart(
                        client,
                        session,
                        album_lookup,
                        chart_type="top_albums",
                        scope=scope,
                        fetch_fn=lambda lim=100, pg=1, t=tag: client.get_tag_top_albums(t, lim, pg),
                        limit=_lim,
                        now=now,
                        summary=summary,
                        cover_client=cover_client,
                    )
                await session.commit()
        except Exception as exc:
            logger.error("Single chart build failed for %s/%s: %s", chart_type, scope, exc, exc_info=True)
            summary["status"] = "error"
            summary["error"] = str(exc)
        finally:
            await client.close()
            if cover_client is not None:
                await cover_client.close()

        return summary
