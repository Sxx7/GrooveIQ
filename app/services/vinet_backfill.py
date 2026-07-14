"""
GrooveIQ – Discogs-VINet version embedding backfill.

After VINet is enabled for the first time, every existing track in
``track_features`` is missing a ``version_embedding`` (it was analysed before
VINet existed). Re-running the full library scanner would waste hours
re-computing DSP + EffNet. This module backfills just the version vector.

Unlike the CLAP backfill — which calls ``pool.analyze()`` and re-runs the
*entire* Essentia+EffNet pipeline per file, throwing all but one column away —
this uses a dedicated compute-only worker path (``pool.compute_vinet_only``)
that runs ONLY decode@22050 → CQT → CQTNet. That's ~1.5–3.5 s/track CPU vs a
full rescan's worth of work.

Key properties:
  - No-op if ``VINET_ENABLED=false``.
  - Processes tracks in batches; cancellable.
  - Skips tracks whose file no longer exists.
  - Idempotent — skips tracks that already have a ``version_embedding``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from sqlalchemy import select, update

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)


async def backfill_vinet_embeddings(limit: int | None = None) -> dict:
    """
    Compute and persist Discogs-VINet version embeddings for every track that
    doesn't have one, then rebuild the version FAISS index.

    Args:
        limit: Optional cap on the number of tracks to process in one call.
               Useful for chunked background runs.

    Returns metrics dict:
    ``{processed, updated, skipped_missing_file, errors, elapsed_seconds}``.
    """
    from app.core.config import settings

    if not settings.VINET_ENABLED:
        return {"processed": 0, "updated": 0, "skipped": "vinet_disabled"}

    # --- Find candidates ------------------------------------------------
    async with AsyncSessionLocal() as session:
        q = (
            select(TrackFeatures.track_id, TrackFeatures.file_path)
            .where(TrackFeatures.version_embedding.is_(None))
            .where(TrackFeatures.analysis_error.is_(None))
            .where(TrackFeatures.file_path.isnot(None))
        )
        if limit:
            q = q.limit(limit)
        rows = (await session.execute(q)).all()

    if not rows:
        return {"processed": 0, "updated": 0, "skipped": "none_pending"}

    logger.info("VINet backfill: %d tracks pending", len(rows))
    started = time.monotonic()
    updated = 0
    missing = 0
    errors = 0

    # --- Submit to the existing worker pool (compute-only path) ---------
    from app.services.analysis_worker import get_worker_pool

    pool = await get_worker_pool()

    for track_id, file_path in rows:
        if not os.path.exists(file_path):
            missing += 1
            continue

        try:
            # Compute-only: decode@22050 → CQT → CQTNet, no Essentia/EffNet.
            emb = await pool.compute_vinet_only(file_path)
            if not emb:
                continue
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(TrackFeatures).where(TrackFeatures.track_id == track_id).values(version_embedding=emb)
                )
                await session.commit()
            updated += 1
        except Exception as e:
            errors += 1
            logger.warning("VINet backfill failed for %s: %s", file_path, e)

        # Yield to event loop between files to keep the API responsive.
        await asyncio.sleep(0)

    elapsed = time.monotonic() - started
    logger.info(
        "VINet backfill complete: %d updated / %d total (%.1fs, %d missing files, %d errors)",
        updated,
        len(rows),
        elapsed,
        missing,
        errors,
    )

    # Rebuild the version FAISS index once so the new embeddings become
    # searchable immediately.
    try:
        from app.services.faiss_index import version_index

        await version_index.rebuild(column="version_embedding")
    except Exception as e:
        logger.warning("VINet index rebuild after backfill failed: %s", e)

    return {
        "processed": len(rows),
        "updated": updated,
        "skipped_missing_file": missing,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
    }
