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
import multiprocessing as mp
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
from app.services.audio_analysis import analyze_track, compute_file_hash, generate_track_id, ANALYSIS_VERSION, ANALYSIS_VERSION_DSP

logger = logging.getLogger(__name__)

# Shared executor for CPU-bound Essentia work
_executor: Optional[ProcessPoolExecutor] = None
_executor_lock = threading.Lock()
_running_scan_id: Optional[int] = None


def _worker_init():
    """Configure worker process for optimal TF/BLAS parallelism.

    Each worker gets a single compute thread so that N workers map 1:1 to
    N CPU cores instead of N×cores threads fighting for the same resources.
    Must run before TensorFlow is imported (spawn context guarantees this).
    """
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["TF_NUM_INTRAOP_PARALLELISM_THREADS"] = "2"
    os.environ["TF_NUM_INTEROP_PARALLELISM_THREADS"] = "1"
    # Suppress TF warnings/logs in workers
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
    # Hide GPUs from worker subprocesses.  Workers only do CPU work
    # (DSP analysis, mel-spec extraction).  GPU inference runs in the
    # main process via ONNX Runtime.  Without this, TF in the subprocess
    # tries to initialise CUDA and deadlocks with ONNX Runtime's CUDA
    # context in the main process.
    os.environ["CUDA_VISIBLE_DEVICES"] = ""


def get_executor() -> ProcessPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ProcessPoolExecutor(
                max_workers=settings.ANALYSIS_WORKERS,
                max_tasks_per_child=500,  # recycle less often to avoid expensive TF model reloads
                initializer=_worker_init,
                mp_context=mp.get_context("spawn"),
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

def is_scan_running() -> bool:
    """Return True if a library scan is currently in progress."""
    return _running_scan_id is not None


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

        two_pass = settings.ANALYSIS_TWO_PASS

        # --- Pass 1: DSP-only (fast) when two-pass is enabled ---
        if two_pass:
            logger.info(f"[Scan {scan_id}] Pass 1/2: fast DSP analysis (no TF models)")

        batch_size = settings.ANALYSIS_BATCH_SIZE
        for i in range(0, len(audio_files), batch_size):
            batch = audio_files[i:i + batch_size]
            await _process_batch(batch, scan_id, counters, scan_start, hash_cache, skip_tf=two_pass)
            _log_progress(scan_id, counters["found"], counters["ok"], counters["skipped"], counters["failed"], scan_start)

        # --- Pass 2: TF enrichment for tracks that only have DSP features ---
        if two_pass:
            dsp_elapsed = round(time.time() - scan_start, 1)
            logger.info(
                f"[Scan {scan_id}] Pass 1 done in {dsp_elapsed}s: "
                f"{counters['ok']} analyzed. Starting TF enrichment pass..."
            )
            # Find tracks that have DSP-only version and need TF enrichment
            tf_files: list[str] = []
            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    select(TrackFeatures.file_path)
                    .where(TrackFeatures.analysis_version == ANALYSIS_VERSION_DSP)
                )).scalars().all()
                tf_files = list(rows)

            if tf_files:
                tf_counters = {"found": len(tf_files), "ok": 0, "skipped": 0, "failed": 0}
                tf_start = time.time()

                # Try ONNX GPU batched path first, fall back to per-file TF
                use_gpu = settings.ANALYSIS_GPU
                if use_gpu:
                    try:
                        from app.services.gpu_inference import is_available
                        use_gpu = is_available()
                    except ImportError:
                        use_gpu = False

                if use_gpu:
                    await _run_gpu_enrichment(scan_id, tf_files, tf_counters, tf_start)
                else:
                    logger.info(f"[Scan {scan_id}] Pass 2/2: TF enrichment (CPU) for {len(tf_files)} tracks")
                    tf_hash_cache: dict[str, tuple[str, str]] = {}
                    for i in range(0, len(tf_files), batch_size):
                        batch = tf_files[i:i + batch_size]
                        await _process_batch(batch, scan_id, tf_counters, tf_start, tf_hash_cache, skip_tf=False)
                        _log_progress(scan_id, tf_counters["found"], tf_counters["ok"], tf_counters["skipped"], tf_counters["failed"], tf_start)

                counters["ok"] += tf_counters["ok"]
                counters["failed"] += tf_counters["failed"]

        # --- Post-scan phases (each timed for diagnostics) ---
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
                del_stmt(ScanLog)
                .where(ScanLog.scan_id == scan_id)
                .where(ScanLog.id.notin_(keep_subq))
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
                    f"{sync_result.tracks_matched} matched, {sync_result.tracks_updated} updated, "
                    f"{time.time() - t_phase:.1f}s"
                )
            else:
                logger.info(f"[Scan {scan_id}] Post-scan phase B (media sync): skipped (not configured)")
        except Exception as e:
            logger.error(f"[Scan {scan_id}] Post-scan phase B (media sync) failed after {time.time() - t_phase:.1f}s: {e}")
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


