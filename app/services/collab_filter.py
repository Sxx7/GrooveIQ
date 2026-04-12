"""
GrooveIQ – Collaborative filtering model manager (Phase 4).

"Users who listened to X also listened to Y" — implemented via
implicit ALS (Alternating Least Squares) on the (user × track)
interaction matrix.

The model is a module-level singleton rebuilt periodically by the
recommendation pipeline scheduler.

Edge cases:
  - User not in training set (cold start) → return empty
  - <10 total interactions → skip training, log warning
  - Single-user system → CF is meaningless, return empty
  - Track not in training set → return empty for similar_items
"""

from __future__ import annotations

import logging
import threading

import numpy as np
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.db import TrackInteraction

logger = logging.getLogger(__name__)

# Singleton state.
_lock = threading.Lock()
_model: object | None = None  # implicit.als.AlternatingLeastSquares
_user_to_idx: dict[str, int] = {}
_idx_to_user: list[str] = []
_track_to_idx: dict[str, int] = {}
_idx_to_track: list[str] = []
_interaction_matrix = None  # scipy.sparse.csr_matrix (user × track)

# Minimum interactions to bother training.
_MIN_INTERACTIONS = 10
_MIN_USERS = 2


def _train_als_sync(rows: list) -> tuple[object, dict[str, int], list[str], dict[str, int], list[str], object]:
    """CPU-bound ALS training.  Runs in a thread executor."""
    import scipy.sparse as sp
    from implicit.als import AlternatingLeastSquares

    user_ids: list[str] = []
    track_ids: list[str] = []
    user_map: dict[str, int] = {}
    track_map: dict[str, int] = {}
    user_indices: list[int] = []
    track_indices: list[int] = []
    values: list[float] = []

    for user_id, track_id, score in rows:
        if user_id not in user_map:
            user_map[user_id] = len(user_ids)
            user_ids.append(user_id)
        if track_id not in track_map:
            track_map[track_id] = len(track_ids)
            track_ids.append(track_id)
        val = max(float(score or 0), 0.01)
        user_indices.append(user_map[user_id])
        track_indices.append(track_map[track_id])
        values.append(val)

    n_users = len(user_ids)
    n_tracks = len(track_ids)

    matrix = sp.csr_matrix(
        (values, (user_indices, track_indices)),
        shape=(n_users, n_tracks),
        dtype=np.float32,
    )

    model = AlternatingLeastSquares(
        factors=64,
        iterations=15,
        regularization=0.1,
        random_state=42,
    )
    model.fit(matrix)

    return model, user_map, user_ids, track_map, track_ids, matrix


async def build_model() -> dict:
    """
    Load interactions from DB, build a sparse matrix, train ALS.

    Returns summary: {tracks, users, interactions, trained: bool}.
    """
    import asyncio

    async with AsyncSessionLocal() as session:
        count_result = await session.execute(select(func.count(TrackInteraction.id)))
        total = count_result.scalar_one()

        if total < _MIN_INTERACTIONS:
            logger.warning(f"CF build: only {total} interactions (<{_MIN_INTERACTIONS}), skipping.")
            return {"tracks": 0, "users": 0, "interactions": total, "trained": False}

        user_count = (await session.execute(select(func.count(func.distinct(TrackInteraction.user_id))))).scalar_one()

        if user_count < _MIN_USERS:
            logger.warning(f"CF build: only {user_count} user(s), CF is meaningless, skipping.")
            return {"tracks": 0, "users": user_count, "interactions": total, "trained": False}

        result = await session.execute(
            select(
                TrackInteraction.user_id,
                TrackInteraction.track_id,
                TrackInteraction.satisfaction_score,
            )
        )
        rows = result.all()

    # Run CPU-heavy ALS training in a thread so the event loop stays responsive.
    loop = asyncio.get_running_loop()
    model, user_map, user_ids, track_map, track_ids, matrix = await loop.run_in_executor(
        None,
        _train_als_sync,
        rows,
    )

    n_users = len(user_ids)
    n_tracks = len(track_ids)

    with _lock:
        global _model, _user_to_idx, _idx_to_user, _track_to_idx, _idx_to_track, _interaction_matrix
        _model = model
        _user_to_idx = user_map
        _idx_to_user = user_ids
        _track_to_idx = track_map
        _idx_to_track = track_ids
        _interaction_matrix = matrix

    logger.info(f"CF model trained: {n_users} users, {n_tracks} tracks, {len(rows)} interactions.")
    return {"tracks": n_tracks, "users": n_users, "interactions": len(rows), "trained": True}


def get_cf_candidates(user_id: str, k: int = 200) -> list[tuple[str, float]]:
    """
    Recommend tracks for a user via collaborative filtering.

    Returns list of (track_id, score) tuples.
    Empty list if user is unknown or model not trained.
    """
    with _lock:
        model = _model
        user_map = _user_to_idx
        idx_to_track = _idx_to_track
        matrix = _interaction_matrix

    if model is None or user_id not in user_map:
        return []

    user_idx = user_map[user_id]
    user_items = matrix[user_idx]

    ids, scores = model.recommend(
        user_idx,
        user_items,
        N=k,
        filter_already_liked_items=False,
    )

    results: list[tuple[str, float]] = []
    for idx, score in zip(ids, scores):
        if 0 <= idx < len(idx_to_track):
            results.append((idx_to_track[idx], float(score)))

    return results


def get_similar_items(track_id: str, k: int = 50) -> list[tuple[str, float]]:
    """
    Item-item collaborative filtering: tracks that co-occur with the given track.

    Returns list of (track_id, score) tuples.
    """
    with _lock:
        model = _model
        track_map = _track_to_idx
        idx_to_track = _idx_to_track

    if model is None or track_id not in track_map:
        return []

    track_idx = track_map[track_id]
    ids, scores = model.similar_items(track_idx, N=k + 1)  # +1 because it includes itself

    results: list[tuple[str, float]] = []
    for idx, score in zip(ids, scores):
        if 0 <= idx < len(idx_to_track):
            tid = idx_to_track[idx]
            if tid != track_id:
                results.append((tid, float(score)))

    return results[:k]


def is_ready() -> bool:
    """True if the CF model has been trained."""
    with _lock:
        return _model is not None


def model_stats() -> dict:
    """Return basic stats about the trained model."""
    with _lock:
        if _model is None:
            return {"trained": False}
        return {
            "trained": True,
            "users": len(_idx_to_user),
            "tracks": len(_idx_to_track),
        }
