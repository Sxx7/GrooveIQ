"""
GrooveIQ -- slskd (Soulseek) download watcher.

Polls slskd's transfer API with backoff until a download reaches a
terminal state, then:

  1. Updates the matching ``download_requests`` row.
  2. Fires the media server rescan + GrooveIQ library scan (reuses
     the same post-download refresh chain as the spotdl watcher).

Soulseek downloads are peer-to-peer and can be much slower than
YouTube Music / Deezer downloads, so the timeout is 2 hours (vs
30 minutes for spotdl/Spotizerr).
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import DownloadRequest
from app.services.slskd import get_slskd_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Terminal state classification (slskd transfer states)
# ---------------------------------------------------------------------------

# slskd uses a flags enum; these are the string representations we see
# in the JSON API responses.
_TERMINAL_SUCCESS = {"completed, succeeded"}
_TERMINAL_ERROR = {
    "completed, errored",
    "completed, cancelled",
    "completed, timedout",
    "completed, rejected",
}
_TERMINAL_STATES = _TERMINAL_SUCCESS | _TERMINAL_ERROR

# Also match simpler state strings for robustness.
_SUCCESS_KEYWORDS = {"succeeded", "completed"}
_ERROR_KEYWORDS = {"errored", "cancelled", "timedout", "rejected"}


def _is_terminal(state: str) -> bool:
    s = state.lower().strip()
    return s in _TERMINAL_STATES or any(k in s for k in _SUCCESS_KEYWORDS | _ERROR_KEYWORDS)


def _is_success(state: str) -> bool:
    s = state.lower().strip()
    return s in _TERMINAL_SUCCESS or ("succeeded" in s and "errored" not in s)


# Poll schedule: Soulseek is slower — poll frequently at first, then back off.
_BACKOFF_SCHEDULE = [5, 10, 15, 30, 30] + [60] * 115  # ~2 hours total
_DEFAULT_TIMEOUT_S = 2 * 3600  # 2 hours

# Reaper age threshold — Soulseek downloads can legitimately take a while.
_REAPER_MAX_AGE_S = 4 * 3600  # 4 hours


# ---------------------------------------------------------------------------
# Active-watcher registry
# ---------------------------------------------------------------------------

_active_watchers: set[int] = set()  # keyed by download_requests.id
_watchers_lock = asyncio.Lock()


def active_watcher_count() -> int:
    return len(_active_watchers)


async def start_watcher(
    download_id: int,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
) -> bool:
    """Spawn a background watcher for a slskd download.

    Keyed by ``download_requests.id`` (not task_id, since slskd
    transfers are identified by username + transfer GUID).
    """
    async with _watchers_lock:
        if download_id in _active_watchers:
            return False
        _active_watchers.add(download_id)

    asyncio.create_task(_watch_loop(download_id, timeout_s))
    return True


# ---------------------------------------------------------------------------
# Watcher loop
# ---------------------------------------------------------------------------


async def _watch_loop(download_id: int, timeout_s: int) -> None:
    logger.info("slskd watcher started for download %d", download_id)
    started_at = time.monotonic()

    # Load the record to get slskd details.
    async with AsyncSessionLocal() as session:
        record = await session.get(DownloadRequest, download_id)
        if not record:
            logger.warning("slskd watcher %d: no DB record found", download_id)
            async with _watchers_lock:
                _active_watchers.discard(download_id)
            return
        username = record.slskd_username
        transfer_id = record.slskd_transfer_id
        filename = record.slskd_filename

    if not username:
        logger.warning("slskd watcher %d: no username on record", download_id)
        async with _watchers_lock:
            _active_watchers.discard(download_id)
        return

    client = get_slskd_client()
    if client is None:
        logger.warning("slskd watcher %d: slskd not configured", download_id)
        async with _watchers_lock:
            _active_watchers.discard(download_id)
        return

    terminal_status: str | None = None
    terminal_error: str = ""

    try:
        # If we don't have a transfer_id yet, try to find it.
        if not transfer_id and filename:
            await asyncio.sleep(3)  # give slskd a moment to register the transfer
            transfer = await client.find_transfer(username, filename)
            if transfer:
                transfer_id = transfer["id"]
                await _update_transfer_id(download_id, transfer_id)

        for delay in _BACKOFF_SCHEDULE:
            if time.monotonic() - started_at > timeout_s:
                logger.warning("slskd watcher %d: timeout after %ds", download_id, timeout_s)
                break

            await asyncio.sleep(delay)

            if transfer_id:
                transfer = await client.get_transfer(username, transfer_id)
            elif filename:
                transfer = await client.find_transfer(username, filename)
                if transfer and transfer.get("id"):
                    transfer_id = transfer["id"]
                    await _update_transfer_id(download_id, transfer_id)
            else:
                break

            if not transfer:
                logger.debug("slskd watcher %d: transfer not found, retrying", download_id)
                continue

            state = transfer.get("state", "")

            if _is_terminal(state):
                if _is_success(state):
                    terminal_status = "completed"
                else:
                    terminal_status = "error"
                    terminal_error = transfer.get("exception") or state
                logger.info("slskd watcher %d: terminal state %s", download_id, state)
                break

            logger.debug("slskd watcher %d: state=%s, still polling", download_id, state)

        # Persist final state.
        await _mark_download_done(
            download_id,
            terminal_status or "stalled",
            terminal_error,
        )

        if terminal_status == "completed":
            from app.services.download_watcher import _trigger_post_download_refresh

            await _trigger_post_download_refresh(f"slskd-{download_id}")

    except asyncio.CancelledError:
        logger.info("slskd watcher %d cancelled", download_id)
        raise
    except Exception as exc:
        logger.exception("slskd watcher %d crashed: %s", download_id, exc)
    finally:
        try:
            await client.close()
        except Exception:
            pass
        async with _watchers_lock:
            _active_watchers.discard(download_id)
        logger.debug("slskd watcher %d exited", download_id)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


async def _update_transfer_id(download_id: int, transfer_id: str) -> None:
    """Persist the slskd transfer ID once we discover it."""
    async with AsyncSessionLocal() as session:
        record = await session.get(DownloadRequest, download_id)
        if record:
            record.slskd_transfer_id = transfer_id
            await session.commit()


async def _mark_download_done(
    download_id: int,
    terminal_status: str,
    error_message: str,
) -> None:
    now = int(time.time())
    async with AsyncSessionLocal() as session:
        record = await session.get(DownloadRequest, download_id)
        if record is None:
            logger.warning("slskd watcher %d: no row to update", download_id)
            return
        record.status = terminal_status
        record.updated_at = now
        if error_message:
            record.error_message = error_message[:1024]
        await session.commit()


# ---------------------------------------------------------------------------
# Reaper — re-attaches watchers after container restart
# ---------------------------------------------------------------------------

_INFLIGHT_STATUSES = ("pending", "queued", "downloading")


async def reap_stuck_slskd_downloads() -> int:
    """Re-attach watchers to in-flight slskd downloads.

    Called on startup and periodically by the scheduler.
    """
    now = int(time.time())

    async with AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(DownloadRequest).where(
                        DownloadRequest.source == "soulseek",
                        DownloadRequest.status.in_(_INFLIGHT_STATUSES),
                        DownloadRequest.slskd_username.isnot(None),
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
    to_start: list[int] = []

    for row in rows:
        if row.id in already_watching:
            continue
        age = now - (row.created_at or now)
        if age > _REAPER_MAX_AGE_S:
            expired.append(row.id)
            continue
        to_start.append(row.id)

    if expired:
        async with AsyncSessionLocal() as session:
            for row_id in expired:
                fresh = await session.get(DownloadRequest, row_id)
                if fresh and fresh.status not in ("completed", "error"):
                    fresh.status = "error"
                    fresh.error_message = (
                        f"slskd watcher timed out: no terminal status within {_REAPER_MAX_AGE_S // 3600}h."
                    )
                    fresh.updated_at = now
            await session.commit()
        logger.info("slskd reaper: expired %d orphaned downloads", len(expired))

    for download_id in to_start:
        if await start_watcher(download_id):
            reaped += 1

    if reaped:
        logger.info("slskd reaper: re-attached %d watchers", reaped)
    return reaped
