"""
GrooveIQ – Session-level GRU for taste drift modeling.

Models how a user's taste evolves across sessions using a GRU (Gated
Recurrent Unit) over session-level audio embeddings.

Each session is summarized as the mean audio embedding of its tracks.
A GRU processes the sequence of session summaries to predict the
"next session's taste vector". At serving time, candidate tracks are
scored by their cosine similarity to this predicted vector.

This captures taste drift that the multi-timescale exponential decay
profiles approximate but cannot fully model — for example, a user
gradually shifting from high-energy to chill music over the past week.

Reference: Hansen et al., "Contextual and Sequential User Embeddings
for Large-Scale Music Recommendation" (Spotify CoSeRNN), RecSys 2020.
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
from app.models.db import ListenEvent, ListenSession, TrackFeatures

logger = logging.getLogger(__name__)

_MODEL_DIR = os.environ.get("GROOVEIQ_MODEL_DIR", "/data/models")

# Singleton state.
_lock = threading.Lock()
_model: GRUModel | None = None
_user_predicted_vectors: dict[str, np.ndarray] = {}  # user_id -> predicted next-session embedding
_track_embeddings: dict[str, np.ndarray] = {}  # track_id -> audio embedding (for scoring)

# Config.
_EMBED_DIM = 64  # matches FAISS/audio embedding dimension
_HIDDEN_DIM = 64  # GRU hidden size
_LEARNING_RATE = 0.005
_EPOCHS = 30
_MIN_SESSIONS_PER_USER = 3  # need at least 3 sessions per user to learn drift
_MIN_USERS = 2  # need at least 2 users with enough sessions


class GRUModel:
    """
    Minimal GRU implementation in pure numpy.

    Single-layer GRU that processes session embeddings and outputs
    a predicted "next session" embedding vector.
    """

    def __init__(self, input_dim: int, hidden_dim: int):
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim

        scale = math.sqrt(2.0 / (input_dim + hidden_dim))

        # GRU gates: update (z), reset (r), candidate (n).
        self.Wz = np.random.randn(input_dim, hidden_dim).astype(np.float32) * scale
        self.Uz = np.random.randn(hidden_dim, hidden_dim).astype(np.float32) * scale
        self.bz = np.zeros(hidden_dim, dtype=np.float32)

        self.Wr = np.random.randn(input_dim, hidden_dim).astype(np.float32) * scale
        self.Ur = np.random.randn(hidden_dim, hidden_dim).astype(np.float32) * scale
        self.br = np.zeros(hidden_dim, dtype=np.float32)

        self.Wn = np.random.randn(input_dim, hidden_dim).astype(np.float32) * scale
        self.Un = np.random.randn(hidden_dim, hidden_dim).astype(np.float32) * scale
        self.bn = np.zeros(hidden_dim, dtype=np.float32)

        # Output projection: hidden_dim -> input_dim (predict in embedding space).
        self.Wo = np.random.randn(hidden_dim, input_dim).astype(np.float32) * scale
        self.bo = np.zeros(input_dim, dtype=np.float32)

    def _sigmoid(self, x: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-np.clip(x, -15, 15)))

    def forward(self, session_sequence: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """
        Process a sequence of session embeddings.

        Args:
            session_sequence: (num_sessions, input_dim)

        Returns:
            predicted: (input_dim,) predicted next-session embedding
            hidden: (hidden_dim,) final hidden state
        """
        h = np.zeros(self.hidden_dim, dtype=np.float32)

        for t in range(len(session_sequence)):
            x = session_sequence[t]
            z = self._sigmoid(x @ self.Wz + h @ self.Uz + self.bz)
            r = self._sigmoid(x @ self.Wr + h @ self.Ur + self.br)
            n = np.tanh(x @ self.Wn + (r * h) @ self.Un + self.bn)
            h = (1 - z) * n + z * h

        # Project to embedding space.
        predicted = h @ self.Wo + self.bo
        return predicted, h

    def train_step(self, session_sequence: np.ndarray, target: np.ndarray, lr: float) -> float:
        """
        One training step: predict next session from sequence, update weights.

        Uses simplified gradient: MSE loss between predicted and target embedding,
        with gradient only through the output projection and final hidden state.
        This is a practical approximation — full BPTT through all GRU steps would
        be more accurate but significantly more complex in pure numpy.
        """
        predicted, h = self.forward(session_sequence)

        # MSE loss.
        diff = predicted - target
        loss = float(np.mean(diff**2))

        # Gradient of output projection.
        d_predicted = 2.0 * diff / len(diff)  # (input_dim,)
        d_Wo = np.outer(h, d_predicted)  # (hidden_dim, input_dim)
        d_bo = d_predicted

        # Update output projection.
        self.Wo -= lr * d_Wo
        self.bo -= lr * d_bo

        # Simplified gradient through hidden state to GRU parameters.
        # d_h = d_predicted @ Wo.T  (backprop through output layer)
        d_h = d_predicted @ self.Wo.T  # (hidden_dim,)

        # Update GRU input weights using last input for gradient estimation.
        if len(session_sequence) > 0:
            last_x = session_sequence[-1]
            # Approximate: update gate weights using d_h signal.
            self.Wn -= lr * 0.01 * np.outer(last_x, d_h)
            self.Wz -= lr * 0.01 * np.outer(last_x, d_h)

        return loss


async def _load_session_embeddings() -> dict[str, list[tuple[int, np.ndarray]]]:
    """
    Load session-level mean audio embeddings per user.

    Returns {user_id: [(started_at, mean_embedding), ...]} sorted by time.
    """
    import base64

    async with AsyncSessionLocal() as session:
        # Get all sessions ordered by user and time.
        sess_result = await session.execute(
            select(
                ListenSession.user_id, ListenSession.started_at, ListenSession.event_id_min, ListenSession.event_id_max
            )
            .where(ListenSession.track_count >= 2)
            .order_by(ListenSession.user_id, ListenSession.started_at)
        )
        all_sessions = sess_result.all()

        if not all_sessions:
            return {}

        # Load all track embeddings.
        emb_result = await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.embedding).where(TrackFeatures.embedding.isnot(None))
        )
        track_embeddings: dict[str, np.ndarray] = {}
        for row in emb_result.all():
            try:
                raw = base64.b64decode(row.embedding)
                vec = np.frombuffer(raw, dtype=np.float32).copy()
                if len(vec) > 0:
                    track_embeddings[row.track_id] = vec
            except Exception:
                continue

        # Store globally for scoring at serve time.
        with _lock:
            global _track_embeddings
            _track_embeddings = track_embeddings

        # For each session, get track IDs and compute mean embedding.
        user_session_embs: dict[str, list[tuple[int, np.ndarray]]] = {}

        for uid, started_at, eid_min, eid_max in all_sessions:
            ev_result = await session.execute(
                select(ListenEvent.track_id)
                .where(
                    ListenEvent.user_id == uid,
                    ListenEvent.id >= eid_min,
                    ListenEvent.id <= eid_max,
                    ListenEvent.event_type.in_(["play_start", "play_end"]),
                )
                .order_by(ListenEvent.timestamp)
            )
            track_ids = list(dict.fromkeys(row[0] for row in ev_result.all()))

            # Compute mean embedding.
            vecs = [track_embeddings[tid] for tid in track_ids if tid in track_embeddings]
            if len(vecs) < 2:
                continue

            mean_emb = np.mean(vecs, axis=0).astype(np.float32)
            user_session_embs.setdefault(uid, []).append((started_at, mean_emb))

    return user_session_embs


def _train_model_sync(
    user_data: dict[str, list[tuple[int, np.ndarray]]],
) -> tuple[GRUModel | None, dict[str, np.ndarray]]:
    """CPU-bound GRU training. Runs in a thread executor."""
    # Filter users with enough sessions.
    eligible = {uid: sessions for uid, sessions in user_data.items() if len(sessions) >= _MIN_SESSIONS_PER_USER}

    if len(eligible) < _MIN_USERS:
        return None, {}

    # Determine embedding dimension from first available session.
    first_user = next(iter(eligible.values()))
    embed_dim = len(first_user[0][1])

    model = GRUModel(input_dim=embed_dim, hidden_dim=_HIDDEN_DIM)

    # Training: for each user, use sessions[:-1] as input, sessions[-1] as target.
    training_data: list[tuple[np.ndarray, np.ndarray]] = []
    for uid, sessions in eligible.items():
        sessions.sort(key=lambda x: x[0])
        embs = np.array([s[1] for s in sessions], dtype=np.float32)
        # Create multiple training examples by sliding window.
        for end in range(2, len(embs)):
            input_seq = embs[:end]
            target = embs[end] if end < len(embs) else embs[-1]
            training_data.append((input_seq, target))

    if not training_data:
        return None, {}

    for epoch in range(_EPOCHS):
        np.random.shuffle(training_data)
        total_loss = 0.0
        for input_seq, target in training_data:
            loss = model.train_step(input_seq, target, lr=_LEARNING_RATE)
            total_loss += loss
        avg_loss = total_loss / len(training_data)
        if epoch % 10 == 0:
            logger.debug(f"Session GRU epoch {epoch}: loss={avg_loss:.6f}")

    # Predict next-session vectors for all eligible users.
    user_predictions: dict[str, np.ndarray] = {}
    for uid, sessions in eligible.items():
        sessions.sort(key=lambda x: x[0])
        embs = np.array([s[1] for s in sessions], dtype=np.float32)
        predicted, _ = model.forward(embs)
        # L2-normalize for cosine similarity at serving time.
        norm = np.linalg.norm(predicted)
        if norm > 1e-9:
            predicted = predicted / norm
        user_predictions[uid] = predicted

    return model, user_predictions


async def train() -> dict[str, Any]:
    """
    Train the session-level GRU model.

    Returns summary dict with training stats.
    """
    import asyncio

    user_data = await _load_session_embeddings()

    eligible_count = sum(1 for sessions in user_data.values() if len(sessions) >= _MIN_SESSIONS_PER_USER)

    if eligible_count < _MIN_USERS:
        logger.info(f"Session GRU: only {eligible_count} users with >= {_MIN_SESSIONS_PER_USER} sessions, skipping.")
        return {
            "trained": False,
            "eligible_users": eligible_count,
            "reason": "insufficient_users",
        }

    loop = asyncio.get_running_loop()
    model, user_predictions = await loop.run_in_executor(
        None,
        _train_model_sync,
        user_data,
    )

    if model is None:
        return {"trained": False, "reason": "training_failed"}

    with _lock:
        global _model, _user_predicted_vectors
        _model = model
        _user_predicted_vectors = user_predictions
        track_embs_snapshot = dict(_track_embeddings)

    _save_model(model, user_predictions, track_embs_snapshot)

    logger.info(
        f"Session GRU trained: {eligible_count} users, {sum(len(s) for s in user_data.values())} total sessions."
    )

    return {
        "trained": True,
        "users": eligible_count,
        "total_sessions": sum(len(s) for s in user_data.values()),
        "hidden_dim": _HIDDEN_DIM,
    }


def _save_model(model, user_predictions, track_embs) -> str | None:
    """Persist the GRU model + predicted next-session vectors + the
    track-embedding cache used at serving time. (#43)"""
    try:
        model_dir = Path(_MODEL_DIR)
        model_dir.mkdir(parents=True, exist_ok=True)
        import pickle

        version = str(int(time.time()))
        path = model_dir / f"session_gru_{version}.pkl"
        with open(path, "wb") as f:
            # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
            pickle.dump(
                {
                    "model": model,
                    "user_predictions": user_predictions,
                    "track_embeddings": track_embs,
                    "version": version,
                },
                f,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        logger.info(f"Session GRU saved: {path}")
        return str(path)
    except Exception as e:
        logger.warning(f"Could not save session GRU to disk: {e}")
        return None


def load_latest() -> bool:
    """Load the most recent session GRU bundle. Returns True on success.

    Restores the GRU model, the per-user predicted next-session vectors and
    the track-embedding cache so ``predict_drift_scores`` works immediately
    after a restart. (#43)
    """
    model_dir = Path(_MODEL_DIR)
    if not model_dir.exists():
        return False

    candidates = sorted(model_dir.glob("session_gru_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return False

    path = candidates[0]
    try:
        import pickle

        with open(path, "rb") as f:
            # nosemgrep: python.lang.security.deserialization.pickle.avoid-pickle
            bundle = pickle.load(f)  # noqa: S301 - trusted local model artefact
    except Exception as e:
        logger.warning(f"Session GRU load_latest failed for {path}: {e}")
        return False

    with _lock:
        global _model, _user_predicted_vectors, _track_embeddings
        _model = bundle["model"]
        _user_predicted_vectors = bundle["user_predictions"]
        _track_embeddings = bundle["track_embeddings"]

    logger.info(
        f"Session GRU loaded from disk: {path.name} "
        f"({len(bundle['user_predictions'])} users, {len(bundle['track_embeddings'])} tracks)"
    )
    return True


def predict_drift_scores(
    user_id: str,
    candidate_ids: set[str],
) -> dict[str, float]:
    """
    Score candidates by cosine similarity to the GRU's predicted
    next-session taste vector.

    Returns {track_id: score} where score is in [-1, 1].
    """
    with _lock:
        predicted = _user_predicted_vectors.get(user_id)
        track_embs = _track_embeddings

    if predicted is None or not track_embs:
        return {}

    scores: dict[str, float] = {}
    for tid in candidate_ids:
        emb = track_embs.get(tid)
        if emb is None:
            continue
        # Cosine similarity (predicted is already L2-normalized).
        norm = np.linalg.norm(emb)
        if norm < 1e-9:
            continue
        cos_sim = float(np.dot(predicted, emb) / norm)
        scores[tid] = cos_sim

    return scores


def is_ready() -> bool:
    with _lock:
        return _model is not None
