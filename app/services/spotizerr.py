"""
GrooveIQ -- Spotizerr integration client.

Talks to a self-hosted Spotizerr instance to download individual tracks.
Spotizerr searches Spotify for metadata and downloads audio from Deezer/YouTube.

GrooveIQ never downloads anything itself — it sends requests to the external
Spotizerr service, same pattern as the Lidarr integration.

API reference: https://spotizerr.readthedocs.io/en/latest/api/
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy matching."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


# ---------------------------------------------------------------------------
# Spotizerr HTTP client
# ---------------------------------------------------------------------------

class SpotizerrClient:
    """Async HTTP client for the Spotizerr REST API."""

    # Conservative margin on JWT expiry (re-auth after 23h of a 30-day token).
    _TOKEN_TTL = 23 * 3600
    _MIN_REQUEST_GAP = 0.3  # 300ms between requests

    def __init__(
        self,
        base_url: str,
        username: str = "",
        password: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._jwt_token: Optional[str] = None
        self._token_expires: float = 0.0
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0, verify=True)

    async def close(self):
        await self._client.aclose()

    # -- Auth ---------------------------------------------------------------

    async def _ensure_auth(self) -> None:
        """Login to Spotizerr if credentials are configured and token expired."""
        if not self._username or not self._password:
            return  # Spotizerr auth disabled, no-op.
        if self._jwt_token and time.monotonic() < self._token_expires:
            return  # Token still valid.

        try:
            resp = await self._client.post(
                f"{self._base_url}/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
            resp.raise_for_status()
            data = resp.json()
            self._jwt_token = data.get("token") or data.get("access_token")
            self._token_expires = time.monotonic() + self._TOKEN_TTL
            logger.info("Spotizerr: authenticated as %s", self._username)
        except Exception as exc:
            logger.error("Spotizerr auth failed: %s", exc)
            self._jwt_token = None

    def _headers(self) -> Dict[str, str]:
        """Build request headers (with JWT if available)."""
        h: Dict[str, str] = {"Accept": "application/json"}
        if self._jwt_token:
            h["Authorization"] = f"Bearer {self._jwt_token}"
        return h

    # -- Throttle -----------------------------------------------------------

    async def _throttle(self) -> None:
        import asyncio
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_REQUEST_GAP:
            await asyncio.sleep(self._MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    # -- Search -------------------------------------------------------------

    async def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Search Spotizerr for tracks matching a query string.

        Returns a list of track dicts with keys: id, name, artists, etc.
        """
        await self._ensure_auth()
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/search",
                params={"q": query, "type": "track", "limit": limit},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("items", [])
        except Exception as exc:
            logger.warning("Spotizerr search failed for %r: %s", query, exc)
            return []

    # -- Cover art ----------------------------------------------------------

    async def resolve_cover_art(
        self,
        artist: str,
        title: str,
    ) -> Optional[str]:
        """Look up an album cover URL for a track via Spotify (through Spotizerr).

        Used as a fallback when Last.fm returns no real image for a chart entry.
        Reuses the existing search endpoint — no extra API is queried.

        Returns the best-matching Spotify album image URL (prefers 300px,
        falls back to largest available), or None if nothing found.
        """
        query = f"{artist} {title}"
        results = await self.search(query, limit=5)
        match = _pick_best_match(results, artist, title)
        if not match:
            return None

        album = match.get("album") or {}
        images = album.get("images") or []
        if not images:
            return None

        # Prefer the 300px image; fall back to first (usually largest).
        for img in images:
            if img.get("width") == 300 or img.get("height") == 300:
                url = img.get("url")
                if url:
                    return url
        return images[0].get("url") or None

    # -- Download -----------------------------------------------------------

    async def download(self, spotify_track_id: str) -> Dict[str, Any]:
        """Trigger download of a track by Spotify ID.

        Returns dict with 'task_id' and 'status'.
        On 409 (duplicate), returns the existing task_id.
        """
        await self._ensure_auth()
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/track/download/{spotify_track_id}",
                headers=self._headers(),
            )
            if resp.status_code == 409:
                # Duplicate — already downloading or downloaded.
                data = resp.json()
                task_id = data.get("existing_task") or data.get("task_id", "")
                return {"task_id": task_id, "status": "duplicate"}
            resp.raise_for_status()
            data = resp.json()
            return {"task_id": data.get("task_id", ""), "status": "downloading"}
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Spotizerr download failed for %s: HTTP %s",
                spotify_track_id, exc.response.status_code,
            )
            return {"task_id": "", "status": "error", "error": str(exc)}
        except Exception as exc:
            logger.warning("Spotizerr download failed for %s: %s", spotify_track_id, exc)
            return {"task_id": "", "status": "error", "error": str(exc)}

    # -- Status -------------------------------------------------------------

    async def get_status(self, task_id: str) -> Dict[str, Any]:
        """Check the status of a download task."""
        await self._ensure_auth()
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/prgs/{task_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("Spotizerr status check failed for %s: %s", task_id, exc)
            return {"status": "error", "error": str(exc)}


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------

def _pick_best_match(
    results: List[Dict[str, Any]],
    target_artist: str,
    target_title: str,
) -> Optional[Dict[str, Any]]:
    """Pick the best search result matching artist + title.

    Prefers exact normalised match on both artist and title.
    Falls back to first result if no exact match.
    """
    if not results:
        return None

    norm_artist = _normalize(target_artist)
    norm_title = _normalize(target_title)

    for item in results:
        item_title = _normalize(item.get("name", ""))
        item_artists = item.get("artists", [])
        for a in item_artists:
            if _normalize(a.get("name", "")) == norm_artist and item_title == norm_title:
                return item

    # Relax: match on artist only (title may differ in Spotify catalog).
    for item in results:
        item_artists = item.get("artists", [])
        for a in item_artists:
            if _normalize(a.get("name", "")) == norm_artist:
                return item

    # Last resort: return first result.
    return results[0]


async def search_and_download(
    artist: str,
    title: str,
) -> Optional[Dict[str, Any]]:
    """Search Spotizerr for a track and trigger download.

    Returns dict with task_id, spotify_id, matched artist/title,
    or None if search returned no results.
    """
    if not settings.spotizerr_enabled:
        return None

    client = SpotizerrClient(
        settings.SPOTIZERR_URL,
        settings.SPOTIZERR_USERNAME,
        settings.SPOTIZERR_PASSWORD,
    )
    try:
        query = f"{artist} {title}"
        results = await client.search(query, limit=5)

        match = _pick_best_match(results, artist, title)
        if not match:
            logger.info("Spotizerr: no results for %r", query)
            return None

        spotify_id = match.get("id", "")
        if not spotify_id:
            return None

        # Extract matched artist name(s).
        matched_artists = [a.get("name", "") for a in match.get("artists", [])]
        matched_artist = matched_artists[0] if matched_artists else ""
        matched_title = match.get("name", "")

        dl_result = await client.download(spotify_id)

        return {
            "task_id": dl_result.get("task_id", ""),
            "status": dl_result.get("status", "unknown"),
            "spotify_id": spotify_id,
            "matched_artist": matched_artist,
            "matched_title": matched_title,
        }
    finally:
        await client.close()
