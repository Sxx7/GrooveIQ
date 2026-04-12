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
from typing import Any

import httpx

from app.services.spotdl import _validate_id

logger = logging.getLogger(__name__)

_STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace for fuzzy matching."""
    n = s.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    n = _STRIP_RE.sub("", n)
    return " ".join(n.split())


def _pick_image(images: list[dict[str, Any]]) -> str | None:
    """Pick the best URL from a Spotify images array.

    Prefers 300px width/height, falls back to the first image (usually largest).
    Returns None if the list is empty or every URL is empty.
    """
    if not images:
        return None
    for img in images:
        if img.get("width") == 300 or img.get("height") == 300:
            url = img.get("url")
            if url:
                return url
    return images[0].get("url") or None


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
        self._jwt_token: str | None = None
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

    def _headers(self) -> dict[str, str]:
        """Build request headers (with JWT if available)."""
        h: dict[str, str] = {"Accept": "application/json"}
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

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
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
    ) -> str | None:
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
        return _pick_image(images)

    async def resolve_artist_image(self, artist: str) -> str | None:
        """Look up a portrait image URL for an artist via Spotify (through Spotizerr).

        Strategy:
          1. Try `type=artist` search — Spotify artist objects have
             top-level `images[]` that are real portraits.
          2. If no artist match, fall back to `type=track` search and
             use the top result's album art as a stand-in.  Visually
             fine, just not a proper portrait — this is what many mobile
             music apps do anyway.

        Returns the best matching image URL, or None.
        """
        # -- 1. Try artist search (type=artist) -----------------------------
        await self._ensure_auth()
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/search",
                params={"q": artist, "type": "artist", "limit": 5},
                headers=self._headers(),
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", []) or []
        except Exception as exc:
            logger.debug("Spotizerr artist search failed for %r: %s", artist, exc)
            items = []

        norm_target = _normalize(artist)

        # Prefer exact normalised-name match.
        for item in items:
            if _normalize(item.get("name", "")) == norm_target:
                url = _pick_image(item.get("images") or [])
                if url:
                    return url

        # Fall back to first result with any image.
        for item in items:
            url = _pick_image(item.get("images") or [])
            if url:
                return url

        # -- 2. Fall back to track search — grab album art -----------------
        results = await self.search(artist, limit=5)
        for item in results:
            album = item.get("album") or {}
            url = _pick_image(album.get("images") or [])
            if url:
                return url
        return None

    # -- Download -----------------------------------------------------------

    async def download(self, spotify_track_id: str) -> dict[str, Any]:
        """Trigger download of a track by Spotify ID.

        Returns dict with 'task_id' and 'status'.
        On 409 (duplicate), returns the existing task_id.
        On 4xx/5xx, returns ``status="error"`` and the upstream
        ``error`` message from Spotizerr's JSON body (not httpx's
        generic "Server error 500" string) so callers can see
        exactly why Spotizerr rejected the request.

        Spotizerr's success response uses ``{"prg_file": ...}``
        (legacy name kept for backwards compat).  We treat both
        ``prg_file`` and ``task_id`` as the task identifier so an
        eventual field rename upstream won't break us.
        """
        _validate_id(spotify_track_id, "spotify_track_id")
        await self._ensure_auth()
        await self._throttle()

        url = f"{self._base_url}/api/track/download/{spotify_track_id}"
        try:
            resp = await self._client.get(url, headers=self._headers())
        except Exception as exc:
            logger.warning(
                "Spotizerr download transport error for %s: %s",
                spotify_track_id,
                exc,
            )
            return {"task_id": "", "status": "error", "error": str(exc)}

        # Always attempt to parse the body — Spotizerr embeds
        # useful error details in the JSON, even on 4xx/5xx.
        try:
            data = resp.json()
        except Exception:
            data = {}

        def _extract_task_id(payload: dict[str, Any]) -> str:
            return payload.get("prg_file") or payload.get("task_id") or payload.get("existing_task") or ""

        if resp.status_code == 409:
            # Duplicate — already downloading or downloaded.
            return {"task_id": _extract_task_id(data), "status": "duplicate"}

        if resp.status_code >= 400:
            err_msg = data.get("error") or data.get("message") or f"Spotizerr HTTP {resp.status_code}"
            logger.warning(
                "Spotizerr download failed for %s: %s",
                spotify_track_id,
                err_msg,
            )
            return {
                "task_id": _extract_task_id(data),
                "status": "error",
                "error": err_msg,
            }

        # Success path: Spotizerr returns 202 + {"prg_file": "..."}.
        return {"task_id": _extract_task_id(data), "status": "downloading"}

    # -- Status -------------------------------------------------------------

    async def get_status(self, task_id: str) -> dict[str, Any]:
        """Check the status of a download task.

        Spotizerr's ``/api/prgs/{task_id}`` endpoint returns:

            {
              "original_url": "...",
              "last_line": {"status": "downloading|complete|error|...", "error": "..."},
              "timestamp": ...,
              "task_id": "...",
              "status_count": N
            }

        We flatten the useful bits into a stable shape so callers
        don't have to dig into ``last_line.status`` themselves:

            {
              "status":   "downloading"|"complete"|"error"|...  (always lower-case)
              "progress": float | None,
              "error":    str | None,
              "raw":      {full upstream response}
            }

        On transport error, returns ``status="error"`` with the
        exception in ``error``.
        """
        _validate_id(task_id, "task_id")
        await self._ensure_auth()
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/prgs/{task_id}",
                headers=self._headers(),
            )
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:
            logger.warning("Spotizerr status check failed for %s: %s", task_id, exc)
            return {
                "status": "error",
                "progress": None,
                "error": str(exc),
                "raw": {},
            }

        last_line = raw.get("last_line") if isinstance(raw.get("last_line"), dict) else {}
        # Fall back to top-level fields if last_line is missing/empty.
        status = (last_line.get("status") if last_line else None) or raw.get("status") or "unknown"
        error = (last_line.get("error") if last_line else None) or raw.get("error")
        progress = (last_line.get("progress") if last_line else None) or raw.get("progress")
        try:
            progress = float(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress = None

        return {
            "status": str(status).lower(),
            "progress": progress,
            "error": str(error) if error else None,
            "raw": raw,
        }


# ---------------------------------------------------------------------------
# High-level helpers
# ---------------------------------------------------------------------------


def _pick_best_match(
    results: list[dict[str, Any]],
    target_artist: str,
    target_title: str,
) -> dict[str, Any] | None:
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
) -> dict[str, Any] | None:
    """Search for a track and trigger download via the configured backend.

    Returns dict with task_id, spotify_id, matched artist/title,
    or None if search returned no results.
    """
    from app.services.spotdl import get_download_client

    client = get_download_client()
    if client is None:
        return None

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
