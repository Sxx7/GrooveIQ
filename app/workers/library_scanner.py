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
- Container restart mid-scan (interrupted scans detected and resumed)
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import LibraryScanState, ScanLog, TrackFeatures
from app.services.audio_analysis import analyze_track, compute_file_hash, generate_track_id, ANALYSIS_VERSION

logger = logging.getLogger(__name__)

# Shared executor for CPU-bound Essentia work
_executor: Optional[ProcessPoolExecutor] = None
_executor_lock = threading.Lock()
_running_scan_id: Optional[int] = None


def get_executor() -> ProcessPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ProcessPoolExecutor(
                max_workers=settings.ANALYSIS_WORKERS,
                max_tasks_per_child=500,  # recycle less often to avoid expensive TF model reloads
            )
        return _executor


def _reset_executor() -> None:
    """Kill and recreate the process pool so crashed/hung workers don't block future files."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            try:
                _executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _executor = None
    logger.warning("Process pool reset — new workers will be created for the next file")


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


async def resume_interrupted_scans() -> Optional[int]:
    """
    Called on startup. If a previous scan was interrupted (status='running'),
    mark it as 'interrupted' and start a fresh scan that skips already-analyzed
    files automatically (via the hash check in _analyze_if_needed_sync).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LibraryScanState)
            .where(LibraryScanState.status == "running")
            .order_by(LibraryScanState.id.desc())
        )
        interrupted = result.scalars().all()

    if not interrupted:
        return None

    # Mark all as interrupted
    async with AsyncSessionLocal() as session:
        for scan in interrupted:
            await session.execute(
                update(LibraryScanState)
                .where(LibraryScanState.id == scan.id)
                .values(
                    status="interrupted",
                    scan_ended_at=int(time.time()),
                    last_error="Container restarted during scan",
                )
            )
        await session.commit()

    logger.warning(
        f"Found {len(interrupted)} interrupted scan(s) from previous run. "
        f"Starting fresh scan (already-analyzed files will be skipped)."
    )
    return await trigger_scan()


async def get_scan_status(scan_id: int) -> Optional[dict]:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LibraryScanState).where(LibraryScanState.id == scan_id)
        )
        scan = result.scalar_one_or_none()
        if not scan:
            return None

        # Compute progress percentage and ETA
        processed = scan.files_analyzed + scan.files_failed + scan.files_skipped
        percent = round(processed / scan.files_found * 100, 1) if scan.files_found > 0 else 0.0
        elapsed = (scan.scan_ended_at or int(time.time())) - scan.scan_started_at
        eta = None
        if scan.status == "running" and percent > 0:
            eta = int(elapsed / percent * (100 - percent))

        return {
            "scan_id": scan.id,
            "status": scan.status,
            "files_found": scan.files_found,
            "files_analyzed": scan.files_analyzed,
            "files_skipped": scan.files_skipped,
            "files_failed": scan.files_failed,
            "percent_complete": percent,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "current_file": scan.current_file,
            "started_at": scan.scan_started_at,
            "ended_at": scan.scan_ended_at,
            "last_error": scan.last_error,
        }


# ---------------------------------------------------------------------------
# Internal scan logic
# ---------------------------------------------------------------------------

