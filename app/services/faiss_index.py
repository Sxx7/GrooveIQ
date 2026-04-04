"""
GrooveIQ – FAISS index lifecycle manager (Phase 4).

Maintains an in-memory ANN index over track embeddings for fast
similarity search.  The index is a module-level singleton that can
be atomically swapped via rebuild().

Index strategy:
  - <50k tracks → IndexFlatIP (exact inner product, no training needed)
  - ≥50k tracks → IndexIVFFlat (approximate, needs training, much faster)

Embeddings are L2-normalised before insertion so inner product == cosine similarity.
"""

from __future__ import annotations

import base64
import logging
import threading
from typing import Dict, List, Optional, Tuple

import numpy as np

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)

# Singleton state — protected by a lock for atomic swap.
_lock = threading.Lock()
_index: Optional[object] = None          # faiss.Index
_id_to_track: List[str] = []             # FAISS int id → track_id
_track_to_id: Dict[str, int] = {}        # track_id → FAISS int id
_embeddings: Optional[np.ndarray] = None # raw normalised matrix (for centroid queries)

# Threshold for switching from flat to IVF.
_IVF_THRESHOLD = 50_000
_EMBEDDING_DIM = 64


def _decode_embedding(b64: str) -> Optional[np.ndarray]:
    """Decode a base64-encoded float32 embedding, return None on failure."""
    try:
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32).copy()
        if vec.shape[0] != _EMBEDDING_DIM:
            return None
        norm = np.linalg.norm(vec)
        if norm < 1e-9:
            return None
        vec /= norm  # L2-normalise → inner product == cosine sim
        return vec
    except Exception:
        return None


def _build_index_sync(rows: List[Tuple[str, str]]) -> Tuple[Optional[object], List[str], Dict[str, int], Optional[np.ndarray], int]:
    """
    CPU-bound FAISS index construction.  Runs in a thread executor so it
    doesn't block the async event loop.
    """
    import faiss

    track_ids: List[str] = []
    vectors: List[np.ndarray] = []
    for track_id, emb_b64 in rows:
        vec = _decode_embedding(emb_b64)
        if vec is not None:
            track_ids.append(track_id)
            vectors.append(vec)

    if not vectors:
        return None, [], {}, None, 0

    matrix = np.stack(vectors).astype(np.float32)
    n = matrix.shape[0]

    if n < _IVF_THRESHOLD:
        index = faiss.IndexFlatIP(_EMBEDDING_DIM)
    else:
        nlist = min(int(np.sqrt(n)), 256)
        quantiser = faiss.IndexFlatIP(_EMBEDDING_DIM)
        index = faiss.IndexIVFFlat(quantiser, _EMBEDDING_DIM, nlist, faiss.METRIC_INNER_PRODUCT)
        index.train(matrix)
        index.nprobe = min(nlist // 4, 16)

    index.add(matrix)
    id_map = {tid: i for i, tid in enumerate(track_ids)}
    return index, track_ids, id_map, matrix, n


async def build_index() -> int:
    """
    Load all embeddings from DB and build a FAISS index.
    Returns the number of tracks indexed.
    """
    import asyncio

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.embedding)
            .where(TrackFeatures.embedding.isnot(None))
        )
        rows = result.all()

    # Run CPU-heavy numpy/FAISS work in a thread so the event loop stays responsive.
    loop = asyncio.get_running_loop()
    index, track_ids, id_map, matrix, n = await loop.run_in_executor(
        None, _build_index_sync, rows,
    )

    if n == 0:
        logger.warning("FAISS build: no valid embeddings found, index empty.")

    # Atomic swap.
    with _lock:
        global _index, _id_to_track, _track_to_id, _embeddings
        _index = index
        _id_to_track = track_ids
        _track_to_id = id_map
        _embeddings = matrix

    if n > 0:
        logger.info(f"FAISS index built: {n} tracks indexed ({'IVF' if n >= _IVF_THRESHOLD else 'Flat'}).")
    return n


async def rebuild() -> int:
    """Convenience alias — rebuilds the full index."""
    return await build_index()


def search(embedding: np.ndarray, k: int = 50, exclude_ids: Optional[set] = None) -> List[Tuple[str, float]]:
    """
    Search the index for the k nearest neighbours of `embedding`.

    Args:
        embedding: 64-dim float32 vector (will be L2-normalised internally).
        k: number of results.
        exclude_ids: set of track_id strings to exclude from results.

    Returns:
        List of (track_id, similarity_score) tuples, descending by score.
    """
    with _lock:
        index = _index
        id_to_track = _id_to_track

    if index is None or len(id_to_track) == 0:
        return []

    # Normalise query vector.
    vec = embedding.astype(np.float32).reshape(1, -1)
    norm = np.linalg.norm(vec)
    if norm < 1e-9:
        return []
    vec /= norm

    # Over-fetch if we need to filter.
    fetch_k = min(k + (len(exclude_ids) if exclude_ids else 0) + 10, len(id_to_track))
    scores, ids = index.search(vec, fetch_k)

    results: List[Tuple[str, float]] = []
    for score, idx in zip(scores[0], ids[0]):
        if idx < 0:
            continue
        track_id = id_to_track[idx]
        if exclude_ids and track_id in exclude_ids:
            continue
        results.append((track_id, float(score)))
        if len(results) >= k:
            break

    return results


def search_by_track_id(track_id: str, k: int = 50, exclude_ids: Optional[set] = None) -> List[Tuple[str, float]]:
    """Search for neighbours of a track already in the index."""
    with _lock:
        track_map = _track_to_id
        embs = _embeddings

    if embs is None or track_id not in track_map:
        return []

    idx = track_map[track_id]
    vec = embs[idx]
    # Exclude the seed track itself.
    excl = {track_id}
    if exclude_ids:
        excl |= exclude_ids
    return search(vec, k=k, exclude_ids=excl)


def get_embedding(track_id: str) -> Optional[np.ndarray]:
    """Return the normalised embedding for a track, or None."""
    with _lock:
        track_map = _track_to_id
        embs = _embeddings

    if embs is None or track_id not in track_map:
        return None
    return embs[track_map[track_id]].copy()


def get_centroid(track_ids: List[str]) -> Optional[np.ndarray]:
    """
    Compute the mean embedding (centroid) of a list of tracks.
    Returns None if none of the tracks are in the index.
    """
    with _lock:
        track_map = _track_to_id
        embs = _embeddings

    if embs is None:
        return None

    vecs = []
    for tid in track_ids:
        idx = track_map.get(tid)
        if idx is not None:
            vecs.append(embs[idx])

    if not vecs:
        return None

    centroid = np.mean(vecs, axis=0).astype(np.float32)
    norm = np.linalg.norm(centroid)
    if norm < 1e-9:
        return None
    centroid /= norm
    return centroid


def is_ready() -> bool:
    """True if the index has been built and contains at least one track."""
    with _lock:
        return _index is not None and len(_id_to_track) > 0


def index_size() -> int:
    """Number of tracks in the index."""
    with _lock:
        return len(_id_to_track)
