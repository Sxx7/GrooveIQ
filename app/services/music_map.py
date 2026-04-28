"""
GrooveIQ – 2D music map service.

Projects the 64-dim EffNet embeddings of every analysed track down to 2D via
UMAP so the dashboard can render the library as an explorable map.

The projection is computed offline (as a pipeline step) and persisted on
``TrackFeatures.map_x`` / ``map_y`` in the normalised ``[0, 1]`` range.
The API layer then just reads these two scalar columns and renders points
on a canvas.

Design notes:
  - Inputs are the existing 64-dim `embedding` columns; no new analysis needed.
  - UMAP is CPU-only; runs in a thread executor to avoid blocking the loop.
  - We bulk-UPDATE in one transaction; SQLite handles this fine up to ~100k rows.
  - If UMAP isn't installed or fewer than ``MIN_TRACKS`` tracks exist, the step
    returns ``{"tracks_mapped": 0, "skipped": <reason>}`` and is treated as a
    successful no-op by the pipeline.
"""

from __future__ import annotations

import asyncio
import base64
import logging

import numpy as np
from sqlalchemy import select, update

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)

# Minimum tracks required for a meaningful UMAP projection.
# Below this, the 2D layout is mostly noise.
MIN_TRACKS = 50

# UMAP hyperparameters. Chosen to give visually pleasant, locally-coherent
# layouts on typical personal libraries (5k–50k tracks).
_UMAP_N_NEIGHBORS = 15
_UMAP_MIN_DIST = 0.1
_UMAP_METRIC = "cosine"


def _decode_embedding(b64: str) -> np.ndarray | None:
    """Decode a base64-encoded float32 embedding. Returns None on failure."""
    try:
        vec = np.frombuffer(base64.b64decode(b64), dtype=np.float32)
        if vec.size != 64:
            return None
        return vec
    except Exception:
        return None


def _project_sync(
    track_ids: list[str],
    matrix: np.ndarray,
) -> tuple[list[str], np.ndarray] | None:
    """Run UMAP in a worker thread. Returns (track_ids, coords_2d) or None."""
    try:
        import umap  # type: ignore
    except (ImportError, RuntimeError) as e:
        # RuntimeError covers numba JIT-cache failures (e.g. no writable
        # NUMBA_CACHE_DIR), which surface at umap import time.
        logger.warning("umap import failed; music-map step skipped: %s", e)
        return None

    n = matrix.shape[0]
    n_neighbors = min(_UMAP_N_NEIGHBORS, max(2, n - 1))
    reducer = umap.UMAP(
        n_components=2,
        n_neighbors=n_neighbors,
        min_dist=_UMAP_MIN_DIST,
        metric=_UMAP_METRIC,
        random_state=42,  # reproducible layouts across runs
    )
    coords = reducer.fit_transform(matrix).astype(np.float32)

    # Normalise each axis independently to [0, 1] so the frontend doesn't need
    # to rescale. Guard against degenerate (zero-range) axes.
    for col in range(2):
        lo, hi = float(coords[:, col].min()), float(coords[:, col].max())
        if hi - lo < 1e-9:
            coords[:, col] = 0.5
        else:
            coords[:, col] = (coords[:, col] - lo) / (hi - lo)

    return track_ids, coords


async def build_map() -> dict:
    """
    Rebuild the 2D music map.

    Reads all rows in ``track_features`` that have a valid 64-dim embedding,
    runs UMAP, and writes the 2D coordinates back to ``map_x`` / ``map_y``.

    Returns a metrics dict suitable for pipeline instrumentation.
    """
    # --- Load embeddings -------------------------------------------------
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(TrackFeatures.track_id, TrackFeatures.embedding).where(TrackFeatures.embedding.isnot(None))
            )
        ).all()

    track_ids: list[str] = []
    vectors: list[np.ndarray] = []
    for tid, b64 in rows:
        vec = _decode_embedding(b64)
        if vec is not None:
            track_ids.append(tid)
            vectors.append(vec)

    if len(vectors) < MIN_TRACKS:
        logger.info(
            "Music map: only %d tracks with embeddings (need %d); skipping",
            len(vectors),
            MIN_TRACKS,
        )
        return {"tracks_mapped": 0, "skipped": "insufficient_tracks"}

    matrix = np.stack(vectors).astype(np.float32)

    # --- Project ---------------------------------------------------------
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _project_sync, track_ids, matrix)
    if result is None:
        return {"tracks_mapped": 0, "skipped": "umap_not_installed"}
    track_ids, coords = result

    # --- Persist (bulk update) ------------------------------------------
    async with AsyncSessionLocal() as session:
        for tid, (x, y) in zip(track_ids, coords):
            await session.execute(
                update(TrackFeatures).where(TrackFeatures.track_id == tid).values(map_x=float(x), map_y=float(y))
            )
        await session.commit()

    logger.info("Music map rebuilt: %d tracks mapped", len(track_ids))
    return {
        "tracks_mapped": len(track_ids),
        "n_neighbors": min(_UMAP_N_NEIGHBORS, max(2, len(track_ids) - 1)),
        "metric": _UMAP_METRIC,
    }