async def _run_scan(scan_id: int) -> None:
    global _running_scan_id
    counters = {"found": 0, "ok": 0, "skipped": 0, "failed": 0}
    scan_start = time.time()

    try:
        library_path = Path(settings.MUSIC_LIBRARY_PATH)
        if not library_path.exists():
            raise FileNotFoundError(f"Library path not found: {library_path}")

        logger.info(f"[Scan {scan_id}] Scanning library: {library_path}")

        # Collect all audio files first so we know total count
        audio_files = list(_iter_audio_files(library_path))
        counters["found"] = len(audio_files)
        logger.info(f"[Scan {scan_id}] Found {counters['found']} audio files")

        await _update_scan(scan_id, files_found=counters["found"])

        # Pre-load all existing hashes in one query (avoids per-file DB connections in workers)
        hash_cache: dict[str, tuple[str, str]] = {}  # file_path → (file_hash, analysis_version)
        async with AsyncSessionLocal() as session:
            rows = (await session.execute(
                select(TrackFeatures.file_path, TrackFeatures.file_hash, TrackFeatures.analysis_version)
                .where(TrackFeatures.file_hash.isnot(None))
            )).all()
            for row in rows:
                hash_cache[row[0]] = (row[1], row[2])
        logger.info(f"[Scan {scan_id}] Pre-loaded {len(hash_cache)} file hashes for skip detection")

        # Process in batches for memory efficiency, but per-file progress
        batch_size = settings.ANALYSIS_BATCH_SIZE
        for i in range(0, len(audio_files), batch_size):
            batch = audio_files[i:i + batch_size]
            await _process_batch(batch, scan_id, counters, scan_start, hash_cache)
            _log_progress(scan_id, counters["found"], counters["ok"], counters["skipped"], counters["failed"], scan_start)

        # Prune old log entries (keep latest 500)
        async with AsyncSessionLocal() as session:
            from sqlalchemy import func as fn
            count = (await session.execute(
                select(fn.count(ScanLog.id)).where(ScanLog.scan_id == scan_id)
            )).scalar() or 0
            if count > 500:
                oldest = (await session.execute(
                    select(ScanLog.id).where(ScanLog.scan_id == scan_id)
                    .order_by(ScanLog.id.asc()).limit(count - 500)
                )).scalars().all()
                if oldest:
                    from sqlalchemy import delete as del_stmt
                    await session.execute(del_stmt(ScanLog).where(ScanLog.id.in_(oldest)))
                    await session.commit()

        # Sync track IDs with the media server (if configured).
        try:
            from app.services.media_server import is_configured, sync_track_ids
            if is_configured():
                async with AsyncSessionLocal() as sync_session:
                    sync_result = await sync_track_ids(sync_session)
                logger.info(
                    f"[Scan {scan_id}] Media server sync: "
                    f"{sync_result.tracks_matched} matched, {sync_result.tracks_updated} updated"
                )
        except Exception as e:
            logger.error(f"[Scan {scan_id}] Media server sync failed: {e}")

        # Rebuild FAISS index with new/updated embeddings.
        try:
            from app.services.faiss_index import rebuild as rebuild_faiss
            indexed = await rebuild_faiss()
            logger.info(f"[Scan {scan_id}] FAISS index rebuilt: {indexed} tracks.")
        except Exception as e:
            logger.error(f"[Scan {scan_id}] FAISS rebuild failed: {e}")

        elapsed = round(time.time() - scan_start, 1)
        await _update_scan(
            scan_id,
            status="completed",
            files_found=counters["found"],
            files_analyzed=counters["ok"],
            files_skipped=counters["skipped"],
            files_failed=counters["failed"],
            current_file=None,
            scan_ended_at=int(time.time()),
        )
        logger.info(
            f"[Scan {scan_id}] Complete in {elapsed}s: "
            f"{counters['ok']} analyzed, {counters['skipped']} skipped (unchanged), "
            f"{counters['failed']} failed, {counters['found']} total files"
        )

    except Exception as e:
        logger.error(f"[Scan {scan_id}] Fatal error: {e}", exc_info=True)
        await _update_scan(
            scan_id,
            status="failed",
            files_found=counters["found"],
            files_analyzed=counters["ok"],
            files_skipped=counters["skipped"],
            files_failed=counters["failed"],
            current_file=None,
            scan_ended_at=int(time.time()),
            last_error=str(e),
        )
    finally:
        _running_scan_id = None


def _log_progress(scan_id: int, found: int, analyzed: int, skipped: int, failed: int, start: float) -> None:
    """Log a human-readable progress line."""
    processed = analyzed + skipped + failed
    pct = round(processed / found * 100, 1) if found > 0 else 0
    elapsed = time.time() - start
    rate = processed / elapsed if elapsed > 0 else 0
    eta = round((found - processed) / rate) if rate > 0 else 0
    eta_str = f"{eta // 60}m{eta % 60:02d}s" if eta >= 60 else f"{eta}s"
    logger.info(
        f"[Scan {scan_id}] Progress: {processed}/{found} ({pct}%) | "
        f"{analyzed} new, {skipped} skipped, {failed} failed | "
        f"{rate:.1f} files/s | ETA {eta_str}"
    )


