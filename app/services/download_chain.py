"""
GrooveIQ – Download backend adapters + cascade orchestrator.

Implements the policy from ``download_routing_schema``:
  * Wraps each download backend in a uniform ``BackendAdapter`` interface.
  * Walks the configured priority chain for a given purpose.
  * Honours per-entry quality thresholds and timeouts.
  * Returns a structured ``CascadeResult`` with per-backend attempt records,
    so the calling route can persist a full attempt log on ``DownloadRequest``.

Adapters wrap (not replace) the existing ``SpotdlClient`` / ``StreamripClient``
/ ``SpotizerrClient`` / ``SlskdClient``. Direct callers of those clients
(legacy charts.py, bulk_download.py) keep working — Phase 4 will migrate
them to the chain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.models.download_routing_schema import (
    DEFAULT_BACKEND_QUALITY,
    BackendChainEntry,
    BackendName,
    QualityTier,
    quality_meets,
)
from app.services.download_routing import get_routing

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class TrackRef:
    """Stable cross-backend reference to a track.

    Adapters use whatever subset of fields they need:
      * spotify_id: spotdl/spotizerr key directly off this
      * service_id + service: streamrip can dispatch by Qobuz/Tidal/Deezer ID
      * artist + title: slskd searches by text; streamrip uses for cross-service fallback
    """

    spotify_id: str | None = None
    service: str | None = None
    service_id: str | None = None
    artist: str | None = None
    title: str | None = None
    album: str | None = None
    duration_ms: int | None = None

    def search_query(self) -> str:
        parts = [s for s in (self.artist, self.title) if s]
        return " ".join(parts).strip()


@dataclass
class AlbumRef:
    """Cross-backend reference to an album for the bulk_album cascade.

    Different backends key off different fields:
      * Lidarr: ``mb_release_group_id`` (preferred), or ``artist_name`` + ``album_name``.
      * Streamrip-album: ``service`` + ``album_id`` (Qobuz/Tidal/Deezer album IDs).
    """

    mb_release_group_id: str | None = None
    artist_name: str | None = None
    album_name: str | None = None
    service: str | None = None  # qobuz | tidal | deezer | soundcloud
    album_id: str | None = None  # service-native album ID


@dataclass
class NormalizedSearchResult:
    """One search hit from any backend, normalised for the multi-agent UI.

    ``download_handle`` is an opaque dict the user POSTs back to
    ``/v1/downloads/from-handle`` to pick this exact result.

    ``album_id`` (when known) lets the GUI group tracks by album and offer a
    whole-album download via the same ``from-handle`` endpoint with a handle
    of ``kind=album``.
    """

    backend: str
    download_handle: dict[str, Any]
    title: str
    artist: str
    album: str | None = None
    album_id: str | None = None
    album_year: int | None = None
    album_track_count: int | None = None
    track_number: int | None = None
    image_url: str | None = None
    quality: QualityTier | None = None
    bitrate_kbps: int | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttemptResult:
    """Outcome of a single backend's attempt at downloading a track."""

    backend: str
    success: bool
    status: str  # "queued" | "downloading" | "skipped" | "timeout" | "error"
    task_id: str | None = None
    error: str | None = None
    quality: QualityTier | None = None
    duration_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "success": self.success,
            "status": self.status,
            "task_id": self.task_id,
            "error": self.error,
            "quality": self.quality.value if self.quality else None,
            "duration_ms": self.duration_ms,
            "extra": self.extra,
        }


@dataclass
class CascadeResult:
    """Aggregate result from walking a priority chain."""

    success: bool
    attempts: list[AttemptResult] = field(default_factory=list)
    final_backend: str | None = None
    final_task_id: str | None = None
    final_status: str = "error"
    final_extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class BackendAdapter(Protocol):
    """Uniform interface every download backend must satisfy.

    Concrete adapters own their underlying HTTP client (spotdl / streamrip /
    spotizerr / slskd) and translate between the cross-backend types above
    and the backend-specific call shapes.
    """

    name: BackendName
    expected_quality: QualityTier

    async def is_configured(self) -> bool: ...
    async def search(self, query: str, limit: int = 10) -> list[NormalizedSearchResult]: ...
    async def try_download(self, track_ref: TrackRef) -> AttemptResult: ...
    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------


