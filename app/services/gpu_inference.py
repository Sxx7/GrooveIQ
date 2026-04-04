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
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ONNX model URLs (official Essentia model zoo — dynamic batch variants)
# ---------------------------------------------------------------------------

_ESSENTIA_MODELS_BASE = "https://essentia.upf.edu/models"

ONNX_MODELS = {
    # Feature extractor (dynamic batch size variant)
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
# Mel-spectrogram parameters matching EffNet-Discogs training pipeline
# ---------------------------------------------------------------------------

_EFFNET_SR = 16000         # EffNet expects 16 kHz mono
_FFT_SIZE = 512
_HOP_SIZE = 256
_N_MELS = 96
_FREQ_MIN = 0.0
_FREQ_MAX = 8000.0
_PATCH_FRAMES = 187        # ~3 s per patch at 16 kHz / 256 hop
_PATCH_HOP = 187           # non-overlapping patches (fastest)

# ---------------------------------------------------------------------------
# Singleton session cache
# ---------------------------------------------------------------------------

_sessions: dict[str, object] = {}
_onnx_available: Optional[bool] = None
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
    return ("CUDAExecutionProvider" in providers
            or "OpenVINOExecutionProvider" in providers)


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
    """Download any missing ONNX models. Returns True if all present."""
    models_dir = _get_onnx_dir()
    all_ok = True
    for filename, remote_path in ONNX_MODELS.items():
        local_path = os.path.join(models_dir, filename)
        if os.path.exists(local_path):
            continue
        url = f"{_ESSENTIA_MODELS_BASE}/{remote_path}"
        tmp_path = local_path + ".tmp"
        try:
            import urllib.request
            logger.info(f"Downloading ONNX model: {filename} ...")
            urllib.request.urlretrieve(url, tmp_path)
            os.rename(tmp_path, local_path)
            logger.info(f"Downloaded: {filename} ({os.path.getsize(local_path) / 1024 / 1024:.1f} MB)")
        except Exception as e:
            logger.warning(f"Failed to download ONNX model {filename}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            all_ok = False
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
        providers.append(("CUDAExecutionProvider", {
            "device_id": 0,
            "arena_extend_strategy": "kSameAsRequested",
            "gpu_mem_limit": 512 * 1024 * 1024,  # 512 MB cap
        }))
    elif backend == "openvino":
        import json
        providers.append(("OpenVINOExecutionProvider", {
            "device_type": "GPU",
            "precision": "FP16",
            "cache_dir": os.path.join(models_dir, "_ov_cache"),
        }))

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
        padded[:len(arr)] = arr
        return padded.T[np.newaxis, np.newaxis, :, :]  # (1, 1, 96, 187)

    frames_arr = np.array(frames, dtype=np.float32)  # (num_frames, 96)

    patches = []
    for i in range(0, len(frames_arr) - _PATCH_FRAMES + 1, _PATCH_HOP):
        patch = frames_arr[i:i + _PATCH_FRAMES].T  # (96, 187)
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
        audio = audio[start:start + max_samples]

    return _compute_melspec_patches(audio)


# ---------------------------------------------------------------------------
# Batched GPU inference
# ---------------------------------------------------------------------------

def infer_batch(file_paths: list[str], max_seconds: float = 15.0) -> list[dict]:
    """
    Run EffNet + classifier heads on a batch of files via ONNX Runtime.

    Args:
        file_paths: list of audio file paths
        max_seconds: max audio duration per file for mel-spec extraction

    Returns:
        list of dicts (one per file) with keys: danceability, instrumentalness,
        valence, speechiness, mood_tags.  On per-file failure, the dict contains
        'analysis_error' instead.
    """
    if not is_available():
        return [{"analysis_error": "onnxruntime not installed"}] * len(file_paths)

    if not ensure_onnx_models():
        return [{"analysis_error": "ONNX models not available"}] * len(file_paths)

    t_total = time.monotonic()

    # --- Step 1: Extract mel-specs per file (CPU) ---
    t0 = time.monotonic()
    per_file_patches: list[np.ndarray] = []       # patches per file
    file_patch_counts: list[int] = []              # how many patches each file contributed
    errors: list[Optional[str]] = [None] * len(file_paths)

    for i, fp in enumerate(file_paths):
        try:
            patches = extract_melspec_for_file(fp, max_seconds=max_seconds)
            per_file_patches.append(patches)
            file_patch_counts.append(len(patches))
        except Exception as e:
            per_file_patches.append(np.zeros((0, 1, _N_MELS, _PATCH_FRAMES), dtype=np.float32))
            file_patch_counts.append(0)
            errors[i] = str(e)

    t_melspec = time.monotonic() - t0

    # Concatenate all patches into one big batch
    valid_patches = [p for p in per_file_patches if len(p) > 0]
    if not valid_patches:
        return [{"analysis_error": errors[i] or "no valid patches"} for i in range(len(file_paths))]

    all_patches = np.concatenate(valid_patches, axis=0)  # (total_patches, 1, 96, 187)
    total_patches = all_patches.shape[0]

    # --- Step 2: EffNet forward pass (GPU) ---
    t0 = time.monotonic()
    effnet = _get_session("discogs-effnet-bsdynamic-1.onnx")
    input_name = effnet.get_inputs()[0].name

    # Run in sub-batches if needed (GPU memory limit)
    gpu_batch_size = int(os.environ.get("ANALYSIS_GPU_BATCH_SIZE", "64"))
    all_embeddings = []
    for start in range(0, total_patches, gpu_batch_size):
        batch = all_patches[start:start + gpu_batch_size]
        emb = effnet.run(None, {input_name: batch})[0]
        all_embeddings.append(emb)

    embeddings = np.concatenate(all_embeddings, axis=0)  # (total_patches, embed_dim)
    t_effnet = time.monotonic() - t0

    # --- Step 3: Classifier heads (GPU, very fast on embeddings) ---
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
            input_name = session.get_inputs()[0].name
            preds = session.run(None, {input_name: embeddings})[0]
            head_results[key] = preds[:, col]  # (total_patches,)
        except Exception as e:
            logger.warning(f"ONNX head {model_file} failed: {e}")

    t_heads = time.monotonic() - t0

    # --- Step 4: Aggregate per-file results ---
    results: list[dict] = []
    patch_offset = 0

    for i, fp in enumerate(file_paths):
        count = file_patch_counts[i]
        if errors[i] or count == 0:
            results.append({"analysis_error": errors[i] or "no patches extracted"})
            continue

        result: dict = {}
        file_slice = slice(patch_offset, patch_offset + count)

        # Average predictions across patches for this file
        for key, preds in head_results.items():
            val = float(np.mean(preds[file_slice]))
            if key.startswith("mood_"):
                continue  # handled below
            result[key] = round(val, 3)

        # Speechiness = 1 - instrumentalness
        if "instrumentalness" in result:
            result["speechiness"] = round(1.0 - result["instrumentalness"], 3)

        # Mood tags
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

    t_total_elapsed = time.monotonic() - t_total
    provider = _detect_backend().upper()
    logger.info(
        f"ONNX batch inference ({provider}): {len(file_paths)} files, "
        f"{total_patches} patches | "
        f"melspec={t_melspec:.1f}s effnet={t_effnet:.1f}s heads={t_heads:.1f}s "
        f"total={t_total_elapsed:.1f}s ({t_total_elapsed / max(len(file_paths), 1):.2f}s/file)"
    )

    return results
