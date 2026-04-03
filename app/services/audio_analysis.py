"""
GrooveIQ – Audio analysis service (Phase 3).

Uses Essentia (https://essentia.upf.edu/) to extract acoustic features
from audio files in your local music library.

Feature extraction pipeline per track:
  1. Load audio → mono, 44.1 kHz
  2. Rhythm analysis   → BPM, beat positions, rhythm strength
  3. Tonal analysis    → key, mode, HPCP chroma
  4. Level analysis    → integrated loudness (EBU R128), dynamic range
  5. High-level        → energy, danceability, valence (via trained models)
  6. Mood              → multi-label classifier (happy/sad/aggressive/relaxed/etc.)
  7. Embed             → 64-dim feature vector for FAISS similarity search

Error handling:
  - Corrupted/unreadable files are logged and skipped (analysis_error set).
  - Short files (<10 s) get partial analysis (rhythm unreliable).
  - Re-analysis triggered when file_hash changes.

NOTE: Essentia must be installed in the container. The Dockerfile handles
this. If Essentia is unavailable, analysis falls back gracefully with a
warning, and feature columns remain null until the library is present.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy import – Essentia is a heavy optional dependency
try:
    import essentia
    import essentia.standard as es
    import numpy as np
    ESSENTIA_AVAILABLE = True
except ImportError:
    ESSENTIA_AVAILABLE = False
    logger.warning(
        "Essentia not installed. Audio analysis unavailable. "
        "Install with: pip install essentia-tensorflow  (or essentia for CPU-only)"
    )


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

ANALYSIS_VERSION = "1.3"  # fix energy calibration (RMS range 0.01–0.30), isolate TF model errors

# 64-dim vector composition (must match FAISS index dimension)
# [bpm_norm, energy, danceability, valence, acousticness,
#  instrumentalness, speechiness, loudness_norm,
#  chroma_12dims, mfcc_20dims, rhythm_features_10dims,
#  mood_onehot_8dims]
EMBEDDING_DIM = 64


# ---------------------------------------------------------------------------
# Model management
# ---------------------------------------------------------------------------

_ESSENTIA_MODELS_BASE = "https://essentia.upf.edu/models"

# Models we use: (local_filename, remote_path)
REQUIRED_MODELS = {
    "discogs-effnet-bs64-1.pb": "feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb",
    "danceability-discogs-effnet-1.pb": "classification-heads/danceability/danceability-discogs-effnet-1.pb",
    "mood_happy-discogs-effnet-1.pb": "classification-heads/mood_happy/mood_happy-discogs-effnet-1.pb",
    "mood_sad-discogs-effnet-1.pb": "classification-heads/mood_sad/mood_sad-discogs-effnet-1.pb",
    "mood_aggressive-discogs-effnet-1.pb": "classification-heads/mood_aggressive/mood_aggressive-discogs-effnet-1.pb",
    "mood_relaxed-discogs-effnet-1.pb": "classification-heads/mood_relaxed/mood_relaxed-discogs-effnet-1.pb",
    "mood_party-discogs-effnet-1.pb": "classification-heads/mood_party/mood_party-discogs-effnet-1.pb",
    "voice_instrumental-discogs-effnet-1.pb": "classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.pb",
    "approachability_regression-discogs-effnet-1.pb": "classification-heads/approachability/approachability_regression-discogs-effnet-1.pb",
    "engagement_regression-discogs-effnet-1.pb": "classification-heads/engagement/engagement_regression-discogs-effnet-1.pb",
}

_models_ready = False


def _get_models_dir() -> str:
    """Return the directory where Essentia model files are stored."""
    return os.environ.get("ESSENTIA_MODELS_PATH", os.path.expanduser("~/.cache/essentia"))


def ensure_models() -> bool:
    """
    Download any missing Essentia TF models to the models directory.
    Returns True if all models are available, False if download failed.
    Thread-safe: multiple workers may call this concurrently.
    """
    global _models_ready
    if _models_ready:
        return True

    models_dir = _get_models_dir()
    os.makedirs(models_dir, exist_ok=True)

    all_ok = True
    for filename, remote_path in REQUIRED_MODELS.items():
        local_path = os.path.join(models_dir, filename)
        if os.path.exists(local_path):
            continue

        url = f"{_ESSENTIA_MODELS_BASE}/{remote_path}"
        tmp_path = local_path + ".tmp"
        try:
            import urllib.request
            logger.info(f"Downloading Essentia model: {filename} ...")
            urllib.request.urlretrieve(url, tmp_path)
            os.rename(tmp_path, local_path)
            logger.info(f"Downloaded: {filename} ({os.path.getsize(local_path) / 1024 / 1024:.1f} MB)")
        except Exception as e:
            logger.warning(f"Failed to download model {filename}: {e}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            all_ok = False

    _models_ready = all_ok
    return all_ok


# ---------------------------------------------------------------------------
# File hashing
# ---------------------------------------------------------------------------

def compute_file_hash(path: str) -> str:
    """SHA-256 of file content. Used to detect file changes without re-running full analysis."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65_536), b""):
            h.update(chunk)
    return h.hexdigest()