class _SpotIdAdapterMixin:
    """Shared logic for backends that download by Spotify ID
    (spotdl, spotizerr, streamrip-via-spotify-id)."""

    name: BackendName
    expected_quality: QualityTier
    _client: Any

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def search(self, query: str, limit: int = 10) -> list[NormalizedSearchResult]:
        if self._client is None:
            return []
        try:
            raw = await self._client.search(query, limit=limit)
        except Exception as exc:
            logger.warning("%s search failed: %s", self.name.value, exc)
            return []
        return [self._reshape_search_hit(item) for item in raw if item]

    async def _resolve_id(self, track_ref: TrackRef) -> tuple[str | None, dict[str, Any] | None]:
        """Search the backend for ``track_ref`` and return (id, raw_match).

        Used when a caller doesn't have a Spotify/service ID upfront (charts
        auto-add, bulk downloads). Picks the best match by artist+title.
        """
        from app.services.spotizerr import _pick_best_match  # reuse vetted matcher

        if self._client is None:
            return None, None
        query = track_ref.search_query()
        if not query:
            return None, None
        try:
            raw = await self._client.search(query, limit=5)
        except Exception as exc:
            logger.warning("%s pre-download search failed: %s", self.name.value, exc)
            return None, None
        match = _pick_best_match(raw, track_ref.artist or "", track_ref.title or "")
        if not match:
            return None, None
        # streamrip search results carry _service_id (Qobuz/Tidal/Deezer ID);
        # spotdl/spotizerr return Spotify IDs in `id`.
        track_id = match.get("_service_id") or match.get("id")
        return (track_id or None), match

    def _reshape_search_hit(self, item: dict[str, Any]) -> NormalizedSearchResult:
        artists = item.get("artists") or []
        artist = artists[0]["name"] if artists and isinstance(artists[0], dict) else ""
        album = (item.get("album") or {}).get("name")
        images = (item.get("album") or {}).get("images") or []
        image_url = None
        for img in images:
            if img.get("width") == 300:
                image_url = img["url"]
                break
        if not image_url and images:
            image_url = images[0].get("url")
        spotify_id = item.get("id", "")
        album_id = item.get("_album_id") or None
        album_year = item.get("_album_year")
        if not isinstance(album_year, int):
            album_year = None
        album_track_count = item.get("_album_track_count")
        if not isinstance(album_track_count, int):
            album_track_count = None
        track_number = item.get("_track_number")
        if not isinstance(track_number, int):
            track_number = None

        return NormalizedSearchResult(
            backend=self.name.value,
            download_handle={
                "backend": self.name.value,
                # Default kind is "track"; the GUI may rewrite this to "album"
                # when the user clicks "Download album" on a grouped card.
                "kind": "track",
                "spotify_id": spotify_id,
                "service": item.get("_service"),
                "service_id": item.get("_service_id"),
                "album_id": album_id,
                "artist": artist,
                "title": item.get("name", ""),
            },
            title=item.get("name", ""),
            artist=artist,
            album=album,
            album_id=album_id,
            album_year=album_year,
            album_track_count=album_track_count,
            track_number=track_number,
            image_url=image_url,
            quality=self.expected_quality,
            extra={k: v for k, v in item.items() if k.startswith("_")},
        )


