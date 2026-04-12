"""
GrooveIQ -- AcousticBrainz Lookup client.

Talks to the optional acousticbrainz-lookup container to find tracks
matching a user's taste profile from the ~29.5M AcousticBrainz dataset.
Discovered tracks can then be auto-downloaded via Lidarr/spotdl-api.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class AcousticBrainzClient:
    """Async HTTP client for the acousticbrainz-lookup REST API."""

    _MIN_REQUEST_GAP = 0.3  # 300ms between requests

    def __init__(self, base_url: str):
        self._base_url = base_url.rstrip("/")
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0, verify=True)

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        import asyncio

        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_REQUEST_GAP:
            await asyncio.sleep(self._MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    async def health_check(self) -> dict[str, Any]:
        """Check if the service is ready."""
        try:
            await self._throttle()
            resp = await self._client.get(f"{self._base_url}/health")
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("AcousticBrainz Lookup health check failed: %s", exc)
            return {"status": "unavailable", "error": str(exc)}

    async def search(
        self,
        taste_profile: dict[str, Any],
        limit: int = 50,
        exclude_mbids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search for tracks matching a user's taste profile.

        Converts GrooveIQ taste profile format to AB Lookup search request.
        Returns list of track dicts with mbid, artist, title, etc.
        """
        audio = taste_profile.get("audio_preferences") or {}
        moods = taste_profile.get("mood_preferences") or {}

        # Build search body from taste profile audio preferences
        body: dict[str, Any] = {"limit": limit, "strategy": "closest"}

        # BPM range (±15 around preference)
        pref_bpm = audio.get("bpm")
        if pref_bpm and isinstance(pref_bpm, (int, float)) and pref_bpm > 0:
            body["bpm"] = {"min": pref_bpm - 15, "max": pref_bpm + 15}

        # Energy, danceability, valence (±0.2 around preference)
        for field in ("energy", "danceability", "valence"):
            val = audio.get(field)
            if val is not None and isinstance(val, (int, float)):
                body[field] = {
                    "min": max(0.0, val - 0.2),
                    "max": min(1.0, val + 0.2),
                }

        # Acousticness / instrumentalness
        for field in ("acousticness", "instrumentalness"):
            val = audio.get(field)
            if val is not None and isinstance(val, (int, float)):
                body[field] = {
                    "min": max(0.0, val - 0.2),
                    "max": min(1.0, val + 0.2),
                }

        # Mood preferences
        mood_filters: dict[str, Any] = {}
        mood_map = {
            "happy": "happy",
            "sad": "sad",
            "aggressive": "aggressive",
            "relaxed": "relaxed",
            "party": "party",
            "acoustic": "acoustic",
            "electronic": "electronic",
        }
        for mood_key, api_key in mood_map.items():
            val = moods.get(mood_key)
            if val is not None and isinstance(val, (int, float)) and val > 0.3:
                mood_filters[api_key] = {"min": max(0.0, val - 0.2)}
        if mood_filters:
            body["moods"] = mood_filters

        if exclude_mbids:
            body["exclude_mbids"] = exclude_mbids

        try:
            await self._throttle()
            resp = await self._client.post(
                f"{self._base_url}/v1/search", json=body
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("results", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 503:
                logger.info("AcousticBrainz Lookup still ingesting, skipping search")
            else:
                logger.warning(
                    "AcousticBrainz search failed (HTTP %d): %s",
                    exc.response.status_code,
                    exc,
                )
            return []
        except Exception as exc:
            logger.warning("AcousticBrainz search failed: %s", exc)
            return []

    async def get_track(self, mbid: str) -> dict[str, Any] | None:
        """Look up a single track by MusicBrainz Recording ID."""
        try:
            await self._throttle()
            resp = await self._client.get(f"{self._base_url}/v1/track/{mbid}")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("AcousticBrainz track lookup failed (%s): %s", mbid, exc)
            return None
