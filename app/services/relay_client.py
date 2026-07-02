"""GrooveIQ – stateless APN relay client (overview §8).

grooveiq owns the device-token registry; this client only forwards a webhook to
the relay, which holds Ampster's Apple ``.p8`` and speaks HTTP/2 to APNs. No
Apple credentials live here.

Discipline mirrored from ``StreamripClient`` (streamrip.py): a transport error
("couldn't reach the relay") is NOT the same as "the relay says this token is
dead". Only a ``410`` inside a ``2xx`` results array prunes a token; a 5xx /
timeout raises ``RelayError`` so the dispatcher keeps the notification
``pending`` and retries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class RelayError(RuntimeError):
    """Transport / non-2xx failure talking to the relay (retryable)."""


class RelayClient:
    _MIN_REQUEST_GAP = 0.3  # 300ms between requests (mirrors StreamripClient._throttle)

    def __init__(self, base_url: str, shared_secret: str):
        self._base_url = base_url.rstrip("/")
        self._last_request = 0.0
        self._client = httpx.AsyncClient(
            timeout=15.0,
            verify=True,
            headers={"Authorization": f"Bearer {shared_secret}"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._MIN_REQUEST_GAP:
            await asyncio.sleep(self._MIN_REQUEST_GAP - elapsed)
        self._last_request = time.monotonic()

    async def push(
        self,
        *,
        tokens: list[str],
        environment: str,
        notification: dict[str, Any],
        collapse_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """POST the payload; return the relay's per-token ``results`` list.

        Raises ``RelayError`` on non-2xx / transport failure so the dispatcher can
        leave the notification ``pending`` and retry with backoff. A 2xx with a
        ``results`` array is the only delivered outcome — a ``410`` inside results
        is a per-token prune signal, NOT a request failure.
        """
        await self._throttle()
        payload: dict[str, Any] = {
            "tokens": tokens,
            "environment": environment,
            "notification": notification,
        }
        if collapse_id:
            payload["collapse_id"] = collapse_id
        try:
            resp = await self._client.post(f"{self._base_url}/v1/push", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # normalise every transport/parse failure to RelayError
            raise RelayError(str(exc)) from exc
        results = data.get("results")
        if not isinstance(results, list):
            raise RelayError("relay returned no results array")
        return results


def get_relay_client() -> RelayClient | None:
    """Factory (mirrors ``spotdl.get_download_client``): returns ``None`` when the
    relay isn't configured so callers degrade to Apprise-only. Instantiate per
    dispatch run and ``close()`` in a ``finally``.
    """
    if not (settings.RELAY_BASE and settings.RELAY_SHARED_SECRET):
        return None
    return RelayClient(settings.RELAY_BASE, settings.RELAY_SHARED_SECRET)