def generate_track_id(file_path: str) -> str:
    """
    Generate a stable track_id from the file's path relative to the music library root.

    Uses SHA-256 of the relative path so that:
    - Two files with the same name in different folders get different IDs
    - The same file always gets the same ID across re-scans
    - IDs are a fixed 16-char hex string (collision-safe for any realistic library)
    """
    from app.core.config import settings
    try:
        rel = os.path.relpath(file_path, settings.MUSIC_LIBRARY_PATH)
    except ValueError:
        rel = file_path
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:16]


def analyze_track(file_path: str) -> dict:
    """
    Run the full Essentia analysis pipeline on a single audio file.

    Returns a dict with keys matching TrackFeatures columns.
    On failure, returns {'analysis_error': str, 'file_path': str}.
    """
    if not ESSENTIA_AVAILABLE:
        return {
            "file_path": file_path,
            "analysis_error": "Essentia not installed",
            "analyzed_at": int(time.time()),
            "analysis_version": ANALYSIS_VERSION,
        }

    result: dict = {
        "file_path": file_path,
        "analyzed_at": int(time.time()),
        "analysis_version": ANALYSIS_VERSION,
    }

    try:
        result["file_hash"] = compute_file_hash(file_path)

        # ------------------------------------------------------------------
        # Load audio
        # ------------------------------------------------------------------
        loader = es.MonoLoader(filename=file_path, sampleRate=44100)
        audio = loader()
        result["duration"] = len(audio) / 44100.0

        if result["duration"] < 10.0:
            result["analysis_error"] = "Track too short (<10 s), skipping"
            return result

        # ------------------------------------------------------------------
        # Rhythm
        # ------------------------------------------------------------------
        rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
        bpm, beats, bpm_confidence, _, _ = rhythm_extractor(audio)
        result["bpm"] = float(round(bpm, 2))
        result["bpm_confidence"] = float(round(bpm_confidence, 3))

        # ------------------------------------------------------------------
        # Tonal
        # ------------------------------------------------------------------
        # Compute HPCP (Harmonic Pitch Class Profile)
        windowing      = es.Windowing(type="blackmanharris62")
        spectrum       = es.Spectrum()
        spectral_peaks = es.SpectralPeaks(orderBy="magnitude", magnitudeThreshold=0.00001, minFrequency=20, maxFrequency=3500, maxPeaks=60)
        hpcp_extractor = es.HPCP()
        key_extractor  = es.Key()

        hpcp_frames = []
        for frame in es.FrameGenerator(audio, frameSize=4096, hopSize=2048, startFromZero=True):
            frame_windowed = windowing(frame)
            frame_spectrum = spectrum(frame_windowed)
            freqs, mags    = spectral_peaks(frame_spectrum)
            hpcp           = hpcp_extractor(freqs, mags)
            hpcp_frames.append(hpcp)

        if hpcp_frames:
            import numpy as np
            mean_hpcp = np.mean(hpcp_frames, axis=0)
            key, scale, key_strength, _ = key_extractor(mean_hpcp.astype("float32"))
            result["key"]            = key
            result["mode"]           = scale
            result["key_confidence"] = float(round(key_strength, 3))

        # ------------------------------------------------------------------
        # Loudness & dynamics
        # LoudnessEBUR128 requires stereo; we have mono from MonoLoader,
        # so use Loudness (Vickers) for integrated and per-frame for LRA.
        # ------------------------------------------------------------------
        import numpy as np

        integrated_loudness = es.Loudness()(audio)
        result["loudness"] = float(round(float(integrated_loudness), 2))

        # Approximate loudness range (LRA) from per-frame loudness
        frame_size = 2048
        hop_size = 1024
        frame_loudness = []
        for i in range(0, len(audio) - frame_size, hop_size):
            frame_l = es.Loudness()(audio[i : i + frame_size])
            frame_loudness.append(frame_l)
        if frame_loudness:
            arr = np.array(frame_loudness)
            p95 = float(np.percentile(arr, 95))
            p10 = float(np.percentile(arr, 10))
            result["dynamic_range"] = float(round(p95 - p10, 2))
        else:
            result["dynamic_range"] = 0.0

        # ------------------------------------------------------------------
        # High-level descriptors via Essentia pre-trained models
        # (requires essentia-tensorflow; falls back gracefully)
        # ------------------------------------------------------------------
        try:
            _extract_highlevel(audio, result)
        except Exception as e:
            logger.warning(f"High-level extraction failed: {e}")
            # Loudness-based energy fallback (no TF needed)
            loudness = result.get("loudness", -23)
            result["energy"] = float(round(max(0.0, min(1.0, (loudness + 30) / 30)), 3))

        # ------------------------------------------------------------------
        # Build embedding vector
        # ------------------------------------------------------------------
        result["embedding"] = _build_embedding(result, hpcp_frames)

        logger.info(f"Analyzed: {Path(file_path).name} | {result.get('bpm')} BPM | key={result.get('key')}{result.get('mode', '')}")

    except Exception as e:
        result["analysis_error"] = str(e)
        logger.error(f"Analysis failed for {file_path}: {e}", exc_info=True)

    return result


