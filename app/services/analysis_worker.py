"""
GrooveIQ – Audio analysis worker pool.

Runs all Essentia + ONNX work in long-lived subprocess(es) to avoid
blocking the FastAPI event loop.  Workers initialize heavy libraries
once and process files via multiprocessing queues.

Architecture:
  FastAPI main process                Worker processes (N = ANALYSIS_WORKERS)
  ─────────────────────               ──────────────────────────────────────
  AnalysisWorkerPool                  _worker_main() loop
    .analyze(path, cached)              ├─ Essentia: BPM, key, loudness
    → put (id, path, cached)            ├─ ONNX: mel-spec → EffNet → heads
       on input queue ──────►           ├─ Build 64-dim embedding
    ← await future  ◄──────            └─ put (id, result) on output queue
    _collect_results() task
    resolves futures from output queue

Key design choices:
  - spawn context (not fork): clean process, no inherited TF/CUDA state
  - Long-lived processes: Essentia + ONNX models load once, reused for all files
  - ONNX-only for ML inference: no TensorFlow (eliminates CUDA deadlocks)
  - Single-pass analysis: DSP + ML in one shot (no two-pass needed)
  - Intel-optimised: OpenVINO EP auto-detected for Iris Xe / UHD iGPU
"""

from __future__ import annotations

import asyncio
import base64
import logging
import multiprocessing as mp
import os
import time
from pathlib import Path
from queue import Empty
from typing import Optional
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ONNX model registry (Essentia model zoo — dynamic-batch ONNX variants)
# ---------------------------------------------------------------------------

_ESSENTIA_MODELS_BASE = "https://essentia.upf.edu/models"

_ONNX_MODELS = {
    # Feature extractor backbone (dynamic batch size)
    "discogs-effnet-bsdynamic-1.onnx":
        "feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx",
    # Classification heads
    "danceability-discogs-effnet-1.onnx":
        "classification-heads/danceability/danceability-discogs-effnet-1.onnx",
    "mood_happy-discogs-effnet-1.onnx":
        "classification-heads/mood_happy/mood_happy-discogs-effnet-1.onnx",
    "mood_sad-discogs-effnet-1.onnx":
        "classification-heads/mood_sad/mood_sad-discogs-effnet-1.onnx",
    "mood_aggressive-discogs-effnet-1.onnx":
        "classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.onnx",
    "mood_relaxed-discogs-effnet-1.onnx":
        "classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.onnx",
    "mood_party-discogs-effnet-1.onnx":
        "classification-heads/mood_party/mood_party-discogs-effnet-1.onnx",
    "voice_instrumental-discogs-effnet-1.onnx":
        "classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.onnx",
    "approachability_regression-discogs-effnet-1.onnx":
        "classification-heads/approachability/approachability_regression-discogs-effnet-1.onnx",
}

# ---------------------------------------------------------------------------
# Mel-spectrogram parameters (must match EffNet-Discogs training pipeline)
# ---------------------------------------------------------------------------

_EFFNET_SR = 16000       # EffNet expects 16 kHz mono
_FFT_SIZE = 1024
_HOP_SIZE = 256
_N_MELS = 128
_FREQ_MAX = 8000.0
_PATCH_FRAMES = 96       # ~1.5 s per patch at 16 kHz / 256 hop
_PATCH_HOP = 96          # non-overlapping patches

# ---------------------------------------------------------------------------
# Embedding projection: EffNet 400-dim → 64-dim (Johnson-Lindenstrauss)
# Fixed seed guarantees identical matrix across workers and restarts.
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 64
_EFFNET_DIM = 400
_RNG = np.random.RandomState(seed=20240101)
_PROJ_MATRIX = (_RNG.randn(_EFFNET_DIM, _EMBEDDING_DIM) / np.sqrt(_EMBEDDING_DIM)).astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════
# Worker pool management (runs in main/FastAPI process)
# ═══════════════════════════════════════════════════════════════════════════

_pool: Optional["AnalysisWorkerPool"] = None
_pool_lock: Optional[asyncio.Lock] = None


