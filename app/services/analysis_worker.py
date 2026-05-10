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
import subprocess
import tempfile
import time
from pathlib import Path
from queue import Empty
from uuid import uuid4

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ONNX model registry (Essentia model zoo — dynamic-batch ONNX variants)
# ---------------------------------------------------------------------------

_ESSENTIA_MODELS_BASE = "https://essentia.upf.edu/models"

_ONNX_MODELS = {
    # Feature extractor backbone (dynamic batch size)
    "discogs-effnet-bsdynamic-1.onnx": "feature-extractors/discogs-effnet/discogs-effnet-bsdynamic-1.onnx",
    # Classification heads
    "danceability-discogs-effnet-1.onnx": "classification-heads/danceability/danceability-discogs-effnet-1.onnx",
    "mood_happy-discogs-effnet-1.onnx": "classification-heads/mood_happy/mood_happy-discogs-effnet-1.onnx",
    "mood_sad-discogs-effnet-1.onnx": "classification-heads/mood_sad/mood_sad-discogs-effnet-1.onnx",
    "mood_aggressive-discogs-effnet-1.onnx": "classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.onnx",
    "mood_relaxed-discogs-effnet-1.onnx": "classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.onnx",
    "mood_party-discogs-effnet-1.onnx": "classification-heads/mood_party/mood_party-discogs-effnet-1.onnx",
    "voice_instrumental-discogs-effnet-1.onnx": "classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.onnx",
    "approachability_regression-discogs-effnet-1.onnx": "classification-heads/approachability/approachability_regression-discogs-effnet-1.onnx",
}

# Expected SHA-256 digests for integrity verification after download.
# Computed from https://essentia.upf.edu/models/ on 2026-04-14.
_ONNX_MODEL_SHA256: dict[str, str] = {
    "discogs-effnet-bsdynamic-1.onnx": "a280825b334797cf677939db8cd5762c0392aedd0ca6415dbc1cd083f045e43c",
    "danceability-discogs-effnet-1.onnx": "9ce9b8c44f1dd5df5ffc124e5d41d67acf254232c1b90c7e057e079ab7cead73",
    "mood_happy-discogs-effnet-1.onnx": "0ca322819ef137b4b87e9866bffe7370a630e6f1165184ec106326cef6f81e06",
    "mood_sad-discogs-effnet-1.onnx": "1a50d11c23181bdfdeabc7d6032a2dad829dfc19c3776d2ee57ef1724cb08805",
    "mood_aggressive-discogs-effnet-1.onnx": "de36550b5d1660791ad732ed6de6ebfdc3e65dcf50b928b2578ddf103dbfb400",
    "mood_relaxed-discogs-effnet-1.onnx": "8ba6515a1e5943a72b3b475e3a25fc7a2ff04142c3eaa6aa0716fca371efdfff",
    "mood_party-discogs-effnet-1.onnx": "c50ac2106ec2f209dd04ad48756582df0e3f3512235310d1a4a3fcc453745f04",
    "voice_instrumental-discogs-effnet-1.onnx": "20155e4c439714b0c45c08644b73c8e12d9dccb173bd4ab9934bf1e5aee837ca",
    "approachability_regression-discogs-effnet-1.onnx": "f783b17ee994394f30f27d05ccd3fb845e589d107802e55e0b9cf1ea041ec894",
}

# ---------------------------------------------------------------------------
# Mel-spectrogram parameters (must match EffNet-Discogs training pipeline)
# ---------------------------------------------------------------------------

_EFFNET_SR = 16000  # EffNet expects 16 kHz mono
_FFT_SIZE = 1024
_HOP_SIZE = 256
_N_MELS = 128
_FREQ_MAX = 8000.0
_PATCH_FRAMES = 96  # ~1.5 s per patch at 16 kHz / 256 hop
_PATCH_HOP = 96  # non-overlapping patches

