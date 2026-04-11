"""
GrooveIQ -- spotdl-api integration client.

Talks to a self-hosted spotdl-api instance (thin REST wrapper around spotDL).
spotDL searches Spotify for metadata and downloads audio from YouTube Music.

GrooveIQ never downloads anything itself -- it sends requests to the external
spotdl-api service, same pattern as the Spotizerr integration.

The client exposes the same interface as SpotizerrClient so the download
routes, charts service, and download watcher can use either backend
transparently.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import re

import httpx

from app.core.config import settings

# Alphanumeric IDs only — prevents path traversal in URL interpolation.
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9]{1,64}$")


def _validate_id(value: str, label: str = "ID") -> None:
    if not _SAFE_ID_RE.match(value):
        raise ValueError(f"Invalid {label}: must be alphanumeric, 1-64 chars")

logger = logging.getLogger(__name__)


class SpotdlClient:
    """Async HTTP client for the spotdl-api REST API.

    API-compatible with SpotizerrClient -- same method signatures and
    return shapes so callers (downloads.py, charts.py, download_watcher.py)
    can use either backend without changes.
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

    # -- Search -------------------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search spotdl-api for tracks matching a query string.

        Returns a list of track dicts shaped like Spotify search results
        (with keys: id, name, artists, album, etc.) so callers that
        already understand Spotizerr responses work without changes.
        """
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/search",
                params={"q": query, "limit": limit},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("spotdl-api search failed for %r: %s", query, exc)
            return []

        # Reshape spotdl-api results to match Spotizerr/Spotify format
        # so _pick_best_match, _flatten_track, etc. work unchanged.
        items = []
        for entry in data:
            items.append({
                "id": entry.get("spotify_id", ""),
                "name": entry.get("title", ""),
                "artists": [
                    {"name": a} for a in (entry.get("artists") or [])
                ],
                "album": {
                    "name": entry.get("album"),
                    "images": (
                        [{"url": entry["cover_url"], "width": 300, "height": 300}]
                        if entry.get("cover_url")
                        else []
                    ),
                },
                "type": "track",
            })
        return items

    # -- Cover art ----------------------------------------------------------

    async def resolve_cover_art(
        self,
        artist: str,
        title: str,
    ) -> Optional[str]:
        """Look up an album cover URL for a track via Spotify (through spotdl-api)."""
        from app.services.spotizerr import _pick_best_match, _pick_image

        query = f"{artist} {title}"
        results = await self.search(query, limit=5)
        match = _pick_best_match(results, artist, title)
        if not match:
            return None

        album = match.get("album") or {}
        images = album.get("images") or []
        return _pick_image(images)

    async def resolve_artist_image(self, artist: str) -> Optional[str]:
        """Look up an artist image via a track search (spotdl-api has no artist search).

        Falls back to album art from the top track result, same as
        SpotizerrClient's fallback path.
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

    async def download(self, spotify_track_id: str) -> Dict[str, Any]:
        """Trigger download of a track by Spotify ID.

        Returns dict with 'task_id' and 'status', shaped identically to
        SpotizerrClient.download() so callers work unchanged.
        """
        _validate_id(spotify_track_id, "spotify_track_id")
        await self._throttle()

        try:
            resp = await self._client.post(
                f"{self._base_url}/download",
                json={"spotify_id": spotify_track_id},
            )
        except Exception as exc:
            logger.warning(
                "spotdl-api download transport error for %s: %s",
                spotify_track_id, exc,
            )
            return {"task_id": "", "status": "error", "error": str(exc)}

        try:
            data = resp.json()
        except Exception:
            data = {}

        if resp.status_code >= 400:
            err_msg = (
                data.get("error")
                or data.get("detail")
                or f"spotdl-api HTTP {resp.status_code}"
            )
            logger.warning(
                "spotdl-api download failed for %s: %s",
                spotify_track_id, err_msg,
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

    # -- Status -------------------------------------------------------------

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """Check the status of a download task.

        Returns the same shape as SpotizerrClient.get_status():
            {"status", "progress", "error", "raw"}
        """
        _validate_id(task_id, "task_id")
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/status/{task_id}",
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            logger.warning("spotdl-api status check failed for %s: %s", task_id, exc)
            return {
                "status": "error",
                "progress": None,
                "error": str(exc),
                "raw": {},
            }

        status = raw.get("status", "unknown")
        # Map spotdl-api statuses to the same terminal states Spotizerr uses
        # so download_watcher.py's terminal-state classification works.
        if status == "complete":
            status = "complete"  # already matches _TERMINAL_SUCCESS
        elif status == "error":
            status = "error"    # already matches _TERMINAL_ERROR

        return {
            "status": status,
            "progress": raw.get("progress"),
            "error": raw.get("error"),
            "raw": raw,
        }


# ---------------------------------------------------------------------------
# Factory — returns the right client based on config
# ---------------------------------------------------------------------------

def get_download_client():
    """Return the appropriate download client based on configuration.

    Prefers spotdl-api if SPOTDL_API_URL is set, falls back to Spotizerr.
    """
    if settings.spotdl_enabled:
        return SpotdlClient(settings.SPOTDL_API_URL)
    elif settings.spotizerr_enabled:
        from app.services.spotizerr import SpotizerrClient
        return SpotizerrClient(
            settings.SPOTIZERR_URL,
            settings.SPOTIZERR_USERNAME,
            settings.SPOTIZERR_PASSWORD,
        )
    else:
        return None
