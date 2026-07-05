"""
GrooveIQ -- Shared download-cascade persistence + watcher dispatch.

Extracted from ``app/api/routes/downloads.py`` so every caller that runs a
``try_download_chain(...)`` cascade — the downloads route, the charts route's
on-demand "get" button, and the charts service's auto-download — persists a
``DownloadRequest`` row and spawns the right completion watcher the same way.

Keeping this in a service module (not a route) lets non-route callers reuse it
without a cross-route import.
"""

from __future__ import annotations

import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import DownloadRequest
from app.models.download_routing_schema import BackendName


async def persist_cascade(
    *,
    session: AsyncSession,
    cascade,
    spotify_id: str | None = None,
    track_title: str | None = None,
    artist_name: str | None = None,
    album_name: str | None = None,
    cover_url: str | None = None,
    requested_by: str | None = None,
    user_id: str | None = None,
) -> DownloadRequest:
    """Write a ``DownloadRequest`` row from a cascade outcome.

    ``requested_by`` is stored as-is (callers pass a hashed API key prefix for
    user-initiated downloads, or a sentinel like ``"__charts__"`` for automated
    ones). The full per-backend attempt log is persisted on ``attempts``.
    """
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
        requested_by=requested_by,
        user_id=user_id or None,
        error_message=err_msg,
        updated_at=int(time.time()),
    )
    session.add(record)
    await session.flush()
    return record


async def spawn_watcher(record: DownloadRequest, cascade) -> None:
    """Pick the right completion watcher based on which backend served the download.

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
