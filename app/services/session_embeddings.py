"""
GrooveIQ – Session skip-gram embeddings.

Treats each listening session as a "sentence" and each track as a "word",
then trains a Word2Vec skip-gram model to learn co-occurrence embeddings.
These capture behavioral similarity that pure audio features miss —
"people who listen to A then listen to B" patterns.

The model is trained periodically (alongside the recommendation pipeline)
and exposed as a module-level singleton for fast similarity lookups.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, ListenSession
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# Singleton state.
_lock = threading.Lock()
_model: object | None = None  # gensim Word2Vec
_track_ids: list[str] = []  # tracks in the model vocabulary

_MODEL_DIR = os.environ.get("GROOVEIQ_MODEL_DIR", "/data/models")


async def _load_session_sequences() -> list[list[str]]:
    """
    Load track sequences from sessions.

    For each session, fetches the ordered play_start events to reconstruct
    the track sequence the user actually listened to.
    """
    async with AsyncSessionLocal() as session:
        # Get all sessions with enough tracks.
        sess_result = await session.execute(
            select(
                ListenSession.session_key, ListenSession.user_id, ListenSession.event_id_min, ListenSession.event_id_max
            )
            .where(ListenSession.track_count >= 2)
            .order_by(ListenSession.started_at)
        )
        sessions = sess_result.all()

        if not sessions:
            return []

        sequences: list[list[str]] = []

        for sess_key, user_id, eid_min, eid_max in sessions:
            # Fetch ordered play events within this session's event range.
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

            # Deduplicate consecutive duplicates (play_start + play_end for same track).
            deduped: list[str] = []
            for tid in track_ids:
                if not deduped or deduped[-1] != tid:
                    deduped.append(tid)

            if len(deduped) >= 2:
                sequences.append(deduped)

    return sequences


def _train_model_sync(sequences: list[list[str]]) -> object | None:
    """CPU-bound Word2Vec training. Runs in a thread executor."""
    from gensim.models import Word2Vec

    cfg = get_config().session_embeddings
    model = Word2Vec(
        sentences=sequences,
        vector_size=cfg.embedding_dim,
        window=cfg.window_size,
        min_count=cfg.min_count,
        sg=1,  # skip-gram (not CBOW)
        workers=1,  # single thread inside executor
        epochs=cfg.epochs,
        seed=42,
    )
    return model


async def train() -> dict:
    """
    Train session skip-gram embeddings from all listening sessions.

    Returns summary dict with training stats.
    """
    import asyncio

    cfg = get_config().session_embeddings
    sequences = await _load_session_sequences()

    if len(sequences) < cfg.min_sessions:
        logger.info(f"Session embeddings: only {len(sequences)} sessions (< {cfg.min_sessions}), skipping training.")
        return {
            "trained": False,
            "sessions": len(sequences),
            "reason": "insufficient_sessions",
        }

    # Check vocabulary size.
    all_tracks = set()
    for seq in sequences:
        all_tracks.update(seq)

    if len(all_tracks) < cfg.min_vocab:
        logger.info(f"Session embeddings: only {len(all_tracks)} unique tracks (< {cfg.min_vocab}), skipping training.")
        return {
            "trained": False,
            "sessions": len(sequences),
            "unique_tracks": len(all_tracks),
            "reason": "insufficient_vocabulary",
        }

    loop = asyncio.get_running_loop()
    model = await loop.run_in_executor(None, _train_model_sync, sequences)

    if model is None:
        return {"trained": False, "reason": "training_failed"}

    vocab_size = len(model.wv)

    with _lock:
        global _model, _track_ids
        _model = model
        _track_ids = list(model.wv.index_to_key)

    _save_model(model)

    logger.info(f"Session embeddings trained: {len(sequences)} sessions, {vocab_size} tracks in vocabulary.")

    return {
        "trained": True,
        "sessions": len(sequences),
        "vocab_size": vocab_size,
        "embedding_dim": cfg.embedding_dim,
    }


def _save_model(model) -> str | None:
    """Persist the Word2Vec model via its built-in serializer so the next
    process boot can warm the singleton without retraining. (#43)"""
    try:
        model_dir = Path(_MODEL_DIR)
        model_dir.mkdir(parents=True, exist_ok=True)
        version = str(int(time.time()))
        path = model_dir / f"word2vec_sessions_{version}.model"
        model.save(str(path))
        logger.info(f"Session embeddings saved: {path}")
        return str(path)
    except Exception as e:
        logger.warning(f"Could not save session embeddings to disk: {e}")
        return None


def load_latest() -> bool:
    """Load the most recent Word2Vec model from ``_MODEL_DIR``. Returns True
    on success, False if no file exists or loading fails. (#43)"""
    model_dir = Path(_MODEL_DIR)
    if not model_dir.exists():
        return False

    candidates = sorted(model_dir.glob("word2vec_sessions_*.model"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return False

    path = candidates[0]
    try:
        from gensim.models import Word2Vec

        model = Word2Vec.load(str(path))
    except Exception as e:
        logger.warning(f"Session embeddings load_latest failed for {path}: {e}")
        return False

    with _lock:
        global _model, _track_ids
        _model = model
        _track_ids = list(model.wv.index_to_key)

    logger.info(f"Session embeddings loaded from disk: {path.name} ({len(model.wv)} tracks)")
    return True


def get_similar_tracks(
    track_id: str,
    k: int = 50,
    exclude_ids: set | None = None,
) -> list[tuple[str, float]]:
    """
    Find tracks that co-occur in sessions with the given track.

    Returns list of (track_id, similarity_score) sorted descending.
    """
    with _lock:
        model = _model

    if model is None or track_id not in model.wv:
        return []

    # Over-fetch to account for exclusions.
    fetch_k = k + (len(exclude_ids) if exclude_ids else 0) + 5
    try:
        similar = model.wv.most_similar(track_id, topn=min(fetch_k, len(model.wv) - 1))
    except KeyError:
        return []

    results: list[tuple[str, float]] = []
    for tid, score in similar:
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, float(score)))
        if len(results) >= k:
            break

    return results


def get_similar_to_tracks(
    track_ids: list[str],
    k: int = 50,
    exclude_ids: set | None = None,
) -> list[tuple[str, float]]:
    """
    Find tracks similar to the centroid of multiple tracks.

    Useful for user-profile-based retrieval: pass in the user's
    top tracks and get session-based recommendations.
    """
    with _lock:
        model = _model

    if model is None:
        return []

    # Compute mean vector of tracks that exist in vocabulary.
    vecs = []
    for tid in track_ids:
        if tid in model.wv:
            vecs.append(model.wv[tid])

    if not vecs:
        return []

    centroid = np.mean(vecs, axis=0)

    fetch_k = k + (len(exclude_ids) if exclude_ids else 0) + len(track_ids) + 5
    try:
        similar = model.wv.similar_by_vector(centroid, topn=min(fetch_k, len(model.wv)))
    except Exception:
        return []

    input_set = set(track_ids)
    results: list[tuple[str, float]] = []
    for tid, score in similar:
        if tid in input_set:
            continue
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, float(score)))
        if len(results) >= k:
            break

    return results


def is_ready() -> bool:
    """True if the model has been trained."""
    with _lock:
        return _model is not None


def vocab_size() -> int:
    """Number of tracks in the model vocabulary."""
    with _lock:
        if _model is None:
            return 0
        return len(_model.wv)