class SpotdlAdapter(_SpotIdAdapterMixin):
    name = BackendName.SPOTDL
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.SPOTDL]

    def __init__(self):
        from app.core.config import settings
        from app.services.spotdl import SpotdlClient

        self._client = SpotdlClient(settings.SPOTDL_API_URL) if settings.spotdl_enabled else None

    async def is_configured(self) -> bool:
        return self._client is not None

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        if not self._client:
            return AttemptResult(backend=self.name.value, success=False, status="skipped", error="not configured")
        spotify_id = track_ref.spotify_id
        if not spotify_id:
            spotify_id, _ = await self._resolve_id(track_ref)
        if not spotify_id:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="no match found and no spotify_id provided",
            )
        return await _run_spot_id_download(
            self.name.value, self.expected_quality, lambda: self._client.download(spotify_id)
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        ref = TrackRef(
            spotify_id=handle.get("spotify_id"),
            artist=handle.get("artist"),
            title=handle.get("title"),
        )
        return await self.try_download(ref)


class StreamripAdapter(_SpotIdAdapterMixin):
    name = BackendName.STREAMRIP
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.STREAMRIP]

    def __init__(self):
        from app.core.config import settings
        from app.services.streamrip import StreamripClient

        self._client = StreamripClient(settings.STREAMRIP_API_URL) if settings.streamrip_enabled else None

    async def is_configured(self) -> bool:
        return self._client is not None

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        if not self._client:
            return AttemptResult(backend=self.name.value, success=False, status="skipped", error="not configured")
        # Streamrip prefers a service_id (Qobuz/Tidal/Deezer ID) when available,
        # but accepts a spotify_id; artist + title supply cross-service fallback.
        primary_id = track_ref.service_id or track_ref.spotify_id
        if not primary_id:
            primary_id, _ = await self._resolve_id(track_ref)
        if not primary_id:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="no match found and no service_id/spotify_id provided",
            )
        return await _run_spot_id_download(
            self.name.value,
            self.expected_quality,
            lambda: self._client.download(
                primary_id, artist=track_ref.artist or "", title=track_ref.title or ""
            ),
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        ref = TrackRef(
            spotify_id=handle.get("spotify_id"),
            service=handle.get("service"),
            service_id=handle.get("service_id"),
            artist=handle.get("artist"),
            title=handle.get("title"),
        )
        return await self.try_download(ref)


class SpotizerrAdapter(_SpotIdAdapterMixin):
    name = BackendName.SPOTIZERR
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.SPOTIZERR]

    def __init__(self):
        from app.core.config import settings
        from app.services.spotizerr import SpotizerrClient

        if settings.spotizerr_enabled:
            self._client = SpotizerrClient(
                settings.SPOTIZERR_URL,
                settings.SPOTIZERR_USERNAME,
                settings.SPOTIZERR_PASSWORD,
            )
        else:
            self._client = None

    async def is_configured(self) -> bool:
        return self._client is not None

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        if not self._client:
            return AttemptResult(backend=self.name.value, success=False, status="skipped", error="not configured")
        spotify_id = track_ref.spotify_id
        if not spotify_id:
            spotify_id, _ = await self._resolve_id(track_ref)
        if not spotify_id:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="no match found and no spotify_id provided",
            )
        return await _run_spot_id_download(
            self.name.value, self.expected_quality, lambda: self._client.download(spotify_id)
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        ref = TrackRef(
            spotify_id=handle.get("spotify_id"),
            artist=handle.get("artist"),
            title=handle.get("title"),
        )
        return await self.try_download(ref)


async def _run_spot_id_download(
    backend_name: str,
    expected_quality: QualityTier,
    coro_factory,
) -> AttemptResult:
    """Common error-handling wrapper for the three Spotify-ID adapters."""
    started = time.monotonic()
    try:
        result = await coro_factory()
    except Exception as exc:
        logger.warning("%s download raised: %s", backend_name, exc)
        return AttemptResult(
            backend=backend_name,
            success=False,
            status="error",
            error=str(exc)[:512],
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    status = result.get("status", "unknown") if isinstance(result, dict) else "unknown"
    task_id = result.get("task_id") if isinstance(result, dict) else None
    err = result.get("error") if isinstance(result, dict) else None
    is_success = status not in ("error", "unknown") and bool(task_id)
    return AttemptResult(
        backend=backend_name,
        success=is_success,
        status=status,
        task_id=task_id,
        error=err,
        quality=expected_quality if is_success else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


class SlskdAdapter:
    """Soulseek adapter — text-search-keyed; no Spotify IDs.

    For ``try_download``: searches the configured slskd instance, picks the
    top-ranked result (already quality-aware via SlskdClient._score_file),
    and queues the file. Handles serve as ``(username, filename, size)``.
    """

    name = BackendName.SLSKD
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.SLSKD]

    def __init__(self):
        from app.services.slskd import get_slskd_client

        self._client = get_slskd_client()

    async def is_configured(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def search(self, query: str, limit: int = 10) -> list[NormalizedSearchResult]:
        if self._client is None:
            return []
        try:
            raw = await self._client.search(query, limit=limit)
        except Exception as exc:
            logger.warning("slskd search failed: %s", exc)
            return []
        results: list[NormalizedSearchResult] = []
        for item in raw:
            results.append(self._reshape_hit(item))
        return results

    def _reshape_hit(self, item: dict[str, Any]) -> NormalizedSearchResult:
        ext = (item.get("extension") or "").lower()
        bitrate = item.get("bitRate") or item.get("bit_rate")
        # Map ext+bitrate → QualityTier
        if ext in (".flac", ".wav", ".alac", ".ape", ".wv"):
            tier = QualityTier.LOSSLESS  # could be HIRES if sample rate known
        elif ext in (".mp3", ".aac", ".m4a", ".ogg", ".opus"):
            if (bitrate and bitrate >= 320) or (bitrate and bitrate >= 256):
                tier = QualityTier.LOSSY_HIGH
            else:
                tier = QualityTier.LOSSY_LOW
        else:
            tier = QualityTier.LOSSY_LOW
        return NormalizedSearchResult(
            backend=self.name.value,
            download_handle={
                "backend": self.name.value,
                "username": item.get("username", ""),
                "filename": item.get("filename", ""),
                "size": item.get("size", 0),
            },
            title=item.get("filename", "").split("/")[-1] or item.get("filename", ""),
            artist=item.get("username", ""),  # slskd doesn't parse artist from filename; placeholder
            album=None,
            image_url=None,
            quality=tier,
            bitrate_kbps=bitrate,
            extra={
                "queue_length": item.get("queueLength"),
                "has_free_slot": item.get("hasFreeUploadSlot"),
                "score": item.get("score"),
                "size": item.get("size"),
            },
        )

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        if self._client is None:
            return AttemptResult(backend=self.name.value, success=False, status="skipped", error="not configured")
        query = track_ref.search_query()
        if not query:
            return AttemptResult(
                backend=self.name.value, success=False, status="skipped", error="slskd needs artist+title"
            )
        started = time.monotonic()
        try:
            hits = await self._client.search(query, limit=20)
        except Exception as exc:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error=f"search failed: {exc}",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        if not hits:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="no peer results",
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        top = hits[0]
        return await self.from_handle(
            {
                "username": top.get("username", ""),
                "filename": top.get("filename", ""),
                "size": top.get("size", 0),
                "_search_started_at": started,
            }
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        if self._client is None:
            return AttemptResult(backend=self.name.value, success=False, status="skipped", error="not configured")
        username = handle.get("username", "")
        filename = handle.get("filename", "")
        size = int(handle.get("size", 0) or 0)
        started = handle.get("_search_started_at") or time.monotonic()
        if not username or not filename:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error="slskd handle requires username and filename",
            )
        try:
            result = await self._client.download(username, filename, size)
        except Exception as exc:
            return AttemptResult(
                backend=self.name.value,
                success=False,
                status="error",
                error=str(exc)[:512],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        ok = bool(result and result.get("enqueued"))
        return AttemptResult(
            backend=self.name.value,
            success=ok,
            status="queued" if ok else "error",
            task_id=str(result.get("transfer_id") or "") or None,
            error=None if ok else (result.get("error") or "slskd refused enqueue"),
            quality=self.expected_quality if ok else None,
            duration_ms=int((time.monotonic() - started) * 1000),
            extra={
                "username": username,
                "filename": filename,
                "size": size,
            },
        )


# ---------------------------------------------------------------------------
# Album-level adapters (bulk_album chain)
# ---------------------------------------------------------------------------


@dataclass
class AlbumAttempt:
    """Result of one album-level backend attempt."""

    backend: str
    success: bool
    status: str  # "queued" | "skipped" | "error"
    album_ids: list[int] = field(default_factory=list)  # Lidarr-internal album IDs
    artist_id: int | None = None  # Lidarr-internal artist ID
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "success": self.success,
            "status": self.status,
            "album_ids": self.album_ids,
            "artist_id": self.artist_id,
            "error": self.error,
            "extra": self.extra,
        }


@dataclass
class AlbumCascadeResult:
    success: bool
    attempts: list[AlbumAttempt] = field(default_factory=list)
    final_backend: str | None = None
    final_extra: dict[str, Any] = field(default_factory=dict)


class LidarrAdapter:
    """Album-level adapter wrapping the existing :class:`LidarrClient`.

    Only operates in the bulk_album chain. Doesn't implement track-level
    methods (raises in :meth:`try_download` / :meth:`from_handle`).
    """

    name = BackendName.LIDARR
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.LIDARR]

    def __init__(self):
        from app.core.config import settings
        from app.services.discovery import LidarrClient

        if settings.discovery_enabled:
            self._client = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
        else:
            self._client = None

    async def is_configured(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def search(self, query: str, limit: int = 10) -> list[NormalizedSearchResult]:
        # Lidarr isn't a track-search backend; surfacing it via multi-search
        # would mislead users. Return empty so it never appears as a hit.
        return []

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        return AttemptResult(
            backend=self.name.value,
            success=False,
            status="skipped",
            error="lidarr is album-level only; not eligible for the individual chain",
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        return await self.try_download(TrackRef())

    # -- Album-level operations ------------------------------------------

    async def try_download_album(self, album_ref: AlbumRef) -> AlbumAttempt:
        """Add the album's artist (unmonitored), monitor the album, trigger search."""
        if self._client is None:
            return AlbumAttempt(
                backend=self.name.value, success=False, status="skipped", error="not configured"
            )
        if not album_ref.mb_release_group_id:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="skipped",
                error="lidarr needs mb_release_group_id",
            )
        try:
            album = await self._client.lookup_album(album_ref.mb_release_group_id)
        except Exception as exc:
            return AlbumAttempt(
                backend=self.name.value, success=False, status="error", error=f"lookup failed: {exc}"
            )
        if not album:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="error",
                error=f"album {album_ref.mb_release_group_id} not in Lidarr metadata",
            )
        # Ensure artist exists; add unmonitored if not.
        artist = album.get("artist") or {}
        artist_mbid = artist.get("foreignArtistId")
        artist_id: int | None = None
        try:
            existing_mbids = await self._client.get_existing_artist_mbids()
            if artist_mbid and artist_mbid not in existing_mbids:
                added = await self._client.add_artist_unmonitored(
                    artist_mbid, artist.get("artistName") or album_ref.artist_name or ""
                )
                artist_id = added.get("id")
        except Exception as exc:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="error",
                error=f"add_artist failed: {exc}",
            )
        # Monitor + search the specific album.
        album_id = album.get("id")
        if not album_id:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="error",
                error="lidarr returned album without id",
            )
        try:
            await self._client.monitor_album([album_id])
            await self._client.search_album([album_id])
        except Exception as exc:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="error",
                error=f"monitor/search failed: {exc}",
            )
        return AlbumAttempt(
            backend=self.name.value,
            success=True,
            status="queued",
            album_ids=[album_id],
            artist_id=artist_id,
            extra={
                "artist_mbid": artist_mbid,
                "mb_release_group_id": album_ref.mb_release_group_id,
            },
        )


