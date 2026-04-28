"""
GrooveIQ – Lidarr backfill engine.

Drains Lidarr's ``/wanted/missing`` (and optionally ``/wanted/cutoff``) queue
by routing each missing album through the streamrip-api download pipeline.
Throughput is rate-limited per hour via a sliding-window query against
``LidarrBackfillRequest`` rows so multi-day backfills spread cleanly without
tripping streaming-service rate limits.

Two scheduler jobs drive the engine:

* ``run_backfill_tick``    — picks the next batch and kicks off downloads
* ``poll_in_flight``       — promotes ``downloading`` rows to ``complete`` /
                             ``failed`` based on streamrip task status, and
                             triggers Lidarr's import scan on success.

The whole engine respects the ``bulk_album`` download-routing gate: if
Lidarr is disabled in routing, the tick is a no-op (mirrors the discovery /
fill_library pattern).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import LidarrBackfillRequest
from app.models.download_routing_schema import QualityTier, quality_meets
from app.models.lidarr_backfill_schema import LidarrBackfillConfigData, QueueOrder, get_defaults
from app.services.discovery import LidarrClient
from app.services.lidarr_backfill_config import get_config
from app.services.streamrip import StreamripClient


# Lidarr sort_key + sort_direction for each user-facing queue order. Random
# uses a stable base sort and gets shuffled in Python after fetch.
_QUEUE_SORT: dict[QueueOrder, tuple[str, str]] = {
    QueueOrder.RECENT_RELEASE: ("albums.releaseDate", "descending"),
    QueueOrder.OLDEST_RELEASE: ("albums.releaseDate", "ascending"),
    QueueOrder.ALPHABETICAL: ("albums.title", "ascending"),
    QueueOrder.RANDOM: ("albums.releaseDate", "descending"),
}

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_NO_MATCH = "no_match"
STATUS_PERMANENTLY_SKIPPED = "permanently_skipped"

_TERMINAL_STATUSES = {STATUS_COMPLETE, STATUS_PERMANENTLY_SKIPPED}
_IN_FLIGHT_STATUSES = {STATUS_QUEUED, STATUS_DOWNLOADING}


# Streamrip's declared quality per service (best case — actual track quality
# can be lower depending on the album's source). Mirrors the values used in
# ``app/models/download_routing_schema.py::DEFAULT_BACKEND_QUALITY``.
_SERVICE_QUALITY: dict[str, QualityTier] = {
    "qobuz": QualityTier.HIRES,
    "tidal": QualityTier.HIRES,
    "deezer": QualityTier.LOSSLESS,
    "soundcloud": QualityTier.LOSSY_HIGH,
}


# ---------------------------------------------------------------------------
# Tick-in-progress flag
# ---------------------------------------------------------------------------
#
# Surfaced via the /stats endpoint so the dashboard can show a live "running
# tick" badge — gives operators clear feedback that work is happening, even
# for scheduled ticks they didn't trigger themselves.

_tick_lock = threading.Lock()
_tick_in_progress = False
_tick_started_at: int | None = None


def is_tick_in_progress() -> bool:
    """Whether ``run_backfill_tick`` is currently doing work (not just spinning early-skips)."""
    with _tick_lock:
        return _tick_in_progress


def get_tick_started_at() -> int | None:
    """Unix timestamp at which the in-flight tick started, or None if no tick is running."""
    with _tick_lock:
        return _tick_started_at


@contextlib.asynccontextmanager
async def _mark_tick_running():
    """Context manager that flips ``_tick_in_progress`` on entry and clears it on exit
    (even if the body raises). Wrapped around the meaningful work in run_backfill_tick.
    """
    global _tick_in_progress, _tick_started_at
    with _tick_lock:
        _tick_in_progress = True
        _tick_started_at = _now_ts()
    try:
        yield
    finally:
        with _tick_lock:
            _tick_in_progress = False
            _tick_started_at = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ts() -> int:
    return int(time.time())


def _fuzzy_ratio(a: str, b: str) -> float:
    """Return a 0–1 similarity ratio. Stdlib SequenceMatcher avoids new deps."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.casefold().strip(), b.casefold().strip()).ratio()


def _extract_year(release_date: str | None) -> int | None:
    if not release_date or len(release_date) < 4:
        return None
    head = release_date[:4]
    return int(head) if head.isdigit() else None


def _normalize_artist_name(name: str) -> str:
    n = (name or "").strip().casefold()
    if n.startswith("the "):
        n = n[4:]
    return n


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------


@dataclass
class MatchScore:
    score: float
    accepted: bool
    reasons: list[str]
    artist_similarity: float
    album_similarity: float
    year_diff: int | None
    track_count_diff: int | None


