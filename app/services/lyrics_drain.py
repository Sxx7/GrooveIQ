"""
GrooveIQ — lyrics acquisition drain.

Mirrors the Lidarr-backfill engine (``app/services/lidarr_backfill.py``): a
rate-limited tick walks the library, resolves each track through the lyrics
cascade (``app/services/lyrics.py``), and persists per-track state in
``LyricsRequest`` so the whole-library backfill is resumable and survives a
GPU-VM outage.

Key differences from the album backfill:

- The scarce resource is the **GPU (ASR)**, not a download slot. Only ASR calls
  count against the per-hour cap (embedded + LRCLIB are cheap), tracked via the
  precise ``last_asr_at`` sliding window. ASR is further smoothed across a tick
  so the GPU load is even rather than bursty.
- Resolution is synchronous (the sidecar's ``/transcribe`` returns the
  transcript directly), so there is no separate "poll for completion" job — the
  tick is self-contained. A stale-``searching`` reaper recovers from a crash
  mid-tick.
- Config is env-only (``LYRICS_*``); promote to a versioned ``LyricsConfig`` +
  GUI later if hand-tuning warrants it.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import LyricsRequest, TrackFeatures
from app.services.audio_analysis import LYRICS_VERSION
from app.services.lyrics import (
    OUTCOME_ASR_DEFERRED,
    OUTCOME_FOUND,
    OUTCOME_INSTRUMENTAL,
    OUTCOME_NO_LYRICS,
    OUTCOME_SEARCH_ERROR,
    apply_resolution,
    resolve_lyrics,
)

logger = logging.getLogger(__name__)

# --- Queue statuses ---
STATUS_QUEUED = "queued"
STATUS_SEARCHING = "searching"
STATUS_COMPLETE = "complete"
STATUS_INSTRUMENTAL = "instrumental"
STATUS_NO_LYRICS = "no_lyrics"
STATUS_FAILED = "failed"
STATUS_SEARCH_ERROR = "search_error"
STATUS_PERMANENTLY_SKIPPED = "permanently_skipped"

_TERMINAL_STATUSES = {STATUS_COMPLETE, STATUS_INSTRUMENTAL, STATUS_PERMANENTLY_SKIPPED}
# Statuses a candidate can be (re-)selected from once any cooldown elapses.
_RETRYABLE_STATUSES = [STATUS_QUEUED, STATUS_NO_LYRICS, STATUS_FAILED, STATUS_SEARCH_ERROR]

_SEARCH_ERROR_COOLDOWN_S = 600  # 10 minutes (transient backend failure)
_ASR_DEFER_COOLDOWN_S = 300  # 5 minutes (GPU budget spent — re-examine soon)
_STALE_SEARCHING_S = 1800  # 30 minutes: reap rows stuck in `searching` (crash recovery)


# ---------------------------------------------------------------------------
# Tick-in-progress flag (for the stats UI; separate from scheduler state)
# ---------------------------------------------------------------------------

_tick_lock = threading.Lock()
_tick_in_progress = False
_tick_started_at: int | None = None
_last_tick_at: int | None = None


def is_tick_in_progress() -> bool:
    with _tick_lock:
        return _tick_in_progress


def get_tick_started_at() -> int | None:
    with _tick_lock:
        return _tick_started_at


def get_last_tick_at() -> int | None:
    with _tick_lock:
        return _last_tick_at


def _begin_tick() -> bool:
    global _tick_in_progress, _tick_started_at
    with _tick_lock:
        if _tick_in_progress:
            return False
        _tick_in_progress = True
        _tick_started_at = _now_ts()
        return True


def _end_tick() -> None:
    global _tick_in_progress, _last_tick_at
    with _tick_lock:
        _tick_in_progress = False
        _last_tick_at = _now_ts()


def _now_ts() -> int:
    return int(time.time())


@dataclass
class _DrainParams:
    batch_size: int
    max_attempts: int
    cooldown_hours: float
    backoff_multiplier: float
    instrumental_max: float
    asr_enabled: bool
    asr_per_tick: int | None  # None = unlimited
    asr_hourly_remaining: int | None  # None = unlimited


def _load_params() -> _DrainParams:
    max_per_hour = int(settings.LYRICS_DRAIN_MAX_PER_HOUR or 0)
    poll_min = max(1, int(settings.LYRICS_DRAIN_POLL_MINUTES or 5))
    asr_per_tick = None
    if max_per_hour > 0:
        asr_per_tick = max(1, math.ceil(max_per_hour * poll_min / 60.0))
    return _DrainParams(
        batch_size=max(1, int(settings.LYRICS_DRAIN_BATCH_SIZE or 100)),
        max_attempts=max(1, int(settings.LYRICS_DRAIN_MAX_ATTEMPTS or 3)),
        cooldown_hours=float(settings.LYRICS_DRAIN_COOLDOWN_HOURS or 24.0),
        backoff_multiplier=float(settings.LYRICS_DRAIN_BACKOFF_MULTIPLIER or 2.0),
        instrumental_max=float(settings.LYRICS_ASR_INSTRUMENTAL_MAX),
        asr_enabled=bool(settings.lyrics_asr_enabled),
        asr_per_tick=asr_per_tick,
        asr_hourly_remaining=None,  # filled per-tick from the DB window
    )


def _next_retry_timestamp(attempt_count: int, cooldown_hours: float, backoff_multiplier: float) -> int:
    """cooldown × multiplier^(attempt_count - 1) hours from now (capped 30 days)."""
    if attempt_count < 1:
        attempt_count = 1
    grown = cooldown_hours * (backoff_multiplier ** (attempt_count - 1))
    grown = min(grown, 24.0 * 30)
    return _now_ts() + int(grown * 3600)


# ---------------------------------------------------------------------------
# Capacity, reaping, candidate selection
# ---------------------------------------------------------------------------


async def _compute_asr_used_in_window(session: AsyncSession) -> int:
    """How many ASR calls have run in the last sliding hour (precise: counts
    rows whose ``last_asr_at`` falls inside the window, regardless of row age)."""
    cutoff = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())
    used = await session.scalar(
        select(func.count())
        .select_from(LyricsRequest)
        .where(LyricsRequest.last_asr_at.isnot(None), LyricsRequest.last_asr_at > cutoff)
    )
    return used or 0


async def _reap_stale_searching(session: AsyncSession) -> int:
    """Reset rows stuck in `searching` past the stale threshold back to `queued`
    (recovers from a process crash mid-tick). Returns the count reset."""
    cutoff = _now_ts() - _STALE_SEARCHING_S
    result = await session.execute(
        update(LyricsRequest)
        .where(LyricsRequest.status == STATUS_SEARCHING, LyricsRequest.updated_at < cutoff)
        .values(status=STATUS_QUEUED, next_retry_at=None, updated_at=_now_ts())
    )
    await session.commit()
    return result.rowcount or 0


async def _select_candidates(
    session: AsyncSession, *, limit: int, max_attempts: int
) -> list[tuple[TrackFeatures, LyricsRequest | None]]:
    """Pick up to ``limit`` (track, existing_row|None) pairs to process.

    Preference order:
      A) existing rows in a retryable state whose cooldown has elapsed, and
      B) fresh tracks with no row yet that were never resolved at the current
         ``LYRICS_VERSION``.
    """
    now = _now_ts()
    out: list[tuple[TrackFeatures, LyricsRequest | None]] = []

    stmt_a = (
        select(LyricsRequest, TrackFeatures)
        .join(TrackFeatures, TrackFeatures.track_id == LyricsRequest.track_id)
        .where(
            LyricsRequest.status.in_(_RETRYABLE_STATUSES),
            or_(LyricsRequest.next_retry_at.is_(None), LyricsRequest.next_retry_at <= now),
            LyricsRequest.attempt_count < max_attempts,
        )
        .order_by(func.coalesce(LyricsRequest.next_retry_at, 0).asc())
        .limit(limit)
    )
    for lr, tf in (await session.execute(stmt_a)).all():
        out.append((tf, lr))

    if len(out) >= limit:
        return out

    remaining = limit - len(out)
    stmt_b = (
        select(TrackFeatures)
        .outerjoin(LyricsRequest, LyricsRequest.track_id == TrackFeatures.track_id)
        .where(
            LyricsRequest.id.is_(None),
            or_(TrackFeatures.lyrics_version.is_(None), TrackFeatures.lyrics_version != LYRICS_VERSION),
        )
        .limit(remaining)
    )
    for tf in (await session.execute(stmt_b)).scalars().all():
        out.append((tf, None))
    return out


# ---------------------------------------------------------------------------
# Per-track processing
# ---------------------------------------------------------------------------


def _apply_outcome_to_row(row: LyricsRequest, res, *, params: _DrainParams) -> None:
    now = _now_ts()
    row.last_attempt_at = now
    row.updated_at = now
    row.source_resolved = res.source
    if res.cheap_exhausted:
        row.cheap_exhausted = True
    if res.asr_used:
        row.last_asr_at = now

    if res.outcome == OUTCOME_FOUND:
        row.status = STATUS_COMPLETE
        row.next_retry_at = None
        row.last_error = None
    elif res.outcome == OUTCOME_INSTRUMENTAL:
        row.status = STATUS_INSTRUMENTAL
        row.next_retry_at = None
        row.last_error = None
    elif res.outcome == OUTCOME_NO_LYRICS:
        row.attempt_count += 1
        row.last_error = res.detail
        if row.attempt_count >= params.max_attempts:
            row.status = STATUS_PERMANENTLY_SKIPPED
            row.next_retry_at = None
        else:
            row.status = STATUS_NO_LYRICS
            row.next_retry_at = _next_retry_timestamp(
                row.attempt_count, params.cooldown_hours, params.backoff_multiplier
            )
    elif res.outcome == OUTCOME_SEARCH_ERROR:
        # Transient: short cooldown, never bumps attempt_count (an outage must
        # not burn an attempt or permanently skip the track — issue #122).
        row.status = STATUS_SEARCH_ERROR
        row.next_retry_at = now + _SEARCH_ERROR_COOLDOWN_S
        row.last_error = res.detail
    elif res.outcome == OUTCOME_ASR_DEFERRED:
        # GPU budget spent: re-queue soon, no penalty.
        row.status = STATUS_QUEUED
        row.next_retry_at = now + _ASR_DEFER_COOLDOWN_S
        row.last_error = None


async def _process_track(
    session: AsyncSession,
    track: TrackFeatures,
    existing: LyricsRequest | None,
    *,
    allow_asr: bool,
    lrclib_client,
    asr_client,
    params: _DrainParams,
):
    now = _now_ts()
    if existing is None:
        row = LyricsRequest(
            track_id=track.track_id,
            status=STATUS_SEARCHING,
            created_at=now,
            updated_at=now,
            attempt_count=0,
        )
        session.add(row)
    else:
        row = existing
        row.status = STATUS_SEARCHING
        row.updated_at = now

    instr = track.instrumentalness
    row.voiced = (instr is None) or (instr < params.instrumental_max)

    # Rows already known to have exhausted the cheap tiers skip straight to ASR
    # so retries don't re-hammer LRCLIB. (Only meaningful while ASR is enabled;
    # a fresh recheck happens after an operator resets the row's scope.)
    skip_cheap = bool(existing is not None and existing.cheap_exhausted and params.asr_enabled)

    try:
        res = await resolve_lyrics(
            track,
            lrclib_client=lrclib_client,
            asr_client=asr_client,
            allow_asr=allow_asr,
            skip_cheap_tiers=skip_cheap,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Lyrics resolve failed for %s: %s", track.track_id, exc)
        row.attempt_count += 1
        row.last_attempt_at = now
        row.updated_at = now
        row.last_error = str(exc)[:1000]
        if row.attempt_count >= params.max_attempts:
            row.status = STATUS_PERMANENTLY_SKIPPED
            row.next_retry_at = None
        else:
            row.status = STATUS_FAILED
            row.next_retry_at = _next_retry_timestamp(
                row.attempt_count, params.cooldown_hours, params.backoff_multiplier
            )
        return None

    apply_resolution(track, res)
    _apply_outcome_to_row(row, res, params=params)
    return res


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


async def run_lyrics_tick(session: AsyncSession) -> dict[str, Any]:
    """Process one batch of the lyrics backfill. Safe to call repeatedly."""
    if not settings.lyrics_enabled:
        return {"skipped": "disabled"}
    if not _begin_tick():
        return {"skipped": "already_running"}
    try:
        return await _run_tick_inner(session)
    finally:
        _end_tick()


async def _run_tick_inner(session: AsyncSession) -> dict[str, Any]:
    params = _load_params()
    reaped = await _reap_stale_searching(session)

    lrclib_client = None
    if settings.lyrics_lrclib_enabled:
        from app.services.lrclib import get_lrclib_client

        lrclib_client = get_lrclib_client()

    asr_client = None
    if params.asr_enabled:
        try:
            from app.services.lyrics_asr import get_lyrics_asr_client

            asr_client = get_lyrics_asr_client()
        except Exception as exc:  # pragma: no cover - Phase B not deployed
            logger.warning("Lyrics ASR client unavailable: %s", exc)
            params.asr_enabled = False

    # GPU budget for this tick (only when ASR is active and capped).
    if params.asr_enabled and params.asr_per_tick is not None:
        max_per_hour = int(settings.LYRICS_DRAIN_MAX_PER_HOUR or 0)
        used = await _compute_asr_used_in_window(session)
        params.asr_hourly_remaining = max(0, max_per_hour - used)

    candidates = await _select_candidates(session, limit=params.batch_size, max_attempts=params.max_attempts)
    if not candidates:
        return {"skipped": "no_candidates", "reaped": reaped}

    processed = 0
    asr_used_this_tick = 0
    outcomes: dict[str, int] = defaultdict(int)

    for track, row in candidates:
        allow_asr = True
        if params.asr_enabled and params.asr_per_tick is not None:
            if asr_used_this_tick >= params.asr_per_tick or (
                params.asr_hourly_remaining is not None and params.asr_hourly_remaining <= 0
            ):
                allow_asr = False

        # Isolate each track: a per-track failure (incl. a rare commit/
        # IntegrityError from a row created concurrently) must not abort the
        # whole batch or strand other rows in `searching`. Roll back and move on.
        try:
            res = await _process_track(
                session,
                track,
                row,
                allow_asr=allow_asr,
                lrclib_client=lrclib_client,
                asr_client=asr_client,
                params=params,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            logger.warning("Lyrics drain: failed to process %s: %s", track.track_id, traceback.format_exc())
            outcomes["failed"] += 1
            processed += 1
            continue
        processed += 1
        if res is None:
            outcomes["failed"] += 1
            continue
        outcomes[res.outcome] += 1
        if res.asr_used:
            asr_used_this_tick += 1
            if params.asr_hourly_remaining is not None:
                params.asr_hourly_remaining -= 1

    return {
        "processed": processed,
        "reaped": reaped,
        "asr_used": asr_used_this_tick,
        "outcomes": dict(outcomes),
    }


# ---------------------------------------------------------------------------
# Operator helpers (used by the routes)
# ---------------------------------------------------------------------------


def _row_to_dict(row: LyricsRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "track_id": row.track_id,
        "status": row.status,
        "source_resolved": row.source_resolved,
        "voiced": row.voiced,
        "cheap_exhausted": row.cheap_exhausted,
        "attempt_count": row.attempt_count,
        "last_attempt_at": row.last_attempt_at,
        "last_asr_at": row.last_asr_at,
        "next_retry_at": row.next_retry_at,
        "last_error": row.last_error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


async def list_requests(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    stmt = select(LyricsRequest)
    if status:
        stmt = stmt.where(LyricsRequest.status == status)
    stmt = stmt.order_by(LyricsRequest.updated_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(r) for r in rows]


async def get_stats(session: AsyncSession) -> dict[str, Any]:
    """Queue counts by status, library coverage, capacity, and a rough ETA."""
    # Counts by queue status.
    rows = await session.execute(
        select(LyricsRequest.status, func.count()).group_by(LyricsRequest.status)
    )
    by_status = {status: count for status, count in rows.all()}

    total_tracks = await session.scalar(select(func.count()).select_from(TrackFeatures)) or 0
    # Resolved at the current cascade version (any displayable / terminal source).
    resolved = (
        await session.scalar(
            select(func.count())
            .select_from(TrackFeatures)
            .where(TrackFeatures.lyrics_version == LYRICS_VERSION)
        )
        or 0
    )
    # Coverage by display source (what the user actually sees).
    src_rows = await session.execute(
        select(TrackFeatures.lyrics_source, func.count())
        .where(TrackFeatures.lyrics_source.isnot(None))
        .group_by(TrackFeatures.lyrics_source)
    )
    by_source = {src: count for src, count in src_rows.all()}

    max_per_hour = int(settings.LYRICS_DRAIN_MAX_PER_HOUR or 0)
    asr_used = await _compute_asr_used_in_window(session)
    capacity_remaining = None if max_per_hour <= 0 else max(0, max_per_hour - asr_used)

    remaining = max(0, total_tracks - resolved)
    eta_hours = None
    if max_per_hour > 0 and remaining > 0:
        # Conservative: assumes every remaining track needs an ASR slot.
        eta_hours = round(remaining / max_per_hour, 1)

    return {
        "enabled": settings.lyrics_enabled,
        "lrclib_enabled": settings.lyrics_lrclib_enabled,
        "asr_enabled": settings.lyrics_asr_enabled,
        "lyrics_version": LYRICS_VERSION,
        "total_tracks": total_tracks,
        "resolved": resolved,
        "remaining": remaining,
        "by_status": by_status,
        "by_source": by_source,
        "max_per_hour": max_per_hour,
        "asr_used_last_hour": asr_used,
        "asr_capacity_remaining": capacity_remaining,
        "eta_hours": eta_hours,
        "tick_in_progress": is_tick_in_progress(),
        "tick_started_at": get_tick_started_at(),
        "last_tick_at": get_last_tick_at(),
    }


_RESET_SCOPES = {
    "no_lyrics": [STATUS_NO_LYRICS],
    "failed": [STATUS_FAILED],
    "search_error": [STATUS_SEARCH_ERROR],
    "permanently_skipped": [STATUS_PERMANENTLY_SKIPPED],
    "instrumental": [STATUS_INSTRUMENTAL],
    "all": [
        STATUS_NO_LYRICS,
        STATUS_FAILED,
        STATUS_SEARCH_ERROR,
        STATUS_PERMANENTLY_SKIPPED,
        STATUS_INSTRUMENTAL,
    ],
}


async def reset_state(session: AsyncSession, scope: str) -> int:
    """Re-queue rows in the given scope (e.g. after enabling ASR, reset
    `no_lyrics`). Clears retry state and ``cheap_exhausted`` so the next pass
    re-checks the cheap tiers once before falling through to ASR."""
    statuses = _RESET_SCOPES.get(scope)
    if statuses is None:
        raise ValueError(f"Unknown reset scope: {scope!r}")
    result = await session.execute(
        update(LyricsRequest)
        .where(LyricsRequest.status.in_(statuses))
        .values(
            status=STATUS_QUEUED,
            attempt_count=0,
            next_retry_at=None,
            cheap_exhausted=False,
            last_error=None,
            updated_at=_now_ts(),
        )
    )
    await session.commit()
    return result.rowcount or 0


async def retry_request(session: AsyncSession, request_id: int) -> bool:
    row = await session.get(LyricsRequest, request_id)
    if row is None:
        return False
    row.status = STATUS_QUEUED
    row.attempt_count = 0
    row.next_retry_at = None
    row.cheap_exhausted = False
    row.last_error = None
    row.updated_at = _now_ts()
    await session.commit()
    return True


async def skip_request(session: AsyncSession, request_id: int) -> bool:
    row = await session.get(LyricsRequest, request_id)
    if row is None:
        return False
    row.status = STATUS_PERMANENTLY_SKIPPED
    row.next_retry_at = None
    row.updated_at = _now_ts()
    await session.commit()
    return True


async def delete_request(session: AsyncSession, request_id: int) -> bool:
    result = await session.execute(delete(LyricsRequest).where(LyricsRequest.id == request_id))
    await session.commit()
    return (result.rowcount or 0) > 0
