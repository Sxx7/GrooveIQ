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
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, ListenSession

logger = logging.getLogger(__name__)

# Singleton state.
_lock = threading.Lock()
_model: Optional[object] = None  # gensim Word2Vec
_track_ids: List[str] = []       # tracks in the model vocabulary

# Training config.
_EMBEDDING_DIM = 64
_WINDOW_SIZE = 5       # context window (tracks before/after)
_MIN_COUNT = 2         # ignore tracks appearing fewer than this many times
_EPOCHS = 20           # training iterations
_MIN_SESSIONS = 10     # don't train if fewer sessions than this
_MIN_VOCAB = 5         # don't train if fewer unique tracks than this


async def _load_session_sequences() -> List[List[str]]:
    """
    Load track sequences from sessions.

    For each session, fetches the ordered play_start events to reconstruct
    the track sequence the user actually listened to.
    """
    async with AsyncSessionLocal() as session:
        # Get all sessions with enough tracks.
        sess_result = await session.execute(
            select(ListenSession.session_key, ListenSession.user_id,
                   ListenSession.event_id_min, ListenSession.event_id_max)
            .where(ListenSession.track_count >= 2)
            .order_by(ListenSession.started_at)
        )
        sessions = sess_result.all()

        if not sessions:
            return []

        sequences: List[List[str]] = []

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
            deduped: List[str] = []
            for tid in track_ids:
                if not deduped or deduped[-1] != tid:
                    deduped.append(tid)

            if len(deduped) >= 2:
                sequences.append(deduped)

    return sequences


def _train_model_sync(sequences: List[List[str]]) -> Optional[object]:
    """CPU-bound Word2Vec training. Runs in a thread executor."""
    from gensim.models import Word2Vec

    model = Word2Vec(
        sentences=sequences,
        vector_size=_EMBEDDING_DIM,
        window=_WINDOW_SIZE,
        min_count=_MIN_COUNT,
        sg=1,           # skip-gram (not CBOW)
        workers=1,      # single thread inside executor
        epochs=_EPOCHS,
        seed=42,
    )
    return model


async def train() -> Dict:
    """
    Train session skip-gram embeddings from all listening sessions.

    Returns summary dict with training stats.
    """
    import asyncio

    sequences = await _load_session_sequences()

    if len(sequences) < _MIN_SESSIONS:
        logger.info(
            f"Session embeddings: only {len(sequences)} sessions "
            f"(< {_MIN_SESSIONS}), skipping training."
        )
        return {
            "trained": False,
            "sessions": len(sequences),
            "reason": "insufficient_sessions",
        }

    # Check vocabulary size.
    all_tracks = set()
    for seq in sequences:
        all_tracks.update(seq)

    if len(all_tracks) < _MIN_VOCAB:
        logger.info(
            f"Session embeddings: only {len(all_tracks)} unique tracks "
            f"(< {_MIN_VOCAB}), skipping training."
        )
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

    logger.info(
        f"Session embeddings trained: {len(sequences)} sessions, "
        f"{vocab_size} tracks in vocabulary."
    )

    return {
        "trained": True,
        "sessions": len(sequences),
        "vocab_size": vocab_size,
        "embedding_dim": _EMBEDDING_DIM,
    }


def get_similar_tracks(
    track_id: str,
    k: int = 50,
    exclude_ids: Optional[set] = None,
) -> List[Tuple[str, float]]:
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

    results: List[Tuple[str, float]] = []
    for tid, score in similar:
        if exclude_ids and tid in exclude_ids:
            continue
        results.append((tid, float(score)))
        if len(results) >= k:
            break

    return results


def get_similar_to_tracks(
    track_ids: List[str],
    k: int = 50,
    exclude_ids: Optional[set] = None,
) -> List[Tuple[str, float]]:
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
    results: List[Tuple[str, float]] = []
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
