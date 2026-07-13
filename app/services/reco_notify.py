"""GrooveIQ – daily mix recommendations for the in-app Activity feed.

Runs AFTER the nightly user-mixes rebuild (``app/services/user_mixes.py``) and is
the producer for the "playlist / mix recommendations" rows of the unified Activity
feed (and the "Recommendations" push category). For each opted-in user it emits a
few ``recommendation`` events — one per fresh session mix — into the generic
outbox. Each event carries the mix's identity + a cover in ``data`` so the feed
renders a rich, tappable playlist row that opens the mix; the push path forwards
only the routing whitelist, so a burst collapses (via digest + the daily budget)
into at most one sparse "new recommendations" push.

Idempotency is per (user, mix): the dedup key is ``reco:mix:{user}:{mix_id}``, so
a re-run — or an unchanged nightly rebuild — adds no new rows; only a genuinely
new mix cluster does (mix ids are stable across rebuilds — user_mixes re-matches
clusters by centroid). Cold-start users with no active session mix are skipped.
Reusing ``event_type="recommendation"`` inherits prefs gating
(``notif_recommendations``), anti-spam, and Apprise routing — no dispatch/relay
changes. Mirrors :mod:`app.services.album_reco_notify`: own session, idempotent,
low-latency inline dispatch at the end.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import Device

logger = logging.getLogger(__name__)

# How many top session mixes to surface per user per run — the feed-density knob,
# kept small so digest/budget collapse the burst into one sparse push.
_MAX_MIXES = 3


async def notify_daily_mixes(now: int | None = None) -> dict[str, Any]:
    """Emit up to ``_MAX_MIXES`` rich mix ``recommendation`` rows per opted-in
    user who has a fresh session mix, then dispatch inline. Opens its own session;
    idempotent per (user, mix) via the ``reco:mix:{user}:{mix_id}`` dedup key (a
    re-run, or an unchanged rebuild, adds no new rows)."""
    if not (settings.NOTIFY_RECOMMENDATIONS_ENABLED and settings.push_enabled):
        return {"skipped": "disabled"}

    from app.services.cover_art import resolve_cover_art
    from app.services.notification_dispatch import emit_recommendation
    from app.services.user_mixes import get_session_mixes

    now = now or int(time.time())

    async with AsyncSessionLocal() as session:
        # Opted-in, reachable users. NULL pref = opted-in (legacy row), matching the
        # dispatcher's isnot(False) channel filter and the shipped iOS toggles.
        opted_in = sorted(
            (
                await session.execute(
                    select(Device.user_id)
                    .where(Device.notif_recommendations.isnot(False), Device.disabled_at.is_(None))
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not opted_in:
            return {"users": 0, "notified": 0, "reason": "no_opted_in_devices"}

        deliveries = 0
        users_with_mixes = 0
        for uid in opted_in:
            had_mix = False
            for mix in (await get_session_mixes(session, uid))[:_MAX_MIXES]:
                mix_id = mix.get("mix_id")
                tracks = mix.get("tracks") or []
                if mix_id is None or not tracks:
                    continue  # cold-start / empty mix — nothing streamable to open
                had_mix = True
                rep = tracks[0]
                cover = await resolve_cover_art(session, rep.get("artist") or "", rep.get("album") or "")
                artists: list[str] = []
                for t in tracks:
                    a = t.get("artist")
                    if a and a not in artists:
                        artists.append(a)
                    if len(artists) >= 2:
                        break
                ordinal = mix.get("ordinal")
                deliveries += await emit_recommendation(
                    session,
                    uid,
                    title=f"Your Mix {ordinal}" if ordinal else "A fresh mix for you",
                    body=", ".join(artists) if artists else "Fresh picks from your recent listening.",
                    dedup_key=f"reco:mix:{uid}:{mix_id}",
                    data_extra={
                        "kind": "playlist",  # iOS renders a playlist row → opens the mix
                        "mix_id": mix_id,
                        "cover_url": cover,
                        "media_server_id": rep.get("media_server_id"),
                    },
                    now=now,
                )
            if had_mix:
                users_with_mixes += 1
        await session.commit()

    # Low-latency push (mirrors album_reco_notify + the download emit).
    if deliveries:
        from app.services.notification_dispatch import dispatch_pending

        async with AsyncSessionLocal() as dispatch_session:
            await dispatch_pending(dispatch_session)

    logger.info(
        "Daily mix reco notify: %d mix row(s) for %d/%d user(s)",
        deliveries,
        users_with_mixes,
        len(opted_in),
    )
    return {"users": len(opted_in), "with_mixes": users_with_mixes, "notified": deliveries}
