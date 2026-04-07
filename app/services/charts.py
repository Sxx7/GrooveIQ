"""
GrooveIQ -- Charts service.

Fetches global and genre-based charts from Last.fm, matches tracks/artists
to the local library, optionally sends missing artists to Lidarr for download,
and persists chart snapshots for the API.

Charts are rebuilt periodically (default: every 24h) by the scheduler.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import ChartEntry, DiscoveryRequest, TrackFeatures

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _pick_image_url(images: list) -> Optional[str]:
    """Pick the best image URL from Last.fm's image array.

    Prefers extralarge (300x300). Falls back through smaller sizes.
    Returns None if all URLs are empty (common since ~2020).
    """
    if not images:
        return None
    by_size = {img.get("size", ""): img.get("#text", "") for img in images if isinstance(img, dict)}
    for preferred in ("extralarge", "large", "mega", "medium", "small"):
        url = by_size.get(preferred, "")
        if url:
            return url
    return None


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


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

    async def get_top_tracks(self, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """Global top tracks (chart.getTopTracks)."""
        try:
            data = await self._get({
                "method": "chart.getTopTracks",
                "limit": limit,
                "page": page,
            })
        except httpx.HTTPStatusError as exc:
            logger.warning("chart.getTopTracks failed: %s", exc)
            return []
        tracks = data.get("tracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_top_artists(self, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """Global top artists (chart.getTopArtists)."""
        try:
            data = await self._get({
                "method": "chart.getTopArtists",
                "limit": limit,
                "page": page,
            })
        except httpx.HTTPStatusError as exc:
            logger.warning("chart.getTopArtists failed: %s", exc)
            return []
        artists = data.get("artists", {}).get("artist", [])
        return artists if isinstance(artists, list) else [artists]

    async def get_geo_top_tracks(self, country: str, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """Top tracks by country (geo.getTopTracks)."""
        try:
            data = await self._get({
                "method": "geo.getTopTracks",
                "country": country,
                "limit": limit,
                "page": page,
            })
        except httpx.HTTPStatusError as exc:
            logger.warning("geo.getTopTracks failed for %r: %s", country, exc)
            return []
        tracks = data.get("tracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_tag_top_tracks(self, tag: str, limit: int = 100, page: int = 1) -> List[Dict[str, Any]]:
        """Top tracks by genre tag (tag.getTopTracks)."""
        try:
            data = await self._get({
                "method": "tag.getTopTracks",
                "tag": tag,
                "limit": limit,
                "page": page,
            })
        except httpx.HTTPStatusError as exc:
            logger.warning("tag.getTopTracks failed for %r: %s", tag, exc)
            return []
        tracks = data.get("toptracks", {}).get("track", [])
        return tracks if isinstance(tracks, list) else [tracks]

    async def get_tag_top_artists(self, tag: str, limit: int = 100) -> List[Dict[str, Any]]:
        """Top artists by genre tag (tag.getTopArtists)."""
        try:
            data = await self._get({
                "method": "tag.getTopArtists",
                "tag": tag,
                "limit": limit,
            })
        except httpx.HTTPStatusError as exc:
            logger.warning("tag.getTopArtists failed for %r: %s", tag, exc)
            return []
        artists = data.get("topartists", {}).get("artist", [])
        return artists if isinstance(artists, list) else [artists]


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------

async def _build_library_lookup(session: AsyncSession) -> Dict[Tuple[str, str], str]:
    """Build (normalized_artist, normalized_title) -> track_id lookup."""
    rows = (await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title)
        .where(TrackFeatures.artist.isnot(None), TrackFeatures.title.isnot(None))
    )).all()
    lookup: Dict[Tuple[str, str], str] = {}
    for track_id, artist, title in rows:
        key = (_normalize(artist), _normalize(title))
        lookup[key] = track_id
    return lookup


async def _build_artist_lookup(session: AsyncSession) -> Dict[str, List[str]]:
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
# Lidarr integration (reuse discovery's LidarrClient)
# ---------------------------------------------------------------------------

async def _send_artists_to_lidarr(
    artist_names_mbids: List[Tuple[str, Optional[str]]],
    max_adds: int = 50,
) -> Dict[str, int]:
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
            rows = (await session.execute(
                select(DiscoveryRequest.artist_mbid)
                .where(DiscoveryRequest.status != "failed")
            )).all()
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
                    session.add(DiscoveryRequest(
                        user_id="__charts__",
                        artist_name=name,
                        artist_mbid=mbid or foreign_id,
                        source="chart",
                        status="sent",
                    ))
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
# Chart build pipeline
# ---------------------------------------------------------------------------

async def build_charts() -> Dict[str, Any]:
    """
    Main entry point. Fetches charts from Last.fm, matches to library,
    optionally sends missing artists to Lidarr, and stores chart entries.
    """
    if not settings.LASTFM_API_KEY:
        logger.warning("Charts build skipped: LASTFM_API_KEY not configured")
        return {"status": "skipped", "reason": "no_lastfm_api_key"}

    client = _ChartClient(settings.LASTFM_API_KEY)
    summary: Dict[str, Any] = {
        "status": "completed",
        "charts_built": 0,
        "total_entries": 0,
        "library_matches": 0,
        "artists_sent_to_lidarr": 0,
        "errors": 0,
    }

    now = int(time.time())

    try:
        async with AsyncSessionLocal() as session:
            track_lookup = await _build_library_lookup(session)
            artist_lookup = await _build_artist_lookup(session)

            # Collect all artists we encounter for Lidarr.
            lidarr_candidates: List[Tuple[str, Optional[str]]] = []

            # --- 1. Global top tracks ---
            await _build_track_chart(
                client, session, track_lookup, artist_lookup, lidarr_candidates,
                chart_type="top_tracks",
                scope="global",
                fetch_fn=client.get_top_tracks,
                limit=settings.CHARTS_TOP_LIMIT,
                now=now,
                summary=summary,
            )

            # --- 2. Global top artists ---
            await _build_artist_chart(
                client, session, artist_lookup, lidarr_candidates,
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
                    client, session, track_lookup, artist_lookup, lidarr_candidates,
                    chart_type="top_tracks",
                    scope=f"tag:{tag}",
                    fetch_fn=lambda lim=100, pg=1, t=tag: client.get_tag_top_tracks(t, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                )
                await _build_artist_chart(
                    client, session, artist_lookup, lidarr_candidates,
                    chart_type="top_artists",
                    scope=f"tag:{tag}",
                    fetch_fn=lambda lim=100, t=tag: client.get_tag_top_artists(t, lim),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                )

            # --- 4. Country charts ---
            for country in settings.charts_countries_list:
                await _build_track_chart(
                    client, session, track_lookup, artist_lookup, lidarr_candidates,
                    chart_type="top_tracks",
                    scope=f"geo:{country}",
                    fetch_fn=lambda lim=100, pg=1, c=country: client.get_geo_top_tracks(c, lim, pg),
                    limit=settings.CHARTS_TOP_LIMIT,
                    now=now,
                    summary=summary,
                )

            await session.commit()

        # Send missing artists to Lidarr (deduplicated).
        if lidarr_candidates and settings.CHARTS_LIDARR_AUTO_ADD:
            seen: Set[str] = set()
            unique: List[Tuple[str, Optional[str]]] = []
            for name, mbid in lidarr_candidates:
                norm = _normalize(name)
                if norm not in seen:
                    seen.add(norm)
                    unique.append((name, mbid))
            lidarr_result = await _send_artists_to_lidarr(unique, max_adds=settings.CHARTS_LIDARR_MAX_ADDS)
            summary["artists_sent_to_lidarr"] = lidarr_result.get("sent", 0)
            summary["lidarr_detail"] = lidarr_result

    except Exception as exc:
        logger.error("Charts build failed: %s", exc, exc_info=True)
        summary["status"] = "error"
        summary["error"] = str(exc)
    finally:
        await client.close()

    logger.info("Charts build finished: %s", summary)
    return summary


async def _build_track_chart(
    client: _ChartClient,
    session: AsyncSession,
    track_lookup: Dict[Tuple[str, str], str],
    artist_lookup: Dict[str, List[str]],
    lidarr_candidates: List[Tuple[str, Optional[str]]],
    *,
    chart_type: str,
    scope: str,
    fetch_fn,
    limit: int,
    now: int,
    summary: Dict[str, Any],
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

    # Delete old entries for this chart.
    await session.execute(
        delete(ChartEntry).where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
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

        # Match to library.
        matched_track_id = None
        key = (_normalize(artist_name), _normalize(title))
        if key[0] and key[1]:
            matched_track_id = track_lookup.get(key)

        # Collect for Lidarr.
        if not matched_track_id and artist_name:
            norm_artist = _normalize(artist_name)
            if norm_artist not in artist_lookup:
                lidarr_candidates.append((artist_name, mbid))

        session.add(ChartEntry(
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
            fetched_at=now,
        ))
        summary["total_entries"] += 1
        if matched_track_id:
            summary["library_matches"] += 1

    summary["charts_built"] += 1


async def _build_artist_chart(
    client: _ChartClient,
    session: AsyncSession,
    artist_lookup: Dict[str, List[str]],
    lidarr_candidates: List[Tuple[str, Optional[str]]],
    *,
    chart_type: str,
    scope: str,
    fetch_fn,
    limit: int,
    now: int,
    summary: Dict[str, Any],
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

    await session.execute(
        delete(ChartEntry).where(
            ChartEntry.chart_type == chart_type,
            ChartEntry.scope == scope,
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

        session.add(ChartEntry(
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
        ))
        summary["total_entries"] += 1
        if matched_tracks:
            summary["library_matches"] += 1

    summary["charts_built"] += 1
