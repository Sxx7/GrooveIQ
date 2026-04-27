"""
GrooveIQ – FAISS index lifecycle manager.

Maintains one or more in-memory ANN indices over track embeddings for
fast similarity search. Index instances are module-level singletons
that can be atomically swapped via ``rebuild()``.

Two indices are exposed:

  - ``effnet_index`` (64-dim)  — primary embedding from the EffNet-Discogs
    backbone; drives similar tracks, playlists, radio, and ranker features.
  - ``clap_index`` (512-dim)  — optional joint text-audio embedding from
    LAION-CLAP; enables natural-language track search. Only built when
    ``CLAP_ENABLED=true`` and at least one track has a ``clap_embedding``.

Index strategy per instance:
  - <50k tracks → ``IndexFlatIP`` (exact inner product, no training needed)
  - ≥50k tracks → ``IndexIVFFlat`` (approximate, needs training, much faster)

Embeddings are L2-normalised before insertion so inner product == cosine.

Back-compat:
  The module still exposes the pre-refactor functions (``build_index``,
  ``rebuild``, ``search``, ``search_by_track_id``, ``get_embedding``,
  ``get_centroid``, ``is_ready``, ``index_size``). They all operate on the
  default 64-dim ``effnet_index`` so existing callers keep working.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import threading

import numpy as np
from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)

# Threshold for switching from flat to IVF per index.
_IVF_THRESHOLD = 50_000


def _decode_embedding(b64: str, expected_dim: int) -> np.ndarray | None:
    """Decode a base64-encoded float32 embedding; return L2-normalised vec."""
    try:
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32).copy()
        if vec.shape[0] != expected_dim:
            return None
        norm = np.linalg.norm(vec)
        if norm < 1e-9:
            return None
        vec /= norm
        return vec
    except Exception:
        return None


class FaissIndex:
    """A single FAISS ANN index over a fixed-dimension embedding column."""

    def __init__(self, dim: int, name: str):
        self.dim = dim
        self.name = name
        self._lock = threading.Lock()
        self._index: object | None = None  # faiss.Index
        self._id_to_track: list[str] = []
        self._track_to_id: dict[str, int] = {}
        self._embeddings: np.ndarray | None = None  # raw normalised matrix

    # -- build ----------------------------------------------------------

    def _build_sync(self, rows: list[tuple[str, str]]):
        import faiss

        track_ids: list[str] = []
        vectors: list[np.ndarray] = []
        for track_id, emb_b64 in rows:
            vec = _decode_embedding(emb_b64, self.dim)
            if vec is not None:
                track_ids.append(track_id)
                vectors.append(vec)

        if not vectors:
            return None, [], {}, None, 0

        matrix = np.stack(vectors).astype(np.float32)
        n = matrix.shape[0]

        if n < _IVF_THRESHOLD:
            index = faiss.IndexFlatIP(self.dim)
        else:
            nlist = min(int(np.sqrt(n)), 256)
            quantiser = faiss.IndexFlatIP(self.dim)
            index = faiss.IndexIVFFlat(quantiser, self.dim, nlist, faiss.METRIC_INNER_PRODUCT)
            index.train(matrix)
            index.nprobe = min(nlist // 4, 16)

        index.add(matrix)
        id_map = {tid: i for i, tid in enumerate(track_ids)}
        return index, track_ids, id_map, matrix, n

    async def rebuild(self, column: str) -> int:
        """Load all rows where ``column`` is not null and rebuild the index."""
        col = getattr(TrackFeatures, column)
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(TrackFeatures.track_id, col).where(col.isnot(None)))
            rows = result.all()

        loop = asyncio.get_running_loop()
        index, track_ids, id_map, matrix, n = await loop.run_in_executor(None, self._build_sync, rows)

        if n == 0:
            logger.warning("FAISS build (%s): no valid embeddings found, index empty.", self.name)

        with self._lock:
            self._index = index
            self._id_to_track = track_ids
            self._track_to_id = id_map
            self._embeddings = matrix

        if n > 0:
            logger.info(
                "FAISS index '%s' built: %d tracks (%s, dim=%d).",
                self.name,
                n,
                "IVF" if n >= _IVF_THRESHOLD else "Flat",
                self.dim,
            )
        return n

    # -- query ---------------------------------------------------------

    def search(
        self,
        embedding: np.ndarray,
        k: int = 50,
        exclude_ids: set | None = None,
    ) -> list[tuple[str, float]]:
        with self._lock:
            index = self._index
            id_to_track = self._id_to_track

        if index is None or len(id_to_track) == 0:
            return []

        vec = embedding.astype(np.float32).reshape(1, -1)
        norm = np.linalg.norm(vec)
        if norm < 1e-9:
            return []
        vec /= norm

        fetch_k = min(k + (len(exclude_ids) if exclude_ids else 0) + 10, len(id_to_track))
        scores, ids = index.search(vec, fetch_k)

        results: list[tuple[str, float]] = []
        for score, idx in zip(scores[0], ids[0]):
            if idx < 0:
                continue
            tid = id_to_track[idx]
            if exclude_ids and tid in exclude_ids:
                continue
            results.append((tid, float(score)))
            if len(results) >= k:
                break
        return results

    def search_by_track_id(
        self,
        track_id: str,
        k: int = 50,
        exclude_ids: set | None = None,
    ) -> list[tuple[str, float]]:
        with self._lock:
            tm = self._track_to_id
            embs = self._embeddings
        if embs is None or track_id not in tm:
            return []
        vec = embs[tm[track_id]]
        excl = {track_id}
        if exclude_ids:
            excl |= exclude_ids
        return self.search(vec, k=k, exclude_ids=excl)

    def get_embedding(self, track_id: str) -> np.ndarray | None:
        with self._lock:
            tm = self._track_to_id
            embs = self._embeddings
        if embs is None or track_id not in tm:
            return None
        return embs[tm[track_id]].copy()

    def get_centroid(self, track_ids: list[str]) -> np.ndarray | None:
        with self._lock:
            tm = self._track_to_id
            embs = self._embeddings
        if embs is None:
            return None
        vecs = [embs[tm[tid]] for tid in track_ids if tid in tm]
        if not vecs:
            return None
        centroid = np.mean(vecs, axis=0).astype(np.float32)
        norm = np.linalg.norm(centroid)
        if norm < 1e-9:
            return None
        return (centroid / norm).astype(np.float32)

    def is_ready(self) -> bool:
        with self._lock:
            return self._index is not None and len(self._id_to_track) > 0

    def size(self) -> int:
        with self._lock:
            return len(self._id_to_track)


# ---------------------------------------------------------------------------
# Singleton instances
# ---------------------------------------------------------------------------

effnet_index = FaissIndex(dim=64, name="effnet")
clap_index = FaissIndex(dim=512, name="clap")


# ---------------------------------------------------------------------------
# Back-compat functional API (operates on the 64-dim EffNet index)
# ---------------------------------------------------------------------------


async def build_index() -> int:
    """Build the primary 64-dim EffNet FAISS index."""
    n = await effnet_index.rebuild(column="embedding")
    # Also rebuild the CLAP index if enabled and populated.
    try:
        from app.core.config import settings

        if settings.CLAP_ENABLED:
            await clap_index.rebuild(column="clap_embedding")
    except Exception as e:
        logger.warning("CLAP index rebuild failed: %s", e)
    return n


async def rebuild() -> int:
    """Convenience alias — rebuilds both indices."""
    return await build_index()


def search(embedding: np.ndarray, k: int = 50, exclude_ids: set | None = None) -> list[tuple[str, float]]:
    return effnet_index.search(embedding, k=k, exclude_ids=exclude_ids)


def search_by_track_id(track_id: str, k: int = 50, exclude_ids: set | None = None) -> list[tuple[str, float]]:
    return effnet_index.search_by_track_id(track_id, k=k, exclude_ids=exclude_ids)


def get_embedding(track_id: str) -> np.ndarray | None:
    return effnet_index.get_embedding(track_id)


def get_centroid(track_ids: list[str]) -> np.ndarray | None:
    return effnet_index.get_centroid(track_ids)


def is_ready() -> bool:
    return effnet_index.is_ready()


def index_size() -> int:
    return effnet_index.size()
