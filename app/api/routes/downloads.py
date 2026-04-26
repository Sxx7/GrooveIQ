"""
GrooveIQ -- Downloads API routes.

Cascades a download attempt across the configured priority chain
(see ``app.services.download_routing``) and persists the request,
including a per-backend attempt log, in the DB.

Search/status endpoints still proxy a single backend (the legacy factory
in ``spotdl.py``) for backward compatibility; the multi-agent search lives
on a separate endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import hash_key, require_api_key
from app.db.session import get_session
from app.models.db import DownloadRequest
from app.models.download_routing_schema import BackendName
from app.models.schemas import (
    DownloadCreateRequest,
    DownloadFromHandleRequest,
    DownloadResponse,
    DownloadStatusResponse,
)
from app.services.download_chain import (
    NormalizedSearchResult,
    TrackRef,
    make_adapter,
    search_via_handle,
    try_download_chain,
)
from app.services.download_routing import get_routing
from app.services.spotdl import get_download_client

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_client():
    """Return the configured download client (spotdl-api, streamrip-api, or Spotizerr)."""
    client = get_download_client()
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="No download backend configured. Set SPOTDL_API_URL, STREAMRIP_API_URL, or SPOTIZERR_URL.",
        )
    return client


def _client_source_name(client) -> str:
    """Return the DB source name for the active download client."""
    from app.services.streamrip import StreamripClient

    if isinstance(client, StreamripClient):
        return "streamrip"
    # SpotizerrClient check
    cls_name = type(client).__name__
    if cls_name == "SpotizerrClient":
        return "spotizerr"
    return "spotdl"


def _require_download_backend() -> None:
    if not settings.download_enabled:
        raise HTTPException(
            status_code=503,
            detail="No download backend configured. Set SPOTDL_API_URL, STREAMRIP_API_URL, or SPOTIZERR_URL.",
        )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get(
    "/downloads/search",
    summary="Search for tracks via Spotizerr",
)
async def search_tracks(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(25, ge=1, le=100),
    _key: str = Depends(require_api_key),
):
    """Proxy Spotizerr track search.  Flattens raw Spotify objects into a
    simple format the mobile client can consume directly."""
    _require_download_backend()
    client = _get_client()
    try:
        raw = await client.search(q, limit=limit)
        results = [_flatten_track(t) for t in raw]
        return results
    finally:
        await client.close()


def _flatten_track(track: dict) -> dict:
    """Convert a raw Spotify track object into a flat search result."""
    artists = track.get("artists") or []
    artist_name = artists[0]["name"] if artists else ""

    album = track.get("album") or {}
    album_name = album.get("name")

    images = album.get("images") or []
    # Prefer the 300px image, fall back to first available.
    image_url = None
    for img in images:
        if img.get("width") == 300 or img.get("height") == 300:
            image_url = img["url"]
            break
    if not image_url and images:
        image_url = images[0]["url"]

    return {
        "spotify_id": track.get("id", ""),
        "title": track.get("name", ""),
        "artist": artist_name,
        "album": album_name,
        "type": track.get("type", "track"),
        "image_url": image_url,
    }


_LIBRARY_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_pair(artist: str | None, title: str | None) -> tuple[str, str]:
    """Normalize an (artist, title) pair for library cross-reference.

    Lowercases, strips non-alphanumeric, collapses to a single space.
    Returns ``("", "")`` if either side is empty after normalization.
    """
    a = _LIBRARY_NORM_RE.sub(" ", (artist or "").lower()).strip()
    t = _LIBRARY_NORM_RE.sub(" ", (title or "").lower()).strip()
    return a, t


def _file_format(path: str | None) -> str | None:
    """Return an upper-case format hint from a file path (FLAC/MP3/M4A/...)."""
    if not path:
        return None
    if "." not in path:
        return None
    ext = path.rsplit(".", 1)[-1].lower()
    return ext.upper() if 1 <= len(ext) <= 5 else None


async def _annotate_library_matches(session: AsyncSession, grouped: list[dict]) -> None:
    """Add an ``in_library`` flag (and library metadata when matched) to every
    result in the multi-search response.

    Strategy: collect the unique normalized (artist, title) pairs the search
    surfaced, then fetch only the matching rows from ``track_features`` using
    a SQL ``IN (...)`` clause keyed by lowercased artist. Refine by normalized
    title in Python to handle punctuation variations (apostrophes, dashes,
    parentheticals like "(Remastered)").
    """
    from app.models.db import TrackFeatures

    pairs_to_results: dict[tuple[str, str], list[dict]] = {}
    artists_lc: set[str] = set()
    for group in grouped:
        if not group.get("ok"):
            continue
        for r in group.get("results") or []:
            r["in_library"] = False  # default for every result
            norm = _norm_pair(r.get("artist"), r.get("title"))
            if not norm[0] or not norm[1]:
                continue
            pairs_to_results.setdefault(norm, []).append(r)
            # Track the lowercased original (not the punctuation-stripped form)
            # so the SQL `lower(artist) IN (...)` clause matches what's stored.
            if r.get("artist"):
                artists_lc.add(r["artist"].lower())

    if not pairs_to_results:
        return

    rows = (
        await session.execute(
            select(
                TrackFeatures.track_id,
                TrackFeatures.artist,
                TrackFeatures.title,
                TrackFeatures.album,
                TrackFeatures.file_path,
            ).where(func.lower(TrackFeatures.artist).in_(artists_lc))
        )
    ).all()

    library_lookup: dict[tuple[str, str], dict] = {}
    for track_id, artist, title, album, file_path in rows:
        norm = _norm_pair(artist, title)
        if norm in pairs_to_results and norm not in library_lookup:
            library_lookup[norm] = {
                "track_id": track_id,
                "album": album,
                "file_path": file_path,
            }

    for norm, results in pairs_to_results.items():
        info = library_lookup.get(norm)
        if not info:
            continue
        for r in results:
            r["in_library"] = True
            r["library_track_id"] = info["track_id"]
            r["library_album"] = info["album"]
            r["library_format"] = _file_format(info["file_path"])


@router.get(
    "/downloads/search/multi",
    summary="Parallel search across all configured backends",
)
async def search_tracks_multi(
    q: str = Query(..., min_length=1, description="Search query"),
    limit: int = Query(25, ge=1, le=100),
    backends: str | None = Query(
        None,
        description=(
            "Comma-separated list of backends to query. "
            "Defaults to the routing config's parallel_search_backends."
        ),
    ),
    timeout_ms: int | None = Query(
        None, ge=500, le=30000, description="Override per-backend timeout"
    ),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Run ``search`` against multiple download backends concurrently and
    return results grouped by backend.

    Each result carries a ``download_handle`` you can POST to
    ``/v1/downloads/from-handle`` to download that specific result.
    """
    import asyncio

    routing = get_routing()
    if backends:
        requested = []
        for raw in backends.split(","):
            raw = raw.strip().lower()
            try:
                requested.append(BackendName(raw))
            except ValueError:
                continue
    else:
        requested = list(routing.parallel_search_backends)

    timeout_s = (timeout_ms or routing.parallel_search_timeout_ms) / 1000.0

    async def _run(backend: BackendName) -> dict:
        adapter = make_adapter(backend)
        if adapter is None:
            return {"backend": backend.value, "ok": False, "error": "no adapter", "results": []}
        try:
            if not await adapter.is_configured():
                return {
                    "backend": backend.value,
                    "ok": False,
                    "error": "not configured",
                    "results": [],
                }
            try:
                results: list[NormalizedSearchResult] = await asyncio.wait_for(
                    adapter.search(q, limit=limit), timeout=timeout_s
                )
            except asyncio.TimeoutError:
                return {
                    "backend": backend.value,
                    "ok": False,
                    "error": f"timeout after {timeout_s:.1f}s",
                    "results": [],
                }
            return {
                "backend": backend.value,
                "ok": True,
                "results": [
                    {
                        "backend": r.backend,
                        "title": r.title,
                        "artist": r.artist,
                        "album": r.album,
                        "album_id": r.album_id,
                        "album_year": r.album_year,
                        "album_track_count": r.album_track_count,
                        "track_number": r.track_number,
                        "image_url": r.image_url,
                        "quality": r.quality.value if r.quality else None,
                        "bitrate_kbps": r.bitrate_kbps,
                        "duration_ms": r.duration_ms,
                        "download_handle": r.download_handle,
                        "extra": r.extra,
                    }
                    for r in results
                ],
            }
        finally:
            try:
                await adapter.close()
            except Exception:
                pass

    grouped = await asyncio.gather(*(_run(b) for b in requested), return_exceptions=False)

    # Cross-reference each result against the analyzed library so the UI can
    # show an "already in library" badge and skip needless re-downloads.
    await _annotate_library_matches(session, grouped)

    return {
        "query": q,
        "limit": limit,
        "timeout_ms": int(timeout_s * 1000),
        "groups": grouped,
    }