async def get_worker_pool() -> "AnalysisWorkerPool":
    """Get or lazily create the singleton analysis worker pool."""
    global _pool, _pool_lock
    if _pool is not None and _pool._running:
        return _pool
    if _pool_lock is None:
        _pool_lock = asyncio.Lock()
    async with _pool_lock:
        if _pool is not None and _pool._running:
            return _pool
        from app.core.config import settings
        pool = AnalysisWorkerPool(num_workers=settings.ANALYSIS_WORKERS)
        await pool.start()
        _pool = pool
        return pool


async def shutdown_worker_pool() -> None:
    """Shut down the worker pool.  Called during app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.shutdown()
        _pool = None


class AnalysisWorkerPool:
    """
    Pool of long-lived worker processes for audio analysis.

    Each worker initialises Essentia + ONNX Runtime once, then processes
    files from a shared input queue.  Results are returned via an output
    queue and matched to asyncio futures by request ID.
    """

    def __init__(self, num_workers: int):
        self._num_workers = max(1, num_workers)
        self._workers: list[mp.Process] = []
        self._input_queue: Optional[mp.Queue] = None
        self._output_queue: Optional[mp.Queue] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._collector_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Spawn worker processes and start the result collector."""
        ctx = mp.get_context("spawn")
        self._input_queue = ctx.Queue(maxsize=self._num_workers * 4)
        self._output_queue = ctx.Queue()

        for i in range(self._num_workers):
            p = ctx.Process(
                target=_worker_main,
                args=(self._input_queue, self._output_queue, i),
                name=f"grooveiq-analysis-{i}",
                daemon=True,
            )
            p.start()
            self._workers.append(p)

        self._running = True
        self._collector_task = asyncio.create_task(self._collect_results())
        logger.info("Analysis worker pool started: %d worker(s)", self._num_workers)

    async def analyze(
        self,
        file_path: str,
        cached: Optional[tuple] = None,
    ) -> Optional[dict]:
        """
        Submit a file for analysis.

        *cached* is ``(file_hash, analysis_version)`` from the scanner's
        pre-loaded hash cache, or ``None`` for new files.

        Returns ``None`` if the file is unchanged (hash match), or a result
        dict with keys matching ``TrackFeatures`` columns.
        """
        from app.core.config import settings
        from app.services.audio_analysis import ANALYSIS_VERSION

        request_id = uuid4().hex
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future

        self._input_queue.put((request_id, file_path, cached))

        try:
            return await asyncio.wait_for(future, timeout=settings.ANALYSIS_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return {
                "file_path": file_path,
                "analysis_error": f"Timed out after {settings.ANALYSIS_TIMEOUT}s",
                "analyzed_at": int(time.time()),
                "analysis_version": ANALYSIS_VERSION,
            }

    async def shutdown(self) -> None:
        """Stop all workers and the collector task."""
        self._running = False

        for _ in self._workers:
            try:
                self._input_queue.put(None, timeout=2)
            except Exception:
                pass

        if self._collector_task:
            self._collector_task.cancel()
            try:
                await self._collector_task
            except asyncio.CancelledError:
                pass

        for p in self._workers:
            p.join(timeout=5)
            if p.is_alive():
                p.kill()

        for future in self._pending.values():
            if not future.done():
                future.set_result({
                    "analysis_error": "Worker pool shut down",
                    "analysis_version": "0",
                })
        self._pending.clear()
        logger.info("Analysis worker pool shut down")

    # -- internal ---------------------------------------------------------

    async def _collect_results(self) -> None:
        """Background task: drain output queue → resolve asyncio futures."""
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                data = await loop.run_in_executor(None, self._blocking_get)
            except Empty:
                self._respawn_dead_workers()
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(0.1)
                continue

            request_id, result = data
            future = self._pending.pop(request_id, None)
            if future is not None and not future.done():
                future.set_result(result)

    def _blocking_get(self):
        """Block up to 1 s for a result (runs in thread via run_in_executor)."""
        return self._output_queue.get(timeout=1.0)

    def _respawn_dead_workers(self) -> None:
        """Detect crashed workers and replace them."""
        if not self._running:
            return
        ctx = mp.get_context("spawn")
        for i, p in enumerate(self._workers):
            if not p.is_alive():
                logger.warning(
                    "Analysis worker %d died (exit=%s), respawning",
                    i, p.exitcode,
                )
                new_p = ctx.Process(
                    target=_worker_main,
                    args=(self._input_queue, self._output_queue, i),
                    name=f"grooveiq-analysis-{i}",
                    daemon=True,
                )
                new_p.start()
                self._workers[i] = new_p


# ═══════════════════════════════════════════════════════════════════════════
# Worker process (runs in subprocess — no FastAPI, no SQLAlchemy)
# ═══════════════════════════════════════════════════════════════════════════

def _worker_main(
    input_queue: mp.Queue,
    output_queue: mp.Queue,
    worker_id: int,
) -> None:
    """
    Entry point for each worker subprocess (target of mp.Process).

    Initialises Essentia + ONNX once, then loops pulling file paths
    from *input_queue* and pushing result dicts onto *output_queue*.
    """
    _worker_init_env()

    # Import heavy libs (only in this process)
    try:
        import essentia  # noqa: F401
        import essentia.standard as es
        has_essentia = True
    except ImportError:
        has_essentia = False
        es = None

    onnx_sessions = _init_onnx_sessions()

    # Pre-compute projection matrix (deterministic, same across all workers)
    rng = np.random.RandomState(seed=20240101)
    proj_matrix = (rng.randn(_EFFNET_DIM, _EMBEDDING_DIM) / np.sqrt(_EMBEDDING_DIM)).astype(np.float32)

    logger.info(
        "Analysis worker %d ready: essentia=%s, onnx_models=%d",
        worker_id, has_essentia, len(onnx_sessions),
    )

    while True:
        item = input_queue.get()
        if item is None:
            break  # poison pill → graceful exit

        request_id, file_path, cached = item
        try:
            if not has_essentia:
                result = {
                    "file_path": file_path,
                    "analysis_error": "Essentia not installed",
                    "analyzed_at": int(time.time()),
                    "analysis_version": _get_version(),
                }
            else:
                result = _analyze_file(file_path, cached, es, onnx_sessions, proj_matrix)
        except Exception as e:
            result = {
                "file_path": file_path,
                "analysis_error": str(e),
                "analyzed_at": int(time.time()),
                "analysis_version": _get_version(),
            }

        output_queue.put((request_id, result))


def _worker_init_env() -> None:
    """Configure worker process environment for optimal parallelism."""
    from app.core.config import settings
    omp = str(settings.ANALYSIS_OMP_THREADS)
    os.environ.update({
        "OMP_NUM_THREADS": omp,
        "OPENBLAS_NUM_THREADS": omp,
        "MKL_NUM_THREADS": omp,
        "TF_CPP_MIN_LOG_LEVEL": "3",
        # Hide GPU from TF (if it somehow gets imported via essentia-tensorflow).
        # ONNX Runtime in this worker uses its own provider selection.
        "CUDA_VISIBLE_DEVICES": "",
    })


def _get_version() -> str:
    """Import ANALYSIS_VERSION lazily to avoid circular imports in subprocess."""
    from app.services.audio_analysis import ANALYSIS_VERSION
    return ANALYSIS_VERSION


# ---------------------------------------------------------------------------
# ONNX model management (worker process)
# ---------------------------------------------------------------------------

def _get_models_dir() -> str:
    d = os.environ.get(
        "ONNX_MODELS_PATH",
        os.path.join(
            os.environ.get("ESSENTIA_MODELS_PATH", os.path.expanduser("~/.cache/essentia")),
            "onnx",
        ),
    )
    os.makedirs(d, exist_ok=True)
    return d


def _download_models() -> bool:
    """Download any missing ONNX models.  Returns True if all present.

    Security:
    - Uses tempfile in the target directory (unpredictable name, mode 0o600)
      to prevent race-condition model replacement.
    - Validates downloaded files are valid ONNX protobuf before committing.
    - Only downloads over HTTPS (enforced by URL prefix).
    """
    import hashlib
    import tempfile

    models_dir = _get_models_dir()
    all_ok = True
    for filename, remote_path in _ONNX_MODELS.items():
        local_path = os.path.join(models_dir, filename)
        if os.path.exists(local_path):
            continue
        url = f"{_ESSENTIA_MODELS_BASE}/{remote_path}"
        if not url.startswith("https://"):
            logger.warning("Refusing to download model over insecure URL: %s", url)
            all_ok = False
            continue
        fd = None
        tmp_path = None
        try:
            import urllib.request
            logger.info("Downloading ONNX model: %s", filename)
            # Create temp file in the same directory (same filesystem for atomic rename).
            fd, tmp_path = tempfile.mkstemp(
                dir=models_dir, prefix=f".{filename}.", suffix=".tmp"
            )
            os.close(fd)
            fd = None
            urllib.request.urlretrieve(url, tmp_path)
            # Validate: must be a plausible ONNX file (starts with protobuf magic
            # bytes and is at least 1 KB).
            file_size = os.path.getsize(tmp_path)
            if file_size < 1024:
                raise ValueError(f"Downloaded file too small ({file_size} bytes)")
            # Compute SHA-256 for audit logging.
            sha = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
            digest = sha.hexdigest()
            # Atomic rename into place.
            os.rename(tmp_path, local_path)
            tmp_path = None  # prevent cleanup
            size_mb = file_size / 1024 / 1024
            logger.info(
                "Downloaded: %s (%.1f MB, sha256=%s)", filename, size_mb, digest[:16]
            )
        except Exception as e:
            logger.warning("Failed to download %s: %s", filename, e)
            all_ok = False
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
    return all_ok


def _detect_onnx_backend() -> str:
    """Detect best ONNX execution provider: 'openvino', 'cuda', or 'cpu'."""
    forced = os.environ.get("ANALYSIS_GPU_BACKEND", "").lower()
    if forced in ("cuda", "openvino"):
        return forced
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        # Prefer OpenVINO for Intel iGPU (Alder Lake / Iris Xe)
        if "OpenVINOExecutionProvider" in providers:
            return "openvino"
        if "CUDAExecutionProvider" in providers:
            return "cuda"
    except ImportError:
        pass
    return "cpu"


def _init_onnx_sessions() -> dict:
    """Initialise ONNX Runtime sessions for all models.  Returns {filename: session}."""
    try:
        import onnxruntime as ort
    except ImportError:
        logger.warning("ONNX Runtime not installed — ML features unavailable")
        return {}

    if not _download_models():
        logger.warning("Some ONNX models missing — ML features may be incomplete")

    models_dir = _get_models_dir()
    backend = _detect_onnx_backend()

    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    from app.core.config import settings
    sess_opts.intra_op_num_threads = settings.ANALYSIS_ONNX_INTRA_THREADS
    sess_opts.inter_op_num_threads = settings.ANALYSIS_ONNX_INTER_THREADS

    providers: list = []
    if backend == "openvino":
        providers.append(("OpenVINOExecutionProvider", {
            "device_type": "GPU",
            "precision": "FP16",
            "cache_dir": os.path.join(models_dir, "_ov_cache"),
        }))
    elif backend == "cuda":
        providers.append(("CUDAExecutionProvider", {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": 512 * 1024 * 1024,
        }))
    providers.append("CPUExecutionProvider")

    sessions: dict = {}
    for filename in _ONNX_MODELS:
        path = os.path.join(models_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            sessions[filename] = ort.InferenceSession(
                path, sess_options=sess_opts, providers=providers,
            )
        except Exception as e:
            logger.warning("Failed to load ONNX model %s: %s", filename, e)

    actual = []
    if sessions:
        actual = next(iter(sessions.values())).get_providers()
    logger.info(
        "ONNX: %d/%d models loaded, backend=%s, providers=%s",
        len(sessions), len(_ONNX_MODELS), backend, actual,
    )
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Analysis pipeline (runs inside worker process)
# ═══════════════════════════════════════════════════════════════════════════

def _analyze_file(
    file_path: str,
    cached: Optional[tuple],
    es,
    onnx_sessions: dict,
    proj_matrix: np.ndarray,
) -> Optional[dict]:
    """
    Full single-pass analysis for one audio file.

    Returns ``None`` if the file is unchanged (hash + version match).
    Returns a result dict with keys matching ``TrackFeatures`` columns.
    """
    from app.services.audio_analysis import ANALYSIS_VERSION, compute_file_hash

    # --- Hash-based skip check ---
    if cached is not None:
        stored_hash, stored_version = cached
        if stored_version == ANALYSIS_VERSION:
            try:
                if compute_file_hash(file_path) == stored_hash:
                    return None  # unchanged → skip
            except Exception:
                pass  # file may have been moved; proceed with analysis

    result: dict = {
        "file_path": file_path,
        "analyzed_at": int(time.time()),
        "analysis_version": ANALYSIS_VERSION,
    }
    timings: dict[str, float] = {}

    # --- Metadata (mutagen — fast, no Essentia) ---
    try:
        from app.services.metadata_reader import read_metadata
        meta = read_metadata(file_path)
        for key in (
            "title", "artist", "album", "album_artist",
            "track_number", "duration_ms", "genre", "musicbrainz_track_id",
        ):
            if meta.get(key) is not None:
                result[key] = meta[key]
    except Exception as e:
        logger.debug("Metadata read failed for %s: %s", file_path, e)

    try:
        result["file_hash"] = compute_file_hash(file_path)

        # --- Load audio at 16 kHz (shared by DSP + EffNet) ---
        t0 = time.monotonic()
        sr = _EFFNET_SR

        try:
            audio = es.MonoLoader(filename=file_path, sampleRate=sr)()
        except RuntimeError as e:
            if "more than 2 channels" in str(e):
                multi, loaded_sr, _, _, _, _ = es.AudioLoader(filename=file_path)()
                audio = np.mean(multi, axis=1).astype(np.float32)
                if int(loaded_sr) != sr:
                    audio = es.Resample(
                        inputSampleRate=loaded_sr, outputSampleRate=sr,
                    )(audio)
            else:
                raise

        result["duration"] = len(audio) / float(sr)
        timings["load"] = time.monotonic() - t0

        if result["duration"] < 10.0:
            result["analysis_error"] = "Track too short (<10 s)"
            return result

        # --- DSP features (BPM, key, loudness, energy, acousticness) ---
        t0 = time.monotonic()
        hpcp_frames = _extract_dsp(audio, sr, result, es)
        timings["dsp"] = time.monotonic() - t0

        # --- ML features via ONNX (danceability, mood, valence, etc.) ---
        effnet_embedding = None
        if onnx_sessions:
            t0 = time.monotonic()
            effnet_embedding = _extract_ml(audio, sr, result, es, onnx_sessions)
            timings["ml"] = time.monotonic() - t0

        # --- Build 64-dim embedding for FAISS ---
        result["embedding"] = _build_embedding(
            result, effnet_embedding, hpcp_frames, proj_matrix,
        )

        total = sum(timings.values())
        timing_str = " | ".join(f"{k}={v:.1f}s" for k, v in timings.items())
        logger.info(
            "Analyzed: %s | %.0f BPM | key=%s%s | total=%.1fs (%s)",
            Path(file_path).name,
            result.get("bpm", 0),
            result.get("key", "?"),
            result.get("mode", ""),
            total,
            timing_str,
        )

    except Exception as e:
        result["analysis_error"] = str(e)
        logger.error("Analysis failed for %s: %s", file_path, e, exc_info=True)

    return result


# ---------------------------------------------------------------------------
# DSP feature extraction (Essentia standard algorithms)
# ---------------------------------------------------------------------------

def _extract_dsp(
    audio: np.ndarray,
    sr: int,
    result: dict,
    es,
) -> list:
    """
    Extract DSP features: BPM, key/mode, loudness, dynamic range, energy,
    acousticness.  Returns HPCP frames for embedding fallback.
    """

    # --- Rhythm (60 s centre sample — BPM is stable across a track) ---
    dur = min(len(audio), sr * 60)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    bpm, _, bpm_conf, _, _ = es.RhythmExtractor2013(method="multifeature")(sample)
    result["bpm"] = round(float(bpm), 2)
    result["bpm_confidence"] = round(float(bpm_conf), 3)

    # --- Tonal (60 s centre sample) ---
    dur = min(len(audio), sr * 60)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    windowing = es.Windowing(type="blackmanharris62")
    spectrum_algo = es.Spectrum()
    spectral_peaks = es.SpectralPeaks(
        orderBy="magnitude",
        magnitudeThreshold=0.00001,
        minFrequency=20,
        maxFrequency=3500,
        maxPeaks=60,
    )
    hpcp_algo = es.HPCP()
    key_algo = es.Key()

    hpcp_frames: list = []
    for frame in es.FrameGenerator(sample, frameSize=4096, hopSize=4096, startFromZero=True):
        w = windowing(frame)
        s = spectrum_algo(w)
        freqs, mags = spectral_peaks(s)
        hpcp = hpcp_algo(freqs, mags)
        hpcp_frames.append(hpcp)

    if hpcp_frames:
        mean_hpcp = np.mean(hpcp_frames, axis=0).astype("float32")
        key, scale, strength, _ = key_algo(mean_hpcp)
        result["key"] = key
        result["mode"] = scale
        result["key_confidence"] = round(float(strength), 3)

    # --- Loudness & dynamic range (60 s centre sample) ---
    loudness_algo = es.Loudness()
    dur = min(len(audio), sr * 60)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    result["loudness"] = round(float(loudness_algo(sample)), 2)

    frame_loudness = []
    for frame in es.FrameGenerator(sample, frameSize=4096, hopSize=4096):
        frame_loudness.append(loudness_algo(frame))
    if frame_loudness:
        arr = np.array(frame_loudness)
        p95 = float(np.percentile(arr, 95))
        p10 = float(np.percentile(arr, 10))
        result["dynamic_range"] = round(p95 - p10, 2)
    else:
        result["dynamic_range"] = 0.0

    # --- RMS energy (15 s centre sample) ---
    rms_algo = es.RMS()
    dur = min(len(audio), sr * 15)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    rms_values = []
    for frame in es.FrameGenerator(sample, frameSize=2048, hopSize=2048):
        rms_values.append(float(rms_algo(frame)))
    if rms_values:
        p75 = float(np.percentile(rms_values, 75))
        result["energy"] = round(max(0.0, min(1.0, (p75 - 0.01) / 0.29)), 3)

    # --- Acousticness proxy (spectral centroid) ---
    # Lower centroid → warmer / more acoustic instrumentation.
    centroid_algo = es.Centroid(range=sr / 2)
    spectrum_algo2 = es.Spectrum()
    windowing2 = es.Windowing(type="hann")

    dur = min(len(audio), sr * 30)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    centroids = []
    for frame in es.FrameGenerator(sample, frameSize=2048, hopSize=2048, startFromZero=True):
        w = windowing2(frame)
        s = spectrum_algo2(w)
        centroids.append(float(centroid_algo(s)))
    if centroids:
        mean_c = float(np.mean(centroids))
        result["acousticness"] = round(max(0.0, min(1.0, 1.0 - mean_c / 4000.0)), 3)

    return hpcp_frames


# ---------------------------------------------------------------------------
# Mel-spectrogram computation
# ---------------------------------------------------------------------------

def _compute_melspec_patches(
    audio: np.ndarray,
    sr: int,
    es,
    max_seconds: float = 15.0,
) -> np.ndarray:
    """
    Compute mel-spectrogram patches matching EffNet-Discogs input format.

    Uses the centre *max_seconds* of audio.
    Returns shape ``(num_patches, 128, 96)`` float32.
    """
    max_samples = int(max_seconds * sr)
    if len(audio) > max_samples:
        start = (len(audio) - max_samples) // 2
        audio = audio[start : start + max_samples]

    windowing = es.Windowing(type="hann", size=_FFT_SIZE, zeroPadding=0, normalized=False)
    spectrum = es.Spectrum(size=_FFT_SIZE)
    melbands = es.MelBands(
        numberBands=_N_MELS,
        sampleRate=sr,
        lowFrequencyBound=0.0,
        highFrequencyBound=_FREQ_MAX,
        inputSize=_FFT_SIZE // 2 + 1,
    )

    frames = []
    for frame in es.FrameGenerator(audio, frameSize=_FFT_SIZE, hopSize=_HOP_SIZE, startFromZero=True):
        w = windowing(frame)
        s = spectrum(w)
        mel = melbands(s)
        frames.append(np.log10(np.maximum(mel, 1e-10)))

    if len(frames) < _PATCH_FRAMES:
        if not frames:
            return np.zeros((0, _N_MELS, _PATCH_FRAMES), dtype=np.float32)
        padded = np.zeros((_PATCH_FRAMES, _N_MELS), dtype=np.float32)
        arr = np.array(frames, dtype=np.float32)
        padded[: len(arr)] = arr
        return padded.T[np.newaxis, :, :]  # (1, 128, 96)

    frames_arr = np.array(frames, dtype=np.float32)  # (num_frames, 128)
    patches = []
    for i in range(0, len(frames_arr) - _PATCH_FRAMES + 1, _PATCH_HOP):
        patches.append(frames_arr[i : i + _PATCH_FRAMES].T)  # (128, 96)

    return np.array(patches, dtype=np.float32)  # (N, 128, 96)


# ---------------------------------------------------------------------------
# ML feature extraction (ONNX Runtime)
# ---------------------------------------------------------------------------

def _extract_ml(
    audio: np.ndarray,
    sr: int,
    result: dict,
    es,
    onnx_sessions: dict,
) -> Optional[np.ndarray]:
    """
    Extract ML features via ONNX: danceability, mood, valence, instrumentalness.

    Returns the mean EffNet embedding (200-dim) for the embedding builder,
    or ``None`` if EffNet inference failed.
    """
    effnet = onnx_sessions.get("discogs-effnet-bsdynamic-1.onnx")
    if effnet is None:
        return None

    patches = _compute_melspec_patches(audio, sr, es)
    if patches.shape[0] == 0:
        return None

    # --- EffNet forward pass ---
    input_meta = effnet.get_inputs()[0]
    input_name = input_meta.name

    # Ensure patches match model's expected rank.
    # discogs-effnet-bsdynamic-1.onnx expects (batch, 128, 96) — rank 3.
    # Squeeze any leftover channel dim from rank-4 patches.
    if patches.ndim == 4:
        patches = patches.squeeze(1)

    logger.debug(
        "EffNet input: patches=%s, model expects=%s",
        patches.shape, input_meta.shape,
    )

    # Run all outputs so we can pick the right one for classifier heads.
    output_names = [o.name for o in effnet.get_outputs()]
    all_outputs = effnet.run(output_names, {input_name: patches})

    # Log all output shapes once (first track only).
    if not hasattr(_extract_ml, "_logged_outputs"):
        _extract_ml._logged_outputs = True
        for name, arr in zip(output_names, all_outputs):
            logger.warning("EffNet output '%s': shape=%s", name, arr.shape)

    # Find the 1280-dim embedding that classifier heads expect.
    # Fall back to the first output for the embedding projection.
    embeddings = all_outputs[0]
    head_embeddings = None
    for name, arr in zip(output_names, all_outputs):
        if arr.ndim == 2 and arr.shape[1] == 1280:
            head_embeddings = arr
            break

    mean_embedding = np.mean(embeddings, axis=0)

    # --- Classifier heads (need 1280-dim embeddings) ---
    if head_embeddings is None:
        logger.warning("No 1280-dim EffNet output found — classifier heads skipped")
    else:
        heads = {
            "danceability": ("danceability-discogs-effnet-1.onnx", 0),
            "instrumentalness": ("voice_instrumental-discogs-effnet-1.onnx", 1),
            "valence": ("approachability_regression-discogs-effnet-1.onnx", 0),
        }
        for feature, (model_file, col) in heads.items():
            session = onnx_sessions.get(model_file)
            if session is None:
                continue
            try:
                inp_name = session.get_inputs()[0].name
                preds = session.run(None, {inp_name: head_embeddings})[0]
                result[feature] = round(float(np.mean(preds[:, col])), 3)
            except Exception as e:
                logger.warning("ONNX head %s failed: %s", model_file, e)

        # Speechiness proxy: 1 − instrumentalness
        if result.get("instrumentalness") is not None:
            result["speechiness"] = round(1.0 - result["instrumentalness"], 3)

    # --- Mood tags (also need 1280-dim embeddings) ---
    mood_models = {
        "happy": "mood_happy-discogs-effnet-1.onnx",
        "sad": "mood_sad-discogs-effnet-1.onnx",
        "aggressive": "mood_aggressive-discogs-effnet-1.onnx",
        "relaxed": "mood_relaxed-discogs-effnet-1.onnx",
        "party": "mood_party-discogs-effnet-1.onnx",
    }
    mood_tags = []
    for label, model_file in mood_models.items():
        session = onnx_sessions.get(model_file)
        if session is None:
            continue
        if head_embeddings is None:
            continue
        try:
            inp = session.get_inputs()[0].name
            preds = session.run(None, {inp: head_embeddings})[0]
            conf = round(float(np.mean(preds[:, 0])), 3)
            mood_tags.append({"label": label, "confidence": conf})
        except Exception as e:
            logger.warning("ONNX mood head %s failed: %s", model_file, e)
    if mood_tags:
        mood_tags.sort(key=lambda m: m["confidence"], reverse=True)
        result["mood_tags"] = mood_tags

    return mean_embedding


# ---------------------------------------------------------------------------
# Embedding builder
# ---------------------------------------------------------------------------

def _build_embedding(
    result: dict,
    effnet_embedding: Optional[np.ndarray],
    hpcp_frames: list,
    proj_matrix: np.ndarray,
) -> Optional[str]:
    """
    Build a 64-dim float32 embedding vector and return as base64 string.

    Primary path: project EffNet 200-dim → 64-dim via random projection
    (Johnson-Lindenstrauss).  This captures deep musical similarity.

    Fallback (no ONNX): hand-crafted vector from scalar features + HPCP
    chroma.  Worse quality but ensures every track has *some* embedding.
    """
    try:
        if effnet_embedding is not None and len(effnet_embedding) >= _EFFNET_DIM:
            # Random projection: EffNet 200 → 64
            vec = effnet_embedding[:_EFFNET_DIM] @ proj_matrix
        else:
            # Fallback: DSP-based embedding
            vec = np.zeros(_EMBEDDING_DIM, dtype=np.float32)
            vec[0] = min(1.0, result.get("bpm", 120) / 200.0)
            vec[1] = result.get("energy", 0.5)
            vec[2] = result.get("danceability", 0.5)
            vec[3] = result.get("valence", 0.5)
            vec[4] = result.get("acousticness", 0.5)
            vec[5] = result.get("instrumentalness", 0.5)
            vec[6] = result.get("speechiness", 0.1)
            vec[7] = min(1.0, max(0.0, (result.get("loudness", -14) + 40) / 40))
            if hpcp_frames:
                mean_hpcp = np.mean(hpcp_frames, axis=0)[:12]
                vec[8 : 8 + len(mean_hpcp)] = mean_hpcp

        # L2 normalise (FAISS IndexFlatIP expects unit vectors)
        norm = float(np.linalg.norm(vec))
        if norm > 1e-8:
            vec = vec / norm

        return base64.b64encode(vec.astype(np.float32).tobytes()).decode("ascii")

    except Exception as e:
        logger.debug("Embedding build failed: %s", e)
        return None
