"""
GrooveIQ – CLAP-derived mood scores (#98).

Replaces the EffNet binary-classifier composite (#88) for tracks that have a
CLAP audio embedding. CLAP gives a single 512-dim shared text/audio space, so
a per-mood "how close is this audio to the concept of X?" reduces to one
dot-product per label — no per-head bias to compensate for, no hand-tuned
weight stack.

Hybrid rollout: this module produces values only when a CLAP audio embedding
is available. Tracks without one keep using the EffNet composite from
analysis_worker._composite_valence(). Hybrid coverage grows track-by-track
as the CLAP backfill progresses.

Design notes:
  - The 5 label prompts are encoded once via app.services.clap_text.encode_text
    and cached in module state. ~10 KB total (5 × 512 × 4 bytes).
  - Cosine similarity → [0, 1] via (sim + 1) / 2, matching the AudioMuse-AI
    approach the issue links to.
  - Valence is derived from the per-label scores with a fixed weighted
    composition (see _derive_valence).
  - All computation runs in the caller's process. The current callers
    (analysis worker pool's `analyze()` post-processing, clap_backfill) live
    in the FastAPI main process so the CLAP text encoder loads only once,
    not once per worker subprocess.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

# Short, musical prompts tuned to what each label means in our existing
# taxonomy. Single-word prompts ("happy") underperform on CLAP — a brief
# musical descriptor is more discriminative.
_MOOD_PROMPTS: dict[str, str] = {
    "happy": "happy upbeat cheerful music",
    "sad": "sad melancholic emotional music",
    "aggressive": "aggressive intense angry music",
    "relaxed": "relaxed calm peaceful music",
    "party": "party dance music for celebrating",
}

_MOOD_LABELS: tuple[str, ...] = tuple(_MOOD_PROMPTS.keys())

_lock = threading.Lock()
_label_embeddings: dict[str, np.ndarray] | None = None


def _get_label_embeddings() -> dict[str, np.ndarray] | None:
    """Lazy-load and cache the 5 label embeddings. Returns None if CLAP text
    encoding isn't available (model files missing, CLAP_ENABLED=false, etc.)."""
    global _label_embeddings
    if _label_embeddings is not None:
        return _label_embeddings

    try:
        from app.services.clap_text import encode_text, is_available
    except Exception as e:
        logger.debug("CLAP text encoder unavailable: %s", e)
        return None

    if not is_available():
        return None

    with _lock:
        if _label_embeddings is not None:
            return _label_embeddings
        try:
            embeddings = {label: encode_text(prompt) for label, prompt in _MOOD_PROMPTS.items()}
        except Exception as e:
            logger.warning("Failed to encode CLAP mood label prompts: %s", e)
            return None
        _label_embeddings = embeddings
        logger.info("CLAP mood label embeddings ready (%d labels)", len(embeddings))
        return _label_embeddings


def compute_mood_scores_from_clap(audio_embedding: np.ndarray) -> dict[str, float] | None:
    """Compute per-label mood scores from a CLAP audio embedding.

    Returns a dict mapping each label to a score in [0, 1], rounded to 3
    decimals. Returns None if the audio embedding is unusable or label
    embeddings can't be loaded.
    """
    if audio_embedding is None:
        return None

    audio = np.asarray(audio_embedding, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return None

    label_embs = _get_label_embeddings()
    if label_embs is None:
        return None

    expected_dim = next(iter(label_embs.values())).size
    if audio.size != expected_dim:
        logger.debug(
            "CLAP audio embedding dim mismatch: got %d, expected %d",
            audio.size,
            expected_dim,
        )
        return None

    norm = float(np.linalg.norm(audio))
    if norm < 1e-9:
        return None  # zero-vector audio embedding can't be scored
    if abs(norm - 1.0) > 1e-3:
        # CLAP audio embeddings should already be L2-normalised on storage,
        # but normalise defensively in case a caller passed a raw vector.
        audio = audio / norm

    scores: dict[str, float] = {}
    for label, text_emb in label_embs.items():
        sim = float(np.dot(audio, text_emb))
        scores[label] = round(max(0.0, min(1.0, (sim + 1.0) / 2.0)), 3)
    return scores


def _derive_valence(scores: dict[str, float]) -> float | None:
    """Compose a single valence value from the per-label CLAP scores.

    Russell-circumplex-flavoured weighted average — happy and party push
    valence up, sad and aggressive push it down. Weights sum to 1.0 so a
    neutral track lands at ~0.5, all-positive at 1.0, all-negative at 0.0.
    """
    needed = ("happy", "sad", "aggressive", "party")
    if not all(k in scores for k in needed):
        return None
    raw = (
        0.4 * scores["happy"] + 0.3 * scores["party"] + 0.2 * (1.0 - scores["sad"]) + 0.1 * (1.0 - scores["aggressive"])
    )
    return round(max(0.0, min(1.0, raw)), 3)


def compute_mood_payload_from_clap(
    audio_embedding: np.ndarray,
) -> tuple[list[dict], float | None] | None:
    """Convenience wrapper: CLAP audio embedding → (mood_tags, valence).

    Returns the values in the exact shape the analysis worker writes to
    TrackFeatures today:
      - mood_tags: list of {"label": str, "confidence": float} sorted desc
      - valence: float in [0, 1] or None if it couldn't be derived

    Returns None if no scores could be computed at all (callers should then
    leave the existing values untouched).
    """
    scores = compute_mood_scores_from_clap(audio_embedding)
    if scores is None:
        return None
    mood_tags = [{"label": label, "confidence": score} for label, score in scores.items()]
    mood_tags.sort(key=lambda m: m["confidence"], reverse=True)
    valence = _derive_valence(scores)
    return mood_tags, valence


def decode_clap_embedding(stored: str | bytes | None) -> np.ndarray | None:
    """Decode the base64-stored CLAP audio embedding back into a numpy vector.

    Mirrors the encode side in analysis_worker.py:
        base64.b64encode(clap_vec.astype(np.float32).tobytes()).decode("ascii")
    """
    if not stored:
        return None
    import base64

    try:
        raw = base64.b64decode(stored)
        return np.frombuffer(raw, dtype=np.float32).copy()
    except Exception as e:
        logger.debug("Failed to decode CLAP embedding: %s", e)
        return None


def apply_clap_mood_to_result(result: dict) -> dict:
    """In-place: if the analysis result has a clap_embedding, replace its
    mood_tags / valence with CLAP-derived values. No-op otherwise.

    The result dict comes back from the analysis worker subprocess with the
    EffNet composite already filled in. We override here in the main process
    so the CLAP text encoder doesn't need to live inside every worker.
    """
    clap_b64 = result.get("clap_embedding")
    if not clap_b64:
        return result
    audio = decode_clap_embedding(clap_b64)
    if audio is None:
        return result
    payload = compute_mood_payload_from_clap(audio)
    if payload is None:
        return result
    mood_tags, valence = payload
    result["mood_tags"] = mood_tags
    if valence is not None:
        result["valence"] = valence
    return result


def _reset_for_tests() -> None:
    """Drop the cached label embeddings. Tests use this to force re-load."""
    global _label_embeddings
    with _lock:
        _label_embeddings = None
