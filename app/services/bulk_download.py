"""
GrooveIQ -- Bulk artist download via Last.fm charts.

Fetches the top N artists from Last.fm's global charts, retrieves each
artist's top tracks, and downloads each via the configured ``bulk_per_track``
priority chain (streamrip → spotdl → spotizerr → slskd by default).

Bypasses Lidarr — Lidarr operates at album granularity and isn't a good fit
for "download these specific top tracks across many artists".
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import DownloadRequest

logger = logging.getLogger(__name__)

# In-memory state for the currently running bulk job (at most one at a time).
_current_job: BulkDownloadJob | None = None
_job_lock = asyncio.Lock()


@dataclass
class BulkDownloadJob:
    """Tracks progress of a bulk download run."""

    job_id: str
    started_at: int = field(default_factory=lambda: int(time.time()))
    status: str = "running"  # running | completed | failed | cancelled
    total_artists: int = 0
    artists_processed: int = 0
    total_tracks: int = 0
    tracks_searched: int = 0
    tracks_queued: int = 0
    tracks_skipped: int = 0
    tracks_failed: int = 0
    errors: list[str] = field(default_factory=list)
    current_artist: str = ""
    finished_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_artists": self.total_artists,
            "artists_processed": self.artists_processed,
            "total_tracks": self.total_tracks,
            "tracks_searched": self.tracks_searched,
            "tracks_queued": self.tracks_queued,
            "tracks_skipped": self.tracks_skipped,
            "tracks_failed": self.tracks_failed,
            "current_artist": self.current_artist,
            "errors": self.errors[-20:],  # last 20 errors
        }


def get_current_job() -> BulkDownloadJob | None:
    return _current_job


async def cancel_job() -> bool:
    global _current_job
    if _current_job and _current_job.status == "running":
        _current_job.status = "cancelled"
        return True
    return False


async def run_bulk_download(
    max_artists: int = 500,
    tracks_per_artist: int = 20,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Fetch top artists from Last.fm and download their top tracks via Soulseek.

    This is a long-running operation -- designed to be called from a background
    task.  Progress is available via ``get_current_job()``.
    """
    global _current_job

    async with _job_lock:
        if _current_job and _current_job.status == "running":
            return {"error": "A bulk download job is already running", "job": _current_job.to_dict()}

    job = BulkDownloadJob(job_id=f"bulk_{int(time.time())}")
    _current_job = job

    try:
        artists = await _fetch_top_artists(max_artists)
        job.total_artists = len(artists)
        logger.info("Bulk download: fetched %d artists from Last.fm", len(artists))

        if not artists:
            job.status = "failed"
            job.errors.append("No artists returned from Last.fm")
            job.finished_at = int(time.time())
            return job.to_dict()

        if not settings.download_enabled:
            job.status = "failed"
            job.errors.append("no download backend configured")
            job.finished_at = int(time.time())
            return job.to_dict()

        for artist_info in artists:
            if job.status == "cancelled":
                break

            artist_name = artist_info.get("name", "")
            if not artist_name:
                continue

            job.current_artist = artist_name

            try:
                await _process_artist(
                    artist_name=artist_name,
                    tracks_per_artist=tracks_per_artist,
                    job=job,
                    requested_by=requested_by,
                )
            except Exception as exc:
                msg = f"Error processing artist {artist_name}: {exc}"
                logger.warning(msg)
                job.errors.append(msg)

            job.artists_processed += 1

        if job.status == "running":
            job.status = "completed"
        job.finished_at = int(time.time())
        logger.info(
            "Bulk download finished: %d artists, %d tracks queued, %d skipped, %d failed",
            job.artists_processed,
            job.tracks_queued,
            job.tracks_skipped,
            job.tracks_failed,
        )
        return job.to_dict()

    except Exception as exc:
        job.status = "failed"
        job.errors.append(str(exc))
        job.finished_at = int(time.time())
        logger.error("Bulk download failed: %s", exc)
        return job.to_dict()


