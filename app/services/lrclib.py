"""
GrooveIQ — LRCLIB client (tier 2 of the lyrics cascade).

LRCLIB (https://lrclib.net) is a free, key-less community lyrics database that
returns both plain and time-synced (LRC) lyrics. We query it with the exact
``(artist, title, album, duration)`` tuple its ``/api/get`` endpoint wants, and
fall back to its fuzzy ``/api/search`` (disambiguated by duration ±2 s) on a
404.

Mirrors the httpx-client conventions used elsewhere in the codebase
(``lastfm_client.py``, ``streamrip.py``): a polite request gap, an identifying
``User-Agent`` (LRCLIB asks for one), and a ``SearchOutcome``-style result that
distinguishes "searched OK, nothing found" (→ ``no_lyrics``, terminal-ish) from
"couldn't reach / error" (→ ``search_error``, re-queueable). A tiny in-process
TTL cache avoids hammering the API for repeated lookups within a drain batch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Polite client behaviour. LRCLIB is a free community service — small gap
# between requests, identify ourselves, honour 429.
_MIN_REQUEST_GAP = 0.34  # seconds (~3 req/s ceiling)
_DEFAULT_TIMEOUT = 15.0
_CACHE_TTL_S = 600.0  # short: a drain batch reuses lookups; not a long-term store
_CACHE_MAX = 2048
_MAX_LYRIC_CHARS = 20000
# Duration tolerance when disambiguating fuzzy /api/search candidates.
_DURATION_TOLERANCE_S = 2.0


@dataclass
class LrclibResult:
    """A resolved LRCLIB record. ``found`` is False for a clean miss."""

    found: bool = False
    instrumental: bool = False
    plain: str | None = None
    synced: str | None = None
    # LRCLIB does not return a language; left None (Phase D may run text LID).
    language: str | None = None


@dataclass
class LrclibOutcome:
    """Whether the lookup ran, plus the result.

    ``ok`` is True only when LRCLIB was actually reached and gave a definitive
    answer (200 with data, or a clean 404 "not found"). Network errors,
    timeouts, 429 and 5xx set ``ok=False`` with ``error`` — the caller treats
    those as a re-queueable ``search_error`` rather than a terminal ``no_lyrics``
    (issue #122 distinction).
    """

    ok: bool
    result: LrclibResult
    error: str | None = None


def _exc_str(exc: Exception) -> str:
    """Stringify an exception so the message is never blank. httpx timeout
    exceptions (ConnectTimeout/ReadTimeout) stringify to '' — that's why the
    drain showed the unhelpful 'lrclib: network error: ' with nothing after it.
    Fall back to the class name so an operator can tell a timeout from a
    connection refusal / DNS failure."""
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


def _clean(text) -> str | None:
    if not text:
        return None
    out = str(text).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not out:
        return None
    return out[:_MAX_LYRIC_CHARS]


class LrclibClient:
    """Async LRCLIB client. Pass ``client`` (an ``httpx.AsyncClient`` over a
    ``MockTransport``) in tests to avoid network access."""

    def __init__(
        self,
        base_url: str | None = None,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.LYRICS_LRCLIB_URL).rstrip("/")
        self._user_agent = user_agent or settings.lyrics_lrclib_user_agent
        self._client = client or httpx.AsyncClient(
            timeout=_DEFAULT_TIMEOUT,
            verify=True,
            headers={"User-Agent": self._user_agent},
        )
        self._last_request = 0.0
        self._cache: dict[tuple, tuple[float, LrclibOutcome]] = {}

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < _MIN_REQUEST_GAP:
            await asyncio.sleep(_MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    async def lookup(
        self,
        artist: str | None,
        title: str | None,
        album: str | None = None,
        duration: float | None = None,
    ) -> LrclibOutcome:
        """Resolve lyrics for one track. Tries /api/get, then fuzzy /api/search."""
        if not artist or not title:
            # Nothing to query with — a clean miss, not an error.
            return LrclibOutcome(ok=True, result=LrclibResult(found=False))

        cache_key = (
            artist.strip().lower(),
            title.strip().lower(),
            (album or "").strip().lower(),
            round(duration) if duration else 0,
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            ts, outcome = cached
            if time.monotonic() - ts < _CACHE_TTL_S:
                return outcome

        outcome = await self._lookup_uncached(artist, title, album, duration)
        # Only cache definitive answers; let transient errors retry immediately.
        if outcome.ok:
            if len(self._cache) >= _CACHE_MAX:
                self._cache.clear()
            self._cache[cache_key] = (time.monotonic(), outcome)
        return outcome

    async def _lookup_uncached(
        self,
        artist: str,
        title: str,
        album: str | None,
        duration: float | None,
    ) -> LrclibOutcome:
        params = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = str(round(duration))

        try:
            await self._throttle()
            resp = await self._client.get(f"{self._base_url}/api/get", params=params)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            return LrclibOutcome(ok=False, result=LrclibResult(), error=f"network error: {_exc_str(exc)}")

        if resp.status_code == 200:
            try:
                return LrclibOutcome(ok=True, result=self._parse_record(resp.json()))
            except Exception as exc:
                return LrclibOutcome(ok=False, result=LrclibResult(), error=f"unparseable response: {exc}")

        if resp.status_code == 404:
            # Exact match missing — try fuzzy search, disambiguated by duration.
            return await self._search_fallback(artist, title, album, duration)

        if resp.status_code == 429:
            return LrclibOutcome(ok=False, result=LrclibResult(), error="rate_limited (429)")

        return LrclibOutcome(ok=False, result=LrclibResult(), error=f"HTTP {resp.status_code}")

    async def _search_fallback(
        self,
        artist: str,
        title: str,
        album: str | None,
        duration: float | None,
    ) -> LrclibOutcome:
        params = {"track_name": title, "artist_name": artist}
        try:
            await self._throttle()
            resp = await self._client.get(f"{self._base_url}/api/search", params=params)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            return LrclibOutcome(ok=False, result=LrclibResult(), error=f"network error: {_exc_str(exc)}")

        if resp.status_code == 429:
            return LrclibOutcome(ok=False, result=LrclibResult(), error="rate_limited (429)")
        if resp.status_code != 200:
            return LrclibOutcome(ok=False, result=LrclibResult(), error=f"HTTP {resp.status_code}")

        try:
            candidates = resp.json()
        except Exception as exc:
            return LrclibOutcome(ok=False, result=LrclibResult(), error=f"unparseable response: {exc}")

        best = self._pick_candidate(candidates, duration)
        if best is None:
            # Searched OK, nothing acceptable — a clean miss (no_lyrics), not error.
            return LrclibOutcome(ok=True, result=LrclibResult(found=False))
        return LrclibOutcome(ok=True, result=self._parse_record(best))

    @staticmethod
    def _pick_candidate(candidates, duration: float | None) -> dict | None:
        if not isinstance(candidates, list) or not candidates:
            return None
        # Without a target duration, fall back to the first candidate that has
        # any lyrics, preferring synced.
        if not duration:
            synced = [c for c in candidates if isinstance(c, dict) and c.get("syncedLyrics")]
            return (synced or [c for c in candidates if isinstance(c, dict)])[0] if candidates else None

        scored = []
        for c in candidates:
            if not isinstance(c, dict):
                continue
            cand_dur = c.get("duration")
            if cand_dur is None:
                continue
            try:
                delta = abs(float(cand_dur) - float(duration))
            except (TypeError, ValueError):
                continue
            if delta <= _DURATION_TOLERANCE_S:
                # Prefer closer duration, then synced over plain.
                has_synced = 1 if c.get("syncedLyrics") else 0
                scored.append((delta, -has_synced, c))
        if not scored:
            return None
        scored.sort(key=lambda t: (t[0], t[1]))
        return scored[0][2]

    @staticmethod
    def _parse_record(record: dict) -> LrclibResult:
        if not isinstance(record, dict):
            return LrclibResult(found=False)
        if record.get("instrumental"):
            return LrclibResult(found=True, instrumental=True)
        plain = _clean(record.get("plainLyrics"))
        synced = _clean(record.get("syncedLyrics"))
        if not plain and not synced:
            return LrclibResult(found=False)
        return LrclibResult(found=True, plain=plain, synced=synced)


# ---------------------------------------------------------------------------
# Module-level lazy singleton (one pooled client per process)
# ---------------------------------------------------------------------------

_singleton: LrclibClient | None = None


def get_lrclib_client() -> LrclibClient:
    global _singleton
    if _singleton is None:
        _singleton = LrclibClient()
    return _singleton


async def close_lrclib_client() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.close()
        _singleton = None