class StreamripAlbumAdapter:
    """Stub adapter — full streamrip album-download support is added in Phase 4e.

    For now this is a no-op so users can wire it into the bulk_album chain
    without breaking: it always returns a "skipped" result and Lidarr handles
    the work. Once the streamrip-api wrapper accepts ``entity_type=album``,
    this adapter will POST the album URL.
    """

    name = BackendName.STREAMRIP
    expected_quality = DEFAULT_BACKEND_QUALITY[BackendName.STREAMRIP]

    def __init__(self):
        from app.core.config import settings
        from app.services.streamrip import StreamripClient

        self._client = StreamripClient(settings.STREAMRIP_API_URL) if settings.streamrip_enabled else None

    async def is_configured(self) -> bool:
        return self._client is not None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def search(self, query: str, limit: int = 10) -> list[NormalizedSearchResult]:
        return []

    async def try_download(self, track_ref: TrackRef) -> AttemptResult:
        return AttemptResult(
            backend=self.name.value,
            success=False,
            status="skipped",
            error="StreamripAlbumAdapter is bulk_album only",
        )

    async def from_handle(self, handle: dict[str, Any]) -> AttemptResult:
        return await self.try_download(TrackRef())

    async def try_download_album(self, album_ref: AlbumRef) -> AlbumAttempt:
        if self._client is None:
            return AlbumAttempt(
                backend=self.name.value, success=False, status="skipped", error="not configured"
            )
        if not album_ref.service or not album_ref.album_id:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="skipped",
                error="streamrip album download needs service + album_id (no MB lookup yet)",
            )
        # Phase 4e wires this up against streamrip-api's `entity_type=album` once the
        # wrapper supports it. Until then we politely decline so Lidarr always wins.
        try:
            result = await self._client.download_album(album_ref.service, album_ref.album_id)
        except AttributeError:
            return AlbumAttempt(
                backend=self.name.value,
                success=False,
                status="skipped",
                error="StreamripClient.download_album not yet implemented",
            )
        except Exception as exc:
            return AlbumAttempt(
                backend=self.name.value, success=False, status="error", error=str(exc)[:512]
            )
        ok = bool(result and result.get("task_id") and result.get("status") not in ("error", "unknown"))
        return AlbumAttempt(
            backend=self.name.value,
            success=ok,
            status=result.get("status") if isinstance(result, dict) else "error",
            error=None if ok else (result.get("error") if isinstance(result, dict) else "unknown"),
            extra={"task_id": result.get("task_id") if isinstance(result, dict) else None},
        )


