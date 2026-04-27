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
        tracks = data.get("toptracks", {}).get("track", [])
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


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------


async def _build_library_lookup(session: AsyncSession) -> dict[tuple[str, str], str]:
    """Build (normalized_artist, normalized_title) -> track_id lookup."""
    rows = (
        await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist, TrackFeatures.title).where(
                TrackFeatures.artist.isnot(None), TrackFeatures.title.isnot(None)
            )
        )
    ).all()
    lookup: dict[tuple[str, str], str] = {}
    for track_id, artist, title in rows:
        key = (_normalize(artist), _normalize(title))
        lookup[key] = track_id
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
# Spotizerr integration (individual track downloads)
# ---------------------------------------------------------------------------


async def _send_tracks_via_cascade(
    tracks: list[tuple[str, str]],
    max_adds: int = 50,
) -> dict[str, int]:
    """Send unmatched chart tracks through the bulk_per_track download cascade.

    The cascade walks the configured priority chain (streamrip → spotdl →
    spotizerr → slskd by default) and stops at the first backend that
    successfully queues the track. Each attempt — successful or not — is
    recorded on the persisted ``DownloadRequest.attempts`` log.

    For each successfully-queued download:
      1. Persists a ``download_requests`` row visible in
         ``GET /v1/downloads`` alongside user-initiated requests.
      2. Spawns the appropriate watcher so completion triggers the
         media-server refresh + GrooveIQ library scan.
    """
    from app.models.db import DownloadRequest
    from app.models.download_routing_schema import BackendName
    from app.services.download_chain import TrackRef, try_download_chain

    if not settings.download_enabled:
        logger.warning("Charts: no download backend configured, skipping track downloads")
        return {"sent": 0, "not_found": 0, "duplicate": 0, "errors": 0}

    stats = {"sent": 0, "not_found": 0, "duplicate": 0, "errors": 0}

    # Deduplicate by normalised artist+title.
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for artist, title in tracks:
        key = (_normalize(artist), _normalize(title))
        if key not in seen and key[0] and key[1]:
            seen.add(key)
            unique.append((artist, title))

    sent = 0
    for artist, title in unique:
        if sent >= max_adds:
            break

        track_ref = TrackRef(artist=artist, title=title)
        try:
            cascade = await try_download_chain(track_ref, purpose="bulk_per_track")
        except Exception as exc:
            logger.error("Charts cascade failed for %s — %s: %s", artist, title, exc)
            stats["errors"] += 1
            continue

        last = cascade.attempts[-1] if cascade.attempts else None
        source = cascade.final_backend or (last.backend if last else "none")
        status = cascade.final_status if cascade.success else (last.status if last else "error")
        err_msg = None if cascade.success else (last.error if last else "no backend succeeded")

        slskd_username = None
        slskd_filename = None
        slskd_transfer_id = None
        if cascade.success and cascade.final_backend == BackendName.SLSKD.value:
            slskd_username = cascade.final_extra.get("username")
            slskd_filename = cascade.final_extra.get("filename")
            slskd_transfer_id = cascade.final_task_id

        record_id: int | None = None
        try:
            async with AsyncSessionLocal() as dl_session:
                row = DownloadRequest(
                    task_id=cascade.final_task_id,
                    status=status,
                    source=source,
                    track_title=title,
                    artist_name=artist,
                    slskd_username=slskd_username,
                    slskd_filename=slskd_filename,
                    slskd_transfer_id=slskd_transfer_id,
                    attempts=[a.to_dict() for a in cascade.attempts] or None,
                    requested_by="__charts__",
                    error_message=err_msg,
                    updated_at=int(time.time()),
                )
                dl_session.add(row)
                await dl_session.commit()
                record_id = row.id
        except Exception as exc:
            logger.warning(
                "Charts: could not persist download row for %s — %s: %s", artist, title, exc
            )

        if not cascade.success:
            # No backend matched/succeeded — bucket between "not_found" (every
            # attempt was a clean skip / no-match) and "errors" (any real failure).
            had_real_failure = any(
                att.status not in ("skipped",) for att in cascade.attempts
            )
            if had_real_failure:
                stats["errors"] += 1
            else:
                stats["not_found"] += 1
            continue

        # Success: pick the right watcher for the chosen backend.
        if cascade.final_backend == BackendName.SLSKD.value:
            if record_id is not None:
                from app.services.slskd_watcher import start_watcher as start_slskd_watcher

                await start_slskd_watcher(record_id)
        elif cascade.final_task_id:
            from app.services.download_watcher import start_watcher

            await start_watcher(cascade.final_task_id, source=cascade.final_backend)

        if status == "duplicate":
            stats["duplicate"] += 1
        else:
            stats["sent"] += 1
            sent += 1
            logger.info("Charts: queued %s — %s via %s", artist, title, source)

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

            # --- 4. Country charts ---
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

        # Send unmatched tracks to Spotizerr for individual download.
        if spotizerr_candidates and settings.CHARTS_SPOTIZERR_AUTO_ADD and settings.spotizerr_enabled:
            spotizerr_result = await _send_tracks_to_spotizerr(
                spotizerr_candidates,
                max_adds=settings.CHARTS_SPOTIZERR_MAX_ADDS,
            )
            summary["tracks_sent_to_spotizerr"] = spotizerr_result.get("sent", 0)
            summary["spotizerr_detail"] = spotizerr_result

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

        # Fallback cover art: Last.fm dropped the placeholder filter above
        # and we have no URL, AND the track isn't in the library yet (matched
        # entries get media-server cover art at render time).  Hit Spotizerr.
        if image_url is None and matched_track_id is None and cover_client is not None and artist_name and title:
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
                fetched_at=now,
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
            )
        )
        summary["total_entries"] += 1
        if matched_tracks:
            summary["library_matches"] += 1

    summary["charts_built"] += 1
