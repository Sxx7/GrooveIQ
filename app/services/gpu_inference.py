"""
GrooveIQ – GPU-accelerated TF model inference via ONNX Runtime.

Replaces the per-file TensorFlow inference in audio_analysis.py with
batched GPU inference using ONNX Runtime's CUDAExecutionProvider.

Architecture:
  1. Mel-spectrogram extraction (CPU, Essentia standard algos) — parallelised
  2. Batch all mel-spec patches into one tensor
  3. Single ONNX forward pass on GPU → embeddings for entire batch
  4. Run lightweight classifier heads on GPU (danceability, mood, etc.)

The key speedup comes from step 3: EffNet is a small CNN and GPU wins
are almost entirely from batching (32-64 patches per forward pass).

Falls back to CPU ONNX provider if no GPU is available.
"""

from __future__ import annotations

import logging
import os
import time

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ONNX model URLs (official Essentia model zoo — dynamic batch variants)
# ---------------------------------------------------------------------------

_ESSENTIA_MODELS_BASE = "https://essentia.upf.edu/models"

ONNX_MODELS = {
    # Feature extractor (dynamic batch size variant)
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
# Mel-spectrogram parameters matching EffNet-Discogs training pipeline
# ---------------------------------------------------------------------------

_EFFNET_SR = 16000  # EffNet expects 16 kHz mono
_FFT_SIZE = 512
_HOP_SIZE = 256
_N_MELS = 96
_FREQ_MIN = 0.0
_FREQ_MAX = 8000.0
_PATCH_FRAMES = 187  # ~3 s per patch at 16 kHz / 256 hop
_PATCH_HOP = 187  # non-overlapping patches (fastest)

# ---------------------------------------------------------------------------
# Singleton session cache
# ---------------------------------------------------------------------------

_sessions: dict[str, object] = {}
_onnx_available: bool | None = None
_models_dir: str = ""


def is_available() -> bool:
    """Check if ONNX Runtime (with optional GPU) is importable."""
    global _onnx_available
    if _onnx_available is None:
        try:
            import onnxruntime  # noqa: F401

            _onnx_available = True
        except ImportError:
            _onnx_available = False
    return _onnx_available


def gpu_detected() -> bool:
    """Check if any GPU provider (CUDA or OpenVINO) is available."""
    if not is_available():
        return False
    import onnxruntime as ort

    providers = ort.get_available_providers()
    return "CUDAExecutionProvider" in providers or "OpenVINOExecutionProvider" in providers


def _detect_backend() -> str:
    """Detect which GPU backend to use: 'cuda', 'openvino', or 'cpu'."""
    # Explicit override via env var
    forced = os.environ.get("ANALYSIS_GPU_BACKEND", "").lower()
    if forced in ("cuda", "openvino"):
        return forced

    if not is_available():
        return "cpu"

    import onnxruntime as ort

    providers = ort.get_available_providers()
    if "CUDAExecutionProvider" in providers:
        return "cuda"
    if "OpenVINOExecutionProvider" in providers:
        return "openvino"
    return "cpu"


def _get_onnx_dir() -> str:
    global _models_dir
    if not _models_dir:
        _models_dir = os.environ.get(
            "ONNX_MODELS_PATH",
            os.path.join(os.environ.get("ESSENTIA_MODELS_PATH", os.path.expanduser("~/.cache/essentia")), "onnx"),
        )
        os.makedirs(_models_dir, exist_ok=True)
    return _models_dir


def ensure_onnx_models() -> bool:
    """Download any missing ONNX models. Returns True if all present.

    Security: HTTPS-only, tempfile for atomic rename, SHA-256 verification.
    """
    import hashlib
    import tempfile

    models_dir = _get_onnx_dir()
    all_ok = True
    for filename, remote_path in ONNX_MODELS.items():
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
            fd, tmp_path = tempfile.mkstemp(dir=models_dir, prefix=f".{filename}.", suffix=".tmp")
            os.close(fd)
            fd = None
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            # `url` comes from the hardcoded _ONNX_MODELS dict (Essentia model zoo);
            # downloads are SHA-256 verified below.
            urllib.request.urlretrieve(url, tmp_path)
            file_size = os.path.getsize(tmp_path)
            if file_size < 1024:
                raise ValueError(f"Downloaded file too small ({file_size} bytes)")
            sha = hashlib.sha256()
            with open(tmp_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
            digest = sha.hexdigest()
            expected = _ONNX_MODEL_SHA256.get(filename)
            if expected and digest != expected:
                raise ValueError(f"SHA-256 mismatch for {filename}: expected {expected[:16]}…, got {digest[:16]}…")
            os.rename(tmp_path, local_path)
            tmp_path = None
            size_mb = file_size / 1024 / 1024
            logger.info("Downloaded: %s (%.1f MB, sha256=%s)", filename, size_mb, digest[:16])
        except Exception as e:
            logger.warning("Failed to download ONNX model %s: %s", filename, e)
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


def _get_session(model_file: str):
    """Get or create an ONNX Runtime inference session (cached)."""
    if model_file in _sessions:
        return _sessions[model_file]

    import onnxruntime as ort

    models_dir = _get_onnx_dir()
    path = os.path.join(models_dir, model_file)

    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess_options.intra_op_num_threads = 2
    sess_options.inter_op_num_threads = 1

    backend = _detect_backend()
    providers = []

    if backend == "cuda":
        providers.append(
            (
                "CUDAExecutionProvider",
                {
                    "device_id": 0,
                    "arena_extend_strategy": "kSameAsRequested",
                    "gpu_mem_limit": 512 * 1024 * 1024,  # 512 MB cap
                },
            )
        )
    elif backend == "openvino":
        providers.append(
            (
                "OpenVINOExecutionProvider",
                {
                    "device_type": "GPU",
                    "precision": "FP16",
                    "cache_dir": os.path.join(models_dir, "_ov_cache"),
                },
            )
        )

    providers.append("CPUExecutionProvider")

    session = ort.InferenceSession(path, sess_options=sess_options, providers=providers)
    _sessions[model_file] = session

    actual = session.get_providers()
    logger.info(f"ONNX session for {model_file}: backend={backend}, providers={actual}")
    return session


# ---------------------------------------------------------------------------
# Mel-spectrogram extraction (CPU, Essentia standard algos)
# ---------------------------------------------------------------------------


def _compute_melspec_patches(audio_16k: np.ndarray) -> np.ndarray:
    """
    Compute mel-spectrogram patches matching EffNet-Discogs input format.

    Args:
        audio_16k: mono audio at 16 kHz (float32 numpy array)

    Returns:
        np.ndarray of shape (num_patches, 1, 96, 187) — float32
        Returns empty array if audio is too short.
    """
    import essentia.standard as es

    windowing = es.Windowing(type="hann", size=_FFT_SIZE, zeroPadding=0, normalized=False)
    spectrum = es.Spectrum(size=_FFT_SIZE)
    melbands = es.MelBands(
        numberBands=_N_MELS,
        sampleRate=_EFFNET_SR,
        lowFrequencyBound=_FREQ_MIN,
        highFrequencyBound=_FREQ_MAX,
        inputSize=_FFT_SIZE // 2 + 1,
    )

    frames = []
    for frame in es.FrameGenerator(audio_16k, frameSize=_FFT_SIZE, hopSize=_HOP_SIZE, startFromZero=True):
        w = windowing(frame)
        s = spectrum(w)
        mel = melbands(s)
        frames.append(np.log10(np.maximum(mel, 1e-10)))

    if len(frames) < _PATCH_FRAMES:
        # Pad short audio to one patch
        if not frames:
            return np.zeros((0, 1, _N_MELS, _PATCH_FRAMES), dtype=np.float32)
        padded = np.zeros((_PATCH_FRAMES, _N_MELS), dtype=np.float32)
        arr = np.array(frames, dtype=np.float32)
        padded[: len(arr)] = arr
        return padded.T[np.newaxis, np.newaxis, :, :]  # (1, 1, 96, 187)

    frames_arr = np.array(frames, dtype=np.float32)  # (num_frames, 96)

    patches = []
    for i in range(0, len(frames_arr) - _PATCH_FRAMES + 1, _PATCH_HOP):
        patch = frames_arr[i : i + _PATCH_FRAMES].T  # (96, 187)
        patches.append(patch)

    return np.array(patches, dtype=np.float32)[:, np.newaxis, :, :]  # (N, 1, 96, 187)


def extract_melspec_for_file(file_path: str, max_seconds: float = 15.0) -> np.ndarray:
    """
    Load audio and compute mel-spectrogram patches for a single file.

    Uses the middle `max_seconds` of the track to limit processing time.
    Returns patches of shape (num_patches, 1, 96, 187).
    """
    import essentia.standard as es

    audio = es.MonoLoader(filename=file_path, sampleRate=_EFFNET_SR)()

    # Use middle portion only
    max_samples = int(max_seconds * _EFFNET_SR)
    if len(audio) > max_samples:
        start = (len(audio) - max_samples) // 2
        audio = audio[start : start + max_samples]

    return _compute_melspec_patches(audio)


# ---------------------------------------------------------------------------
# Batched GPU inference
# ---------------------------------------------------------------------------


def extract_melspecs_batch(file_paths: list[str], max_seconds: float = 15.0) -> dict:
    """
    CPU-bound mel-spectrogram extraction for a batch of files.

    Designed to run in a ProcessPoolExecutor so the GIL-heavy Essentia
    frame loop doesn't block the async event loop.

    Returns a dict with keys:
        all_patches: np.ndarray (total_patches, 1, 96, 187) or None
        file_patch_counts: list[int]
        errors: list[Optional[str]]
    """
    per_file_patches: list[np.ndarray] = []
    file_patch_counts: list[int] = []
    errors: list[str | None] = [None] * len(file_paths)

    for i, fp in enumerate(file_paths):
        try:
            patches = extract_melspec_for_file(fp, max_seconds=max_seconds)
            per_file_patches.append(patches)
            file_patch_counts.append(len(patches))
        except Exception as e:
            per_file_patches.append(np.zeros((0, 1, _N_MELS, _PATCH_FRAMES), dtype=np.float32))
            file_patch_counts.append(0)
            errors[i] = str(e)

    valid_patches = [p for p in per_file_patches if len(p) > 0]
    if not valid_patches:
        return {
            "all_patches": None,
            "file_patch_counts": file_patch_counts,
            "errors": errors,
        }

    return {
        "all_patches": np.concatenate(valid_patches, axis=0),
        "file_patch_counts": file_patch_counts,
        "errors": errors,
    }


def infer_from_patches(
    all_patches: np.ndarray,
    file_patch_counts: list[int],
    errors: list[str | None],
    file_paths: list[str],
) -> list[dict]:
    """
    GPU-bound ONNX inference on pre-extracted mel-spectrogram patches.

    Designed to run in a ThreadPoolExecutor — ONNX Runtime releases the
    GIL during inference so the async event loop stays responsive.

    Returns list of result dicts (one per file).
    """
    if not is_available():
        return [{"analysis_error": "onnxruntime not installed"}] * len(file_paths)

    if not ensure_onnx_models():
        return [{"analysis_error": "ONNX models not available"}] * len(file_paths)

    total_patches = all_patches.shape[0]

    # --- EffNet forward pass (GPU) ---
    t0 = time.monotonic()
    effnet = _get_session("discogs-effnet-bsdynamic-1.onnx")
    input_name = effnet.get_inputs()[0].name

    gpu_batch_size = int(os.environ.get("ANALYSIS_GPU_BATCH_SIZE", "64"))
    all_embeddings = []
    for start in range(0, total_patches, gpu_batch_size):
        batch = all_patches[start : start + gpu_batch_size]
        emb = effnet.run(None, {input_name: batch})[0]
        all_embeddings.append(emb)

    embeddings = np.concatenate(all_embeddings, axis=0)
    t_effnet = time.monotonic() - t0

    # --- Classifier heads (GPU, very fast on embeddings) ---
    t0 = time.monotonic()
    head_results = {}

    heads = {
        "danceability": ("danceability-discogs-effnet-1.onnx", "softmax", 0),
        "instrumentalness": ("voice_instrumental-discogs-effnet-1.onnx", "softmax", 1),
        "valence": ("approachability_regression-discogs-effnet-1.onnx", "identity", 0),
        "mood_happy": ("mood_happy-discogs-effnet-1.onnx", "softmax", 0),
        "mood_sad": ("mood_sad-discogs-effnet-1.onnx", "softmax", 0),
        "mood_aggressive": ("mood_aggressive-discogs-effnet-1.onnx", "softmax", 0),
        "mood_relaxed": ("mood_relaxed-discogs-effnet-1.onnx", "softmax", 0),
        "mood_party": ("mood_party-discogs-effnet-1.onnx", "softmax", 0),
    }

    for key, (model_file, output_type, col) in heads.items():
        try:
            session = _get_session(model_file)
            inp = session.get_inputs()[0].name
            preds = session.run(None, {inp: embeddings})[0]
            head_results[key] = preds[:, col]
        except Exception as e:
            logger.warning(f"ONNX head {model_file} failed: {e}")

    t_heads = time.monotonic() - t0

    # --- Aggregate per-file results ---
    results: list[dict] = []
    patch_offset = 0

    for i, fp in enumerate(file_paths):
        count = file_patch_counts[i]
        if errors[i] or count == 0:
            results.append({"analysis_error": errors[i] or "no patches extracted"})
            continue

        result: dict = {}
        file_slice = slice(patch_offset, patch_offset + count)

        for key, preds in head_results.items():
            val = float(np.mean(preds[file_slice]))
            if key.startswith("mood_"):
                continue
            result[key] = round(val, 3)

        if "instrumentalness" in result:
            result["speechiness"] = round(1.0 - result["instrumentalness"], 3)

        mood_tags = []
        for label in ["happy", "sad", "aggressive", "relaxed", "party"]:
            key = f"mood_{label}"
            if key in head_results:
                conf = float(np.mean(head_results[key][file_slice]))
                mood_tags.append({"label": label, "confidence": round(conf, 3)})
        if mood_tags:
            mood_tags.sort(key=lambda m: m["confidence"], reverse=True)
            result["mood_tags"] = mood_tags

        results.append(result)
        patch_offset += count

    provider = _detect_backend().upper()
    logger.info(
        f"ONNX batch inference ({provider}): {len(file_paths)} files, "
        f"{total_patches} patches | "
        f"effnet={t_effnet:.1f}s heads={t_heads:.1f}s"
    )

    return results


def infer_batch(file_paths: list[str], max_seconds: float = 15.0) -> list[dict]:
    """
    Run EffNet + classifier heads on a batch of files via ONNX Runtime.

    Combined mel-spec extraction + inference in one call. Used when both
    steps run in the same executor. For split execution (mel-spec in
    ProcessPool, inference in ThreadPool), use extract_melspecs_batch()
    and infer_from_patches() separately.
    """
    if not is_available():
        return [{"analysis_error": "onnxruntime not installed"}] * len(file_paths)

    if not ensure_onnx_models():
        return [{"analysis_error": "ONNX models not available"}] * len(file_paths)

    t_total = time.monotonic()

    mel_data = extract_melspecs_batch(file_paths, max_seconds=max_seconds)
    t_melspec = time.monotonic() - t_total

    if mel_data["all_patches"] is None:
        return [{"analysis_error": mel_data["errors"][i] or "no valid patches"} for i in range(len(file_paths))]

    results = infer_from_patches(
        mel_data["all_patches"],
        mel_data["file_patch_counts"],
        mel_data["errors"],
        file_paths,
    )

    t_total_elapsed = time.monotonic() - t_total
    logger.info(
        f"ONNX combined: melspec={t_melspec:.1f}s "
        f"total={t_total_elapsed:.1f}s ({t_total_elapsed / max(len(file_paths), 1):.2f}s/file)"
    )

    return results