def _extract_highlevel(audio, result: dict) -> None:
    """
    Extract energy, danceability, valence, mood using Essentia's
    pre-trained TensorFlow models (MTG-Jamendo, Discogs-EffNet).

    Models are auto-downloaded on first run to ESSENTIA_MODELS_PATH.
    """
    import essentia.standard as es
    import numpy as np

    # ------------------------------------------------------------------
    # Energy from RMS (spectral, no TF needed)
    # Normalized against typical music RMS range (0.01–0.15)
    # ------------------------------------------------------------------
    sample = audio[:min(len(audio), 44100 * 60)]
    rms_values = []
    rms_algo = es.RMS()
    for frame in es.FrameGenerator(sample, frameSize=2048, hopSize=512):
        rms_values.append(float(rms_algo(frame)))

    if rms_values:
        rms_p75 = float(np.percentile(rms_values, 75))
        # Map RMS to 0–1 range.  Typical music RMS values:
        #   quiet classical/ambient: 0.01–0.05
        #   moderate pop/indie:      0.05–0.15
        #   loud rock/hip-hop:       0.15–0.30
        #   heavily compressed:      0.30+
        energy = max(0.0, min(1.0, (rms_p75 - 0.01) / 0.29))
        result["energy"] = float(round(energy, 3))

    # ------------------------------------------------------------------
    # TF models (danceability, mood, instrumentalness, valence proxy)
    # ------------------------------------------------------------------
    models_dir = _get_models_dir()
    if not ensure_models():
        logger.warning("Some Essentia TF models unavailable — skipping TF-based features")
        return

    try:
        from essentia.standard import TensorflowPredict2D, TensorflowPredictEffnetDiscogs

        effnet_path = os.path.join(models_dir, "discogs-effnet-bs64-1.pb")

        # Discogs-EffNet embeddings (shared input for all downstream classifiers)
        embed_model = TensorflowPredictEffnetDiscogs(
            graphFilename=effnet_path,
            output="PartitionedCall:1",
        )
        embeddings = embed_model(audio)

    except Exception as e:
        logger.warning(f"EffNet embedding extraction failed: {e}")
        return

    # Helper: run a classifier head and return mean prediction.
    # Each call is wrapped in its own try/except so one bad model
    # doesn't prevent the others from running.
    def _predict(model_file, output="model/Softmax", col=0):
        try:
            path = os.path.join(models_dir, model_file)
            if not os.path.exists(path):
                return None
            model = TensorflowPredict2D(graphFilename=path, output=output)
            preds = model(embeddings)
            return float(round(float(np.mean(preds[:, col])), 3))
        except Exception as e:
            logger.debug(f"TF prediction failed for {model_file}: {e}")
            return None

    # Danceability
    val = _predict("danceability-discogs-effnet-1.pb")
    if val is not None:
        result["danceability"] = val

    # Voice/instrumental → instrumentalness
    val = _predict("voice_instrumental-discogs-effnet-1.pb", col=1)  # col 1 = instrumental
    if val is not None:
        result["instrumentalness"] = val

    # Valence proxy: approachability maps well to musical positivity
    val = _predict("approachability_regression-discogs-effnet-1.pb", output="model/Identity")
    if val is not None:
        result["valence"] = val

    # Mood tags: run all mood classifiers, collect those above threshold
    mood_models = {
        "happy": "mood_happy-discogs-effnet-1.pb",
        "sad": "mood_sad-discogs-effnet-1.pb",
        "aggressive": "mood_aggressive-discogs-effnet-1.pb",
        "relaxed": "mood_relaxed-discogs-effnet-1.pb",
        "party": "mood_party-discogs-effnet-1.pb",
    }
    mood_tags = []
    for label, model_file in mood_models.items():
        conf = _predict(model_file)
        if conf is not None:
            mood_tags.append({"label": label, "confidence": conf})
    if mood_tags:
        # Sort by confidence descending
        mood_tags.sort(key=lambda m: m["confidence"], reverse=True)
        result["mood_tags"] = mood_tags

    # Speechiness proxy: 1 - instrumental confidence
    if result.get("instrumentalness") is not None:
        result["speechiness"] = float(round(1.0 - result["instrumentalness"], 3))

    logger.debug(
        f"TF features: dance={result.get('danceability')} "
        f"valence={result.get('valence')} "
        f"moods={[m['label'] for m in (result.get('mood_tags') or []) if m['confidence'] > 0.5]}"
    )