async def _run_gpu_enrichment(scan_id: int, tf_files: list[str], counters: dict, scan_start: float) -> None:
    """
    Run TF enrichment pass using ONNX Runtime GPU batched inference.

    Split into two executor phases per batch to keep the event loop alive:
      1. Mel-spec extraction → ProcessPoolExecutor  (CPU-bound; Essentia's
         SWIG bindings hold the GIL, so threads can't help)
      2. ONNX inference → default ThreadPoolExecutor (GPU-bound; ONNX
         Runtime releases the GIL during .run())

    Worker subprocesses have CUDA_VISIBLE_DEVICES="" so TF doesn't try
    to grab the GPU and deadlock with ONNX Runtime in the main process.
    """
    from app.services.gpu_inference import (
        gpu_detected, extract_melspecs_batch, infer_from_patches,
        ensure_onnx_models, is_available,
    )
    from app.services.audio_analysis import ANALYSIS_VERSION, generate_track_id

    if not is_available():
        logger.error(f"[Scan {scan_id}] ONNX Runtime not available, skipping GPU enrichment")
        return
    ensure_onnx_models()

    provider = "GPU" if gpu_detected() else "CPU/ONNX"
    logger.info(
        f"[Scan {scan_id}] Pass 2/2: {provider} batched enrichment for "
        f"{len(tf_files)} tracks (batch_size={settings.ANALYSIS_GPU_BATCH_SIZE})"
    )

    gpu_batch = settings.ANALYSIS_GPU_BATCH_SIZE
    total_batches = (len(tf_files) + gpu_batch - 1) // gpu_batch
    loop = asyncio.get_running_loop()
    proc_executor = get_executor()

    for batch_num, batch_start in enumerate(range(0, len(tf_files), gpu_batch)):
        batch_paths = tf_files[batch_start:batch_start + gpu_batch]

        await asyncio.sleep(0.05)  # yield before starting batch

        t_infer = time.monotonic()
        try:
            # Phase 1: mel-spec in ProcessPoolExecutor (separate GIL)
            mel_data = await asyncio.wait_for(
                loop.run_in_executor(proc_executor, extract_melspecs_batch, batch_paths),
                timeout=settings.ANALYSIS_TIMEOUT * len(batch_paths),
            )

            if mel_data["all_patches"] is None:
                for i, fp in enumerate(batch_paths):
                    err = mel_data["errors"][i] or "no valid patches"
                    logger.warning(f"[Scan {scan_id}] TF FAIL {Path(fp).name}: {err}")
                counters["failed"] += len(batch_paths)
                continue

            await asyncio.sleep(0)  # yield between phases

            # Phase 2: ONNX inference in ThreadPoolExecutor (GPU, releases GIL)
            results = await asyncio.wait_for(
                loop.run_in_executor(
                    None, infer_from_patches,
                    mel_data["all_patches"],
                    mel_data["file_patch_counts"],
                    mel_data["errors"],
                    batch_paths,
                ),
                timeout=settings.ANALYSIS_TIMEOUT * len(batch_paths),
            )
        except Exception as e:
            logger.error(f"[Scan {scan_id}] GPU batch inference failed: {e}")
            counters["failed"] += len(batch_paths)
            continue
        infer_s = time.monotonic() - t_infer

        # Persist results — collect updates and do one bulk commit.
        fail_logs: list[ScanLog] = []
        update_map: dict[str, dict] = {}

        for fp, result in zip(batch_paths, results):
            fname = Path(fp).name
            if result.get("analysis_error"):
                logger.warning(f"[Scan {scan_id}] TF FAIL {fname}: {result['analysis_error']}")
                fail_logs.append(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=result["analysis_error"]))
                counters["failed"] += 1
            else:
                track_id = generate_track_id(fp)
                vals = {k: v for k, v in result.items() if k not in ("id", "track_id", "file_path")}
                vals["analysis_version"] = ANALYSIS_VERSION
                update_map[track_id] = vals
                counters["ok"] += 1
                logger.debug(f"[Scan {scan_id}] TF OK  {fname}")

        t_db = time.monotonic()
        async with AsyncSessionLocal() as session:
            for track_id, vals in update_map.items():
                await session.execute(
                    update(TrackFeatures)
                    .where(TrackFeatures.track_id == track_id)
                    .values(**vals)
                )
            if fail_logs:
                session.add_all(fail_logs)
            await session.commit()
        db_s = time.monotonic() - t_db

        if batch_num % 10 == 0 or infer_s > 30 or db_s > 5:
            logger.info(
                f"[Scan {scan_id}] GPU batch {batch_num+1}/{total_batches}: "
                f"infer={infer_s:.1f}s db={db_s:.1f}s"
            )

        await asyncio.sleep(0.05)  # yield after DB work

        _log_progress(scan_id, counters["found"], counters["ok"], counters["skipped"], counters["failed"], scan_start)
        await _update_scan(
            scan_id,
            files_found=counters["found"],
            files_analyzed=counters["ok"],
            files_skipped=counters["skipped"],
            files_failed=counters["failed"],
        )