# ---------------------------------------------------------------------------
# Embedding projection: EffNet 400-dim → 64-dim (Johnson-Lindenstrauss)
# Fixed seed guarantees identical matrix across workers and restarts.
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 64
# Discogs-EffNet exposes two outputs: a 400-dim style classifier head
# (`activations`) and a 1280-dim trunk feature vector (`embeddings`). We
# project the trunk — the classifier head is sparse for tracks that don't
# match any of the 400 styles, which left ~37% of the library with NULL
# embeddings post-#42 (issue #83).
_EFFNET_DIM = 1280
_RNG = np.random.RandomState(seed=20240101)
_PROJ_MATRIX = (_RNG.randn(_EFFNET_DIM, _EMBEDDING_DIM) / np.sqrt(_EMBEDDING_DIM)).astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════
# Worker pool management (runs in main/FastAPI process)
# ═══════════════════════════════════════════════════════════════════════════

_pool: AnalysisWorkerPool | None = None
_pool_lock: asyncio.Lock | None = None


async def get_worker_pool() -> AnalysisWorkerPool:
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
        self._input_queue: mp.Queue | None = None
        self._output_queue: mp.Queue | None = None
        self._pending: dict[str, asyncio.Future] = {}
        # Tracks which worker is currently processing each in-flight request
        # so analyze()'s timeout path can SIGKILL the specific subprocess
        # that's stuck (e.g. spinning in libavcodec recovery on a corrupted
        # FLAC). See #30.
        self._in_flight: dict[str, dict] = {}
        self._collector_task: asyncio.Task | None = None
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
        cached: tuple | None = None,
    ) -> dict | None:
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
        except TimeoutError:
            self._pending.pop(request_id, None)
            self._kill_worker_holding(request_id, settings.ANALYSIS_TIMEOUT)
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
                future.set_result(
                    {
                        "analysis_error": "Worker pool shut down",
                        "analysis_version": "0",
                    }
                )
        self._pending.clear()
        self._in_flight.clear()
        logger.info("Analysis worker pool shut down")

    # -- internal ---------------------------------------------------------

    async def _collect_results(self) -> None:
        """Background task: drain output queue → resolve asyncio futures.

        Output-queue protocol is a 3-tuple ``(request_id, kind, payload)``:
          - ``kind="started"`` — payload is the worker subprocess pid.
            Recorded in ``_in_flight`` so a future timeout in ``analyze()``
            can SIGKILL the right subprocess. (#30)
          - ``kind="result"`` — payload is the analysis result dict. Clears
            the in-flight entry and resolves the asyncio future.
        """
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

            try:
                request_id, kind, payload = data
            except (TypeError, ValueError):
                continue  # malformed message from a crashing worker

            if kind == "started":
                pid = payload
                worker_index = next(
                    (i for i, w in enumerate(self._workers) if w.pid == pid),
                    None,
                )
                self._in_flight[request_id] = {
                    "worker_index": worker_index,
                    "pid": pid,
                    "started_at": time.monotonic(),
                }
            elif kind == "result":
                self._in_flight.pop(request_id, None)
                future = self._pending.pop(request_id, None)
                if future is not None and not future.done():
                    future.set_result(payload)

    def _blocking_get(self):
        """Block up to 1 s for a result (runs in thread via run_in_executor)."""
        return self._output_queue.get(timeout=1.0)

    def _kill_worker_holding(self, request_id: str, timeout_s: float) -> None:
        """SIGKILL the worker subprocess that's currently processing
        ``request_id``. No-op if the request finished or was never started.

        Called from ``analyze()`` when the per-task timeout fires. Without
        this, a worker spinning in C code (e.g. libavcodec on a corrupted
        FLAC bitstream — see #31) keeps holding the CPU after we've already
        returned a synthetic timeout to the caller, blocking the entire pool
        for many minutes per stuck task. (#30)
        """
        info = self._in_flight.pop(request_id, None)
        if info is None:
            return  # finished cleanly between deadline and lookup
        pid = info["pid"]
        worker_index = info["worker_index"]
        if worker_index is None or worker_index >= len(self._workers):
            return
        proc = self._workers[worker_index]
        # Only kill if this worker still holds that pid (race: a prior
        # respawn may have already replaced it).
        if proc.pid != pid or not proc.is_alive():
            return
        try:
            proc.kill()
            logger.warning(
                "Killed hung analysis worker %d (pid=%d) holding request %s after %.1fs",
                worker_index,
                pid,
                request_id,
                timeout_s,
            )
        except Exception as e:
            logger.warning("Failed to SIGKILL worker %d (pid=%d): %s", worker_index, pid, e)
            return
        # Drop any other in-flight entries pointing at the dead pid — the
        # work in flight on that worker is gone too.
        for rid, meta in list(self._in_flight.items()):
            if meta.get("pid") == pid:
                self._in_flight.pop(rid, None)
        # Trigger respawn now instead of waiting for the next idle tick.
        self._respawn_dead_workers()

    def _respawn_dead_workers(self) -> None:
        """Detect crashed workers and replace them."""
        if not self._running:
            return
        ctx = mp.get_context("spawn")
        for i, p in enumerate(self._workers):
            if not p.is_alive():
                logger.warning(
                    "Analysis worker %d died (exit=%s), respawning",
                    i,
                    p.exitcode,
                )
                # Reap the zombie before discarding the handle.
                try:
                    p.join(timeout=0)
                except Exception:
                    pass
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
    clap_audio_session = _init_clap_audio_session()

    # Pre-compute projection matrix (deterministic, same across all workers)
    rng = np.random.RandomState(seed=20240101)
    proj_matrix = (rng.randn(_EFFNET_DIM, _EMBEDDING_DIM) / np.sqrt(_EMBEDDING_DIM)).astype(np.float32)

    logger.info(
        "Analysis worker %d ready: essentia=%s, onnx_models=%d, clap_audio=%s",
        worker_id,
        has_essentia,
        len(onnx_sessions),
        clap_audio_session is not None,
    )

    my_pid = os.getpid()
    while True:
        item = input_queue.get()
        if item is None:
            break  # poison pill → graceful exit

        request_id, file_path, cached = item
        # Heartbeat so the pool's collector can record which worker (by pid)
        # is processing this request. Lets analyze()'s timeout path SIGKILL
        # the right subprocess if we hang in libavcodec C code. (#30)
        try:
            output_queue.put((request_id, "started", my_pid))
        except Exception:
            pass  # if the queue is broken we'll fail loudly on the result put

        try:
            if not has_essentia:
                result = {
                    "file_path": file_path,
                    "analysis_error": "Essentia not installed",
                    "analyzed_at": int(time.time()),
                    "analysis_version": _get_version(),
                }
            else:
                result = _analyze_file(
                    file_path,
                    cached,
                    es,
                    onnx_sessions,
                    proj_matrix,
                    clap_audio_session,
                )
        except Exception as e:
            result = {
                "file_path": file_path,
                "analysis_error": str(e),
                "analyzed_at": int(time.time()),
                "analysis_version": _get_version(),
            }

        output_queue.put((request_id, "result", result))


