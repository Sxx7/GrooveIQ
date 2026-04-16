"""
GrooveIQ -- slskd (Soulseek) integration client.

Talks to a self-hosted slskd instance to search and download music from
the Soulseek peer-to-peer network.  slskd exposes a REST API on top of
the Soulseek.NET library.

Unlike spotdl-api / Spotizerr, Soulseek is text-search-based (no Spotify
IDs).  The search flow is asynchronous: submit a query, poll for results,
rank files by quality, then queue a download.

API docs: https://github.com/slskd/slskd
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Audio extensions we consider valid results.
_AUDIO_EXTENSIONS = frozenset(
    {
        ".mp3",
        ".flac",
        ".ogg",
        ".m4a",
        ".wav",
        ".aac",
        ".opus",
        ".wv",
        ".alac",
        ".ape",
        ".wma",
    }
)

# Minimum file size (bytes) to filter out corrupt/stub files.
_MIN_FILE_SIZE = 500_000  # 500 KB


def _ext(filename: str) -> str:
    """Extract lowercase extension from a filename."""
    _, ext = os.path.splitext(filename)
    return ext.lower()


class SlskdClient:
    """Async HTTP client for the slskd REST API."""

    _MIN_REQUEST_GAP = 0.3  # 300ms between requests

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._last_request: float = 0.0
        self._client = httpx.AsyncClient(timeout=30.0, verify=True)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key, "Accept": "application/json"}

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_REQUEST_GAP:
            await asyncio.sleep(self._MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    # -- Health ----------------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        """Check if slskd is reachable and connected to Soulseek."""
        try:
            await self._throttle()
            resp = await self._client.get(
                f"{self._base_url}/api/v0/server",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("slskd health check failed: %s", exc)
            return {"state": "unavailable", "error": str(exc)}

    # -- Search ----------------------------------------------------------------

    async def search(
        self,
        query: str,
        timeout_s: int | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Search Soulseek for files matching a query.

        Submits a search, polls until complete or timeout, ranks results
        by audio quality, and returns the top ``limit`` matches.
        """
        if timeout_s is None:
            timeout_s = settings.SLSKD_SEARCH_TIMEOUT

        search_id = await self._submit_search(query)
        if not search_id:
            return []

        try:
            await self._wait_for_search(search_id, timeout_s)
            responses = await self._get_search_responses(search_id)
            results = self._flatten_and_rank(responses, query)
            if not results:
                # Diagnose: show raw counts so we can see whether peers responded
                # at all vs whether everything was filtered out.
                total_files = sum(len(r.get("files") or []) for r in responses)
                total_locked = sum(
                    1
                    for r in responses
                    for f in r.get("files") or []
                    if f.get("isLocked")
                )
                total_audio = sum(
                    1
                    for r in responses
                    for f in r.get("files") or []
                    if not f.get("isLocked") and _ext(f.get("filename", "")) in _AUDIO_EXTENSIONS
                )
                logger.info(
                    "slskd search %r empty after filtering: %d responses, %d files, "
                    "%d locked, %d audio (rejected by min-size %d B)",
                    query,
                    len(responses),
                    total_files,
                    total_locked,
                    total_audio,
                    _MIN_FILE_SIZE,
                )
            return results[:limit]
        finally:
            # Cleanup the search on slskd side.
            await self._delete_search(search_id)

    async def _submit_search(self, query: str) -> str | None:
        """POST a new search. Returns the search ID (GUID)."""
        await self._throttle()
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v0/searches",
                headers=self._headers(),
                json={"searchText": query},
            )
            if resp.status_code == 429:
                logger.warning("slskd search rate-limited, try again later")
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("id") or None
        except Exception as exc:
            logger.warning("slskd search submit failed for %r: %s", query, exc)
            return None

    async def _wait_for_search(self, search_id: str, timeout_s: int) -> None:
        """Poll until the search completes or timeout is reached."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            await self._throttle()
            try:
                resp = await self._client.get(
                    f"{self._base_url}/api/v0/searches/{search_id}",
                    headers=self._headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("isComplete"):
                    return
            except Exception as exc:
                logger.debug("slskd search poll error: %s", exc)
                continue

    async def _get_search_responses(self, search_id: str) -> list[dict[str, Any]]:
        """Fetch all responses for a completed search."""
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v0/searches/{search_id}/responses",
                headers=self._headers(),
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.warning("slskd search responses fetch failed: %s", exc)
            return []

    async def _delete_search(self, search_id: str) -> None:
        """Delete a search to free resources on slskd."""
        try:
            await self._throttle()
            await self._client.delete(
                f"{self._base_url}/api/v0/searches/{search_id}",
                headers=self._headers(),
            )
        except Exception:
            pass  # best-effort cleanup

    def _flatten_and_rank(
        self,
        responses: list[dict[str, Any]],
        query: str,
    ) -> list[dict[str, Any]]:
        """Flatten search responses into a ranked list of file results."""
        results: list[dict[str, Any]] = []
        prefer_lossless = settings.SLSKD_PREFER_LOSSLESS

        for response in responses:
            username = response.get("username", "")
            has_free_slot = bool(response.get("hasFreeUploadSlot"))
            queue_length = response.get("queueLength", 0)

            for f in response.get("files") or []:
                if f.get("isLocked"):
                    continue

                filename = f.get("filename", "")
                ext = _ext(filename)
                if ext not in _AUDIO_EXTENSIONS:
                    continue

                size = f.get("size", 0)
                if size < _MIN_FILE_SIZE:
                    continue

                bit_rate = f.get("bitRate")
                sample_rate = f.get("sampleRate")
                bit_depth = f.get("bitDepth")
                length = f.get("length")

                score = _score_file(
                    ext=ext,
                    bit_rate=bit_rate,
                    has_free_slot=has_free_slot,
                    queue_length=queue_length,
                    filename=filename,
                    query=query,
                    prefer_lossless=prefer_lossless,
                )

                results.append(
                    {
                        "username": username,
                        "filename": filename,
                        "size": size,
                        "extension": ext.lstrip("."),
                        "bit_rate": bit_rate,
                        "sample_rate": sample_rate,
                        "bit_depth": bit_depth,
                        "length": length,
                        "has_free_slot": has_free_slot,
                        "queue_length": queue_length,
                        "score": round(score, 2),
                    }
                )

        results.sort(key=lambda r: r["score"], reverse=True)
        return results

    # -- Download --------------------------------------------------------------

    async def download(
        self,
        username: str,
        filename: str,
        size: int,
    ) -> dict[str, Any]:
        """Queue a file for download from a Soulseek peer.

        Returns dict with transfer details or error info.
        """
        await self._throttle()
        try:
            resp = await self._client.post(
                f"{self._base_url}/api/v0/transfers/downloads/{_url_encode(username)}",
                headers=self._headers(),
                json=[{"filename": filename, "size": size}],
            )
            if resp.status_code == 429:
                return {"id": "", "state": "error", "error": "slskd concurrent download limit exceeded"}
            resp.raise_for_status()

            data = resp.json()
            # slskd returns {"enqueued": N, "failed": N} on bulk queue.
            # Individual transfer IDs are obtained by listing downloads.
            return {
                "enqueued": data.get("enqueued", 0),
                "failed": data.get("failed", 0),
                "state": "queued" if data.get("enqueued", 0) > 0 else "error",
                "username": username,
                "filename": filename,
            }
        except Exception as exc:
            logger.warning("slskd download queue failed for %s/%s: %s", username, filename, exc)
            return {"id": "", "state": "error", "error": str(exc)}

    async def find_transfer(
        self,
        username: str,
        filename: str,
    ) -> dict[str, Any] | None:
        """Find a specific transfer by username and filename.

        Used after queueing to get the transfer ID and current state.
        """
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v0/transfers/downloads/{_url_encode(username)}",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            # Response is grouped by directory. Walk all files.
            for directory in data if isinstance(data, list) else [data]:
                for transfer in directory.get("files") or []:
                    if transfer.get("filename") == filename:
                        return _normalize_transfer(transfer, username)
            return None
        except Exception as exc:
            logger.warning("slskd find_transfer failed for %s: %s", username, exc)
            return None

    async def get_transfer(
        self,
        username: str,
        transfer_id: str,
    ) -> dict[str, Any] | None:
        """Get status of a specific transfer by ID."""
        await self._throttle()
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v0/transfers/downloads/{_url_encode(username)}/{transfer_id}",
                headers=self._headers(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return _normalize_transfer(resp.json(), username)
        except Exception as exc:
            logger.warning("slskd get_transfer failed for %s/%s: %s", username, transfer_id, exc)
            return None

    async def cancel_transfer(
        self,
        username: str,
        transfer_id: str,
    ) -> bool:
        """Cancel a download transfer."""
        await self._throttle()
        try:
            resp = await self._client.delete(
                f"{self._base_url}/api/v0/transfers/downloads/{_url_encode(username)}/{transfer_id}",
                headers=self._headers(),
            )
            return resp.status_code == 204
        except Exception as exc:
            logger.warning("slskd cancel_transfer failed: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_encode(username: str) -> str:
    """URL-encode a username for path interpolation."""
    from urllib.parse import quote

    return quote(username, safe="")


def _normalize_transfer(raw: dict[str, Any], username: str) -> dict[str, Any]:
    """Normalize a slskd Transfer object into a stable shape."""
    return {
        "id": raw.get("id", ""),
        "username": username,
        "filename": raw.get("filename", ""),
        "size": raw.get("size", 0),
        "state": raw.get("state", "unknown"),
        "bytes_transferred": raw.get("bytesTransferred", 0),
        "percent_complete": raw.get("percentComplete", 0.0),
        "average_speed": raw.get("averageSpeed", 0.0),
        "place_in_queue": raw.get("placeInQueue"),
        "exception": raw.get("exception"),
        "started_at": raw.get("startedAt"),
        "ended_at": raw.get("endedAt"),
    }


# Normalize query for fuzzy filename matching.
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")


def _normalize(s: str) -> str:
    return _NON_ALNUM_RE.sub("", s.lower()).strip()


def _score_file(
    *,
    ext: str,
    bit_rate: int | None,
    has_free_slot: bool,
    queue_length: int,
    filename: str,
    query: str,
    prefer_lossless: bool,
) -> float:
    """Score a search result file by quality and availability."""
    score = 0.0

    # Format quality
    if ext == ".flac":
        score += 100 if prefer_lossless else 70
    elif ext == ".wav":
        score += 90 if prefer_lossless else 60
    elif ext in (".alac", ".ape", ".wv"):
        score += 85 if prefer_lossless else 65
    elif ext == ".mp3":
        if bit_rate and bit_rate >= 320:
            score += 80
        elif bit_rate and bit_rate >= 256:
            score += 60
        elif bit_rate and bit_rate >= 192:
            score += 40
        else:
            score += 20
    elif ext in (".ogg", ".opus", ".m4a", ".aac"):
        if bit_rate and bit_rate >= 256:
            score += 70
        elif bit_rate and bit_rate >= 192:
            score += 50
        else:
            score += 30
    else:
        score += 10

    # Availability bonuses
    if has_free_slot:
        score += 20
    if queue_length < 5:
        score += 10
    elif queue_length < 20:
        score += 5

    # Filename relevance: check if query words appear in the filename
    query_words = _normalize(query).split()
    filename_norm = _normalize(filename)
    if query_words:
        matched = sum(1 for w in query_words if w in filename_norm)
        relevance = matched / len(query_words)
        score += relevance * 15

    return score


def get_slskd_client() -> SlskdClient | None:
    """Return a SlskdClient if slskd is configured, else None."""
    if not settings.slskd_enabled:
        return None
    return SlskdClient(settings.SLSKD_URL, settings.SLSKD_API_KEY)
