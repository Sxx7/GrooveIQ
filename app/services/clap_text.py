"""
GrooveIQ – CLAP text encoder (main process).

Wraps the CLAP text-tower ONNX model so the API layer can convert a
natural-language prompt into a 512-dim embedding comparable to the
CLAP audio embeddings stored on ``TrackFeatures.clap_embedding``.

The audio tower runs inside the analysis worker pool (see
``analysis_worker.py``). The text tower is small (~50 MB on disk,
<100 MB RAM) so we run it lazily in the FastAPI process itself —
no separate worker needed.

Lifecycle:
  - First call to ``encode_text()`` loads the ONNX session + tokenizer.
  - Subsequent calls are in-process and fast (~a few ms on CPU).
  - Results for the last 256 prompts are cached (LRU) so a dashboard
    typing-indicator doesn't hammer the model.

If the model files aren't present or the ``tokenizers`` lib is missing
we fail soft with a clear exception; the API route catches it and
returns 503.
"""

from __future__ import annotations

import logging
import os
import threading
from functools import lru_cache

import numpy as np

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_session = None  # onnxruntime.InferenceSession
_tokenizer = None  # tokenizers.Tokenizer
_input_name: str | None = None


def _model_paths() -> tuple[str, str]:
    """Resolve absolute paths to the ONNX model + tokenizer JSON."""
    from app.core.config import settings

    model_dir = settings.CLAP_MODEL_DIR
    model_path = os.path.join(model_dir, settings.CLAP_TEXT_MODEL_FILE)
    tokenizer_path = os.path.join(model_dir, settings.CLAP_TOKENIZER_FILE)
    return model_path, tokenizer_path


def _load() -> None:
    """Load the ONNX session + tokenizer. Called lazily on first use."""
    global _session, _tokenizer, _input_name

    if _session is not None:
        return

    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        raise RuntimeError("CLAP is disabled (CLAP_ENABLED=false)")

    model_path, tokenizer_path = _model_paths()
    if not os.path.exists(model_path):
        raise FileNotFoundError(
            f"CLAP text model not found at {model_path}. "
            "See CLAUDE.md § CLAP for export instructions."
        )
    if not os.path.exists(tokenizer_path):
        raise FileNotFoundError(f"CLAP tokenizer not found at {tokenizer_path}")

    try:
        import onnxruntime as ort
    except ImportError as e:
        raise RuntimeError("onnxruntime is required for CLAP text encoding") from e

    try:
        from tokenizers import Tokenizer
    except ImportError as e:
        raise RuntimeError(
            "The `tokenizers` package is required for CLAP text encoding. "
            "Install with: pip install tokenizers>=0.15"
        ) from e

    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1

    session = ort.InferenceSession(
        model_path,
        sess_options=opts,
        providers=["CPUExecutionProvider"],
    )
    tokenizer = Tokenizer.from_file(tokenizer_path)

    inputs = session.get_inputs()
    if not inputs:
        raise RuntimeError("CLAP text ONNX model has no inputs")

    with _lock:
        global _session, _tokenizer, _input_name
        _session = session
        _tokenizer = tokenizer
        _input_name = inputs[0].name

    logger.info(
        "CLAP text encoder loaded: model=%s, input=%s",
        os.path.basename(model_path),
        _input_name,
    )


@lru_cache(maxsize=256)
def _encode_cached(prompt: str) -> bytes:
    """Inner cached encoder. Returns raw bytes so lru_cache can hash safely."""
    _load()
    assert _tokenizer is not None and _session is not None and _input_name is not None  # noqa: S101 - type narrowing after _load()

    encoded = _tokenizer.encode(prompt)
    # LAION-CLAP typically expects a fixed 77-token window (CLIP convention).
    # We pad/truncate to the length that the exported ONNX expects. Most
    # exports use 77, but we read it off the model to stay portable.
    try:
        expected_len = _session.get_inputs()[0].shape[1]
        if not isinstance(expected_len, int):
            expected_len = 77
    except Exception:
        expected_len = 77

    ids = encoded.ids[:expected_len]
    if len(ids) < expected_len:
        ids = ids + [0] * (expected_len - len(ids))

    input_ids = np.asarray([ids], dtype=np.int64)
    outputs = _session.run(None, {_input_name: input_ids})
    vec = np.asarray(outputs[0], dtype=np.float32).reshape(-1)

    # L2-normalise so dot-product == cosine similarity, matching how we store
    # audio embeddings.
    norm = float(np.linalg.norm(vec))
    if norm > 1e-9:
        vec = vec / norm
    return vec.astype(np.float32).tobytes()


def encode_text(prompt: str) -> np.ndarray:
    """
    Encode a natural-language prompt to a 512-dim L2-normalised float32 vector.

    Cached (last 256 prompts). Raises RuntimeError if CLAP is disabled or the
    model can't be loaded.
    """
    from app.core.config import settings

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt must be a non-empty string")
    raw = _encode_cached(prompt)
    vec = np.frombuffer(raw, dtype=np.float32).copy()
    if vec.size != settings.CLAP_EMBEDDING_DIM:
        logger.warning(
            "CLAP text vector size mismatch: got %d, expected %d",
            vec.size,
            settings.CLAP_EMBEDDING_DIM,
        )
    return vec


def is_available() -> bool:
    """True if CLAP is enabled and the model files are on disk."""
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        return False
    model_path, tokenizer_path = _model_paths()
    return os.path.exists(model_path) and os.path.exists(tokenizer_path)