# ---------------------------------------------------------------------------
# Factory + cascade
# ---------------------------------------------------------------------------


_ADAPTER_FACTORIES: dict[BackendName, Any] = {
    BackendName.SPOTDL: SpotdlAdapter,
    BackendName.STREAMRIP: StreamripAdapter,
    BackendName.SPOTIZERR: SpotizerrAdapter,
    BackendName.SLSKD: SlskdAdapter,
    # Lidarr only appears in bulk_album — kept out of the per-track factory.
}


_ALBUM_ADAPTER_FACTORIES: dict[BackendName, Any] = {
    BackendName.LIDARR: LidarrAdapter,
    BackendName.STREAMRIP: StreamripAlbumAdapter,
}


def make_album_adapter(backend: BackendName):
    factory = _ALBUM_ADAPTER_FACTORIES.get(backend)
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:
        logger.warning("Failed to construct %s album adapter: %s", backend.value, exc)
        return None


def make_adapter(backend: BackendName) -> BackendAdapter | None:
    factory = _ADAPTER_FACTORIES.get(backend)
    if factory is None:
        return None
    try:
        return factory()
    except Exception as exc:
        logger.warning("Failed to construct %s adapter: %s", backend.value, exc)
        return None


_VALID_PURPOSES = {"individual", "bulk_per_track", "bulk_album"}


