"""
GrooveIQ – Last.fm API client for per-user integration.

Handles both read-only (profile, top artists/tracks) and authenticated
(scrobbling, now-playing) API calls.

Security:
- Session keys are Fernet-encrypted at rest (AES-128-CBC + HMAC-SHA256).
- Passwords are never stored — used once to obtain a session key, then discarded.
- API signatures use HMAC-grade MD5 as required by Last.fm's protocol.
- Rate limited to 5 req/s (Last.fm's published limit).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_BASE_URL = "https://ws.audioscrobbler.com/2.0/"
_MIN_REQUEST_GAP = 0.2  # 200ms = 5 req/s


# ---------------------------------------------------------------------------
# Fernet encryption for session keys
# ---------------------------------------------------------------------------

def encrypt_session_key(plaintext: str) -> str:
    """Encrypt a Last.fm session key for database storage."""
    from cryptography.fernet import Fernet
    key = settings.LASTFM_SESSION_ENCRYPTION_KEY
    if not key:
        raise ValueError("LASTFM_SESSION_ENCRYPTION_KEY is not configured")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_session_key(ciphertext: str) -> str:
    """Decrypt a Last.fm session key from database storage."""
    from cryptography.fernet import Fernet
    key = settings.LASTFM_SESSION_ENCRYPTION_KEY
    if not key:
        raise ValueError("LASTFM_SESSION_ENCRYPTION_KEY is not configured")
    f = Fernet(key.encode() if isinstance(key, str) else key)
    return f.decrypt(ciphertext.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------------------
# API signature (required by Last.fm for all write operations)
# ---------------------------------------------------------------------------

def _generate_api_sig(params: dict[str, str]) -> str:
    """
    Generate Last.fm API signature.

    Algorithm: sort params alphabetically by key, concatenate key+value pairs,
    append the shared secret, MD5 hash the result.
    """
    # Exclude 'format' and 'callback' per Last.fm spec
    filtered = {k: v for k, v in params.items() if k not in ("format", "callback")}
    sig_string = "".join(f"{k}{v}" for k, v in sorted(filtered.items()))
    sig_string += settings.LASTFM_API_SECRET
    return hashlib.md5(sig_string.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class LastFmClient:
    """Async Last.fm API client with rate limiting."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "GrooveIQ/1.0"},
        )
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        """Enforce minimum gap between requests (5 req/s)."""
        async with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < _MIN_REQUEST_GAP:
                await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
            self._last_request = time.monotonic()

    # -- Read-only API calls (no auth needed) --------------------------------

    async def _get(self, method: str, params: Optional[dict] = None) -> dict:
        """Execute a read-only Last.fm API call."""
        await self._throttle()
        req_params = {
            "method": method,
            "api_key": settings.LASTFM_API_KEY,
            "format": "json",
            **(params or {}),
        }
        resp = await self._client.get(_BASE_URL, params=req_params)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise LastFmError(data.get("error", 0), data.get("message", "Unknown error"))
        return data

    async def get_user_info(self, username: str) -> dict:
        data = await self._get("user.getInfo", {"user": username})
        return data.get("user", {})

    async def get_top_artists(
        self, username: str, period: str = "overall", limit: int = 50
    ) -> list[dict]:
        data = await self._get("user.getTopArtists", {
            "user": username, "period": period, "limit": str(limit),
        })
        return data.get("topartists", {}).get("artist", [])

    async def get_top_tracks(
        self, username: str, period: str = "overall", limit: int = 50
    ) -> list[dict]:
        data = await self._get("user.getTopTracks", {
            "user": username, "period": period, "limit": str(limit),
        })
        return data.get("toptracks", {}).get("track", [])

    async def get_loved_tracks(self, username: str, limit: int = 50) -> list[dict]:
        data = await self._get("user.getLovedTracks", {
            "user": username, "limit": str(limit),
        })
        return data.get("lovedtracks", {}).get("track", [])

    async def get_artist_tags(self, artist: str, limit: int = 10) -> list[dict]:
        data = await self._get("artist.getTopTags", {
            "artist": artist, "limit": str(limit),
        })
        return data.get("toptags", {}).get("tag", [])

    # -- Authenticated API calls (require session key) -----------------------

    async def _post_signed(self, params: dict[str, str]) -> dict:
        """Execute a signed POST request (for write operations)."""
        await self._throttle()
        params["api_key"] = settings.LASTFM_API_KEY
        params["api_sig"] = _generate_api_sig(params)
        params["format"] = "json"
        resp = await self._client.post(_BASE_URL, data=params)
        # Last.fm returns error details in JSON body even on non-200 status codes
        try:
            data = resp.json()
        except Exception:
            raise LastFmError(
                resp.status_code,
                f"Last.fm returned HTTP {resp.status_code} with non-JSON body",
            )
        if "error" in data:
            raise LastFmError(data.get("error", 0), data.get("message", "Unknown error"))
        if resp.status_code >= 400:
            raise LastFmError(resp.status_code, f"HTTP {resp.status_code}: {data}")
        return data

    async def get_mobile_session(self, username: str, password: str) -> str:
        """
        Authenticate via auth.getMobileSession.

        Called by client apps — GrooveIQ exchanges the credentials for a
        session key and discards the password immediately.
        Returns the session key string.
        """
        params = {
            "method": "auth.getMobileSession",
            "username": username,
            "password": password,
        }
        data = await self._post_signed(params)
        session_key = data.get("session", {}).get("key")
        if not session_key:
            raise LastFmError(0, "No session key in response")
        return session_key

    async def update_now_playing(
        self,
        session_key: str,
        artist: str,
        track: str,
        album: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> dict:
        """Set the currently-playing track. Best-effort, no retry."""
        params: dict[str, str] = {
            "method": "track.updateNowPlaying",
            "sk": session_key,
            "artist": artist,
            "track": track,
        }
        if album:
            params["album"] = album
        if duration is not None:
            params["duration"] = str(duration)
        return await self._post_signed(params)

    async def scrobble(
        self,
        session_key: str,
        tracks: list[dict[str, Any]],
    ) -> dict:
        """
        Scrobble up to 50 tracks in a single batch.

        Each track dict must have: artist, track, timestamp.
        Optional: album, duration.
        """
        if not tracks:
            return {"accepted": 0, "ignored": 0}
        if len(tracks) > 50:
            raise ValueError("Last.fm batch limit is 50 tracks per request")

        params: dict[str, str] = {
            "method": "track.scrobble",
            "sk": session_key,
        }
        for i, t in enumerate(tracks):
            params[f"artist[{i}]"] = t["artist"]
            params[f"track[{i}]"] = t["track"]
            params[f"timestamp[{i}]"] = str(t["timestamp"])
            if t.get("album"):
                params[f"album[{i}]"] = t["album"]
            if t.get("duration"):
                params[f"duration[{i}]"] = str(t["duration"])

        return await self._post_signed(params)


class LastFmError(Exception):
    """Last.fm API error with error code."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"Last.fm error {code}: {message}")


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_client: Optional[LastFmClient] = None


def get_lastfm_client() -> LastFmClient:
    """Return the shared Last.fm client instance."""
    global _client
    if _client is None:
        _client = LastFmClient()
    return _client
