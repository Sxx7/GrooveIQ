"""
GrooveIQ – Shared orphan-row prune helper.

Deletes ``track_features`` rows that point at files no longer on disk, in
bounded, committed chunks. Used by two callers:

  * ``POST /v1/library/cleanup-stale`` (manual, one-shot; deletes history too),
  * the library scanner's post-scan Phase A2 (unattended; preserves history by
    default — the listening heatmap + pipeline stats depend on ``listen_events``).

History deletion is opt-in (``delete_history``) precisely because Phase A2 runs
on an unattended cadence and must not silently destroy play history; the manual
endpoint keeps its original behaviour (deletes the orphan's interactions/events).
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import delete as sql_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ListenEvent, TrackFeatures, TrackInteraction

logger = logging.getLogger(__name__)


async def prune_orphan_track_features(
    session: AsyncSession,
    orphans: list[tuple[int, str]],
    *,
    delete_history: bool,
    chunk_size: int = 500,
) -> dict[str, int]:
    """Delete the given orphan ``track_features`` rows in committed chunks.

    ``orphans`` is a list of ``(track_features.id, track_id)`` pairs — the PK is
    used to delete the feature row, the ``track_id`` to optionally delete the
    orphan's history. A *present duplicate* of the same audio (different relpath)
    has a different ``track_id``, so its history is never touched.

    Commits per chunk and yields to the event loop between chunks so a large
    first prune (~37k rows) neither holds a single huge transaction nor blocks
    other work. Returns counts of what was deleted.
    """
    deleted_tf = 0
    deleted_interactions = 0
    deleted_events = 0

    for start in range(0, len(orphans), max(1, chunk_size)):
        chunk = orphans[start : start + chunk_size]
        if not chunk:
            continue
        ids = [tf_id for tf_id, _ in chunk]
        track_ids = [tid for _, tid in chunk]

        if delete_history:
            r = await session.execute(sql_delete(TrackInteraction).where(TrackInteraction.track_id.in_(track_ids)))
            deleted_interactions += r.rowcount or 0
            r = await session.execute(sql_delete(ListenEvent).where(ListenEvent.track_id.in_(track_ids)))
            deleted_events += r.rowcount or 0

        r = await session.execute(sql_delete(TrackFeatures).where(TrackFeatures.id.in_(ids)))
        deleted_tf += r.rowcount or 0

        await session.commit()
        await asyncio.sleep(0)  # yield between chunks

    logger.info(
        "Orphan prune: deleted %d track_features, %d interactions, %d events (delete_history=%s)",
        deleted_tf,
        deleted_interactions,
        deleted_events,
        delete_history,
    )
    return {
        "deleted_track_features": deleted_tf,
        "deleted_interactions": deleted_interactions,
        "deleted_events": deleted_events,
    }
