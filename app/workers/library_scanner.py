"""
GrooveIQ – Library scanner worker (Phase 3).

Responsibilities:
1. Walk the music library directory recursively.
2. Detect new files (not yet in track_features) and changed files (hash mismatch).
3. Run audio analysis in a thread pool (CPU-bound, not async).
4. Persist results to track_features table.
5. Update LibraryScanState for progress reporting.

Edge cases handled:
- Symlinked directories (followed, de-duped by inode)
- Files that disappear mid-scan (logged, not fatal)
- Analysis crashes per file (isolated, rest of batch continues)
- Concurrent scan requests (second request returns existing running scan)
- Very large libraries (streaming walk, no full list held in memory)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import LibraryScanState, TrackFeatures
from app.services.audio_analysis import analyze_track, compute_file_hash, ANALYSIS_VERSION

logger = logging.getLogger(__name__)

# Shared executor for CPU-bound Essentia work
_executor: Optional[ProcessPoolExecutor] = None
_running_scan_id: Optional[int] = None


def get_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=settings.ANALYSIS_WORKERS)
    return _executor


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

async def trigger_scan() -> int:
    """
    Start a library scan in the background.
    Returns the scan_id. If a scan is already running, returns its id.
    """
    global _running_scan_id
    if _running_scan_id is not None:
        return _running_scan_id

    async with AsyncSessionLocal() as session:
        scan = LibraryScanState(
            scan_started_at=int(time.time()),
            status="running",
        )
        session.add(scan)
        await session.commit()
        await session.refresh(scan)
        scan_id = scan.id

    _running_scan_id = scan_id
    asyncio.create_task(_run_scan(scan_id))
    logger.info(f"Library scan started (id={scan_id})")
    return scan_id


async def get_scan_status(scan_id: int) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LibraryScanState).where(LibraryScanState.id == scan_id)
        )
        scan = result.scalar_one_or_none()
        if not scan:
            return None
        return {
            "scan_id": scan.id,
            "status": scan.status,
            "files_found": scan.files_found,
            "files_analyzed": scan.files_analyzed,
            "files_failed": scan.files_failed,
            "started_at": scan.scan_started_at,
            "ended_at": scan.scan_ended_at,
            "last_error": scan.last_error,
        }


# ---------------------------------------------------------------------------
# Internal scan logic
# ---------------------------------------------------------------------------

async def _run_scan(scan_id: int) -> None:
    global _running_scan_id
    files_found = 0
    files_analyzed = 0
    files_failed = 0
    last_error = None

    try:
        library_path = Path(settings.MUSIC_LIBRARY_PATH)
        if not library_path.exists():
            raise FileNotFoundError(f"Library path not found: {library_path}")

        # Stream files to avoid loading entire directory tree into memory
        audio_files = _iter_audio_files(library_path)

        batch = []
        for file_path in audio_files:
            files_found += 1
            batch.append(file_path)

            if len(batch) >= settings.ANALYSIS_BATCH_SIZE:
                a, f = await _process_batch(batch)
                files_analyzed += a
                files_failed += f
                batch = []

                # Update progress
                await _update_scan(scan_id, files_found=files_found,
                                   files_analyzed=files_analyzed, files_failed=files_failed)

        # Final batch
        if batch:
            a, f = await _process_batch(batch)
            files_analyzed += a
            files_failed += f

        await _update_scan(
            scan_id,
            status="completed",
            files_found=files_found,
            files_analyzed=files_analyzed,
            files_failed=files_failed,
            ended_at=int(time.time()),
        )
        logger.info(
            f"Scan {scan_id} complete: {files_analyzed} analyzed, "
            f"{files_failed} failed, {files_found} total"
        )

    except Exception as e:
        last_error = str(e)
        logger.error(f"Scan {scan_id} failed: {e}", exc_info=True)
        await _update_scan(
            scan_id,
            status="failed",
            files_found=files_found,
            files_analyzed=files_analyzed,
            files_failed=files_failed,
            ended_at=int(time.time()),
            last_error=last_error,
        )
    finally:
        _running_scan_id = None


async def _process_batch(file_paths: list[str]) -> tuple[int, int]:
    """Analyze a batch of files, persisting results to DB. Returns (ok, failed)."""
    loop = asyncio.get_running_loop()
    executor = get_executor()

    # Submit all files to the process pool concurrently
    futures = [
        loop.run_in_executor(executor, _analyze_if_needed_sync, fp)
        for fp in file_paths
    ]
    results = await asyncio.gather(*futures, return_exceptions=True)

    ok = failed = 0
    async with AsyncSessionLocal() as session:
        for fp, result in zip(file_paths, results):
            if isinstance(result, Exception):
                logger.error(f"Analysis exception for {fp}: {result}")
                failed += 1
                continue
            if result is None:
                # File unchanged, skipped
                continue
            if result.get("analysis_error"):
                failed += 1
            else:
                ok += 1

            await _upsert_track_features(session, result)
        await session.commit()

    return ok, failed


def _analyze_if_needed_sync(file_path: str) -> Optional[dict]:
    """
    Sync function run in process pool.
    Returns None if the file is unchanged and already analyzed.
    Returns result dict otherwise.
    """
    # Re-import needed in subprocess context
    from app.services.audio_analysis import analyze_track, compute_file_hash, ANALYSIS_VERSION
    import asyncio, time
    from pathlib import Path

    # Quick hash check via a synchronous DB connection
    # (We use a separate sync session here to avoid asyncio in subprocess)
    try:
        from sqlalchemy import create_engine, select as sync_select
        from app.core.config import settings as cfg
        from app.models.db import TrackFeatures as TF

        sync_url = cfg.DATABASE_URL.replace("+aiosqlite", "").replace("+asyncpg", "")
        engine = create_engine(sync_url, connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            row = conn.execute(
                sync_select(TF.file_hash, TF.analysis_version)
                .where(TF.file_path == file_path)
            ).fetchone()

        if row:
            current_hash = compute_file_hash(file_path)
            if row.file_hash == current_hash and row.analysis_version == ANALYSIS_VERSION:
                return None   # unchanged, skip

    except Exception:
        pass  # If check fails, proceed with analysis

    return analyze_track(file_path)


async def _upsert_track_features(session: AsyncSession, data: dict) -> None:
    """Insert or update track_features row from analysis result dict."""
    from app.models.db import TrackFeatures

    file_path = data.get("file_path", "")

    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.file_path == file_path)
    )
    existing = result.scalar_one_or_none()

    if existing is None:
        # Extract track_id from filename stem if not provided
        track_id = data.get("track_id") or Path(file_path).stem
        row = TrackFeatures(track_id=track_id, **{
            k: v for k, v in data.items()
            if hasattr(TrackFeatures, k) and k != "track_id"
        })
        session.add(row)
    else:
        for k, v in data.items():
            if hasattr(existing, k) and k not in ("id", "track_id"):
                setattr(existing, k, v)


def _iter_audio_files(root: Path):
    """
    Yield absolute paths to audio files under root.
    Follows symlinks, de-duplicates by resolved path.
    """
    seen = set()
    extensions = set(settings.AUDIO_EXTENSIONS)

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        # Prune hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for fname in filenames:
            if Path(fname).suffix.lower() not in extensions:
                continue
            full = Path(dirpath) / fname
            try:
                resolved = full.resolve()
            except OSError:
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            yield str(full)


async def _update_scan(scan_id: int, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(LibraryScanState)
            .where(LibraryScanState.id == scan_id)
            .values(**kwargs)
        )
        await session.commit()