@router.get(
    "/downloads/search/artist",
    summary="Artist search — returns artists with their full album discography",
)
async def search_artist(
    q: str = Query(..., min_length=1, description="Artist name"),
    limit: int = Query(2, ge=1, le=10, description="How many artist matches per backend"),
    albums_per_artist: int = Query(100, ge=1, le=300, description="Cap primary albums per artist"),
    backend: str = Query("streamrip", description="Backend to query (only streamrip is supported today)"),
    _key: str = Depends(require_api_key),
):
    """Return top-N artist matches per backend. Each artist carries its full
    album list (id, title, year, cover, track_count) but **no tracks** — those
    lazy-load via ``GET /v1/downloads/album-tracks?service=…&album_id=…``.

    Currently only ``streamrip`` supports this mode. Other backends return an
    empty artists list. The response shape mirrors ``/search/multi`` so the
    dashboard can use one rendering pipeline.
    """
    backend = backend.lower()
    try:
        backend_enum = BackendName(backend)
    except ValueError:
        raise HTTPException(400, f"Unknown backend {backend!r}")

    if backend_enum is not BackendName.STREAMRIP:
        return {
            "query": q,
            "groups": [{
                "backend": backend, "ok": False,
                "error": f"{backend} doesn't support artist search yet",
                "artists": [],
            }],
        }

    from app.services.streamrip import StreamripClient

    if not settings.streamrip_enabled:
        return {
            "query": q,
            "groups": [{"backend": "streamrip", "ok": False, "error": "not configured", "artists": []}],
        }
    client = StreamripClient(settings.STREAMRIP_API_URL)
    try:
        result = await client.search_artist(q, limit=limit, albums_per_artist=albums_per_artist)
    finally:
        try:
            await client.close()
        except Exception:
            pass

    err = result.get("error")
    return {
        "query": q,
        "groups": [{
            "backend": "streamrip",
            "ok": err is None,
            "error": err,
            "artists": result.get("artists") or [],
        }],
    }