async def _fetch_top_artists(limit: int) -> list[dict[str, Any]]:
    """Fetch top artists from Last.fm charts, paginating if needed."""
    from app.services.lastfm_client import LastFmClient

    client = LastFmClient()
    try:
        artists: list[dict] = []
        page_size = min(limit, 250)  # Last.fm max per page
        pages_needed = (limit + page_size - 1) // page_size

        for page in range(1, pages_needed + 1):
            remaining = limit - len(artists)
            if remaining <= 0:
                break

            data = await client._get(
                "chart.getTopArtists",
                {"limit": str(min(page_size, remaining)), "page": str(page)},
            )
            page_artists = data.get("artists", {}).get("artist", [])
            if isinstance(page_artists, dict):
                page_artists = [page_artists]
            if not page_artists:
                break
            artists.extend(page_artists)

        return artists[:limit]
    finally:
        await client.close()


async def _process_artist(
    *,
    artist_name: str,
    tracks_per_artist: int,
    job: BulkDownloadJob,
    requested_by: str | None,
) -> None:
    """Fetch an artist's top tracks from Last.fm and queue each via the cascade."""
    from app.services.lastfm_client import LastFmClient

    client = LastFmClient()
    try:
        tracks = await client.get_artist_top_tracks(artist_name, limit=tracks_per_artist)
    finally:
        await client.close()

    job.total_tracks += len(tracks)

    for track in tracks:
        if job.status == "cancelled":
            return

        track_name = track.get("name", "")
        if not track_name:
            continue

        try:
            await _queue_track_via_cascade(
                artist=artist_name,
                title=track_name,
                job=job,
                requested_by=requested_by,
            )
        except Exception as exc:
            job.tracks_failed += 1
            msg = f"Failed: {artist_name} - {track_name}: {exc}"
            logger.debug(msg)
            job.errors.append(msg)


async def _queue_track_via_cascade(
    *,
    artist: str,
    title: str,
    job: BulkDownloadJob,
    requested_by: str | None,
) -> None:
    """Walk the bulk_per_track cascade for a single track."""
    from sqlalchemy import func, select

    from app.models.download_routing_schema import BackendName
    from app.services.download_chain import TrackRef, try_download_chain

    job.tracks_searched += 1

    # Skip if there's already a successful/in-flight download for this track,
    # regardless of which backend served it. Avoids duplicate queueing across
    # repeated runs.
    async with AsyncSessionLocal() as session:
        existing = (
            await session.execute(
                select(func.count())
                .select_from(DownloadRequest)
                .where(
                    DownloadRequest.artist_name == artist,
                    DownloadRequest.track_title == title,
                    DownloadRequest.status.in_(["queued", "downloading", "complete", "completed", "duplicate"]),
                )
            )
        ).scalar()
        if existing:
            job.tracks_skipped += 1
            return

    cascade = await try_download_chain(TrackRef(artist=artist, title=title), purpose="bulk_per_track")
    last = cascade.attempts[-1] if cascade.attempts else None
    source = cascade.final_backend or (last.backend if last else "none")
    status = cascade.final_status if cascade.success else (last.status if last else "error")
    err_msg = None if cascade.success else (last.error if last else "no backend succeeded")

    slskd_username = None
    slskd_filename = None
    slskd_transfer_id = None
    if cascade.success and cascade.final_backend == BackendName.SLSKD.value:
        slskd_username = cascade.final_extra.get("username")
        slskd_filename = cascade.final_extra.get("filename")
        slskd_transfer_id = cascade.final_task_id

    record_id: int | None = None
    async with AsyncSessionLocal() as session:
        record = DownloadRequest(
            task_id=cascade.final_task_id,
            status=status,
            source=source,
            track_title=title,
            artist_name=artist,
            slskd_username=slskd_username,
            slskd_filename=slskd_filename,
            slskd_transfer_id=slskd_transfer_id,
            attempts=[a.to_dict() for a in cascade.attempts] or None,
            requested_by=requested_by,
            error_message=err_msg,
            updated_at=int(time.time()),
        )
        session.add(record)
        await session.commit()
        record_id = record.id

    if not cascade.success:
        job.tracks_failed += 1
        job.errors.append(f"{artist} - {title}: {err_msg}")
        return

    if cascade.final_backend == BackendName.SLSKD.value and record_id is not None:
        from app.services.slskd_watcher import start_watcher as start_slskd_watcher

        await start_slskd_watcher(record_id)
    elif cascade.final_task_id:
        from app.services.download_watcher import start_watcher

        await start_watcher(cascade.final_task_id, source=cascade.final_backend)

    job.tracks_queued += 1
    logger.debug("Queued: %s - %s via %s", artist, title, source)