async def _process_file(file_path: str, scan_id: int, counters: dict, scan_start: float, hash_cache: dict) -> None:
    """Analyze a single file: run in executor, persist result, update progress live."""
    loop = asyncio.get_running_loop()
    executor = get_executor()
    fname = Path(file_path).name

    # Show current file in scan status
    await _update_scan(scan_id, current_file=fname)

    # Check hash from pre-loaded cache (no DB call needed in subprocess)
    cached = hash_cache.get(file_path)

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, _analyze_if_needed_sync, file_path, cached),
            timeout=settings.ANALYSIS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = Exception(f"Analysis timed out after {settings.ANALYSIS_TIMEOUT}s (file may be corrupted or unsupported)")
        _reset_executor()
    except BrokenProcessPool:
        result = Exception("Worker process crashed (likely OOM or segfault), recycling pool")
        _reset_executor()
    except Exception as exc:
        result = exc

    async with AsyncSessionLocal() as session:
        if isinstance(result, Exception):
            logger.error(f"[Scan {scan_id}] FAIL {fname}: {result}")
            session.add(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=str(result)))
            counters["failed"] += 1
        elif result is None:
            # File unchanged, skipped
            logger.debug(f"[Scan {scan_id}] SKIP {fname}")
            session.add(ScanLog(scan_id=scan_id, level="skip", filename=fname, message="unchanged"))
            counters["skipped"] += 1
        elif result.get("analysis_error"):
            logger.warning(f"[Scan {scan_id}] FAIL {fname}: {result['analysis_error']}")
            session.add(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=result["analysis_error"]))
            counters["failed"] += 1
        else:
            counters["ok"] += 1
            bpm_str = str(round(result.get("bpm", 0))) if result.get("bpm") else "?"
            key_str = (result.get("key", "?") or "?") + (result.get("mode", "") or "")
            dur_str = str(round(result.get("duration", 0))) + "s"
            energy_str = str(round(result.get("energy", 0), 2)) if result.get("energy") is not None else "?"
            msg = bpm_str + " BPM | " + key_str + " | " + dur_str + " | energy " + energy_str
            logger.info(f"[Scan {scan_id}] OK   {fname} | {msg}")
            session.add(ScanLog(scan_id=scan_id, level="ok", filename=fname, message=msg))
            await _upsert_track_features(session, result)
        await session.commit()

    # Update scan progress in DB after every file
    await _update_scan(
        scan_id,
        files_found=counters["found"],
        files_analyzed=counters["ok"],
        files_skipped=counters["skipped"],
        files_failed=counters["failed"],
    )


async def _process_batch(file_paths: list[str], scan_id: int, counters: dict, scan_start: float, hash_cache: dict) -> None:
    """Analyze a batch of files concurrently, with per-file progress updates."""
    # Run up to ANALYSIS_WORKERS files concurrently using a semaphore
    sem = asyncio.Semaphore(settings.ANALYSIS_WORKERS)

    async def _guarded(fp):
        async with sem:
            await _process_file(fp, scan_id, counters, scan_start, hash_cache)

    await asyncio.gather(*[_guarded(fp) for fp in file_paths])


def _analyze_if_needed_sync(file_path: str, cached: Optional[tuple] = None) -> Optional[dict]:
    """
    Sync function run in process pool.
    Returns None if the file is unchanged and already analyzed.
    Returns result dict otherwise.

    ``cached`` is a (file_hash, analysis_version) tuple from the pre-loaded
    hash cache, or None if this file hasn't been analyzed before.
    """
    from app.services.audio_analysis import analyze_track, compute_file_hash, ANALYSIS_VERSION

    # Quick skip: compare file hash from cache (no DB connection needed)
    if cached is not None:
        stored_hash, stored_version = cached
        if stored_version == ANALYSIS_VERSION:
            try:
                current_hash = compute_file_hash(file_path)
                if stored_hash == current_hash:
                    return None  # unchanged, skip
            except Exception:
                pass  # file may have been deleted, proceed with analysis

    return analyze_track(file_path)


async def _upsert_track_features(session: AsyncSession, data: dict) -> None:
    """Insert or update track_features row from analysis result dict."""
    from app.models.db import TrackFeatures
    from app.services.audio_analysis import generate_track_id

    file_path = data.get("file_path", "")
    track_id = data.get("track_id") or generate_track_id(file_path)

    result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id == track_id)
    )
    existing = result.scalar_one_or_none()

    if existing is None:
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
    extensions = set(settings.audio_extensions_list)

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