def get_chain(purpose: str) -> list[BackendChainEntry]:
    if purpose not in _VALID_PURPOSES:
        raise ValueError(f"Unknown chain purpose: {purpose!r}")
    routing = get_routing()
    return list(getattr(routing, purpose))


async def try_download_chain(track_ref: TrackRef, purpose: str = "individual") -> CascadeResult:
    """Walk the priority chain for ``purpose``, return the first success.

    Each backend is constructed fresh per attempt so config changes pick up
    immediately. Quality thresholds are evaluated against the adapter's
    declared expected quality (variable-quality backends like slskd compare
    against the actual picked file inside their adapter).
    """
    chain = get_chain(purpose)
    cascade = CascadeResult(success=False)

    for entry in chain:
        if not entry.enabled:
            continue
        adapter = make_adapter(entry.backend)
        if adapter is None:
            cascade.attempts.append(
                AttemptResult(
                    backend=entry.backend.value,
                    success=False,
                    status="skipped",
                    error="no adapter for this backend in this chain",
                )
            )
            continue

        try:
            if not await adapter.is_configured():
                cascade.attempts.append(
                    AttemptResult(
                        backend=entry.backend.value,
                        success=False,
                        status="skipped",
                        error="backend not configured",
                    )
                )
                continue

            # Pre-flight quality gate against declared expected quality.
            if entry.min_quality and not quality_meets(adapter.expected_quality, entry.min_quality):
                cascade.attempts.append(
                    AttemptResult(
                        backend=entry.backend.value,
                        success=False,
                        status="skipped",
                        error=f"expected quality {adapter.expected_quality.value} below threshold {entry.min_quality.value}",
                    )
                )
                continue

            try:
                result = await asyncio.wait_for(
                    adapter.try_download(track_ref), timeout=entry.timeout_s
                )
            except TimeoutError:
                result = AttemptResult(
                    backend=entry.backend.value,
                    success=False,
                    status="timeout",
                    error=f"exceeded {entry.timeout_s}s",
                )

            # Post-flight quality gate (handles slskd's variable quality).
            if (
                result.success
                and entry.min_quality
                and not quality_meets(result.quality, entry.min_quality)
            ):
                # Treat as a quality-rejection failure so the cascade tries the next one.
                # NOTE: at this point slskd has already enqueued the file, so we just
                # log and let the next backend race it.
                rejected = AttemptResult(
                    backend=entry.backend.value,
                    success=False,
                    status="skipped",
                    error=f"actual quality below threshold {entry.min_quality.value}",
                    task_id=result.task_id,
                    quality=result.quality,
                    extra=result.extra,
                )
                cascade.attempts.append(rejected)
                continue

            cascade.attempts.append(result)
            if result.success:
                cascade.success = True
                cascade.final_backend = entry.backend.value
                cascade.final_task_id = result.task_id
                cascade.final_status = result.status
                cascade.final_extra = result.extra
                return cascade
        finally:
            try:
                await adapter.close()
            except Exception:
                pass

    return cascade


