"""GrooveIQ – goal C: "newly-added media" notification (P4).

Runs post-scan (after media sync stamps ``media_server_id`` and after the
followed-artist reconciler). Coalesces a scan's newly-*playable* tracks into
per-album ``newly_added`` events and fans them out to every user with an opted-in
device — "new music, regardless of origin".

Why a per-track marker instead of a time cursor: a track can be inserted in one
scan (no ``media_server_id`` yet) and only become streamable in a later scan. The
candidate set is ``media_server_id IS NOT NULL AND new_media_notified_at IS NULL``,
so such a track is caught exactly when it becomes playable — not missed by a
global "added since T" cursor (which its old ``created_at`` would fall behind).

Dedup + precedence (goal D): each album emits with dedup_key
``norm(artist)|norm(album)``, and ``notification_deliveries`` is unique on
``(user_id, dedup_key)``. Because this runs AFTER the download emit (which fires
before the rescan) and AFTER the followed-artist reconciler, a same-album
download (goal B) or new-release (goal A) delivery already exists and the
newly-added insert is suppressed — the user gets one push, not two/three.

First-scan storm guard: a scan that turns MORE than
``NOTIFY_NEW_MEDIA_BASELINE_TRACKS`` tracks newly-playable is treated as a bulk
import / initial library population and *baselined* — the rows are marked
processed (so they never fire) with no push. Normal incremental scans add a
handful of tracks and notify per album, capped at
``NOTIFY_NEW_MEDIA_MAX_ALBUMS_PER_SCAN`` (the rest drain on later scans).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy import update as sql_update

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import Device, TrackFeatures

logger = logging.getLogger(__name__)


async def _mark_processed(session, ids: list[int], now: int, *, chunk_size: int = 500) -> None:
    """Stamp ``new_media_notified_at`` on the given track ids, chunked to stay
    under SQLite's bound-parameter limit."""
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        if chunk:
            await session.execute(
                sql_update(TrackFeatures).where(TrackFeatures.id.in_(chunk)).values(new_media_notified_at=now)
            )


async def notify_newly_added_media() -> dict[str, Any]:
    """Emit ``newly_added`` events for newly-playable tracks. Opens its own
    session; idempotent (the per-track marker + the delivery unique)."""
    if not (settings.NOTIFY_NEW_MEDIA_ENABLED and settings.push_enabled):
        return {"skipped": "disabled"}

    from app.services.notification_dispatch import media_dedup_key

    now = int(time.time())
    async with AsyncSessionLocal() as session:
        candidate_count = (
            await session.scalar(
                select(func.count())
                .select_from(TrackFeatures)
                .where(TrackFeatures.media_server_id.isnot(None), TrackFeatures.new_media_notified_at.is_(None))
            )
        ) or 0
        if candidate_count == 0:
            return {"candidates": 0}

        # Bulk-baseline guard: an initial population / large import is marked
        # processed in one UPDATE (no row load, no push).
        if candidate_count > settings.NOTIFY_NEW_MEDIA_BASELINE_TRACKS:
            await session.execute(
                sql_update(TrackFeatures)
                .where(TrackFeatures.media_server_id.isnot(None), TrackFeatures.new_media_notified_at.is_(None))
                .values(new_media_notified_at=now)
            )
            await session.commit()
            logger.info("Newly-added: baselined %d tracks (bulk import / initial scan) — no push", candidate_count)
            return {"baselined": candidate_count, "notified_albums": 0}

        rows = (
            await session.execute(
                select(TrackFeatures.id, TrackFeatures.artist, TrackFeatures.album).where(
                    TrackFeatures.media_server_id.isnot(None), TrackFeatures.new_media_notified_at.is_(None)
                )
            )
        ).all()

        # Group candidates by album dedup key. Untagged rows (no usable key) are
        # marked processed and skipped so they don't linger as permanent
        # candidates. The autoincrement id is the recency proxy (newest = highest
        # id) since track_features carries no insert timestamp.
        albums: dict[str, dict[str, Any]] = {}
        untagged_ids: list[int] = []
        for tid, artist, album in rows:
            key = media_dedup_key(artist, album)
            if key is None:
                untagged_ids.append(tid)
                continue
            g = albums.setdefault(key, {"artist": artist, "album": album, "ids": [], "newest": 0})
            g["ids"].append(tid)
            g["newest"] = max(g["newest"], tid)

        # Audience: distinct users with an active, opted-in device. No audience →
        # baseline everything (a user who opts in later gets FUTURE additions only).
        audience = list(
            (
                await session.execute(
                    select(Device.user_id)
                    .where(Device.notif_new_media.isnot(False), Device.disabled_at.is_(None))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not audience:
            await _mark_processed(session, [tid for tid, *_ in rows], now)
            await session.commit()
            logger.info("Newly-added: no opted-in devices; baselined %d tracks", len(rows))
            return {"baselined": len(rows), "notified_albums": 0}

        # Emit OLDEST-first (FIFO drain), capped; the rest stay unprocessed and
        # drain on later scans. Oldest-first is deliberate: under sustained inflow
        # a newest-first order would starve an old deferred album forever (and a
        # later bulk-baseline sweep would then silently drop it). FIFO guarantees
        # every deferred album eventually fires.
        ordered = sorted(albums.items(), key=lambda kv: kv[1]["newest"])
        cap = settings.NOTIFY_NEW_MEDIA_MAX_ALBUMS_PER_SCAN
        to_emit = ordered if cap <= 0 else ordered[:cap]
        dropped = len(ordered) - len(to_emit)

        from app.services.notification_dispatch import emit_notification

        processed_ids: list[int] = list(untagged_ids)
        deliveries = 0
        for key, g in to_emit:
            artist = g["artist"]
            album = g["album"]
            body = f'{artist} — "{album}" was added to your library' if artist else f'"{album}" was added to your library'
            deliveries += await emit_notification(
                session,
                event_type="newly_added",
                title="New music added",
                body=body,
                user_ids=audience,
                dedup_key=key,
                # data.type is the client's tap-routing key (iOS routeTap = "new_media");
                # the event_type discriminator stays "newly_added" for backend dispatch.
                data={"type": "new_media", "artist": artist, "album": album},
                now=now,
            )
            processed_ids.extend(g["ids"])

        await _mark_processed(session, processed_ids, now)
        await session.commit()

        if dropped:
            logger.info("Newly-added: emitted %d albums, %d deferred to next scan (per-scan cap)", len(to_emit), dropped)

    # Low-latency push (mirrors the reconciler + download emit).
    if deliveries:
        from app.services.notification_dispatch import dispatch_pending

        async with AsyncSessionLocal() as dispatch_session:
            await dispatch_pending(dispatch_session)

    return {"notified_albums": len(to_emit), "deferred_albums": dropped, "deliveries": deliveries}
