"""
GrooveIQ – CLAP embedding backfill.

After CLAP is enabled for the first time, every existing track in
``track_features`` is missing a ``clap_embedding`` (since it was analysed
before CLAP existed). Re-running the full library scanner would waste
hours re-computing DSP + EffNet. This module backfills just the CLAP
vector by reading each track's file, running it through the analysis
worker pool's CLAP session, and writing the result back.

Key properties:
  - No-op if ``CLAP_ENABLED=false``.
  - Processes tracks in batches; cancellable.
  - Skips tracks whose file no longer exists.
  - Idempotent — skips tracks that already have a ``clap_embedding``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select, update

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures

logger = logging.getLogger(__name__)


async def backfill_clap_embeddings(limit: int | None = None) -> dict:
    """
    Compute and persist CLAP embeddings for every track that doesn't have one.

    Runs the CLAP audio encoder inside each analysis worker (same pool used
    by the scanner). Writes results back to the ``clap_embedding`` column.

    Args:
        limit: Optional cap on the number of tracks to process in one call.
               Useful for chunked background runs.

    Returns metrics dict: ``{processed, updated, skipped_missing_file, errors}``.
    """
    from app.core.config import settings

    if not settings.CLAP_ENABLED:
        return {"processed": 0, "updated": 0, "skipped": "clap_disabled"}

    # --- Find candidates ------------------------------------------------
    async with AsyncSessionLocal() as session:
        q = (
            select(TrackFeatures.track_id, TrackFeatures.file_path)
            .where(TrackFeatures.clap_embedding.is_(None))
            .where(TrackFeatures.analysis_error.is_(None))
            .where(TrackFeatures.file_path.isnot(None))
        )
        if limit:
            q = q.limit(limit)
        rows = (await session.execute(q)).all()

    if not rows:
        return {"processed": 0, "updated": 0, "skipped": "none_pending"}

    logger.info("CLAP backfill: %d tracks pending", len(rows))
    started = time.monotonic()
    updated = 0
    missing = 0
    errors = 0

    # --- Submit to the existing worker pool ----------------------------
    from app.services.analysis_worker import get_worker_pool

    pool = await get_worker_pool()

    # The analysis worker returns the *entire* result dict (BPM, embedding,
    # everything). We only care about the CLAP embedding column so we
    # surgically update just that on success.
    for track_id, file_path in rows:
        import os

        if not os.path.exists(file_path):
            missing += 1
            continue

        try:
            # cached=None forces full re-analysis; the worker will compute
            # everything. We only persist clap_embedding to avoid churning
            # unrelated columns or bumping analysis_version.
            res = await pool.analyze(file_path, cached=None)
            if not res:
                continue
            clap = res.get("clap_embedding")
            if not clap:
                continue
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(TrackFeatures).where(TrackFeatures.track_id == track_id).values(clap_embedding=clap)
                )
                await session.commit()
            updated += 1
        except Exception as e:
            errors += 1
            logger.warning("CLAP backfill failed for %s: %s", file_path, e)

        # Yield to event loop between files to keep the API responsive.
        await asyncio.sleep(0)

    elapsed = time.monotonic() - started
    logger.info(
        "CLAP backfill complete: %d updated / %d total (%.1fs, %d missing files, %d errors)",
        updated,
        len(rows),
        elapsed,
        missing,
        errors,
    )

    # Rebuild the CLAP FAISS index once so the new embeddings become
    # searchable immediately.
    try:
        from app.services.faiss_index import clap_index

        await clap_index.rebuild(column="clap_embedding")
    except Exception as e:
        logger.warning("CLAP index rebuild after backfill failed: %s", e)

    return {
        "processed": len(rows),
        "updated": updated,
        "skipped_missing_file": missing,
        "errors": errors,
        "elapsed_seconds": round(elapsed, 1),
    }