def _worker_init_env() -> None:
    """Configure worker process environment for optimal parallelism."""
    from app.core.config import settings

    omp = str(settings.ANALYSIS_OMP_THREADS)
    os.environ.update(
        {
            "OMP_NUM_THREADS": omp,
            "OPENBLAS_NUM_THREADS": omp,
            "MKL_NUM_THREADS": omp,
            "TF_CPP_MIN_LOG_LEVEL": "3",
            # Hide GPU from TF (if it somehow gets imported via essentia-tensorflow).
            # ONNX Runtime in this worker uses its own provider selection.
            "CUDA_VISIBLE_DEVICES": "",
        }
    )


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
            fd, tmp_path = tempfile.mkstemp(dir=models_dir, prefix=f".{filename}.", suffix=".tmp")
            os.close(fd)
            fd = None
            # `url` comes from the hardcoded _ONNX_MODELS dict (Essentia model zoo);
            # downloads are SHA-256 verified after fetch.
            urllib.request.urlretrieve(url, tmp_path)  # nosemgrep
            # Validate: must be a plausible ONNX file (starts with protobuf magic
            # bytes and is at least 1 KB).
            file_size = os.path.getsize(tmp_path)
            if file_size < 1024:
                raise ValueError(f"Downloaded file too small ({file_size} bytes)")
            # Compute SHA-256 and verify against known-good digest.
            sha = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
            digest = sha.hexdigest()
            expected = _ONNX_MODEL_SHA256.get(filename)
            if expected and digest != expected:
                raise ValueError(f"SHA-256 mismatch for {filename}: expected {expected[:16]}…, got {digest[:16]}…")
            # Atomic rename into place.
            os.rename(tmp_path, local_path)
            tmp_path = None  # prevent cleanup
            size_mb = file_size / 1024 / 1024
            logger.info("Downloaded: %s (%.1f MB, sha256=%s)", filename, size_mb, digest[:16])
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