@router.get(
    "/downloads/album-tracks",
    summary="Lazy-load tracks for a specific album",
)
async def get_album_tracks(
    service: str = Query(..., description="qobuz, tidal, deezer, soundcloud"),
    album_id: str = Query(..., description="Service-native album ID"),
    backend: str = Query("streamrip", description="Backend (only streamrip today)"),
    _key: str = Depends(require_api_key),
):
    """Used by the artist-search UI to populate an album's track list when the
    user clicks 'Show tracks'. Returns the same fields as the streamrip
    track search results so the existing track-row renderer works unchanged.
    """
    if backend.lower() != "streamrip":
        raise HTTPException(400, f"backend {backend!r} doesn't support album-tracks lookup")
    if not settings.streamrip_enabled:
        raise HTTPException(503, "streamrip not configured")

    from app.services.streamrip import StreamripClient

    client = StreamripClient(settings.STREAMRIP_API_URL)
    try:
        return await client.get_album_tracks(service, album_id)
    finally:
        try:
            await client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@router.post(
    "/downloads",
    summary="Download a track via the configured backend cascade",
    response_model=DownloadResponse,
)
async def create_download(
    body: DownloadCreateRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Trigger download of a specific Spotify track ID.

    Walks the active *individual* priority chain — first backend to
    succeed wins. The full attempt log is persisted on the request record.
    """
    _require_download_backend()

    track_ref = TrackRef(
        spotify_id=body.spotify_id,
        artist=body.artist_name,
        title=body.track_title,
        album=body.album_name,
    )
    cascade = await try_download_chain(track_ref, purpose="individual")
    record = await _persist_cascade_request(
        session=session,
        cascade=cascade,
        spotify_id=body.spotify_id,
        track_title=body.track_title,
        artist_name=body.artist_name,
        album_name=body.album_name,
        cover_url=body.cover_url,
        api_key=_key,
    )

    if not cascade.success:
        # Persisted as an error record so telemetry sees the failure;
        # surface it via 502 with the last attempted backend's error.
        last = cascade.attempts[-1] if cascade.attempts else None
        detail = (last.error if last else None) or "all download backends failed"
        raise HTTPException(status_code=502, detail=detail)

    await _spawn_watcher_for(record, cascade)
    return DownloadResponse.model_validate(record)


@router.post(
    "/downloads/from-handle",
    summary="Download a specific result returned by GET /v1/downloads/search/multi",
    response_model=DownloadResponse,
)
async def create_download_from_handle(
    body: DownloadFromHandleRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Bypass the cascade — the user already chose a specific search result."""
    _require_download_backend()

    backend_name = body.handle.get("backend") or "unknown"
    attempt = await search_via_handle(body.handle)

    # Build a synthetic single-attempt cascade so persistence + watcher
    # branching share the create_download path.
    from app.services.download_chain import CascadeResult

    cascade = CascadeResult(
        success=attempt.success,
        attempts=[attempt],
        final_backend=backend_name if attempt.success else None,
        final_task_id=attempt.task_id if attempt.success else None,
        final_status=attempt.status,
        final_extra=attempt.extra,
    )

    record = await _persist_cascade_request(
        session=session,
        cascade=cascade,
        spotify_id=body.handle.get("spotify_id") or None,
        track_title=body.track_title,
        artist_name=body.artist_name,
        album_name=body.album_name,
        cover_url=body.cover_url,
        api_key=_key,
    )

    if not cascade.success:
        raise HTTPException(status_code=502, detail=attempt.error or f"{backend_name} download failed")

    await _spawn_watcher_for(record, cascade)
    return DownloadResponse.model_validate(record)


# ---------------------------------------------------------------------------
# Cascade persistence + watcher dispatch helpers
# ---------------------------------------------------------------------------


async def _persist_cascade_request(
    *,
    session: AsyncSession,
    cascade,
    spotify_id: str | None,
    track_title: str | None,
    artist_name: str | None,
    album_name: str | None,
    cover_url: str | None,
    api_key: str,
) -> DownloadRequest:
    """Write a DownloadRequest row from the cascade outcome."""
    last = cascade.attempts[-1] if cascade.attempts else None
    source = cascade.final_backend or (last.backend if last else "none")
    status = cascade.final_status if cascade.success else (last.status if last else "error")
    err_msg: str | None = None
    if not cascade.success:
        err_msg = (last.error if last else None) or "no backend succeeded"

    slskd_username = None
    slskd_filename = None
    slskd_transfer_id = None
    if cascade.success and cascade.final_backend == BackendName.SLSKD.value:
        slskd_username = cascade.final_extra.get("username")
        slskd_filename = cascade.final_extra.get("filename")
        slskd_transfer_id = cascade.final_task_id

    record = DownloadRequest(
        spotify_id=spotify_id,
        task_id=cascade.final_task_id,
        status=status,
        source=source,
        track_title=track_title,
        artist_name=artist_name,
        album_name=album_name,
        cover_url=cover_url,
        slskd_username=slskd_username,
        slskd_filename=slskd_filename,
        slskd_transfer_id=slskd_transfer_id,
        attempts=[a.to_dict() for a in cascade.attempts] or None,
        requested_by=hash_key(api_key)[:16] if api_key != "anonymous" else None,
        error_message=err_msg,
        updated_at=int(time.time()),
    )
    session.add(record)
    await session.flush()
    return record


async def _spawn_watcher_for(record: DownloadRequest, cascade) -> None:
    """Pick the right watcher based on which backend served the download.

    spotdl/streamrip/spotizerr → ``download_watcher.start_watcher(task_id)``
    slskd                       → ``slskd_watcher.start_watcher(record.id)``
    """
    if cascade.final_backend == BackendName.SLSKD.value:
        from app.services.slskd_watcher import start_watcher as start_slskd_watcher

        await start_slskd_watcher(record.id)
        return

    if record.task_id and record.status not in ("error", "unknown"):
        from app.services.download_watcher import start_watcher

        await start_watcher(record.task_id, source=cascade.final_backend)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@router.get(
    "/downloads/status/{task_id}",
    summary="Check download progress",
    response_model=DownloadStatusResponse,
)
async def get_download_status(
    task_id: str,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Proxy Spotizerr task progress.  Also updates the DB record if found."""
    _require_download_backend()
    client = _get_client()
    try:
        status_data = await client.get_status(task_id)
    finally:
        await client.close()

    # Opportunistically update DB record.
    result = await session.execute(select(DownloadRequest).where(DownloadRequest.task_id == task_id))
    record = result.scalar_one_or_none()

    # Flattened shape from SpotizerrClient.get_status():
    # {"status", "progress", "error", "raw"}
    spotizerr_status = status_data.get("status", "unknown")
    # Map Spotizerr terminal states onto our DB statuses so the history
    # table shows "completed" / "error" rather than Spotizerr's internal
    # enum strings.
    db_status = spotizerr_status
    if spotizerr_status in ("complete", "done"):
        db_status = "completed"
    if record and db_status != record.status:
        record.status = db_status
        record.updated_at = int(time.time())
        if status_data.get("error"):
            record.error_message = str(status_data["error"])[:1024]

    return DownloadStatusResponse(
        task_id=task_id,
        status=spotizerr_status,
        progress=status_data.get("progress"),
        details=status_data.get("raw") or status_data,
    )


# ---------------------------------------------------------------------------
# Live queue (in-flight + recently finished)
# ---------------------------------------------------------------------------


# Initial status set by ``_persist_cascade_request`` is whatever the cascade
# returned — for streamrip-api / spotdl-api / spotizerr that's "queued"; for
# the slskd path it can be "queued" too. The watcher only writes the terminal
# status (completed/duplicate/error), so a row sits at "queued" for the
# entirety of its in-flight lifetime. Including "pending"/"downloading" too
# in case a future watcher revision starts emitting them.
_IN_FLIGHT_STATUSES = ("queued", "pending", "downloading")
_TERMINAL_SUCCESS_DB = ("completed", "duplicate")
_TERMINAL_FAILED_DB = ("error", "failed", "stalled", "cancelled")  # noqa: F841 — kept in sync with _TERMINAL_FAILURE_STATUSES below

# Cache live backend status probes briefly so the queue panel can poll at 3s
# without thrashing the upstream APIs when there are many in-flight rows.
_PROGRESS_CACHE: dict[str, tuple[float, dict]] = {}
_PROGRESS_CACHE_TTL_S = 2.0
_PROGRESS_FETCH_TIMEOUT_S = 1.5


async def _probe_live_progress(task_id: str, source: str | None) -> dict | None:
    """Pull a single task's live progress from its backend, with a TTL cache.

    Returns ``{"status", "progress", "error"}`` (progress is 0.0–1.0 or None)
    or ``None`` if the backend isn't probeable (e.g. slskd via this client
    factory, or backend unconfigured / unreachable / timing out).
    """
    if not task_id or not source:
        return None
    now = time.monotonic()
    cached = _PROGRESS_CACHE.get(task_id)
    if cached and now - cached[0] < _PROGRESS_CACHE_TTL_S:
        return cached[1]

    # Reuse the same client-construction logic as the watcher so adding new
    # backends is a one-place change.
    from app.services.download_watcher import _client_for_source

    client = _client_for_source(source)
    if client is None or not hasattr(client, "get_status"):
        return None
    try:
        result = await asyncio.wait_for(
            client.get_status(task_id), timeout=_PROGRESS_FETCH_TIMEOUT_S
        )
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
        logger.debug("Queue probe failed for %s/%s: %s", source, task_id, exc)
        result = None
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass

    if result is None:
        return None
    # Normalise progress: backends report it as 0.0–1.0 (spotdl, streamrip)
    # or 0–100 (some Spotizerr endpoints). Coerce to [0, 1].
    progress = result.get("progress")
    if isinstance(progress, (int, float)):
        if progress > 1.0:
            progress = max(0.0, min(1.0, float(progress) / 100.0))
        else:
            progress = max(0.0, min(1.0, float(progress)))
    else:
        progress = None
    normalised = {
        "status": result.get("status"),
        "progress": progress,
        "error": result.get("error"),
    }
    _PROGRESS_CACHE[task_id] = (now, normalised)
    return normalised


def _serialize_queue_row(
    record: DownloadRequest, live: dict | None
) -> dict:
    """Convert a DB row + optional live probe into a queue-panel JSON row."""
    now = int(time.time())
    return {
        "id": record.id,
        "task_id": record.task_id,
        "source": record.source,
        "status": record.status,
        "live_status": (live or {}).get("status"),
        "progress": (live or {}).get("progress"),
        "error_message": record.error_message,
        "track_title": record.track_title,
        "artist_name": record.artist_name,
        "album_name": record.album_name,
        "cover_url": record.cover_url,
        "spotify_id": record.spotify_id,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "elapsed_s": max(0, now - (record.created_at or now)),
        "attempts": record.attempts or [],
    }


@router.get(
    "/downloads/queue",
    summary="Live snapshot of in-flight + recently finished downloads",
)
async def get_download_queue(
    recent_limit: int = Query(10, ge=0, le=50),
    in_flight_limit: int = Query(50, ge=1, le=200),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns three buckets: ``in_flight`` (status pending/downloading),
    ``recent_completed`` (last N successes/duplicates), and ``recent_failed``
    (last N errors). Each in-flight row is augmented with a live probe of the
    backend's status endpoint when one is available, so percentage progress
    surfaces for spotdl / spotizerr (and for streamrip when a future revision
    of streamrip-api starts emitting it).

    Slskd transfers don't currently come back through this probe path — they
    show DB-level state only. Probes are cached for 2s so dashboard polling
    doesn't hammer the upstream backends.
    """
    in_flight_rows = (
        await session.execute(
            select(DownloadRequest)
            .where(DownloadRequest.status.in_(_IN_FLIGHT_STATUSES))
            .order_by(DownloadRequest.created_at.asc())
            .limit(in_flight_limit)
        )
    ).scalars().all()

    completed_rows = (
        await session.execute(
            select(DownloadRequest)
            .where(DownloadRequest.status.in_(_TERMINAL_SUCCESS_DB))
            .order_by(DownloadRequest.updated_at.desc())
            .limit(recent_limit)
        )
    ).scalars().all() if recent_limit else []

    failed_rows = (
        await session.execute(
            select(DownloadRequest)
            .where(DownloadRequest.status.in_(_TERMINAL_FAILED_DB))
            .order_by(DownloadRequest.updated_at.desc())
            .limit(recent_limit)
        )
    ).scalars().all() if recent_limit else []

    # Probe in-flight rows in parallel; each call is timeout-capped + cached.
    probes = await asyncio.gather(
        *[_probe_live_progress(r.task_id, r.source) for r in in_flight_rows],
        return_exceptions=True,
    )
    live_by_id: dict[str, dict | None] = {}
    for row, probe in zip(in_flight_rows, probes):
        live_by_id[row.task_id] = probe if isinstance(probe, dict) else None

    # Active-watcher count is a better "is anything actually running" signal
    # than the DB row count (the DB lags the watcher by a poll cycle).
    from app.services.download_watcher import active_watcher_count

    return {
        "now": int(time.time()),
        "active_watchers": active_watcher_count(),
        "in_flight": [
            _serialize_queue_row(r, live_by_id.get(r.task_id)) for r in in_flight_rows
        ],
        "recent_completed": [_serialize_queue_row(r, None) for r in completed_rows],
        "recent_failed": [_serialize_queue_row(r, None) for r in failed_rows],
    }


# ---------------------------------------------------------------------------
# Telemetry — per-backend success rate
# ---------------------------------------------------------------------------


_TERMINAL_SUCCESS_STATUSES = ("completed", "complete", "duplicate")
_TERMINAL_FAILURE_STATUSES = ("error", "failed", "stalled", "cancelled")


@router.get(
    "/downloads/stats",
    summary="Per-backend download success rates",
)
async def download_stats(
    days: int = Query(30, ge=1, le=365, description="Look-back window in days"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Aggregate DownloadRequest history into per-backend success/failure
    counts. Drives the routing-policy GUI's reliability panel and informs
    priority-tweaking decisions.
    """
    cutoff = int(time.time()) - days * 86400
    rows = (
        await session.execute(
            select(
                DownloadRequest.source,
                DownloadRequest.status,
                func.count().label("n"),
            )
            .where(DownloadRequest.created_at >= cutoff)
            .group_by(DownloadRequest.source, DownloadRequest.status)
        )
    ).all()

    by_backend: dict[str, dict[str, int]] = {}
    for source, status, n in rows:
        bucket = by_backend.setdefault(source or "unknown", {"success": 0, "failure": 0, "in_flight": 0, "total": 0})
        bucket["total"] += int(n)
        if status in _TERMINAL_SUCCESS_STATUSES:
            bucket["success"] += int(n)
        elif status in _TERMINAL_FAILURE_STATUSES:
            bucket["failure"] += int(n)
        else:
            bucket["in_flight"] += int(n)

    backends = []
    for name, counts in sorted(by_backend.items()):
        terminal = counts["success"] + counts["failure"]
        success_rate = (counts["success"] / terminal) if terminal else None
        backends.append(
            {
                "backend": name,
                "total": counts["total"],
                "success": counts["success"],
                "failure": counts["failure"],
                "in_flight": counts["in_flight"],
                "success_rate": success_rate,
            }
        )

    return {
        "window_days": days,
        "since_unix": cutoff,
        "backends": backends,
    }


# ---------------------------------------------------------------------------
# Manual cancel
# ---------------------------------------------------------------------------


@router.delete(
    "/downloads/{download_id}",
    summary="Cancel / dismiss a download row",
)
async def cancel_download(
    download_id: int,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Manual escape hatch for stuck rows.

    The cascade backends don't all expose a real "cancel this task" RPC, so
    we don't try to abort the upstream rip — we just mark the DB row as
    ``cancelled`` and let any active watcher exit naturally on its next
    terminal poll. The ``_mark_download_done`` writer respects an existing
    ``cancelled`` status so it won't be silently overwritten back to
    completed/error if the upstream finishes after the user dismissed it.
    """
    record = (
        await session.execute(
            select(DownloadRequest).where(DownloadRequest.id == download_id)
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail=f"Download {download_id} not found")
    if record.status in _TERMINAL_SUCCESS_STATUSES + _TERMINAL_FAILURE_STATUSES:
        # Already terminal — return current state without touching it.
        return {"id": record.id, "status": record.status, "message": "already terminal"}
    record.status = "cancelled"
    record.error_message = "Cancelled by user"
    record.updated_at = int(time.time())
    await session.commit()
    return {"id": record.id, "status": record.status, "message": "cancelled"}


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


@router.get(
    "/downloads",
    summary="List download history",
)
async def list_downloads(
    status: str | None = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    """Returns persisted download requests, newest first."""
    q = select(DownloadRequest).order_by(DownloadRequest.created_at.desc())
    if status:
        q = q.where(DownloadRequest.status == status)

    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0

    records = (await session.execute(q.offset(offset).limit(limit))).scalars().all()

    return {
        "total": total,
        "downloads": [DownloadResponse.model_validate(r) for r in records],
    }