def _score_match(
    lidarr_album: dict[str, Any],
    streamrip_album: dict[str, Any],
    cfg: LidarrBackfillConfigData,
) -> MatchScore:
    """Score a streamrip album hit against the Lidarr target.

    Components:
        artist similarity   weight 0.50  (hard reject below threshold)
        album-title sim     weight 0.40  (hard reject below threshold,
                                          unless structural fallback applies)
        track-count match   weight 0.05  (binary 0/1)
        year match          weight 0.05  (binary 0/1; |diff| ≤ 1 counts)

    When ``cfg.match.allow_structural_fallback`` is True, an album-title
    similarity below the threshold is forgiven if the artist matches
    exactly (≥ 0.95) AND track count matches AND year diff ≤ 1 — catches
    multi-disc / re-issue / localized-title editions where the printable
    text diverges but the structural metadata aligns.
    """
    target_artist = (lidarr_album.get("artist") or {}).get("artistName") or ""
    target_title = lidarr_album.get("title") or ""
    target_year = _extract_year(lidarr_album.get("releaseDate"))
    target_track_count = lidarr_album.get("trackCount")
    if not isinstance(target_track_count, int):
        target_track_count = None

    sr_artist = streamrip_album.get("artist") or ""
    sr_title = streamrip_album.get("album") or ""
    sr_year = streamrip_album.get("album_year")
    if not isinstance(sr_year, int):
        sr_year = None
    sr_track_count = streamrip_album.get("album_track_count")
    if not isinstance(sr_track_count, int):
        sr_track_count = None

    artist_sim = _fuzzy_ratio(target_artist, sr_artist)
    album_sim = _fuzzy_ratio(target_title, sr_title)

    year_diff: int | None = None
    if target_year is not None and sr_year is not None:
        year_diff = abs(target_year - sr_year)

    track_count_diff: int | None = None
    if target_track_count is not None and sr_track_count is not None:
        track_count_diff = abs(target_track_count - sr_track_count)

    track_count_ok = track_count_diff == 0 if track_count_diff is not None else False
    year_ok = (year_diff is not None and year_diff <= 1)

    score = 0.50 * artist_sim + 0.40 * album_sim + 0.05 * (1.0 if track_count_ok else 0.0) + 0.05 * (
        1.0 if year_ok else 0.0
    )

    reasons: list[str] = []
    accepted = True

    if artist_sim < cfg.match.min_artist_similarity:
        accepted = False
        reasons.append(f"artist_similarity={artist_sim:.2f}<{cfg.match.min_artist_similarity}")
    if album_sim < cfg.match.min_album_similarity:
        # Optional structural fallback: forgive a low album-title similarity
        # when artist + track count + year all align tightly.
        artist_exact = artist_sim >= 0.95
        track_count_exact = track_count_diff is not None and track_count_diff == 0
        year_close = year_diff is not None and year_diff <= 1
        if (
            cfg.match.allow_structural_fallback
            and artist_exact
            and track_count_exact
            and year_close
        ):
            reasons.append(
                f"album_similarity={album_sim:.2f}<{cfg.match.min_album_similarity}_structural_ok"
            )
        else:
            accepted = False
            reasons.append(f"album_similarity={album_sim:.2f}<{cfg.match.min_album_similarity}")
    if cfg.match.require_year_match:
        if year_diff is None:
            accepted = False
            reasons.append("year_unknown_but_required")
        elif year_diff > 1:
            accepted = False
            reasons.append(f"year_diff={year_diff}>1")
    if cfg.match.require_track_count_match:
        if track_count_diff is None:
            accepted = False
            reasons.append("track_count_unknown_but_required")
        elif track_count_diff != 0:
            accepted = False
            reasons.append(f"track_count_diff={track_count_diff}")

    if accepted:
        reasons.append("accepted")

    return MatchScore(
        score=score,
        accepted=accepted,
        reasons=reasons,
        artist_similarity=artist_sim,
        album_similarity=album_sim,
        year_diff=year_diff,
        track_count_diff=track_count_diff,
    )


# ---------------------------------------------------------------------------
# Streamrip lookup
# ---------------------------------------------------------------------------


@dataclass
class StreamripMatch:
    service: str
    album_id: str
    album_artist: str
    album_title: str
    score: MatchScore
    track_count: int | None
    year: int | None