def _build_onnx_providers(cache_dir: str) -> list:
    """Build the ONNX Runtime provider chain (OpenVINO → CUDA → CPU).

    ``cache_dir`` is used as the OpenVINO compile-cache location; pass a
    per-model directory if you want isolated caches.
    """
    backend = _detect_onnx_backend()
    providers: list = []
    if backend == "openvino":
        providers.append(
            (
                "OpenVINOExecutionProvider",
                {
                    "device_type": "GPU",
                    "precision": "FP16",
                    "cache_dir": cache_dir,
                },
            )
        )
    elif backend == "cuda":
        providers.append(
            (
                "CUDAExecutionProvider",
                {
                    "device_id": 0,
                    "arena_extend_strategy": "kSameAsRequested",
                    "gpu_mem_limit": 512 * 1024 * 1024,
                },
            )
        )
    providers.append("CPUExecutionProvider")
    return providers


def _init_clap_audio_session() -> object | None:
    """
    Load the CLAP audio tower ONNX session, if CLAP is enabled and the model
    file exists. Returns an ``onnxruntime.InferenceSession`` or ``None``.

    Auto-downloaded by ``app/services/clap_setup.py`` on first start (issue
    #91). Operators can pre-place the file in ``CLAP_MODEL_DIR`` for
    air-gapped installs. We fail soft here so workers still boot when CLAP
    is disabled or download failed.
    """
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        return None

    try:
        import onnxruntime as ort
    except ImportError:
        logger.warning("onnxruntime missing — CLAP audio encoding disabled")
        return None

    model_path = os.path.join(settings.CLAP_MODEL_DIR, settings.CLAP_AUDIO_MODEL_FILE)
    if not os.path.exists(model_path):
        logger.warning(
            "CLAP_ENABLED=true but audio model not found at %s — CLAP embeddings will be skipped",
            model_path,
        )
        return None

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = settings.ANALYSIS_ONNX_INTRA_THREADS
    opts.inter_op_num_threads = settings.ANALYSIS_ONNX_INTER_THREADS

    providers = _build_onnx_providers(os.path.join(settings.CLAP_MODEL_DIR, "_ov_cache"))

    try:
        session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=providers,
        )
        logger.info(
            "CLAP audio encoder loaded: %s, providers=%s",
            os.path.basename(model_path),
            session.get_providers(),
        )
        return session
    except Exception as e:
        logger.warning("Failed to load CLAP audio model: %s", e)
        return None


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

    sess_opts = ort.SessionOptions()
    sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    from app.core.config import settings

    sess_opts.intra_op_num_threads = settings.ANALYSIS_ONNX_INTRA_THREADS
    sess_opts.inter_op_num_threads = settings.ANALYSIS_ONNX_INTER_THREADS

    providers = _build_onnx_providers(os.path.join(models_dir, "_ov_cache"))
    backend = _detect_onnx_backend()

    sessions: dict = {}
    for filename in _ONNX_MODELS:
        path = os.path.join(models_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            sessions[filename] = ort.InferenceSession(
                path,
                sess_options=sess_opts,
                providers=providers,
            )
        except Exception as e:
            logger.warning("Failed to load ONNX model %s: %s", filename, e)

    actual = []
    if sessions:
        actual = next(iter(sessions.values())).get_providers()
    logger.info(
        "ONNX: %d/%d models loaded, backend=%s, providers=%s",
        len(sessions),
        len(_ONNX_MODELS),
        backend,
        actual,
    )
    return sessions


# ═══════════════════════════════════════════════════════════════════════════
# Analysis pipeline (runs inside worker process)
# ═══════════════════════════════════════════════════════════════════════════


def _analyze_file(
    file_path: str,
    cached: tuple | None,
    es,
    onnx_sessions: dict,
    proj_matrix: np.ndarray,
    clap_audio_session: object | None = None,
) -> dict | None:
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
            "title",
            "artist",
            "album",
            "album_artist",
            "track_number",
            "duration_ms",
            "genre",
            "musicbrainz_track_id",
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
        except (RuntimeError, FileNotFoundError) as e:
            if isinstance(e, FileNotFoundError) or "more than 2 channels" in str(e):
                # Essentia can't load this audio; downmix via ffmpeg
                try:
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as tmp:
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-i",
                            file_path,
                            "-ac",
                            "1",
                            "-ar",
                            str(sr),
                            "-f",
                            "wav",
                            tmp.name,
                        ]
                        proc = subprocess.run(  # noqa: S603 - fixed argv, internal file path
                            cmd,
                            capture_output=True,
                            timeout=60,
                            check=False,
                        )
                        if proc.returncode != 0:
                            raise RuntimeError(f"ffmpeg downmix failed: {proc.stderr.decode(errors='replace')}") from e
                        audio = es.MonoLoader(filename=tmp.name, sampleRate=sr)()
                    logger.info("Downmixed via ffmpeg: %s", file_path)
                except FileNotFoundError:
                    raise RuntimeError(
                        "ffmpeg not found — install ffmpeg to analyse multichannel/unsupported audio"
                    ) from e
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
            result,
            effnet_embedding,
            hpcp_frames,
            proj_matrix,
        )

        # --- CLAP audio embedding (optional, 512-dim) ---
        if clap_audio_session is not None:
            try:
                t0 = time.monotonic()
                clap_vec = _compute_clap_embedding(audio, sr, clap_audio_session, es)
                timings["clap"] = time.monotonic() - t0
                if clap_vec is not None:
                    result["clap_embedding"] = base64.b64encode(clap_vec.astype(np.float32).tobytes()).decode("ascii")
            except Exception as e:
                logger.debug("CLAP audio embedding failed for %s: %s", file_path, e)

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
    bpm = float(bpm)
    bpm_conf = float(bpm_conf)
    # RhythmExtractor2013 has known degenerate outputs (notably an exact
    # 738.28 BPM for very low-rhythm content) that escape the algorithm's
    # internal sanity checks. Clamp to a musically plausible range and
    # null otherwise rather than poisoning downstream features.
    if 30.0 <= bpm <= 250.0:
        result["bpm"] = round(bpm, 2)
        result["bpm_confidence"] = round(bpm_conf, 3)
    else:
        result["bpm"] = None
        result["bpm_confidence"] = None

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

    # --- Loudness & dynamic range (60 s centre sample, EBU R128 LUFS) ---
    # Previous code called es.Loudness() which is Stevens-law power-sum
    # (Σ(x²)^0.67) without normalisation by N — output scaled with the
    # length of the input and was nowhere near LUFS despite the comment
    # in the schema. LoudnessEBUR128 returns the proper integrated LUFS
    # value plus the EBU R128 loudness range, both in dB.
    dur = min(len(audio), sr * 60)
    start = max(0, (len(audio) - dur) // 2)
    sample = audio[start : start + dur]

    try:
        # EBUR128 expects stereo (vector_stereosample); duplicate the mono
        # channel. Both channels identical → integrated LUFS unchanged vs
        # true mono, but the algorithm now accepts the input.
        stereo = np.column_stack((sample, sample)).astype(np.float32)
        _, _, integrated_lufs, loudness_range = es.LoudnessEBUR128(sampleRate=sr)(stereo)
        result["loudness"] = round(float(integrated_lufs), 2)
        result["dynamic_range"] = round(float(loudness_range), 2)
    except Exception as e:
        # Fallback to RMS dBFS — also in dB, also bounded, just not
        # weighted to BS.1770. Keeps `loudness` semantically dB-shaped
        # if EBUR128 trips on a pathological input.
        logger.warning("LoudnessEBUR128 failed (%s); falling back to RMS dBFS", e)
        rms = float(np.sqrt(np.mean(sample.astype(np.float64) ** 2)))
        result["loudness"] = round(20.0 * np.log10(rms + 1e-10), 2)
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
) -> np.ndarray | None:
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
        patches.shape,
        input_meta.shape,
    )

    # Run all outputs so we can pick the right one for classifier heads.
    output_names = [o.name for o in effnet.get_outputs()]
    all_outputs = effnet.run(output_names, {input_name: patches})

    # Log all output shapes once (first track only).
    if not hasattr(_extract_ml, "_logged_outputs"):
        _extract_ml._logged_outputs = True
        for name, arr in zip(output_names, all_outputs):
            logger.warning("EffNet output '%s': shape=%s", name, arr.shape)

    # Find the 1280-dim trunk output. Used both for the external classifier
    # heads below and as the source of the FAISS embedding projection (issue
    # #83 — `all_outputs[0]` is the 400-dim style classifier head and is
    # sparse for off-genre tracks, producing degenerate zero-norm projections).
    head_embeddings = None
    for name, arr in zip(output_names, all_outputs):
        if arr.ndim == 2 and arr.shape[1] == 1280:
            head_embeddings = arr
            break

    # Defensive fallback: if the model variant doesn't expose a 1280-dim
    # output, project from `all_outputs[0]` instead so we still produce
    # *something*. Should never trigger with the current Discogs-EffNet ONNX.
    embedding_source = head_embeddings if head_embeddings is not None else all_outputs[0]
    mean_embedding = np.mean(embedding_source, axis=0)

    # --- Classifier heads (need 1280-dim embeddings) ---
    if head_embeddings is None:
        logger.warning("No 1280-dim EffNet output found — classifier heads skipped")
    else:
        heads = {
            "danceability": ("danceability-discogs-effnet-1.onnx", 0),
            "instrumentalness": ("voice_instrumental-discogs-effnet-1.onnx", 1),
            # Valence used to be mapped to approachability_regression here,
            # but that model (a) measures a different concept (approach-
            # ability ≠ Russell-circumplex valence) and (b) has an unbounded
            # regression head that emitted values up to 711. Valence is now
            # derived from the mood_happy probability after the mood loop
            # below, since that's the closest sigmoid-bounded match in the
            # Discogs-EffNet model family.
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
    # Column ordering for these binary softmax heads is alphabetical on the
    # class name. Models named with a positive-first class (happy < non_happy,
    # aggressive < non_aggressive) put the named class at col=0; models whose
    # negative class sorts first (non_sad < sad, non_relaxed < relaxed,
    # non_party < party) put the named class at col=1. Reading col=0 for all
    # of them — as the previous version did — meant `sad`, `relaxed`, and
    # `party` were stored as the *negation* of what the field name promised.
    mood_models = {
        "happy": ("mood_happy-discogs-effnet-1.onnx", 0),
        "sad": ("mood_sad-discogs-effnet-1.onnx", 1),
        "aggressive": ("mood_aggressive-discogs-effnet-1.onnx", 0),
        "relaxed": ("mood_relaxed-discogs-effnet-1.onnx", 1),
        "party": ("mood_party-discogs-effnet-1.onnx", 1),
    }
    mood_tags = []
    for label, (model_file, col) in mood_models.items():
        session = onnx_sessions.get(model_file)
        if session is None:
            continue
        if head_embeddings is None:
            continue
        try:
            inp = session.get_inputs()[0].name
            preds = session.run(None, {inp: head_embeddings})[0]
            conf = round(float(np.mean(preds[:, col])), 3)
            mood_tags.append({"label": label, "confidence": conf})
        except Exception as e:
            logger.warning("ONNX mood head %s failed: %s", model_file, e)
    if mood_tags:
        mood_tags.sort(key=lambda m: m["confidence"], reverse=True)
        result["mood_tags"] = mood_tags
        valence = _composite_valence({m["label"]: m["confidence"] for m in mood_tags})
        if valence is not None:
            result["valence"] = valence

    return mean_embedding


def _composite_valence(confs: dict[str, float]) -> float | None:
    """
    Composite valence proxy from EffNet mood-head confidences.

    The single `mood_happy` channel that v2.4/2.5 used is heavily compressed
    on real libraries — observed [0, 0.46] range with 88% of tracks below
    0.1 — so the ranker effectively can't use it (#88). `mood_sad` and
    `mood_aggressive` are similarly pinned at the low end, while
    `mood_party` is the only head with real spread across [0, 1]. This
    composite is the issue's option-3 "weighted combination" tuned to the
    actual signal each head carries: party dominates, happy is stretched
    2x to compensate for its compression, aggressive applies a mild
    penalty. `mood_sad` is dropped because (1 − sad) ≈ constant 1.0 across
    the library, contributing no information.

    Returns None when neither the full composite nor the happy fallback
    can be computed, so callers can leave the field unset.
    """
    if "party" in confs and "happy" in confs and "aggressive" in confs:
        raw = 0.6 * confs["party"] + 0.3 * min(1.0, confs["happy"] / 0.5) + 0.1 * (1.0 - confs["aggressive"])
        return round(max(0.0, min(1.0, raw)), 3)
    if "happy" in confs:
        # Fallback if the party/aggressive heads failed to load: the
        # stretched happy signal alone is still better than a hole.
        return round(min(1.0, confs["happy"] / 0.5), 3)
    return None


# ---------------------------------------------------------------------------
# Embedding builder
# ---------------------------------------------------------------------------


def _build_embedding(
    result: dict,
    effnet_embedding: np.ndarray | None,
    hpcp_frames: list,
    proj_matrix: np.ndarray,
) -> str | None:
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
            # Fallback: DSP-based embedding. bpm/loudness can be None now
            # (clamp-rejected BPM, EBUR128 fallback paths) so coalesce
            # explicitly — `dict.get(k, default)` only returns the default
            # when the key is missing, not when the stored value is None.
            bpm_v = result.get("bpm")
            if bpm_v is None:
                bpm_v = 120.0
            loud_v = result.get("loudness")
            if loud_v is None:
                loud_v = -14.0
            vec = np.zeros(_EMBEDDING_DIM, dtype=np.float32)
            vec[0] = min(1.0, bpm_v / 200.0)
            vec[1] = result.get("energy", 0.5)
            vec[2] = result.get("danceability", 0.5)
            vec[3] = result.get("valence", 0.5)
            vec[4] = result.get("acousticness", 0.5)
            vec[5] = result.get("instrumentalness", 0.5)
            vec[6] = result.get("speechiness", 0.1)
            vec[7] = min(1.0, max(0.0, (loud_v + 40) / 40))
            if hpcp_frames:
                mean_hpcp = np.mean(hpcp_frames, axis=0)[:12]
                vec[8 : 8 + len(mean_hpcp)] = mean_hpcp

        # L2 normalise (FAISS IndexFlatIP expects unit vectors)
        # If the source vector is degenerate (all-zero — happens occasionally
        # with EffNet on silent intros / sub-1s clips), return None rather than
        # base64-encoding the zero vector. Storing zeros silently breaks FAISS
        # and ~36% of our library went invisible to similarity search. (#42)
        norm = float(np.linalg.norm(vec))
        if norm < 1e-8:
            logger.debug("Embedding is degenerate (zero norm); returning None")
            return None
        vec = vec / norm

        return base64.b64encode(vec.astype(np.float32).tobytes()).decode("ascii")

    except Exception as e:
        logger.debug("Embedding build failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# CLAP audio embedding
# ---------------------------------------------------------------------------


# Per-process cache of the HF feature extractor. Lazily built on first use
# inside the worker, then reused for the lifetime of the worker.
_clap_feature_extractor = None


def _get_clap_feature_extractor():
    """Return a cached ``ClapFeatureExtractor`` configured for Xenova's
    ``larger_clap_music_and_speech`` export.

    Parameters are pinned to match the upstream ``preprocessor_config.json``
    (truncation=rand_trunc, frequency_min=50, …) so we don't need network
    access to ``from_pretrained`` at runtime.

    The Xenova export expects a 4-D ``(batch, 1, 1001, 64)`` mel input —
    not the 4-channel fusion stack used by ``clap-htsat-fused``. The
    ``rand_trunc`` truncation produces this shape directly.

    HuggingFace's ``feature_extraction_clap`` does ``import torch`` at
    module level (only used by ``_random_mel_fusion`` in fusion mode,
    which we never trigger). If torch is already importable — e.g. on the
    test runner where ``implicit`` and other ML deps pull it in — we use
    the real module. In the production image (no torch in requirements)
    we install a tiny no-op stub so the import succeeds; saves ~700 MB.
    """
    global _clap_feature_extractor
    if _clap_feature_extractor is not None:
        return _clap_feature_extractor

    import importlib.machinery
    import importlib.util
    import sys
    import types

    if "torch" not in sys.modules and importlib.util.find_spec("torch") is None:
        torch_stub = types.ModuleType("torch")
        torch_stub.__spec__ = importlib.machinery.ModuleSpec("torch", loader=None)
        torch_stub.__version__ = "0.0.0+grooveiq-stub"
        # scipy.array_api_compat / sklearn / lightgbm probe ``torch.Tensor``
        # via ``issubclass`` to detect torch arrays. Provide a sentinel class
        # so the probe returns False (correct: torch isn't really there)
        # instead of crashing with AttributeError.
        torch_stub.Tensor = type("Tensor", (), {})
        sys.modules["torch"] = torch_stub

    from transformers.models.clap.feature_extraction_clap import ClapFeatureExtractor

    _clap_feature_extractor = ClapFeatureExtractor(
        feature_size=64,
        sampling_rate=48000,
        hop_length=480,
        max_length_s=10,
        fft_window_size=1024,
        padding_value=0.0,
        frequency_min=50,
        frequency_max=14000,
        padding="repeatpad",
        truncation="rand_trunc",
    )
    return _clap_feature_extractor


def _compute_clap_embedding(
    audio: np.ndarray,
    sr: int,
    clap_session: object,
    es,
) -> np.ndarray | None:
    """
    Encode ``audio`` into a 512-dim CLAP embedding. Returns an L2-normalised
    float32 vector, or ``None`` on failure.

    Steps:
      1. Slice the central ``CLAP_AUDIO_CLIP_SECONDS`` of audio (bounded
         CPU/memory; also makes the embedding deterministic — without
         this, ``ClapFeatureExtractor`` random-crops longer audio).
      2. Resample from the source sample rate (16 kHz upstream from the
         EffNet pipeline) → ``CLAP_AUDIO_SR`` (default 48 kHz) via
         Essentia's Resample.
      3. Build the 4-D mel-spectrogram input ``(1, 1, 1001, 64)`` via
         HF's ``ClapFeatureExtractor`` — Xenova's ONNX export expects
         pre-computed log-mel features, not raw waveforms.
      4. Run through the CLAP audio encoder ONNX session.
      5. L2-normalise so the vector is comparable via dot-product to the
         text embeddings stored in ``TrackFeatures.clap_embedding``.
    """
    from app.core.config import settings

    clip_seconds = float(settings.CLAP_AUDIO_CLIP_SECONDS)
    target_sr = int(settings.CLAP_AUDIO_SR)
    target_len = int(clip_seconds * target_sr)

    # 1. Central slice at the source sample rate (bounds resample cost).
    src_clip_samples = int(clip_seconds * sr)
    if len(audio) > src_clip_samples:
        start = (len(audio) - src_clip_samples) // 2
        clip = audio[start : start + src_clip_samples]
    else:
        clip = audio

    # 2. Resample to the model's expected rate.
    if sr != target_sr:
        try:
            resample = es.Resample(inputSampleRate=sr, outputSampleRate=target_sr, quality=1)
            clip = resample(clip.astype(np.float32))
        except Exception as e:
            logger.debug("Resample to %d Hz failed, using raw: %s", target_sr, e)

    clip = np.asarray(clip, dtype=np.float32)

    # Hard-cap to target_len samples so the extractor takes the deterministic
    # pad path (audio == max_length, no random crop). Resample rounding can
    # produce ±1 sample of slack so we always truncate.
    if len(clip) > target_len:
        clip = clip[:target_len]

    # 3. Mel-spectrogram features. The extractor pads (via repeat-pad) to
    #    max_length_s, computes a log-mel spectrogram, and returns shape
    #    ``(batch=1, channels=1, height=1001, width=64)``.
    try:
        fe = _get_clap_feature_extractor()
        features = fe(
            clip,
            sampling_rate=target_sr,
            return_tensors="np",
        )
    except Exception as e:
        logger.debug("CLAP feature extraction failed: %s", e)
        return None

    input_features = np.asarray(features["input_features"], dtype=np.float32)

    # 4. Run model.
    inputs = clap_session.get_inputs()
    if not inputs:
        return None
    input_name = inputs[0].name

    try:
        outputs = clap_session.run(None, {input_name: input_features})
    except Exception as e:
        # Log shape on first failure so operators can diagnose export mismatches.
        if not hasattr(_compute_clap_embedding, "_logged_shape"):
            _compute_clap_embedding._logged_shape = True
            logger.warning(
                "CLAP inference failed: input_shape=%s, model_expects=%s, err=%s",
                input_features.shape,
                inputs[0].shape,
                e,
            )
        return None

    vec = np.asarray(outputs[0], dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm < 1e-9:
        return None
    return (vec / norm).astype(np.float32)