async def try_album_download_chain(album_ref: AlbumRef) -> AlbumCascadeResult:
    """Walk the bulk_album priority chain for an album reference.

    Lidarr is the canonical backend (album monitoring + search). Streamrip
    can grab specific album URLs once enabled; per-track backends never
    handle albums and aren't registered here.
    """
    chain = get_chain("bulk_album")
    cascade = AlbumCascadeResult(success=False)

    for entry in chain:
        if not entry.enabled:
            continue
        adapter = make_album_adapter(entry.backend)
        if adapter is None:
            cascade.attempts.append(
                AlbumAttempt(
                    backend=entry.backend.value,
                    success=False,
                    status="skipped",
                    error="no album adapter registered for this backend",
                )
            )
            continue

        try:
            if not await adapter.is_configured():
                cascade.attempts.append(
                    AlbumAttempt(
                        backend=entry.backend.value,
                        success=False,
                        status="skipped",
                        error="backend not configured",
                    )
                )
                continue

            if entry.min_quality and not quality_meets(adapter.expected_quality, entry.min_quality):
                cascade.attempts.append(
                    AlbumAttempt(
                        backend=entry.backend.value,
                        success=False,
                        status="skipped",
                        error=(
                            f"expected quality {adapter.expected_quality.value} "
                            f"below threshold {entry.min_quality.value}"
                        ),
                    )
                )
                continue

            try:
                result = await asyncio.wait_for(
                    adapter.try_download_album(album_ref), timeout=entry.timeout_s
                )
            except TimeoutError:
                result = AlbumAttempt(
                    backend=entry.backend.value,
                    success=False,
                    status="timeout",
                    error=f"exceeded {entry.timeout_s}s",
                )

            cascade.attempts.append(result)
            if result.success:
                cascade.success = True
                cascade.final_backend = entry.backend.value
                cascade.final_extra = result.extra
                return cascade
        finally:
            try:
                await adapter.close()
            except Exception:
                pass

    return cascade