def _group_tracks_by_album(track_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Collapse the track-shaped streamrip /search response into one entry per album."""
    by_album: dict[str, dict[str, Any]] = {}
    for t in track_results:
        album_id = t.get("_album_id")
        if not album_id:
            continue
        if album_id not in by_album:
            artists = t.get("artists") or []
            artist_name = ""
            if artists and isinstance(artists[0], dict):
                artist_name = artists[0].get("name") or ""
            by_album[album_id] = {
                "album_id": album_id,
                "service": t.get("_service") or "",
                "artist": artist_name,
                "album": (t.get("album") or {}).get("name") or "",
                "album_year": t.get("_album_year"),
                "album_track_count": t.get("_album_track_count"),
                "_tracks_seen": 1,
            }
        else:
            by_album[album_id]["_tracks_seen"] += 1
    return by_album


async def _find_streamrip_album(
    streamrip_client: StreamripClient,
    lidarr_album: dict[str, Any],
    cfg: LidarrBackfillConfigData,
    *,
    available_services: list[str] | None = None,
    search_limit: int = 25,
) -> StreamripMatch | None:
    """Walk ``cfg.service_priority`` and return the first acceptable album hit.

    ``available_services`` (when not None) restricts the walk to services
    that streamrip-api is actually configured for — avoids the noisy 503
    chain when only a subset of services are wired up. ``None`` means
    "don't filter" (current behaviour, preserved for callers that don't
    want to probe ``/health``).
    """
    target_artist = (lidarr_album.get("artist") or {}).get("artistName") or ""
    target_title = lidarr_album.get("title") or ""
    if not target_artist or not target_title:
        return None

    query = f"{target_artist} {target_title}".strip()

    for service in cfg.service_priority:
        # Skip services not configured on streamrip-api (silent — keeps logs clean).
        if available_services is not None and service not in available_services:
            continue
        # Quality floor: skip the whole service if its declared best-case quality
        # is below the configured floor.
        declared = _SERVICE_QUALITY.get(service)
        if declared is not None and not quality_meets(declared, cfg.min_quality_floor):
            continue

        try:
            results = await streamrip_client.search(query, limit=search_limit, service=service)
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: streamrip search failed (%s): %s", service, exc)
            continue
        if not results:
            continue

        grouped = _group_tracks_by_album(results)
        if not grouped:
            continue

        scored: list[tuple[float, StreamripMatch]] = []
        for entry in grouped.values():
            score = _score_match(lidarr_album, entry, cfg)
            if not score.accepted:
                continue
            match = StreamripMatch(
                service=entry.get("service") or service,
                album_id=str(entry["album_id"]),
                album_artist=entry.get("artist") or "",
                album_title=entry.get("album") or "",
                score=score,
                track_count=entry.get("album_track_count")
                if isinstance(entry.get("album_track_count"), int)
                else None,
                year=entry.get("album_year") if isinstance(entry.get("album_year"), int) else None,
            )
            scored.append((score.score, match))

        if not scored:
            continue

        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    return None


# ---------------------------------------------------------------------------
# Capacity / candidate pickers
# ---------------------------------------------------------------------------


async def _compute_capacity(session: AsyncSession, cfg: LidarrBackfillConfigData) -> int:
    """How many more downloads can we start in the current sliding hour."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    cutoff_ts = int(cutoff.timestamp())
    in_window = await session.scalar(
        select(func.count())
        .select_from(LidarrBackfillRequest)
        .where(LidarrBackfillRequest.created_at > cutoff_ts)
    )
    in_window = in_window or 0
    capacity = cfg.max_downloads_per_hour - in_window
    return max(0, capacity)


async def _filter_by_cooldown_and_state(
    session: AsyncSession,
    candidates: list[dict[str, Any]],
    cfg: LidarrBackfillConfigData,
) -> list[dict[str, Any]]:
    """Drop albums already queued/in-progress/permanently-skipped/complete,
    plus any failed or no_match rows still inside their cooldown window or
    past max_attempts."""
    if not candidates:
        return []
    ids = [c.get("id") for c in candidates if c.get("id")]
    if not ids:
        return []
    rows = await session.execute(
        select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id.in_(ids))
    )
    state_by_id: dict[int, LidarrBackfillRequest] = {r.lidarr_album_id: r for r in rows.scalars().all()}
    now = _now_ts()
    out: list[dict[str, Any]] = []
    for c in candidates:
        cid = c.get("id")
        if not cid:
            continue
        existing = state_by_id.get(cid)
        if existing is None:
            out.append(c)
            continue
        if existing.status in _IN_FLIGHT_STATUSES:
            continue
        if existing.status == STATUS_PERMANENTLY_SKIPPED:
            continue
        if existing.status == STATUS_COMPLETE:
            continue
        # failed / no_match / skipped — honour cooldown / max_attempts.
        if existing.attempt_count >= cfg.retry.max_attempts:
            continue
        if existing.next_retry_at is not None and existing.next_retry_at > now:
            continue
        out.append(c)
    return out