async def _process_file(file_path: str, scan_id: int, counters: dict, scan_start: float, hash_cache: dict, skip_tf: bool = False) -> None:
    """Analyze a single file: run in executor, persist result, update progress."""
    loop = asyncio.get_running_loop()
    executor = get_executor()
    fname = Path(file_path).name

    # Check hash from pre-loaded cache (no DB call needed in subprocess)
    cached = hash_cache.get(file_path)

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(executor, _analyze_if_needed_sync, file_path, cached, skip_tf),
            timeout=settings.ANALYSIS_TIMEOUT,
        )
    except asyncio.TimeoutError:
        result = Exception(f"Analysis timed out after {settings.ANALYSIS_TIMEOUT}s (file may be corrupted or unsupported)")
        # Don't reset the pool — the timed-out task is cancelled but other
        # workers keep running with their cached TF models intact.
    except BrokenProcessPool:
        result = Exception("Worker process crashed (likely OOM or segfault), recycling pool")
        _reset_executor()
    except Exception as exc:
        result = exc

    if isinstance(result, Exception):
        logger.error(f"[Scan {scan_id}] FAIL {fname}: {result}")
        async with AsyncSessionLocal() as session:
            session.add(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=str(result)))
            await session.commit()
        counters["failed"] += 1
    elif result is None:
        # File unchanged, skipped — only bump counter, no DB write.
        # Writing 20k+ skip rows per scan bloats the WAL and triggers
        # a massive prune DELETE that stalls SQLite.
        logger.debug(f"[Scan {scan_id}] SKIP {fname}")
        counters["skipped"] += 1
    elif result.get("analysis_error"):
        logger.warning(f"[Scan {scan_id}] FAIL {fname}: {result['analysis_error']}")
        async with AsyncSessionLocal() as session:
            session.add(ScanLog(scan_id=scan_id, level="fail", filename=fname, message=result["analysis_error"]))
            await session.commit()
        counters["failed"] += 1
    else:
        counters["ok"] += 1
        bpm_str = str(round(result.get("bpm", 0))) if result.get("bpm") else "?"
        key_str = (result.get("key", "?") or "?") + (result.get("mode", "") or "")
        dur_str = str(round(result.get("duration", 0))) + "s"
        energy_str = str(round(result.get("energy", 0), 2)) if result.get("energy") is not None else "?"
        msg = bpm_str + " BPM | " + key_str + " | " + dur_str + " | energy " + energy_str
        logger.info(f"[Scan {scan_id}] OK   {fname} | {msg}")
        async with AsyncSessionLocal() as session:
            session.add(ScanLog(scan_id=scan_id, level="ok", filename=fname, message=msg))
            await _upsert_track_features(session, result)
            await session.commit()

    # Update scan progress in DB every 10 files (reduces SQLite write contention)
    processed = counters["ok"] + counters["skipped"] + counters["failed"]
    if processed % 10 == 0 or processed == counters["found"]:
        await _update_scan(
            scan_id,
            current_file=fname,
            files_found=counters["found"],
            files_analyzed=counters["ok"],
            files_skipped=counters["skipped"],
            files_failed=counters["failed"],
        )


async def _process_batch(file_paths: list[str], scan_id: int, counters: dict, scan_start: float, hash_cache: dict, skip_tf: bool = False) -> None:
    """Analyze a batch of files concurrently, with per-file progress updates."""
    # Run up to ANALYSIS_WORKERS files concurrently using a semaphore
    sem = asyncio.Semaphore(settings.ANALYSIS_WORKERS)

    async def _guarded(fp):
        async with sem:
            await _process_file(fp, scan_id, counters, scan_start, hash_cache, skip_tf=skip_tf)

    await asyncio.gather(*[_guarded(fp) for fp in file_paths])


def _analyze_if_needed_sync(file_path: str, cached: Optional[tuple] = None, skip_tf: bool = False) -> Optional[dict]:
    """
    Sync function run in process pool.
    Returns None if the file is unchanged and already analyzed.
    Returns result dict otherwise.

    ``cached`` is a (file_hash, analysis_version) tuple from the pre-loaded
    hash cache, or None if this file hasn't been analyzed before.
    """
    from app.services.audio_analysis import analyze_track, compute_file_hash, ANALYSIS_VERSION, ANALYSIS_VERSION_DSP

    # Quick skip: compare file hash from cache (no DB connection needed)
    if cached is not None:
        stored_hash, stored_version = cached
        # Skip if already at target version (full or DSP-only depending on mode)
        target_version = ANALYSIS_VERSION_DSP if skip_tf else ANALYSIS_VERSION
        if stored_version == target_version or stored_version == ANALYSIS_VERSION:
            try:
                current_hash = compute_file_hash(file_path)
                if stored_hash == current_hash:
                    return None  # unchanged, skip
            except Exception:
                pass  # file may have been deleted, proceed with analysis

    return analyze_track(file_path, skip_tf=skip_tf)


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