def lidarr_enabled_in_chain(purpose: str = "bulk_album") -> bool:
    """Whether Lidarr is currently a participating backend in the named chain.

    Used by the legacy discovery + fill_library flows as a feature gate before
    they reach for ``LidarrClient`` directly. Once those flows fully migrate
    to ``try_album_download_chain``, this becomes redundant.
    """
    chain = get_chain(purpose) if purpose in _VALID_PURPOSES else []
    for entry in chain:
        if entry.backend == BackendName.LIDARR and entry.enabled:
            return True
    return False


async def search_via_handle(handle: dict[str, Any]) -> AttemptResult:
    """Dispatch a download from an opaque handle returned by the multi-search.

    The handle's ``backend`` field decides which adapter to invoke. When
    ``handle.kind == "album"``, the album-level adapter is used and its
    ``AlbumAttempt`` result is repackaged as an ``AttemptResult`` so the
    caller's persistence path (``DownloadRequest`` row) doesn't need to fork.
    """
    backend_name = handle.get("backend")
    if not backend_name:
        return AttemptResult(
            backend="unknown", success=False, status="error", error="handle missing backend"
        )
    try:
        backend = BackendName(backend_name)
    except ValueError:
        return AttemptResult(
            backend=backend_name, success=False, status="error", error=f"unknown backend {backend_name}"
        )

    kind = (handle.get("kind") or "track").lower()

    if kind == "album":
        adapter = make_album_adapter(backend)
        if adapter is None:
            return AttemptResult(
                backend=backend_name, success=False, status="error",
                error=f"{backend_name} doesn't support album downloads via from-handle",
            )
        try:
            if not await adapter.is_configured():
                return AttemptResult(
                    backend=backend_name, success=False, status="skipped", error="not configured"
                )
            album_ref = AlbumRef(
                service=handle.get("service"),
                album_id=handle.get("album_id"),
                artist_name=handle.get("artist"),
                album_name=handle.get("title") or handle.get("album"),
                mb_release_group_id=handle.get("mb_release_group_id"),
            )
            album_attempt = await adapter.try_download_album(album_ref)
        finally:
            try:
                await adapter.close()
            except Exception:
                pass
        # Repackage AlbumAttempt → AttemptResult so the caller's DB write
        # treats album downloads identically to track downloads (one row,
        # one task, one watcher).
        task_id = album_attempt.extra.get("task_id") if album_attempt.extra else None
        return AttemptResult(
            backend=album_attempt.backend,
            success=album_attempt.success,
            status=album_attempt.status,
            task_id=task_id,
            error=album_attempt.error,
            quality=getattr(adapter, "expected_quality", None) if album_attempt.success else None,
            extra={**(album_attempt.extra or {}), "kind": "album", "album_id": handle.get("album_id")},
        )

    # kind == "track" (default)
    adapter = make_adapter(backend)
    if adapter is None:
        return AttemptResult(
            backend=backend_name, success=False, status="error", error="no adapter for backend"
        )
    try:
        if not await adapter.is_configured():
            return AttemptResult(
                backend=backend_name, success=False, status="skipped", error="not configured"
            )
        return await adapter.from_handle(handle)
    finally:
        try:
            await adapter.close()
        except Exception:
            pass
