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
    """Load the ONNX session + tokenizer. Called lazily on first use.

    Double-checked locking: the lock-free fast path returns once the model is
    loaded, but the *build* runs entirely under ``_lock``. The earlier version
    checked ``_session is not None`` and built outside the lock, then assigned
    the three globals under it — so two concurrent first-callers could both run
    the (expensive) ONNX build, and a partially-assigned module state was
    observable. This matters now that the playlist text strategy can be driven
    from worker threads (``asyncio.to_thread``): pre-warming via ``_load()`` on
    the event loop before offloading keeps the model build single-flighted.
    """
    global _session, _tokenizer, _input_name

    if _session is not None:
        return

    with _lock:
        # Re-check under the lock: another thread may have finished loading
        # while we were blocked acquiring it.
        if _session is not None:
            return

        from app.core.config import settings

        if not settings.CLAP_ENABLED:
            raise RuntimeError("CLAP is disabled (CLAP_ENABLED=false)")

        model_path, tokenizer_path = _model_paths()
        if not os.path.exists(model_path):
            raise FileNotFoundError(
                f"CLAP text model not found at {model_path}. See CLAUDE.md § CLAP for export instructions."
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
                "The `tokenizers` package is required for CLAP text encoding. Install with: pip install tokenizers>=0.15"
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

        _session = session
        _tokenizer = tokenizer
        _input_name = inputs[0].name

        logger.info(
            "CLAP text encoder loaded: model=%s, input=%s",
            os.path.basename(model_path),
            _input_name,
        )


# Maximum tokens fed to the text tower. CLIP/CLAP exports are trained on a
# 77-token window; longer inputs get truncated to stay within that budget.
_MAX_TOKENS = 77


@lru_cache(maxsize=256)
def _encode_cached(prompt: str) -> bytes:
    """Inner cached encoder. Returns raw bytes so lru_cache can hash safely.

    Concurrency contract: call this (via ``encode_text``) on the event loop,
    not inside a worker thread. ``functools.lru_cache`` only guards its own
    dict bookkeeping with the GIL — it does *not* prevent two threads with the
    same prompt from both running this body. That would be benign here (the
    function is pure and ``onnxruntime`` inference is thread-safe), but every
    caller (this service's text strategy, ``/tracks`` text search) already
    encodes on the loop and then offloads only the numpy ranking, so the cache
    is never hit concurrently. Keep it that way.
    """
    _load()
    assert _tokenizer is not None and _session is not None and _input_name is not None  # noqa: S101 - type narrowing after _load()

    encoded = _tokenizer.encode(prompt)
    # The Xenova/larger_clap_music_and_speech ONNX export declares
    # sequence_length as dynamic and only takes input_ids (no attention_mask),
    # so any padding tokens contribute to attention and dilute the prompt's
    # representation. Earlier versions padded with id 0 — which is the
    # tokenizer's BOS token (<s>) for RoBERTa-BPE, not <pad> (id 1) —
    # collapsing every prompt's vector to >0.99 cosine similarity with every
    # other prompt's, since each one was content + ~70 BOS tokens. Pass the
    # tokenizer's natural BOS+content+EOS sequence as-is, truncated only to
    # stay within the trained 77-token budget.
    ids = encoded.ids[:_MAX_TOKENS] or [0]

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
