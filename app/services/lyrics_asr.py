"""
GrooveIQ — lyrics-api (ASR) client (tier 3 of the lyrics cascade).

Thin httpx client for the faster-whisper sidecar deployed on the GPU VM
(``lyrics-api/``). The cascade (``app/services/lyrics.py``) calls
``transcribe(file_path)`` and reads ``.ok / .text / .synced / .language /
.error``; a non-OK result is treated as a re-queueable ``search_error`` so a
GPU-VM outage never permanently buries a track (issue #122).

Path mapping mirrors ``MEDIA_SERVER_MUSIC_PATH``: when the VM mounts the
library at a different path than GrooveIQ's ``MUSIC_LIBRARY_PATH``, set
``LYRICS_API_MUSIC_PATH`` to remap the prefix the sidecar reads from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AsrResult:
    """Outcome of one transcription request.

    ``ok`` is True only when the sidecar was reached and returned a definitive
    response (200). Empty ``text``/``synced`` on an OK result means "no speech"
    (the cascade maps that to ``no_lyrics``). ``ok=False`` means the sidecar was
    unreachable / errored / timed out → the cascade re-queues as ``search_error``.
    """

    ok: bool
    text: str | None = None
    synced: str | None = None  # LRC built from segment timestamps
    language: str | None = None
    error: str | None = None


class LyricsAsrClient:
    """Async client for the lyrics-api sidecar. Inject ``client`` (an
    ``httpx.AsyncClient`` over a ``MockTransport``) in tests."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or settings.LYRICS_API_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else float(settings.LYRICS_API_TIMEOUT_S)
        self._client = client or httpx.AsyncClient(timeout=self._timeout, verify=True)

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass

    def _map_path(self, file_path: str) -> str:
        """Remap GrooveIQ's library prefix to the sidecar's mount, if configured."""
        src = (settings.MUSIC_LIBRARY_PATH or "").rstrip("/")
        dst = (settings.LYRICS_API_MUSIC_PATH or "").rstrip("/")
        if dst and src and (file_path == src or file_path.startswith(src + "/")):
            return dst + file_path[len(src):]
        return file_path

    async def health_check(self) -> dict[str, Any]:
        try:
            resp = await self._client.get(f"{self._base_url}/health")
            return resp.json()
        except Exception as exc:
            logger.warning("lyrics-api health check failed: %s", exc)
            return {"status": "unavailable", "error": str(exc)}

    async def transcribe(self, file_path: str) -> AsrResult:
        """Transcribe one file by path. Never raises — failures map to ok=False."""
        if not self._base_url:
            return AsrResult(ok=False, error="LYRICS_API_URL not configured")
        payload = {
            "path": self._map_path(file_path),
            "vad": bool(settings.LYRICS_ASR_VAD),
            "word_timestamps": False,
        }
        try:
            resp = await self._client.post(f"{self._base_url}/transcribe", json=payload)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            return AsrResult(ok=False, error=f"network error: {exc}")

        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("detail", "")
            except Exception:
                detail = resp.text[:200]
            return AsrResult(ok=False, error=f"HTTP {resp.status_code}: {detail}")

        try:
            data = resp.json()
        except Exception as exc:
            return AsrResult(ok=False, error=f"unparseable response: {exc}")

        text = (data.get("text") or "").strip() or None
        synced = (data.get("lrc") or "").strip() or None
        return AsrResult(ok=True, text=text, synced=synced, language=data.get("language"))


# ---------------------------------------------------------------------------
# Module-level lazy singleton (one pooled client per process)
# ---------------------------------------------------------------------------

_singleton: LyricsAsrClient | None = None


def get_lyrics_asr_client() -> LyricsAsrClient:
    global _singleton
    if _singleton is None:
        _singleton = LyricsAsrClient()
    return _singleton


async def close_lyrics_asr_client() -> None:
    global _singleton
    if _singleton is not None:
        await _singleton.close()
        _singleton = None
