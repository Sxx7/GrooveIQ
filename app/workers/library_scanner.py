"""
GrooveIQ – Library scanner worker (Phase 3).

Responsibilities:
1. Walk the music library directory recursively.
2. Detect new files (not yet in track_features) and changed files (hash mismatch).
3. Submit files to the analysis worker pool (separate long-lived processes).
4. Persist results to track_features table (batched per chunk).
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
import time
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import LibraryScanState, ScanLog, TrackFeatures
from app.services.audio_analysis import generate_track_id

logger = logging.getLogger(__name__)

# Active scan tracking
_running_scan_id: int | None = None

# In-memory scan metadata (phase tracking, processing timestamps).
# Avoids DB schema changes; lost on restart (acceptable — scan restarts anyway).
_scan_meta: dict = {}  # {phase, processing_started_at, check_elapsed, analyze_elapsed}


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def is_scan_running() -> bool:
    """Return True if a library scan is currently in progress."""
    return _running_scan_id is not None


async def trigger_scan() -> int:
    """
    Start a library scan in the background.
    Returns the scan_id.  If a scan is already running, returns its id.
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


async def resume_interrupted_scans() -> int | None:
    """
    Called on startup.  If a previous scan was interrupted (status='running'),
    mark it as 'interrupted' and start a fresh scan that skips already-analyzed
    files automatically (via the hash check in the worker).
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LibraryScanState).where(LibraryScanState.status == "running").order_by(LibraryScanState.id.desc())
        )
        interrupted = result.scalars().all()

    if not interrupted:
        return None

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


async def get_scan_status(scan_id: int) -> dict | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(LibraryScanState).where(LibraryScanState.id == scan_id))
        scan = result.scalar_one_or_none()
        if not scan:
            return None

        skipped = scan.files_skipped or 0
        analyzed = scan.files_analyzed
        failed = scan.files_failed
        processed = analyzed + failed + skipped
        percent = round(processed / scan.files_found * 100, 1) if scan.files_found > 0 else 0.0
        elapsed = (scan.scan_ended_at or int(time.time())) - scan.scan_started_at

        # --- Per-type rates for accurate ETA ---
        # Checking (hash compare + skip) and analysis have very different
        # speeds.  Computing a blended rate gives a wildly wrong ETA when
        # the mix shifts.  Instead, estimate remaining time from each type's
        # observed rate and the ratio seen so far.
        check_elapsed = _scan_meta.get("check_elapsed", 0.0)
        analyze_elapsed = _scan_meta.get("analyze_elapsed", 0.0)

        check_rate = skipped / check_elapsed if check_elapsed > 0 else None  # files/s
        analyze_rate = (analyzed + failed) / analyze_elapsed if analyze_elapsed > 0 else None

        remaining = scan.files_found - processed
        eta = None
        throughput = None

        if scan.status == "running" and processed > 0 and remaining > 0:
            # Project remaining files using the observed skip/analyze ratio
            skip_ratio = skipped / processed if processed > 0 else 1.0
            analyze_ratio = 1.0 - skip_ratio
            remaining_checks = remaining * skip_ratio
            remaining_analyses = remaining * analyze_ratio

            time_for_checks = remaining_checks / check_rate if check_rate else 0.0
            time_for_analyses = remaining_analyses / analyze_rate if analyze_rate else 0.0
            eta = int(time_for_checks + time_for_analyses)

            # Displayed throughput: total processed / total processing time
            total_proc_time = check_elapsed + analyze_elapsed
            throughput = round(processed / total_proc_time, 1) if total_proc_time > 0 else None

        phase = _scan_meta.get("phase", "idle") if scan.status == "running" else scan.status

        return {
            "scan_id": scan.id,
            "status": scan.status,
            "phase": phase,
            "files_found": scan.files_found,
            "files_analyzed": analyzed,
            "files_skipped": skipped,
            "files_failed": failed,
            "percent_complete": percent,
            "elapsed_seconds": elapsed,
            "eta_seconds": eta,
            "rate_per_sec": throughput,
            "check_rate": round(check_rate, 1) if check_rate else None,
            "analyze_rate": round(analyze_rate, 2) if analyze_rate else None,
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

        # Phase: discovering files on disk
        _scan_meta.update({"phase": "discovering", "processing_started_at": None})

        # Collect all audio files first so we know total count
        audio_files = list(_iter_audio_files(library_path))
        counters["found"] = len(audio_files)
        logger.info(f"[Scan {scan_id}] Found {counters['found']} audio files")

        await _update_scan(scan_id, files_found=counters["found"])

        # Pre-load all existing hashes in one query (avoids per-file DB round-trips)
        hash_cache: dict[str, tuple[str, str]] = {}  # file_path → (file_hash, analysis_version)
        async with AsyncSessionLocal() as session:
            rows = (
                await session.execute(
                    select(TrackFeatures.file_path, TrackFeatures.file_hash, TrackFeatures.analysis_version).where(
                        TrackFeatures.file_hash.isnot(None)
                    )
                )
            ).all()
            for row in rows:
                hash_cache[row[0]] = (row[1], row[2])
        logger.info(f"[Scan {scan_id}] Pre-loaded {len(hash_cache)} file hashes for skip detection")

        # Ensure the worker pool is running before processing
        from app.services.analysis_worker import get_worker_pool

        pool = await get_worker_pool()

        # Phase: processing files (checking existing + analyzing new/changed)
        _scan_meta["phase"] = "processing"
        _scan_meta["processing_started_at"] = time.time()

        # Process in batches, yielding to the event loop between batches
        batch_size = settings.ANALYSIS_BATCH_SIZE
        for i in range(0, len(audio_files), batch_size):
            batch = audio_files[i : i + batch_size]
            await _process_batch(batch, scan_id, counters, scan_start, hash_cache, pool)
            _log_progress(
                scan_id, counters["found"], counters["ok"], counters["skipped"], counters["failed"], scan_start
            )
            await asyncio.sleep(0.05)  # yield between batches

        # --- Post-scan phases (each timed for diagnostics) ---
        _scan_meta["phase"] = "finalizing"
        logger.info(f"[Scan {scan_id}] Post-scan: starting cleanup and rebuild phases")

        # Phase A: Prune old log entries (keep latest 500).
        t_phase = time.time()
        async with AsyncSessionLocal() as session:
            keep_subq = (
                select(ScanLog.id)
                .where(ScanLog.scan_id == scan_id)
                .order_by(ScanLog.id.desc())
                .limit(500)
                .correlate(None)
                .scalar_subquery()
            )
            from sqlalchemy import delete as del_stmt

            result = await session.execute(
                del_stmt(ScanLog).where(ScanLog.scan_id == scan_id).where(ScanLog.id.notin_(keep_subq))
            )
            if result.rowcount:
                await session.commit()
                logger.debug(f"[Scan {scan_id}] Pruned {result.rowcount} old log entries")
        logger.info(f"[Scan {scan_id}] Post-scan phase A (log prune): {time.time() - t_phase:.1f}s")
        await asyncio.sleep(0.05)

        # Phase B: Sync track IDs with the media server (if configured).
        t_phase = time.time()
        try:
            from app.services.media_server import is_configured, sync_track_ids

            if is_configured():
                async with AsyncSessionLocal() as sync_session:
                    sync_result = await sync_track_ids(sync_session)
                logger.info(
                    f"[Scan {scan_id}] Post-scan phase B (media sync): "
                    f"{sync_result.tracks_matched} matched, "
                    f"{sync_result.media_server_id_updated} ids updated, "
                    f"{sync_result.metadata_updated} metadata refreshed, "
                    f"{time.time() - t_phase:.1f}s"
                )
            else:
                logger.info(f"[Scan {scan_id}] Post-scan phase B (media sync): skipped (not configured)")
        except Exception as e:
            logger.error(
                f"[Scan {scan_id}] Post-scan phase B (media sync) failed after {time.time() - t_phase:.1f}s: {e}"
            )
        await asyncio.sleep(0.05)

        # Phase C: Rebuild FAISS index with new/updated embeddings.
        t_phase = time.time()
        try:
            from app.services.faiss_index import rebuild as rebuild_faiss

            indexed = await rebuild_faiss()
            logger.info(f"[Scan {scan_id}] Post-scan phase C (FAISS): {indexed} tracks, {time.time() - t_phase:.1f}s")
        except Exception as e:
            logger.error(f"[Scan {scan_id}] Post-scan phase C (FAISS) failed after {time.time() - t_phase:.1f}s: {e}")
        await asyncio.sleep(0.05)

        logger.info(f"[Scan {scan_id}] Post-scan: all phases complete")

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
        _scan_meta.clear()


def _log_progress(scan_id: int, found: int, analyzed: int, skipped: int, failed: int, start: float) -> None:
    """Log a human-readable progress line."""
    total_done = analyzed + skipped + failed
    pct = round(total_done / found * 100, 1) if found > 0 else 0
    remaining = found - total_done

    check_elapsed = _scan_meta.get("check_elapsed", 0.0)
    analyze_elapsed = _scan_meta.get("analyze_elapsed", 0.0)
    total_proc = check_elapsed + analyze_elapsed
    rate = total_done / total_proc if total_proc > 0 else 0

    # ETA from per-type rates
    check_rate = skipped / check_elapsed if check_elapsed > 0 else 0
    analyze_rate = (analyzed + failed) / analyze_elapsed if analyze_elapsed > 0 else 0
    skip_ratio = skipped / total_done if total_done > 0 else 1.0
    eta_checks = (remaining * skip_ratio / check_rate) if check_rate > 0 else 0
    eta_analysis = (remaining * (1 - skip_ratio) / analyze_rate) if analyze_rate > 0 else 0
    eta = round(eta_checks + eta_analysis)

    eta_str = f"{eta // 60}m{eta % 60:02d}s" if eta >= 60 else f"{eta}s"
    logger.info(
        f"[Scan {scan_id}] Progress: {total_done}/{found} ({pct}%) | "
        f"{analyzed} analyzed, {skipped} skipped, {failed} failed | "
        f"{rate:.1f} files/s | ETA {eta_str}"
    )


async def _process_batch(
    file_paths: list[str],
    scan_id: int,
    counters: dict,
    scan_start: float,
    hash_cache: dict,
    pool,
) -> None:
    """
    Analyze a batch of files concurrently via the worker pool, then
    persist all results in a single DB transaction.
    """
    sem = asyncio.Semaphore(pool._num_workers * 2)
    batch_results: list[tuple[str, object]] = []
    lock = asyncio.Lock()

    async def _analyze_one(fp: str):
        t_file = time.monotonic()
        async with sem:
            cached = hash_cache.get(fp)
            try:
                result = await pool.analyze(fp, cached)
            except Exception as exc:
                result = exc
            elapsed_file = time.monotonic() - t_file
            async with lock:
                batch_results.append((fp, result, elapsed_file))

    await asyncio.gather(*[_analyze_one(fp) for fp in file_paths])

    # --- Classify results and prepare DB writes ---
    to_upsert: list[dict] = []
    log_entries: list[ScanLog] = []

    for fp, result, elapsed_file in batch_results:
        fname = Path(fp).name

        if isinstance(result, Exception):
            logger.error("[Scan %d] FAIL %s: %s", scan_id, fname, result)
            log_entries.append(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=str(result)))
            counters["failed"] += 1
            _scan_meta["analyze_elapsed"] = _scan_meta.get("analyze_elapsed", 0.0) + elapsed_file

        elif result is None:
            logger.debug("[Scan %d] SKIP %s", scan_id, fname)
            counters["skipped"] += 1
            _scan_meta["check_elapsed"] = _scan_meta.get("check_elapsed", 0.0) + elapsed_file

        elif result.get("analysis_error"):
            logger.warning("[Scan %d] FAIL %s: %s", scan_id, fname, result["analysis_error"])
            log_entries.append(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=result["analysis_error"]))
            counters["failed"] += 1
            _scan_meta["analyze_elapsed"] = _scan_meta.get("analyze_elapsed", 0.0) + elapsed_file
            # Persist a "previously failed" marker iff we got far enough to
            # hash the file (deterministic failure for THIS file content —
            # too-short, unsupported codec, LOST_SYNC, etc.). The hash is
            # what powers _analyze_file's skip check, so on the next scan
            # the file is treated as already-known and not re-attempted.
            # Transient failures (worker timeout, pool shutdown, hash error)
            # never have file_hash set so they skip this branch and remain
            # eligible for retry. Downstream queries already filter
            # `analysis_error IS NOT NULL` (tracks.py / playlist_service /
            # clap_backfill) so these rows don't pollute recommendations.
            if result.get("file_hash"):
                to_upsert.append(result)

        else:
            counters["ok"] += 1
            _scan_meta["analyze_elapsed"] = _scan_meta.get("analyze_elapsed", 0.0) + elapsed_file
            to_upsert.append(result)
            bpm_str = str(round(result.get("bpm", 0))) if result.get("bpm") else "?"
            key_str = (result.get("key", "?") or "?") + (result.get("mode", "") or "")
            dur_str = str(round(result.get("duration", 0))) + "s"
            energy_str = str(round(result.get("energy", 0), 2)) if result.get("energy") is not None else "?"
            msg = f"{bpm_str} BPM | {key_str} | {dur_str} | energy {energy_str}"
            logger.info("[Scan %d] OK   %s | %s", scan_id, fname, msg)
            log_entries.append(ScanLog(scan_id=scan_id, level="ok", filename=fname, message=msg))

    # --- Batch DB write (single transaction per batch) ---
    if to_upsert or log_entries:
        async with AsyncSessionLocal() as session:
            for data in to_upsert:
                await _upsert_track_features(session, data)
            if log_entries:
                session.add_all(log_entries)
            await session.commit()

    # Update scan progress
    await _update_scan(
        scan_id,
        files_found=counters["found"],
        files_analyzed=counters["ok"],
        files_skipped=counters["skipped"],
        files_failed=counters["failed"],
    )


async def _upsert_track_features(session: AsyncSession, data: dict) -> None:
    """Insert or update track_features row from analysis result dict.

    The internal ``track_id`` is ALWAYS derived from the file's relative path
    via ``generate_track_id`` — never copied from the analysis worker's
    output. Historically, a numeric streamrip / Qobuz / Tidal service ID
    occasionally leaked in through ``data["track_id"]`` and ended up
    overwriting the canonical hash; issue #37 closes that hole.

    Any service-specific identifier the worker can extract (Spotify, Qobuz,
    Tidal, Deezer, SoundCloud, Navidrome) is welcome — it lands in its
    dedicated per-backend column thanks to the ``hasattr(TrackFeatures, k)``
    filter, since those columns now exist on the model.
    """
    file_path = data.get("file_path", "")
    track_id = generate_track_id(file_path)

    # Drop any track_id smuggled in via the analysis pipeline — the only
    # legitimate source of TrackFeatures.track_id is the file-path hash.
    data = {k: v for k, v in data.items() if k != "track_id"}

    result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id == track_id))
    existing = result.scalar_one_or_none()

    if existing is None:
        row = TrackFeatures(
            track_id=track_id, **{k: v for k, v in data.items() if hasattr(TrackFeatures, k)}
        )
        session.add(row)
    else:
        for k, v in data.items():
            if hasattr(existing, k) and k != "id":
                setattr(existing, k, v)


def _iter_audio_files(root: Path):
    """
    Yield absolute paths to audio files under root.
    Follows symlinks, de-duplicates by resolved path.

    Security: every resolved path is verified to be inside the library
    root, preventing symlink-based directory traversal.
    """
    seen = set()
    extensions = set(settings.audio_extensions_list)
    # Resolve the root itself once so symlink comparisons are consistent.
    resolved_root = root.resolve()

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        # Prune hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        # Check the directory itself is within the library root.
        try:
            resolved_dir = Path(dirpath).resolve()
        except OSError:
            dirnames.clear()
            continue
        if not str(resolved_dir).startswith(str(resolved_root)):
            dirnames.clear()
            continue

        for fname in filenames:
            if Path(fname).suffix.lower() not in extensions:
                continue
            full = Path(dirpath) / fname
            try:
                resolved = full.resolve()
            except OSError:
                continue
            # Reject files that resolve outside the library root.
            if not str(resolved).startswith(str(resolved_root)):
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            yield str(full)


async def _update_scan(scan_id: int, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(update(LibraryScanState).where(LibraryScanState.id == scan_id).values(**kwargs))
        await session.commit()
