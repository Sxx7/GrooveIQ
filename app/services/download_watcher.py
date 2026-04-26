"""
GrooveIQ -- Spotizerr download watcher.

Polls Spotizerr's ``/api/prgs/{task_id}`` endpoint with backoff until a
download reaches a terminal state, then:

  1. Updates the matching ``download_requests`` row with the final status.
  2. Fires the configured media server's rescan API (Plex partial scan
     or Navidrome Subsonic ``startScan.view``).
  3. Triggers a GrooveIQ library scan so ``track_features`` learns about
     the new file and subsequent chart rebuilds see ``matched_track_id``
     populated (cover-art priority chain then flips to the media server).

The watcher is spawned as an asyncio task from:
  - ``POST /v1/downloads`` (user-initiated track download)
  - ``charts._send_tracks_to_spotizerr`` (auto-downloaded chart entries)
  - The periodic reaper (see ``reap_stuck_downloads`` below) which
    re-attaches watchers on container restart.

Terminal state mapping follows Spotizerr's ``ProgressState`` enum:
  success: complete, done
  failure: error, cancelled, error_retried, error_auto_cleaned

In-flight states (queued, initializing, downloading, processing, …)
simply cause the watcher to sleep and retry.

Watcher identity is keyed on ``task_id``; ``start_watcher`` is
idempotent so repeated calls (e.g. from the reaper) don't spawn
duplicate pollers.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import DownloadRequest
from app.services.spotdl import get_download_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal state classification
# ---------------------------------------------------------------------------

_TERMINAL_SUCCESS = {"complete", "done"}
_TERMINAL_DUPLICATE = {"duplicate"}
_TERMINAL_ERROR = {
    "error",
    "cancelled",
    "error_retried",
    "error_auto_cleaned",
}
_TERMINAL_STATES = _TERMINAL_SUCCESS | _TERMINAL_DUPLICATE | _TERMINAL_ERROR

# Poll schedule (seconds): tight at the start, loose once we know the
# download is in flight.  Small tracks land in 10-30s on a decent pipe;
# albums can take a few minutes; pathological cases can stretch to 10+.
# 5s + 10s + 15s + 30s + 60s * ~N => ~10 min at the 5th minute, 30 min cap.
_BACKOFF_SCHEDULE = [5, 10, 15, 30] + [60] * 200

# Default overall timeout for a single download's watcher (30 min).
# Past this we give up polling and let the reaper handle it if the
# container restarts; the row stays in "downloading" until a fresh
# poll sees it.
_DEFAULT_TIMEOUT_S = 30 * 60

# How old a non-terminal download_request can be before the reaper
# marks it as a stale/orphaned error record.  2 hours is long enough
# for legitimate slow downloads, short enough to prevent runaway rows.
_REAPER_MAX_AGE_S = 2 * 3600


# ---------------------------------------------------------------------------
# Active-watcher registry (process-local)
# ---------------------------------------------------------------------------

_active_watchers: set[str] = set()
_watchers_lock = asyncio.Lock()


def active_watcher_count() -> int:
    """Return the number of currently-running watcher tasks."""
    return len(_active_watchers)


async def start_watcher(
    task_id: str,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    source: str | None = None,
) -> bool:
    """Spawn a background watcher for an HTTP-task download backend
    (spotdl-api / streamrip-api / Spotizerr).

    ``source`` ties the watcher to the backend that owns ``task_id`` —
    a streamrip task ID is meaningless to spotdl-api and vice versa.
    When omitted, the source is looked up from the ``download_requests``
    row at start time.

    Safe to call multiple times for the same ``task_id`` — only one
    watcher runs at a time.  Returns True if a new watcher was
    actually started, False if one was already running.
    """
    if not task_id:
        return False
    async with _watchers_lock:
        if task_id in _active_watchers:
            logger.debug("Watcher already active for %s", task_id)
            return False
        _active_watchers.add(task_id)

    asyncio.create_task(_watch_loop(task_id, timeout_s, source))
    return True


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------


def _client_for_source(source: str | None):
    """Construct the right backend client for the given DownloadRequest.source.

    Returns ``None`` if the source's infra isn't configured. Falls back to
    the legacy default-backend factory only when ``source`` is missing.
    """
    from app.core.config import settings

    if source == "streamrip" and settings.streamrip_enabled:
        from app.services.streamrip import StreamripClient
        return StreamripClient(settings.STREAMRIP_API_URL)
    if source == "spotdl" and settings.spotdl_enabled:
        from app.services.spotdl import SpotdlClient
        return SpotdlClient(settings.SPOTDL_API_URL)
    if source == "spotizerr" and settings.spotizerr_enabled:
        from app.services.spotizerr import SpotizerrClient
        return SpotizerrClient(
            settings.SPOTIZERR_URL, settings.SPOTIZERR_USERNAME, settings.SPOTIZERR_PASSWORD
        )
    # Unknown / legacy rows with no source — fall back to whatever the env
    # default picks. Logged so we notice when it happens.
    if source:
        logger.warning("Watcher: no client constructor for source=%r, falling back to default", source)
    return get_download_client()


async def _watch_loop(task_id: str, timeout_s: int, source: str | None = None) -> None:
    logger.info("Download watcher started for task %s (source=%s)", task_id, source or "?")
    started_at = time.monotonic()

    # Resolve source from DB if the caller didn't supply one. Avoids the
    # historical bug where every watcher used the env-default backend.
    if source is None:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    select(DownloadRequest.source).where(DownloadRequest.task_id == task_id)
                )
            ).scalar_one_or_none()
            source = row

    client = _client_for_source(source)
    if client is None:
        logger.warning("Watcher %s: no download backend configured for source=%s", task_id, source)
        async with _watchers_lock:
            _active_watchers.discard(task_id)
        return

    terminal_status: str | None = None
    terminal_error: str = ""

    try:
        for delay in _BACKOFF_SCHEDULE:
            if time.monotonic() - started_at > timeout_s:
                logger.warning(
                    "Download watcher %s: overall timeout after %ds",
                    task_id,
                    timeout_s,
                )
                break

            await asyncio.sleep(delay)

            status_data = await client.get_status(task_id)
            status = status_data.get("status") or ""

            # get_status() may return "error" as its *own* error sentinel
            # (e.g. connection refused) — we can't distinguish that from
            # an actual Spotizerr-reported "error" state without peeking
            # at raw. Be conservative: if raw is empty, treat as transient
            # and keep polling until timeout.
            if status == "error" and not status_data.get("raw"):
                logger.debug(
                    "Watcher %s: transient status poll error (%s), retrying",
                    task_id,
                    status_data.get("error"),
                )
                continue

            if status in _TERMINAL_STATES:
                terminal_status = status
                if status in _TERMINAL_ERROR:
                    terminal_error = status_data.get("error") or ""
                logger.info(
                    "Watcher %s: reached terminal status %s",
                    task_id,
                    status,
                )
                break

            logger.debug("Watcher %s: status=%s, still polling", task_id, status)

        # Persist the final state.
        await _mark_download_done(
            task_id,
            terminal_status or "stalled",
            terminal_error,
        )

        # On success, kick off media server + GrooveIQ refreshes.
        if terminal_status in _TERMINAL_SUCCESS:
            await _trigger_post_download_refresh(task_id)

    except asyncio.CancelledError:
        logger.info("Watcher %s cancelled", task_id)
        raise
    except Exception as exc:
        logger.exception("Watcher %s crashed: %s", task_id, exc)
    finally:
        try:
            await client.close()
        except Exception:
            pass
        async with _watchers_lock:
            _active_watchers.discard(task_id)
        logger.debug("Watcher %s exited", task_id)


# ---------------------------------------------------------------------------
# DB update
# ---------------------------------------------------------------------------


async def _mark_download_done(
    task_id: str,
    terminal_status: str,
    error_message: str,
) -> None:
    """Update the download_requests row for a finished task."""
    if terminal_status in _TERMINAL_SUCCESS:
        final_status = "completed"
    elif terminal_status in _TERMINAL_DUPLICATE:
        final_status = "duplicate"
    elif terminal_status in _TERMINAL_ERROR:
        final_status = "error"
    else:
        final_status = terminal_status
    now = int(time.time())
    async with AsyncSessionLocal() as session:
        record = (
            await session.execute(select(DownloadRequest).where(DownloadRequest.task_id == task_id))
        ).scalar_one_or_none()
        if record is None:
            logger.warning(
                "Watcher %s: no download_requests row to update",
                task_id,
            )
            return
        # Respect manual cancellation — if the user dismissed the row via
        # DELETE /v1/downloads/{id} while we were polling, don't silently
        # overwrite that decision when the upstream eventually terminates.
        if record.status == "cancelled":
            logger.info("Watcher %s: row was cancelled by user; not overwriting", task_id)
            return
        record.status = final_status
        record.updated_at = now
        if error_message:
            record.error_message = error_message[:1024]
        await session.commit()


# ---------------------------------------------------------------------------
# Post-download refresh chain
# ---------------------------------------------------------------------------


async def _trigger_post_download_refresh(task_id: str) -> None:
    """After a download succeeds, tell the media server and GrooveIQ to scan.

    Both calls are best-effort — failures are logged but don't
    propagate, because the download itself already succeeded.
    """
    # 1. Media server refresh (Plex partial, Navidrome full).
    try:
        from app.services.media_server import is_configured, refresh_library

        if is_configured():
            ok = await refresh_library()
            if ok:
                logger.info("Download %s: media server scan triggered", task_id)
            else:
                logger.warning(
                    "Download %s: media server scan trigger returned false",
                    task_id,
                )
        else:
            logger.debug(
                "Download %s: no media server configured, skipping refresh",
                task_id,
            )
    except Exception as exc:
        logger.warning(
            "Download %s: media server refresh error: %s",
            task_id,
            exc,
        )

    # 2. GrooveIQ library scan — only if one isn't already running.
    #    trigger_scan() is already idempotent in that respect but we
    #    check first to avoid the log noise.
    try:
        from app.workers.library_scanner import is_scan_running, trigger_scan

        if is_scan_running():
            logger.debug(
                "Download %s: GrooveIQ scan already running, skipping",
                task_id,
            )
            return
        scan_id = await trigger_scan()
        logger.info(
            "Download %s: GrooveIQ library scan triggered (scan_id=%s)",
            task_id,
            scan_id,
        )
    except Exception as exc:
        logger.warning(
            "Download %s: GrooveIQ scan trigger error: %s",
            task_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Reaper — re-attaches watchers after container restart
# ---------------------------------------------------------------------------

# Non-terminal statuses that indicate "in flight, needs watching".
_INFLIGHT_STATUSES = (
    "pending",
    "queued",
    "initializing",
    "downloading",
    "processing",
    "progress",
    "retrying",
    "duplicate",
)


async def reap_stuck_downloads() -> int:
    """Re-attach watchers to rows left in-flight by a previous process.

    Called on startup and every 5 minutes by the scheduler.  For each
    download_request in a non-terminal state:

      - If it has no task_id, skip (there's nothing to poll against).
      - If a watcher is already active for that task_id (e.g. this is a
        periodic run, not a restart), skip.
      - If the row is older than ``_REAPER_MAX_AGE_S`` and still not
        terminal, mark it errored with a "likely orphaned" message
        instead of polling indefinitely.
      - Otherwise spawn a fresh watcher.

    Returns the number of watchers newly attached.
    """
    now = int(time.time())

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadRequest).where(
                        DownloadRequest.status.in_(_INFLIGHT_STATUSES),
                        DownloadRequest.task_id.isnot(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return 0

    async with _watchers_lock:
        already_watching = set(_active_watchers)

    reaped = 0
    expired: list[int] = []
    to_start: list[str] = []

    for row in rows:
        if not row.task_id or row.task_id in already_watching:
            continue
        age = now - (row.created_at or now)
        if age > _REAPER_MAX_AGE_S:
            expired.append(row.id)
            continue
        to_start.append(row.task_id)

    # Expire ancient rows in a single session.
    if expired:
        async with AsyncSessionLocal() as session:
            for row_id in expired:
                fresh = await session.get(DownloadRequest, row_id)
                if fresh and fresh.status not in ("completed", "error"):
                    fresh.status = "error"
                    fresh.error_message = (
                        f"Watcher timed out: no terminal status observed within {_REAPER_MAX_AGE_S // 60} minutes."
                    )
                    fresh.updated_at = now
            await session.commit()
        logger.info("Reaper: expired %d orphaned download rows", len(expired))

    # Spawn fresh watchers.
    for task_id in to_start:
        if await start_watcher(task_id):
            reaped += 1

    if reaped:
        logger.info("Reaper: re-attached %d download watchers", reaped)
    return reaped