async def _fetch_candidates(
    cfg: LidarrBackfillConfigData,
    lidarr_client: LidarrClient,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Pull missing + cutoff-unmet rows from Lidarr, apply allow/denylists.

    The Lidarr sort key is selected from ``cfg.sources.queue_order``. For
    ``random`` we use the recent-release sort as the base fetch order and
    then shuffle in Python — Lidarr's API doesn't support random sort.
    """
    sort_key, sort_direction = _QUEUE_SORT.get(
        cfg.sources.queue_order, _QUEUE_SORT[QueueOrder.RECENT_RELEASE]
    )

    candidates: list[dict[str, Any]] = []
    if cfg.sources.missing:
        try:
            rows = await lidarr_client.get_missing_albums(
                monitored=cfg.sources.monitored_only,
                sort_key=sort_key,
                sort_direction=sort_direction,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: fetching /wanted/missing failed: %s", exc)
            rows = []
        for r in rows:
            r = dict(r)
            r["_source"] = "missing"
            candidates.append(r)
    if cfg.sources.cutoff_unmet:
        try:
            rows = await lidarr_client.get_cutoff_unmet_albums(
                monitored=cfg.sources.monitored_only,
                sort_key=sort_key,
                sort_direction=sort_direction,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("backfill: fetching /wanted/cutoff failed: %s", exc)
            rows = []
        for r in rows:
            r = dict(r)
            r["_source"] = "cutoff"
            candidates.append(r)

    allow = {_normalize_artist_name(a) for a in cfg.filters.artist_allowlist if a.strip()}
    deny = {_normalize_artist_name(a) for a in cfg.filters.artist_denylist if a.strip()}

    filtered: list[dict[str, Any]] = []
    for c in candidates:
        artist_name = (c.get("artist") or {}).get("artistName") or ""
        norm = _normalize_artist_name(artist_name)
        if allow and norm not in allow:
            continue
        if deny and norm in deny:
            continue
        filtered.append(c)

    if cfg.sources.queue_order == QueueOrder.RANDOM:
        import random

        random.shuffle(filtered)

    return filtered[: max(1, limit)]


# ---------------------------------------------------------------------------
# Per-album processing
# ---------------------------------------------------------------------------


async def _persist_request(
    session: AsyncSession,
    lidarr_album: dict[str, Any],
    *,
    status: str,
    match: StreamripMatch | None,
    streamrip_task_id: str | None,
    error: str | None,
    cfg: LidarrBackfillConfigData,
    bump_attempt: bool = False,
) -> LidarrBackfillRequest:
    """Insert a new row, or update an existing one if we're retrying."""
    artist_name = (lidarr_album.get("artist") or {}).get("artistName") or ""
    album_title = lidarr_album.get("title") or ""
    mb_album_id = lidarr_album.get("foreignAlbumId")
    lidarr_album_id = lidarr_album["id"]
    source = lidarr_album.get("_source") or "missing"
    now = _now_ts()

    existing = (
        await session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.lidarr_album_id == lidarr_album_id)
        )
    ).scalar_one_or_none()

    if existing is None:
        attempt_count = 1 if bump_attempt else 0
        next_retry_at: int | None = None
        # Both `failed` (download error) and `no_match` (catalog gap) get the
        # same cooldown curve so retries spread over days, not minutes — the
        # success rate of immediate back-to-back retries is near zero anyway.
        if status in (STATUS_FAILED, STATUS_NO_MATCH) and bump_attempt:
            next_retry_at = _next_retry_timestamp(attempt_count, cfg)
        row = LidarrBackfillRequest(
            lidarr_album_id=lidarr_album_id,
            mb_album_id=mb_album_id,
            artist=artist_name,
            album_title=album_title,
            source=source,
            match_score=match.score.score if match else None,
            picked_service=match.service if match else None,
            picked_album_id=match.album_id if match else None,
            streamrip_task_id=streamrip_task_id,
            status=status,
            attempt_count=attempt_count,
            last_attempt_at=now if bump_attempt else None,
            next_retry_at=next_retry_at,
            last_error=error,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    existing.artist = artist_name
    existing.album_title = album_title
    existing.mb_album_id = mb_album_id
    existing.source = source
    if match is not None:
        existing.match_score = match.score.score
        existing.picked_service = match.service
        existing.picked_album_id = match.album_id
    if streamrip_task_id is not None:
        existing.streamrip_task_id = streamrip_task_id
    existing.status = status
    if bump_attempt:
        existing.attempt_count += 1
        existing.last_attempt_at = now
        if status in (STATUS_FAILED, STATUS_NO_MATCH):
            existing.next_retry_at = _next_retry_timestamp(existing.attempt_count, cfg)
        elif status in _TERMINAL_STATUSES or status in (STATUS_DOWNLOADING, STATUS_QUEUED):
            existing.next_retry_at = None
    existing.last_error = error
    existing.updated_at = now
    return existing


def _next_retry_timestamp(attempt_count: int, cfg: LidarrBackfillConfigData) -> int:
    """cooldown × multiplier^(attempt_count - 1) hours from now (capped 30 days)."""
    if attempt_count < 1:
        attempt_count = 1
    base_hours = cfg.retry.cooldown_hours
    grown = base_hours * (cfg.retry.backoff_multiplier ** (attempt_count - 1))
    grown = min(grown, 24.0 * 30)  # cap
    return _now_ts() + int(grown * 3600)


async def _process_album(
    session: AsyncSession,
    lidarr_album: dict[str, Any],
    cfg: LidarrBackfillConfigData,
    streamrip_client: StreamripClient,
    *,
    available_services: list[str] | None = None,
) -> dict[str, Any]:
    """Match + (download or skip) a single album. Persists state.

    Returns a metrics dict for the tick summary.
    """
    artist = (lidarr_album.get("artist") or {}).get("artistName") or "?"
    title = lidarr_album.get("title") or "?"
    decision: dict[str, Any] = {
        "lidarr_album_id": lidarr_album.get("id"),
        "artist": artist,
        "album": title,
        "decision": "pending",
    }

    match = await _find_streamrip_album(
        streamrip_client, lidarr_album, cfg,
        available_services=available_services,
    )
    if match is None:
        await _persist_request(
            session,
            lidarr_album,
            status=STATUS_NO_MATCH,
            match=None,
            streamrip_task_id=None,
            error="no acceptable streamrip match",
            cfg=cfg,
            bump_attempt=True,
        )
        decision["decision"] = "no_match"
        logger.info(
            "lidarr_backfill: no_match album_id=%s artist=%r album=%r",
            lidarr_album.get("id"),
            artist,
            title,
        )
        return decision

    decision["picked_service"] = match.service
    decision["picked_album_id"] = match.album_id
    decision["match_score"] = round(match.score.score, 3)

    if cfg.dry_run:
        await _persist_request(
            session,
            lidarr_album,
            status=STATUS_SKIPPED,
            match=match,
            streamrip_task_id=None,
            error="dry_run",
            cfg=cfg,
            bump_attempt=False,
        )
        decision["decision"] = "dry_run"
        logger.info(
            "lidarr_backfill: dry_run album_id=%s service=%s album=%s score=%.3f",
            lidarr_album.get("id"),
            match.service,
            match.album_id,
            match.score.score,
        )
        return decision

    try:
        result = await streamrip_client.download_album(match.service, match.album_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("lidarr_backfill: download_album raised: %s", exc)
        await _persist_request(
            session,
            lidarr_album,
            status=STATUS_FAILED,
            match=match,
            streamrip_task_id=None,
            error=str(exc)[:1000],
            cfg=cfg,
            bump_attempt=True,
        )
        decision["decision"] = "failed"
        decision["error"] = str(exc)[:200]
        return decision

    task_id = result.get("task_id") if isinstance(result, dict) else None
    status = result.get("status") if isinstance(result, dict) else "error"
    if not task_id or status == "error":
        err = (result.get("error") if isinstance(result, dict) else None) or "streamrip refused download"
        await _persist_request(
            session,
            lidarr_album,
            status=STATUS_FAILED,
            match=match,
            streamrip_task_id=task_id,
            error=err[:1000],
            cfg=cfg,
            bump_attempt=True,
        )
        decision["decision"] = "failed"
        decision["error"] = err[:200]
        return decision

    await _persist_request(
        session,
        lidarr_album,
        status=STATUS_DOWNLOADING,
        match=match,
        streamrip_task_id=task_id,
        error=None,
        cfg=cfg,
        bump_attempt=True,
    )
    decision["decision"] = "downloading"
    decision["task_id"] = task_id
    logger.info(
        "lidarr_backfill: queued album_id=%s service=%s album=%s task_id=%s score=%.3f",
        lidarr_album.get("id"),
        match.service,
        match.album_id,
        task_id,
        match.score.score,
    )
    return decision


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def run_backfill_tick(session: AsyncSession) -> dict[str, Any]:
    """One scheduler tick. Picks the next batch and dispatches downloads."""
    from app.services.download_chain import lidarr_enabled_in_chain  # local import to avoid cycle

    cfg = get_config()
    if not cfg.enabled:
        return {"skipped": "disabled"}
    if not lidarr_enabled_in_chain("bulk_album"):
        return {"skipped": "lidarr_disabled_in_routing"}
    if not (settings.LIDARR_URL and settings.LIDARR_API_KEY):
        return {"skipped": "lidarr_not_configured"}
    if not settings.STREAMRIP_API_URL:
        return {"skipped": "streamrip_not_configured"}

    capacity = await _compute_capacity(session, cfg)
    if capacity == 0:
        return {"skipped": "rate_limited", "capacity": 0}

    batch_target = min(capacity, cfg.max_batch_size)

    lidarr_client = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
    streamrip_client = StreamripClient(settings.STREAMRIP_API_URL)
    try:
        async with _mark_tick_running():
            # Probe streamrip-api once per tick so we can skip non-configured
            # services silently instead of logging a 503 warning per album.
            available_services = await streamrip_client.get_available_services()

            candidates = await _fetch_candidates(cfg, lidarr_client, limit=batch_target * 4)
            candidates = await _filter_by_cooldown_and_state(session, candidates, cfg)
            candidates = candidates[:batch_target]

            results: list[dict[str, Any]] = []
            for album in candidates:
                try:
                    results.append(
                        await _process_album(
                            session, album, cfg, streamrip_client,
                            available_services=available_services,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("lidarr_backfill: _process_album crashed: %s", exc, exc_info=True)
                    results.append(
                        {
                            "lidarr_album_id": album.get("id"),
                            "decision": "failed",
                            "error": f"crashed: {exc}",
                        }
                    )
                await session.commit()

            return {
                "processed": len(results),
                "capacity_remaining": max(0, capacity - len(results)),
                "available_services": available_services,
                "results": results,
            }
    finally:
        await lidarr_client.close()
        await streamrip_client.close()


async def poll_in_flight(session: AsyncSession) -> dict[str, Any]:
    """Promote ``downloading`` rows to terminal states and trigger Lidarr scan."""
    cfg = get_config()
    if not cfg.enabled:
        return {"skipped": "disabled"}
    if not settings.STREAMRIP_API_URL:
        return {"skipped": "streamrip_not_configured"}

    rows = (
        await session.execute(
            select(LidarrBackfillRequest).where(
                LidarrBackfillRequest.status == STATUS_DOWNLOADING,
                LidarrBackfillRequest.streamrip_task_id.isnot(None),
            )
        )
    ).scalars().all()

    if not rows:
        return {"checked": 0}

    streamrip_client = StreamripClient(settings.STREAMRIP_API_URL)
    lidarr_client: LidarrClient | None = None
    if cfg.import_options.trigger_lidarr_scan and settings.LIDARR_URL and settings.LIDARR_API_KEY:
        lidarr_client = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)

    summary = {"checked": len(rows), "completed": 0, "failed": 0, "still_running": 0}
    try:
        for row in rows:
            try:
                status = await streamrip_client.get_status(row.streamrip_task_id or "")
            except Exception as exc:  # noqa: BLE001
                logger.warning("lidarr_backfill: status check raised: %s", exc)
                summary["still_running"] += 1
                continue

            sr_status = (status.get("status") or "unknown").lower()
            if sr_status == "complete":
                row.status = STATUS_COMPLETE
                row.last_error = None
                row.next_retry_at = None
                row.updated_at = _now_ts()
                summary["completed"] += 1
                logger.info(
                    "lidarr_backfill: complete album_id=%s task_id=%s service=%s",
                    row.lidarr_album_id,
                    row.streamrip_task_id,
                    row.picked_service,
                )
                if lidarr_client is not None:
                    try:
                        await lidarr_client.trigger_downloaded_scan(cfg.import_options.scan_path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("lidarr_backfill: import scan failed: %s", exc)
            elif sr_status == "error":
                row.attempt_count += 1
                row.last_attempt_at = _now_ts()
                row.last_error = (status.get("error") or "streamrip reported error")[:1000]
                if row.attempt_count >= cfg.retry.max_attempts:
                    row.status = STATUS_PERMANENTLY_SKIPPED
                    row.next_retry_at = None
                else:
                    row.status = STATUS_FAILED
                    row.next_retry_at = _next_retry_timestamp(row.attempt_count, cfg)
                row.updated_at = _now_ts()
                summary["failed"] += 1
                logger.info(
                    "lidarr_backfill: failed album_id=%s task_id=%s attempts=%d/%d next_retry=%s",
                    row.lidarr_album_id,
                    row.streamrip_task_id,
                    row.attempt_count,
                    cfg.retry.max_attempts,
                    row.next_retry_at,
                )
            else:
                summary["still_running"] += 1
        await session.commit()
    finally:
        await streamrip_client.close()
        if lidarr_client is not None:
            await lidarr_client.close()

    return summary


# ---------------------------------------------------------------------------
# Preview / state-mutation helpers (called from API routes)
# ---------------------------------------------------------------------------


async def preview_matches(
    cfg_override: dict[str, Any] | None,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Run the matcher against the live missing queue *without* persisting.

    Used by the dashboard "Preview Match" button so operators can calibrate
    ``min_artist_similarity`` before flipping ``enabled``.
    """
    if cfg_override:
        merged = get_defaults().model_dump(mode="json")
        # Shallow merge top-level then deep merge known sub-objects.
        for k, v in cfg_override.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k].update(v)
            else:
                merged[k] = v
        try:
            cfg = LidarrBackfillConfigData.model_validate(merged)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"invalid config_override: {exc}", "candidates": []}
    else:
        cfg = get_config()

    if not (settings.LIDARR_URL and settings.LIDARR_API_KEY):
        return {"error": "lidarr_not_configured", "candidates": []}
    if not settings.STREAMRIP_API_URL:
        return {"error": "streamrip_not_configured", "candidates": []}

    lidarr_client = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
    streamrip_client = StreamripClient(settings.STREAMRIP_API_URL)
    out: list[dict[str, Any]] = []
    try:
        available_services = await streamrip_client.get_available_services()
        rows: list[dict[str, Any]] = []
        if cfg.sources.missing:
            try:
                rows.extend(
                    {**r, "_source": "missing"}
                    for r in await lidarr_client.get_missing_albums(monitored=cfg.sources.monitored_only)
                )
            except Exception as exc:  # noqa: BLE001
                return {"error": f"lidarr_fetch_failed: {exc}", "candidates": []}
        if cfg.sources.cutoff_unmet:
            try:
                rows.extend(
                    {**r, "_source": "cutoff"}
                    for r in await lidarr_client.get_cutoff_unmet_albums(monitored=cfg.sources.monitored_only)
                )
            except Exception:  # noqa: BLE001
                pass

        # Apply filters (non-persisted).
        allow = {_normalize_artist_name(a) for a in cfg.filters.artist_allowlist if a.strip()}
        deny = {_normalize_artist_name(a) for a in cfg.filters.artist_denylist if a.strip()}

        for album in rows[: max(1, limit) * 4]:  # over-sample for filters
            artist_name = (album.get("artist") or {}).get("artistName") or ""
            norm = _normalize_artist_name(artist_name)
            if allow and norm not in allow:
                continue
            if deny and norm in deny:
                continue

            match = await _find_streamrip_album(
                streamrip_client, album, cfg,
                available_services=available_services,
            )
            if match is None:
                out.append(
                    {
                        "lidarr_album_id": album.get("id"),
                        "artist": artist_name,
                        "album": album.get("title"),
                        "decision": "no_match",
                        "match_score": None,
                        "picked_service": None,
                        "reasons": [],
                    }
                )
            else:
                out.append(
                    {
                        "lidarr_album_id": album.get("id"),
                        "artist": artist_name,
                        "album": album.get("title"),
                        "decision": "would_queue",
                        "match_score": round(match.score.score, 3),
                        "picked_service": match.service,
                        "picked_album_id": match.album_id,
                        "matched_artist": match.album_artist,
                        "matched_album": match.album_title,
                        "reasons": match.score.reasons,
                    }
                )
            if len(out) >= limit:
                break
    finally:
        await lidarr_client.close()
        await streamrip_client.close()

    return {
        "candidates": out,
        "config_used": cfg.model_dump(mode="json"),
        "available_services": available_services,
    }


async def reset_backfill_state(session: AsyncSession, scope: str) -> int:
    """Bulk-delete rows in the named scope. Returns the number of rows deleted.

    Scopes:
      * ``failed``      — only rows currently in the failed bucket
      * ``no_match``    — only rows currently in no_match
      * ``permanently_skipped`` — only rows the engine gave up on
      * ``all``         — wipe every backfill row (dev / "start over")
    """
    scope = (scope or "").strip().lower()
    if scope == "all":
        stmt = delete(LidarrBackfillRequest)
    elif scope in {STATUS_FAILED, STATUS_NO_MATCH, STATUS_PERMANENTLY_SKIPPED}:
        stmt = delete(LidarrBackfillRequest).where(LidarrBackfillRequest.status == scope)
    else:
        raise ValueError(
            f"unknown scope {scope!r}; expected one of failed / no_match / permanently_skipped / all"
        )
    result = await session.execute(stmt)
    return result.rowcount or 0


async def retry_request(session: AsyncSession, request_id: int) -> bool:
    """Reset a single row so it gets re-picked on the next tick."""
    row = (
        await session.execute(
            select(LidarrBackfillRequest).where(LidarrBackfillRequest.id == request_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.status = STATUS_QUEUED
    row.attempt_count = 0
    row.next_retry_at = None
    row.last_error = None
    row.streamrip_task_id = None
    # Clearing created_at would break the unique window guarantee; leave it.
    row.updated_at = _now_ts()
    return True


async def skip_request(session: AsyncSession, request_id: int) -> bool:
    """Mark a row as permanently skipped (won't be retried)."""
    result = await session.execute(
        update(LidarrBackfillRequest)
        .where(LidarrBackfillRequest.id == request_id)
        .values(
            status=STATUS_PERMANENTLY_SKIPPED,
            next_retry_at=None,
            updated_at=_now_ts(),
        )
    )
    return (result.rowcount or 0) > 0


async def delete_request(session: AsyncSession, request_id: int) -> bool:
    """Remove a row entirely; the album will be re-picked from Lidarr next tick."""
    result = await session.execute(delete(LidarrBackfillRequest).where(LidarrBackfillRequest.id == request_id))
    return (result.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# Stats (for the dashboard top panel)
# ---------------------------------------------------------------------------


async def get_stats(session: AsyncSession) -> dict[str, Any]:
    """Counts + ETA for the dashboard ``Backfill Status`` card."""
    cfg = get_config()
    cutoff_ts = _now_ts() - 3600
    day_ago = _now_ts() - 86400

    by_status: dict[str, int] = {}
    rows = await session.execute(
        select(LidarrBackfillRequest.status, func.count())
        .group_by(LidarrBackfillRequest.status)
    )
    for status_, n in rows.all():
        by_status[status_] = int(n or 0)

    in_window = (
        await session.scalar(
            select(func.count())
            .select_from(LidarrBackfillRequest)
            .where(LidarrBackfillRequest.created_at > cutoff_ts)
        )
    ) or 0

    completed_24h = (
        await session.scalar(
            select(func.count())
            .select_from(LidarrBackfillRequest)
            .where(
                LidarrBackfillRequest.status == STATUS_COMPLETE,
                LidarrBackfillRequest.updated_at > day_ago,
            )
        )
    ) or 0
    failed_24h = (
        await session.scalar(
            select(func.count())
            .select_from(LidarrBackfillRequest)
            .where(
                LidarrBackfillRequest.status == STATUS_FAILED,
                LidarrBackfillRequest.updated_at > day_ago,
            )
        )
    ) or 0

    capacity_remaining = max(0, cfg.max_downloads_per_hour - in_window)

    # Try to fetch live missing/cutoff totals from Lidarr (best-effort).
    missing_total = None
    cutoff_total = None
    if settings.LIDARR_URL and settings.LIDARR_API_KEY:
        client = LidarrClient(settings.LIDARR_URL, settings.LIDARR_API_KEY)
        try:
            try:
                resp = await client._client.get(
                    f"{client._base_url}/api/v1/wanted/missing",
                    params={"page": 1, "pageSize": 1, "monitored": "true" if cfg.sources.monitored_only else "false"},
                )
                resp.raise_for_status()
                missing_total = resp.json().get("totalRecords")
            except Exception:  # noqa: BLE001
                missing_total = None
            try:
                resp = await client._client.get(
                    f"{client._base_url}/api/v1/wanted/cutoff",
                    params={"page": 1, "pageSize": 1, "monitored": "true" if cfg.sources.monitored_only else "false"},
                )
                resp.raise_for_status()
                cutoff_total = resp.json().get("totalRecords")
            except Exception:  # noqa: BLE001
                cutoff_total = None
        finally:
            await client.close()

    work_remaining = (missing_total or 0) + (cutoff_total or 0 if cfg.sources.cutoff_unmet else 0)
    eta_hours: float | None = None
    eta_days: float | None = None
    if cfg.max_downloads_per_hour > 0 and work_remaining > 0:
        eta_hours = round(work_remaining / cfg.max_downloads_per_hour, 1)
        eta_days = round(eta_hours / 24.0, 2)

    return {
        "enabled": cfg.enabled,
        "tick_in_progress": is_tick_in_progress(),
        "tick_started_at": get_tick_started_at(),
        "missing_total": missing_total,
        "cutoff_total": cutoff_total,
        "queued": by_status.get(STATUS_QUEUED, 0),
        "downloading": by_status.get(STATUS_DOWNLOADING, 0),
        "complete": by_status.get(STATUS_COMPLETE, 0),
        "complete_24h": int(completed_24h),
        "failed": by_status.get(STATUS_FAILED, 0),
        "failed_24h": int(failed_24h),
        "no_match": by_status.get(STATUS_NO_MATCH, 0),
        "permanently_skipped": by_status.get(STATUS_PERMANENTLY_SKIPPED, 0),
        "skipped": by_status.get(STATUS_SKIPPED, 0),
        "in_window": int(in_window),
        "max_per_hour": cfg.max_downloads_per_hour,
        "capacity_remaining": capacity_remaining,
        "eta_hours": eta_hours,
        "eta_days": eta_days,
    }


# ---------------------------------------------------------------------------
# Listing / read helpers
# ---------------------------------------------------------------------------


async def list_requests(
    session: AsyncSession,
    *,
    status: str | None = None,
    artist: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    stmt = select(LidarrBackfillRequest)
    if status:
        stmt = stmt.where(LidarrBackfillRequest.status == status)
    if artist:
        stmt = stmt.where(LidarrBackfillRequest.artist.ilike(f"%{artist}%"))
    stmt = stmt.order_by(LidarrBackfillRequest.updated_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


def _row_to_dict(row: LidarrBackfillRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "lidarr_album_id": row.lidarr_album_id,
        "mb_album_id": row.mb_album_id,
        "artist": row.artist,
        "album_title": row.album_title,
        "source": row.source,
        "match_score": row.match_score,
        "picked_service": row.picked_service,
        "picked_album_id": row.picked_album_id,
        "streamrip_task_id": row.streamrip_task_id,
        "status": row.status,
        "attempt_count": row.attempt_count,
        "last_attempt_at": row.last_attempt_at,
        "next_retry_at": row.next_retry_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


# Tiny helper used by the routes to keep async signatures clean.
async def _noop() -> None:  # pragma: no cover
    await asyncio.sleep(0)