def _build_embedding(result: dict, hpcp_frames: list) -> Optional[str]:
    """
    Build a 64-dim float32 feature vector and return as base64 string.
    This vector is loaded into FAISS for similarity search.
    """
    try:
        import numpy as np

        vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)

        # Slots 0–7: scalar features (normalized 0–1)
        vec[0] = min(1.0, result.get("bpm", 120) / 200.0)
        vec[1] = result.get("energy", 0.5)
        vec[2] = result.get("danceability", 0.5)
        vec[3] = result.get("valence", 0.5)
        vec[4] = result.get("acousticness", 0.5)
        vec[5] = result.get("instrumentalness", 0.5)
        vec[6] = result.get("speechiness", 0.1)
        vec[7] = min(1.0, max(0.0, (result.get("loudness", -14) + 40) / 40))

        # Slots 8–19: mean HPCP chroma (12 dims)
        if hpcp_frames:
            mean_hpcp = np.mean(hpcp_frames, axis=0)[:12]
            vec[8:8 + len(mean_hpcp)] = mean_hpcp

        # Slots 20–63: reserved for MFCC / rhythm features in Phase 3b
        # (populated by the extended analysis pass)

        return base64.b64encode(vec.tobytes()).decode("ascii")

    except Exception as e:
        logger.debug(f"Embedding build failed: {e}")
        return None
