"""
GrooveIQ – SASRec: Self-Attentive Sequential Recommendation.

A small transformer decoder trained on listening session sequences to predict
the next track. Captures sequential patterns that static features miss:
"after listening to A then B, users tend to listen to C".

Architecture (deliberately small for self-hosted deployment):
  - 2 transformer decoder layers, 4 attention heads, 64-dim embeddings
  - Causal (left-to-right) self-attention over the last N tracks
  - Trained with cross-entropy loss on next-item prediction

At serving time, we feed the user's recent listening sequence and get
per-track probabilities used as a ranking feature (`sequential_score`).

Reference: Kang & McAuley, "Self-Attentive Sequential Recommendation", 2018.
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, ListenSession

logger = logging.getLogger(__name__)

_MODEL_DIR = os.environ.get("GROOVEIQ_MODEL_DIR", "/data/models")

# Singleton state.
_lock = threading.Lock()
_model: object | None = None  # SASRecModel instance
_vocab: dict[str, int] | None = None  # track_id -> token index
_inv_vocab: dict[int, str] | None = None  # token index -> track_id
_user_sequences: dict[str, list[str]] = {}  # cached recent sequences per user

# Config.
_EMBED_DIM = 64
_NUM_HEADS = 4
_NUM_LAYERS = 2
_MAX_SEQ_LEN = 50  # max tracks in sequence
_DROPOUT = 0.1
_LEARNING_RATE = 0.001
_EPOCHS = 30
_BATCH_SIZE = 64
_MIN_SESSIONS = 20  # don't train with fewer sessions
_MIN_VOCAB = 10  # don't train with fewer unique tracks


class SASRecModel:
    """
    Minimal SASRec implementation using numpy for inference
    and a simple training loop. No PyTorch dependency required.

    Uses learned embeddings + positional encodings + causal self-attention.
    """

    def __init__(
        self, vocab_size: int, embed_dim: int, num_heads: int, num_layers: int, max_seq_len: int, dropout: float = 0.1
    ):
        self.vocab_size = vocab_size
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.num_layers = num_layers
        self.max_seq_len = max_seq_len
        self.head_dim = embed_dim // num_heads

        # Initialize weights (Xavier uniform).
        scale = math.sqrt(2.0 / (vocab_size + embed_dim))
        self.item_embeddings = np.random.randn(vocab_size, embed_dim).astype(np.float32) * scale
        self.position_embeddings = np.random.randn(max_seq_len, embed_dim).astype(np.float32) * 0.02

        # Per-layer weights: Q, K, V projections + FFN.
        self.layers: list[dict[str, np.ndarray]] = []
        for _ in range(num_layers):
            layer = {
                "Wq": np.random.randn(embed_dim, embed_dim).astype(np.float32) * scale,
                "Wk": np.random.randn(embed_dim, embed_dim).astype(np.float32) * scale,
                "Wv": np.random.randn(embed_dim, embed_dim).astype(np.float32) * scale,
                "Wo": np.random.randn(embed_dim, embed_dim).astype(np.float32) * scale,
                "ln1_gamma": np.ones(embed_dim, dtype=np.float32),
                "ln1_beta": np.zeros(embed_dim, dtype=np.float32),
                "ff1": np.random.randn(embed_dim, embed_dim * 2).astype(np.float32) * scale,
                "ff1_bias": np.zeros(embed_dim * 2, dtype=np.float32),
                "ff2": np.random.randn(embed_dim * 2, embed_dim).astype(np.float32) * scale,
                "ff2_bias": np.zeros(embed_dim, dtype=np.float32),
                "ln2_gamma": np.ones(embed_dim, dtype=np.float32),
                "ln2_beta": np.zeros(embed_dim, dtype=np.float32),
            }
            self.layers.append(layer)

        # Final layer norm.
        self.final_ln_gamma = np.ones(embed_dim, dtype=np.float32)
        self.final_ln_beta = np.zeros(embed_dim, dtype=np.float32)

    def _layer_norm(self, x: np.ndarray, gamma: np.ndarray, beta: np.ndarray) -> np.ndarray:
        mean = x.mean(axis=-1, keepdims=True)
        std = x.std(axis=-1, keepdims=True) + 1e-6
        return gamma * (x - mean) / std + beta

    def _causal_attention(self, Q: np.ndarray, K: np.ndarray, V: np.ndarray) -> np.ndarray:
        """Multi-head causal self-attention."""
        seq_len = Q.shape[0]
        # Reshape for multi-head: (seq, heads, head_dim)
        Q = Q.reshape(seq_len, self.num_heads, self.head_dim)
        K = K.reshape(seq_len, self.num_heads, self.head_dim)
        V = V.reshape(seq_len, self.num_heads, self.head_dim)

        # Attention scores: (heads, seq, seq)
        scores = np.einsum("ihd,jhd->hij", Q, K) / math.sqrt(self.head_dim)

        # Causal mask: prevent attending to future positions.
        mask = np.triu(np.ones((seq_len, seq_len), dtype=np.float32) * -1e9, k=1)
        scores = scores + mask[np.newaxis, :, :]

        # Softmax.
        scores_max = scores.max(axis=-1, keepdims=True)
        exp_scores = np.exp(scores - scores_max)
        attn = exp_scores / (exp_scores.sum(axis=-1, keepdims=True) + 1e-9)

        # Weighted values: (heads, seq, head_dim) -> (seq, embed_dim)
        out = np.einsum("hij,jhd->ihd", attn, V)
        return out.reshape(seq_len, self.embed_dim)

    def forward(self, token_ids: np.ndarray) -> np.ndarray:
        """
        Forward pass.

        Args:
            token_ids: 1D array of token indices, shape (seq_len,)

        Returns:
            Output embeddings, shape (seq_len, embed_dim)
        """
        seq_len = len(token_ids)
        x = self.item_embeddings[token_ids] + self.position_embeddings[:seq_len]

        for layer in self.layers:
            # Self-attention with residual + layer norm.
            normed = self._layer_norm(x, layer["ln1_gamma"], layer["ln1_beta"])
            Q = normed @ layer["Wq"]
            K = normed @ layer["Wk"]
            V = normed @ layer["Wv"]
            attn_out = self._causal_attention(Q, K, V)
            x = x + attn_out @ layer["Wo"]

            # FFN with residual + layer norm.
            normed = self._layer_norm(x, layer["ln2_gamma"], layer["ln2_beta"])
            ff = np.maximum(normed @ layer["ff1"] + layer["ff1_bias"], 0)  # ReLU
            ff = ff @ layer["ff2"] + layer["ff2_bias"]
            x = x + ff

        x = self._layer_norm(x, self.final_ln_gamma, self.final_ln_beta)
        return x

    def predict_next(self, token_ids: np.ndarray) -> np.ndarray:
        """
        Predict next-item scores for all items in vocabulary.

        Takes the last position's output embedding and computes dot product
        with all item embeddings.

        Returns: scores array of shape (vocab_size,)
        """
        output = self.forward(token_ids)
        last_hidden = output[-1]  # (embed_dim,)
        # Dot product with all item embeddings.
        scores = last_hidden @ self.item_embeddings.T
        return scores

    def _collect_params(self) -> list[np.ndarray]:
        """Collect all trainable parameters as a flat list."""
        params = [self.item_embeddings, self.position_embeddings]
        for layer in self.layers:
            params.extend(layer.values())
        params.extend([self.final_ln_gamma, self.final_ln_beta])
        return params

    def train_epoch(self, sequences: list[np.ndarray], lr: float) -> float:
        """
        One training epoch with simple SGD + cross-entropy loss.

        For each sequence, predicts the next item at each position and
        backpropagates through the output layer (simplified: only updates
        item embeddings via the prediction head, not full backprop through
        attention — a practical approximation for small-scale deployment).
        """
        total_loss = 0.0
        n_samples = 0

        np.random.shuffle(sequences)

        for seq in sequences:
            if len(seq) < 2:
                continue

            # Truncate to max sequence length.
            seq = seq[-self.max_seq_len :]
            input_ids = seq[:-1]
            target_ids = seq[1:]

            output = self.forward(input_ids)  # (seq_len-1, embed_dim)

            # Compute logits: (seq_len-1, vocab_size)
            logits = output @ self.item_embeddings.T

            # Softmax + cross-entropy loss (numerically stable).
            for t in range(len(target_ids)):
                logit = logits[t]
                logit_max = logit.max()
                exp_logit = np.exp(logit - logit_max)
                softmax = exp_logit / (exp_logit.sum() + 1e-9)

                target = target_ids[t]
                loss = -math.log(softmax[target] + 1e-9)
                total_loss += loss
                n_samples += 1

                # Gradient of cross-entropy w.r.t. logits: softmax - one_hot.
                grad = softmax.copy()
                grad[target] -= 1.0

                # Update item embeddings via output layer gradient.
                # d_loss/d_embedding = output[t] * grad (for prediction head).
                hidden = output[t]
                for v in range(self.vocab_size):
                    if abs(grad[v]) > 1e-6:
                        self.item_embeddings[v] -= lr * grad[v] * hidden

                # Update the hidden state's contributing embedding.
                embed_grad = grad @ self.item_embeddings
                self.item_embeddings[input_ids[t]] -= lr * 0.1 * embed_grad

        return total_loss / max(n_samples, 1)


async def _load_sequences() -> tuple[list[list[str]], dict[str, list[str]]]:
    """
    Load track sequences from sessions for training.

    Returns:
        - sequences: list of track_id lists (one per session)
        - user_recent: {user_id: recent_track_ids} for serving
    """
    async with AsyncSessionLocal() as session:
        sess_result = await session.execute(
            select(
                ListenSession.session_key,
                ListenSession.user_id,
                ListenSession.event_id_min,
                ListenSession.event_id_max,
                ListenSession.started_at,
            )
            .where(ListenSession.track_count >= 2)
            .order_by(ListenSession.started_at)
        )
        sessions = sess_result.all()

        if not sessions:
            return [], {}

        sequences: list[list[str]] = []
        user_sessions: dict[str, list[tuple[int, list[str]]]] = {}

        for sess_key, user_id, eid_min, eid_max, started_at in sessions:
            ev_result = await session.execute(
                select(ListenEvent.track_id)
                .where(
                    ListenEvent.user_id == user_id,
                    ListenEvent.id >= eid_min,
                    ListenEvent.id <= eid_max,
                    ListenEvent.event_type.in_(["play_start", "play_end"]),
                )
                .order_by(ListenEvent.timestamp, ListenEvent.id)
            )
            track_ids = [row[0] for row in ev_result.all()]

            # Deduplicate consecutive.
            deduped: list[str] = []
            for tid in track_ids:
                if not deduped or deduped[-1] != tid:
                    deduped.append(tid)

            if len(deduped) >= 2:
                sequences.append(deduped)
                user_sessions.setdefault(user_id, []).append((started_at, deduped))

        # Build user_recent: last N tracks from most recent sessions.
        user_recent: dict[str, list[str]] = {}
        for uid, sess_list in user_sessions.items():
            sess_list.sort(key=lambda x: x[0])
            recent: list[str] = []
            for _, tracks in sess_list[-5:]:  # last 5 sessions
                recent.extend(tracks)
            user_recent[uid] = recent[-_MAX_SEQ_LEN:]

    return sequences, user_recent


def _train_model_sync(
    sequences: list[list[str]],
) -> tuple[SASRecModel | None, dict[str, int], dict[int, str]]:
    """CPU-bound SASRec training. Runs in a thread executor."""
    # Build vocabulary.
    track_counts: dict[str, int] = {}
    for seq in sequences:
        for tid in seq:
            track_counts[tid] = track_counts.get(tid, 0) + 1

    # Filter: only tracks appearing at least twice.
    vocab_tracks = [t for t, c in track_counts.items() if c >= 2]
    if len(vocab_tracks) < _MIN_VOCAB:
        return None, {}, {}

    # Add padding token at index 0.
    vocab = {"<PAD>": 0}
    for i, tid in enumerate(sorted(vocab_tracks)):
        vocab[tid] = i + 1
    inv_vocab = {v: k for k, v in vocab.items()}
    vocab_size = len(vocab)

    # Convert sequences to token arrays.
    token_sequences: list[np.ndarray] = []
    for seq in sequences:
        tokens = [vocab[tid] for tid in seq if tid in vocab]
        if len(tokens) >= 2:
            token_sequences.append(np.array(tokens, dtype=np.int32))

    if len(token_sequences) < _MIN_SESSIONS:
        return None, {}, {}

    # Create and train model.
    model = SASRecModel(
        vocab_size=vocab_size,
        embed_dim=_EMBED_DIM,
        num_heads=_NUM_HEADS,
        num_layers=_NUM_LAYERS,
        max_seq_len=_MAX_SEQ_LEN,
        dropout=_DROPOUT,
    )

    for epoch in range(_EPOCHS):
        loss = model.train_epoch(token_sequences, lr=_LEARNING_RATE)
        if epoch % 10 == 0:
            logger.debug(f"SASRec epoch {epoch}: loss={loss:.4f}")

    return model, vocab, inv_vocab


async def train() -> dict[str, Any]:
    """
    Train SASRec model from listening sessions.

    Returns summary dict with training stats.
    """
    import asyncio

    sequences, user_recent = await _load_sequences()

    if len(sequences) < _MIN_SESSIONS:
        logger.info(f"SASRec: only {len(sequences)} sessions (< {_MIN_SESSIONS}), skipping.")
        return {"trained": False, "sessions": len(sequences), "reason": "insufficient_sessions"}

    loop = asyncio.get_running_loop()
    model, vocab, inv_vocab = await loop.run_in_executor(
        None,
        _train_model_sync,
        sequences,
    )

    if model is None:
        return {"trained": False, "reason": "insufficient_vocabulary"}

    with _lock:
        global _model, _vocab, _inv_vocab, _user_sequences
        _model = model
        _vocab = vocab
        _inv_vocab = inv_vocab
        _user_sequences = user_recent

    _save_model(model, vocab, inv_vocab, user_recent)

    logger.info(f"SASRec trained: {len(sequences)} sessions, {len(vocab)} tracks in vocabulary.")

    return {
        "trained": True,
        "sessions": len(sequences),
        "vocab_size": len(vocab),
        "embed_dim": _EMBED_DIM,
        "num_layers": _NUM_LAYERS,
    }


def _save_model(model, vocab, inv_vocab, user_sequences) -> str | None:
    """Persist the SASRec model + vocab + per-user recent sequences. (#43)

    Pickles the whole bundle — the SASRecModel is small (a handful of numpy
    arrays) and pickling is the simplest way to round-trip the layer dicts.
    """
    try:
        model_dir = Path(_MODEL_DIR)
        model_dir.mkdir(parents=True, exist_ok=True)
        import pickle

        version = str(int(time.time()))
        path = model_dir / f"sasrec_{version}.pkl"
        with open(path, "wb") as f:
            # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
            pickle.dump(
                {
                    "model": model,
                    "vocab": vocab,
                    "inv_vocab": inv_vocab,
                    "user_sequences": user_sequences,
                    "version": version,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        logger.info(f"SASRec model saved: {path}")
        return str(path)
    except Exception as e:
        logger.warning(f"Could not save SASRec model to disk: {e}")
        return None


def load_latest() -> bool:
    """Load the most recent SASRec bundle. Returns True on success. (#43)"""
    model_dir = Path(_MODEL_DIR)
    if not model_dir.exists():
        return False

    candidates = sorted(model_dir.glob("sasrec_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return False

    path = candidates[0]
    try:
        import pickle

        with open(path, "rb") as f:
            # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
            bundle = pickle.load(f)  # noqa: S301 - trusted local model artefact
    except Exception as e:
        logger.warning(f"SASRec load_latest failed for {path}: {e}")
        return False

    with _lock:
        global _model, _vocab, _inv_vocab, _user_sequences
        _model = bundle["model"]
        _vocab = bundle["vocab"]
        _inv_vocab = bundle["inv_vocab"]
        _user_sequences = bundle["user_sequences"]

    logger.info(
        f"SASRec loaded from disk: {path.name} ({len(bundle['vocab'])} tracks, {len(bundle['user_sequences'])} users)"
    )
    return True


def predict_next_scores(
    user_id: str,
    candidate_ids: set[str],
) -> dict[str, float]:
    """
    Get SASRec next-track probability scores for candidate tracks.

    Uses the user's recent listening sequence to predict which candidates
    are most likely to be listened to next.

    Returns {track_id: score} where score is a softmax probability.
    """
    with _lock:
        model = _model
        vocab = _vocab
        recent = _user_sequences.get(user_id)

    if model is None or vocab is None or recent is None or len(recent) < 2:
        return {}

    # Convert recent sequence to tokens.
    tokens = [vocab[tid] for tid in recent if tid in vocab]
    if len(tokens) < 2:
        return {}

    tokens = tokens[-_MAX_SEQ_LEN:]
    token_array = np.array(tokens, dtype=np.int32)

    # Get raw scores for all vocab items.
    raw_scores = model.predict_next(token_array)

    # Softmax over candidates only.
    candidate_tokens = [(tid, vocab[tid]) for tid in candidate_ids if tid in vocab]
    if not candidate_tokens:
        return {}

    candidate_scores = np.array([raw_scores[tok] for _, tok in candidate_tokens])
    # Stable softmax.
    max_score = candidate_scores.max()
    exp_scores = np.exp(candidate_scores - max_score)
    softmax_scores = exp_scores / (exp_scores.sum() + 1e-9)

    return {tid: float(score) for (tid, _), score in zip(candidate_tokens, softmax_scores)}


def get_top_predictions(
    user_id: str,
    k: int = 50,
    exclude_ids: set[str] | None = None,
) -> list[tuple[str, float]]:
    """
    Get top-k next-track predictions for a user.

    Can be used as an additional candidate source.
    """
    with _lock:
        model = _model
        vocab = _vocab
        inv_vocab = _inv_vocab
        recent = _user_sequences.get(user_id)

    if model is None or vocab is None or inv_vocab is None or recent is None:
        return []

    tokens = [vocab[tid] for tid in recent if tid in vocab]
    if len(tokens) < 2:
        return []

    tokens = tokens[-_MAX_SEQ_LEN:]
    token_array = np.array(tokens, dtype=np.int32)

    raw_scores = model.predict_next(token_array)

    # Get top-k by raw score.
    top_indices = np.argsort(raw_scores)[::-1]

    results: list[tuple[str, float]] = []
    recent_set = set(recent)
    for idx in top_indices:
        tid = inv_vocab.get(idx)
        if tid is None or tid == "<PAD>":
            continue
        if tid in recent_set:
            continue
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, float(raw_scores[idx])))
        if len(results) >= k:
            break

    return results


def is_ready() -> bool:
    with _lock:
        return _model is not None


def vocab_size() -> int:
    with _lock:
        return len(_vocab) if _vocab else 0
