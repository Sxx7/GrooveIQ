"""
GrooveIQ -- streamrip-api integration client.

Talks to a self-hosted streamrip-api instance (thin REST wrapper around
streamrip).  streamrip downloads from Qobuz, Tidal, Deezer, or SoundCloud
in lossless / hi-res quality.

The client exposes the same interface as SpotdlClient and SpotizerrClient
so the download routes, charts service, and download watcher can use any
backend transparently.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

# Alphanumeric IDs only — prevents path traversal in URL interpolation.
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9]{1,64}$")


def _validate_id(value: str, label: str = "ID") -> None:
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"Invalid {label}: must be alphanumeric, 1-64 chars")


logger = logging.getLogger(__name__)


class StreamripClient:
    """Async HTTP client for the streamrip-api REST API.

    API-compatible with SpotdlClient / SpotizerrClient — same method
    signatures and return shapes so callers (downloads.py, charts.py,
    download_watcher.py) can use any backend without changes.
    """

    _MIN_REQUEST_GAP = 0.3  # 300ms between requests

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0, verify=True)

    async def close(self):
        await self._client.aclose()

    # -- Throttle -----------------------------------------------------------

    async def _throttle(self) -> None:
        import asyncio

        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_REQUEST_GAP:
            await asyncio.sleep(self._MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    # -- Health / capabilities ---------------------------------------------

    async def get_available_services(self) -> list[str] | None:
        """Return the streaming services streamrip-api is configured for.

        Reads ``available_services`` from ``/health``. The Lidarr backfill
        engine uses this to pre-filter ``service_priority`` so it doesn't
        emit 503 search warnings for services that aren't configured.

        Returns ``None`` (meaning "unknown") on probe failure so callers
        can degrade gracefully — they should treat ``None`` as "don't
        filter" instead of "filter out everything".
        """
        await self._throttle()
        try:
            resp = await self._client.get(f"{self._base_url}/health", timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("streamrip-api /health probe failed: %s", exc)
            return None
        services = data.get("available_services", [])
        if not isinstance(services, list):
            return None
        return [s for s in services if isinstance(s, str)]

    # -- Search -------------------------------------------------------------

    async def search(
        self,
        query: str,
        limit: int = 10,
        service: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search streamrip-api for tracks matching a query string.

        Returns a list of track dicts shaped like Spotify search results
        (with keys: id, name, artists, album, etc.) so callers that
        already understand Spotizerr/SpotdlClient responses work unchanged.

        ``service`` (qobuz / tidal / deezer / soundcloud) overrides
        streamrip-api's DEFAULT_SERVICE. Used by the Lidarr backfill engine
        to walk the configured service-priority list per request.
        """
        await self._throttle()
        params: dict[str, Any] = {"q": query, "limit": limit}
        if service:
            params["service"] = service
        try:
            resp = await self._client.get(
                f"{self._base_url}/search",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning(
                "streamrip-api search failed for %r (service=%s): %s",
                query,
                service or "default",
                exc,
            )
            return []

        # Reshape streamrip-api results to match Spotify-like format
        # so _pick_best_match, _flatten_track, etc. work unchanged.
        items = []
        for entry in data:
            cover_url = entry.get("cover_url") or ""
            items.append(
                {
                    "id": entry.get("service_id") or entry.get("spotify_id", ""),
                    "name": entry.get("title", ""),
                    "artists": [{"name": a} for a in (entry.get("artists") or [])],
                    "album": {
                        "name": entry.get("album"),
                        "images": ([{"url": cover_url, "width": 300, "height": 300}] if cover_url else []),
                    },
                    "type": "track",
                    # Preserve streamrip-specific fields. ``_album_id`` lets
                    # downstream callers group tracks by album and request
                    # whole-album downloads via download_album().
                    "_service": entry.get("service", ""),
                    "_service_id": entry.get("service_id", ""),
                    "_album_id": entry.get("album_id", ""),
                    "_album_year": entry.get("album_year"),
                    "_album_track_count": entry.get("album_track_count"),
                    "_track_number": entry.get("track_number"),
                    "_quality": entry.get("quality", ""),
                }
            )
        return items

    # -- Cover art ----------------------------------------------------------

    async def resolve_cover_art(
        self,
        artist: str,
        title: str,
    ) -> str | None:
        """Look up an album cover URL for a track via streamrip-api search."""
        from app.services.spotizerr import _pick_best_match, _pick_image

        query = f"{artist} {title}"
        results = await self.search(query, limit=5)
        match = _pick_best_match(results, artist, title)
        if not match:
            return None

        album = match.get("album") or {}
        images = album.get("images") or []
        return _pick_image(images)

    async def resolve_artist_image(self, artist: str) -> str | None:
        """Look up an artist image via a track search.

        Falls back to album art from the top track result.
        """
        from app.services.spotizerr import _pick_image

        results = await self.search(artist, limit=5)
        for item in results:
            album = item.get("album") or {}
            url = _pick_image(album.get("images") or [])
            if url:
                return url
        return None

    # -- Download -----------------------------------------------------------

    async def download(
        self,
        spotify_track_id: str,
        artist: str = "",
        title: str = "",
    ) -> dict[str, Any]:
        """Trigger download of a track by its service ID.

        Accepts a service-specific track ID (Qobuz, Tidal, Deezer, etc.)
        or a Spotify ID (for compatibility).  When artist + title are
        provided, streamrip-api can fall back to search-based download
        if the ID doesn't match the configured service.

        Returns dict with 'task_id' and 'status', shaped identically to
        SpotdlClient.download().
        """
        _validate_id(spotify_track_id, "track_id")
        await self._throttle()

        body: dict[str, Any] = {"service_id": spotify_track_id}
        if artist:
            body["artist"] = artist
        if title:
            body["title"] = title

        try:
            resp = await self._client.post(
                f"{self._base_url}/download",
                json=body,
            )
        except Exception as exc:
            logger.warning(
                "streamrip-api download transport error for %s: %s",
                spotify_track_id,
                exc,
            )
            return {"task_id": "", "status": "error", "error": str(exc)}

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            err_msg = data.get("error") or data.get("detail") or f"streamrip-api HTTP {resp.status_code}"
            logger.warning(
                "streamrip-api download failed for %s: %s",
                spotify_track_id,
                err_msg,
            )
            return {
                "task_id": data.get("task_id", ""),
                "status": "error",
                "error": err_msg,
            }

        return {
            "task_id": data.get("task_id", ""),
            "status": data.get("status", "downloading"),
        }

    # -- Artist search ------------------------------------------------------

    async def search_artist(self, query: str, limit: int = 2, albums_per_artist: int = 50) -> dict[str, Any]:
        """Return top-N artist matches plus each artist's discography (no
        tracks). Tracks are lazy-loaded per-album via :meth:`get_album_tracks`.
        """
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/search/artist",
                params={"q": query, "limit": limit, "albums_per_artist": albums_per_artist},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("streamrip-api artist-search failed for %r: %s", query, exc)
            return {"query": query, "artists": [], "error": str(exc)}

    async def get_album_tracks(self, service: str, album_id: str) -> dict[str, Any]:
        """Fetch the full track list for an album."""
        _validate_id(album_id, "album_id")
        if service not in ("qobuz", "tidal", "deezer", "soundcloud"):
            return {"album_id": album_id, "tracks": [], "error": f"unknown service {service!r}"}
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/album/{album_id}/tracks",
                params={"service": service},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("streamrip-api get_album_tracks failed for %s/%s: %s", service, album_id, exc)
            return {"album_id": album_id, "tracks": [], "error": str(exc)}

    # -- Album download -----------------------------------------------------

    async def download_album(self, service: str, album_id: str) -> dict[str, Any]:
        """Download a whole album by service + service-native album ID.

        ``service`` is one of ``qobuz`` / ``tidal`` / ``deezer``. ``album_id``
        is the numeric ID streamrip-api can build into an album URL. Requires
        streamrip-api ≥ the entity_type-aware revision (post-Phase 4e).
        """
        _validate_id(album_id, "album_id")
        if service not in ("qobuz", "tidal", "deezer", "soundcloud"):
            return {"task_id": "", "status": "error", "error": f"unknown service {service!r}"}
        await self._throttle()

        try:
            resp = await self._client.post(
                f"{self._base_url}/download",
                json={"service_id": album_id, "service": service, "entity_type": "album"},
            )
        except Exception as exc:
            logger.warning(
                "streamrip-api album download transport error for %s/%s: %s",
                service,
                album_id,
                exc,
            )
            return {"task_id": "", "status": "error", "error": str(exc)}

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            err_msg = data.get("error") or data.get("detail") or f"streamrip-api HTTP {resp.status_code}"
            return {"task_id": data.get("task_id", ""), "status": "error", "error": err_msg}

        return {
            "task_id": data.get("task_id", ""),
            "status": data.get("status", "downloading"),
        }

    # -- Status -------------------------------------------------------------

    async def get_status(self, task_id: str) -> dict[str, Any]:
        """Check the status of a download task.

        Returns the same shape as SpotdlClient.get_status():
            {"status", "progress", "error", "raw"}

        Distinguishes three failure modes so callers can react correctly:

        - ``"lost"`` — streamrip-api returned 404. Its task store is in-memory
          only, so any restart drops every previously-issued task_id.  The
          caller should re-queue the work from scratch rather than penalise
          the row (it might have downloaded fine before the restart).
        - ``"transient_error"`` — network-level failure (timeout, connection
          refused, DNS).  The caller should leave state untouched and try
          again on the next poll.
        - ``"error"`` — streamrip-api responded with a real download error
          payload.  The caller should treat the album as failed and apply
          the configured retry/cooldown logic.
        """
        _validate_id(task_id, "task_id")
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/status/{task_id}",
            )
        except httpx.RequestError as exc:
            # Network-level: timeout, connection refused, DNS, etc.  Caller
            # should treat as transient — streamrip-api may simply be slow
            # to come back up after a restart of its own.
            logger.warning("streamrip-api network error for task %s: %s", task_id, exc)
            return {
                "status": "transient_error",
                "progress": None,
                "error": str(exc),
                "raw": {},
            }
        if resp.status_code == 404:
            # streamrip-api is up but has no record of this task — it
            # restarted (its task store is in-memory) or the task was
            # cleared.  Don't penalise: the caller should re-queue.
            return {
                "status": "lost",
                "progress": None,
                "error": "streamrip-api has no record of this task (likely restarted)",
                "raw": {},
            }
        try:
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            logger.warning("streamrip-api status check failed for %s: %s", task_id, exc)
            return {
                "status": "error",
                "progress": None,
                "error": str(exc),
                "raw": {},
            }

        status = raw.get("status", "unknown")
        # Map streamrip-api statuses to the same terminal states
        # so download_watcher.py's terminal-state classification works.
        if status == "complete":
            status = "complete"  # matches _TERMINAL_SUCCESS
        elif status == "error":
            status = "error"  # matches _TERMINAL_ERROR

        return {
            "status": status,
            "progress": raw.get("progress"),
            "error": raw.get("error"),
            "raw": raw,
        }
