"""GrooveIQ – goal F: the daily "your mix is ready" recommendation notification.

Runs AFTER the nightly user-mixes rebuild (``app/services/user_mixes.py``) and is
the missing producer that makes the "Recommendations" category actually fire —
until now ``emit_recommendation`` had only a manual admin caller, so the iOS
toggle controlled a push that never happened.

Sparseness is the whole point, so every push is keyed on
``reco:daily:{user}:{YYYY-MM-DD}``. The outbox's ``UNIQUE(user_id, dedup_key)``
then collapses a re-run — or a user's six freshly-rebuilt mixes — into a single
delivery. (A naive daily cron keyed on a fresh ``playlist_id`` would push
unbounded; the date key is load-bearing.)

Audience = users who BOTH (a) have an active, recommendations-opted-in device (so
a push can land) AND (b) have at least one *active session* mix (so there is a
genuinely fresh mix to point at — cold-start users who only have generic genre
mixes are skipped, since telling them "your daily mix is ready" would be a lie).
Mirrors :mod:`app.services.new_media_notify`: opens its own session, idempotent,
low-latency inline dispatch at the end.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import Device, Mix

logger = logging.getLogger(__name__)


def _utc_day(now: int) -> str:
    """``YYYY-MM-DD`` (UTC) — the per-user daily dedup bucket. UTC matches the
    scheduler's UTC crons; per-user local-time bucketing (quiet hours) is a later
    phase and needs a device timezone that does not exist yet."""
    return time.strftime("%Y-%m-%d", time.gmtime(now))


async def notify_daily_mixes(now: int | None = None) -> dict[str, Any]:
    """Emit one ``recommendation`` push per opted-in user who has a fresh session
    mix, then dispatch inline. Opens its own session; idempotent within a UTC day
    via the date-scoped dedup key (a second run the same day is a no-op)."""
    if not (settings.NOTIFY_RECOMMENDATIONS_ENABLED and settings.push_enabled):
        return {"skipped": "disabled"}

    from app.services.notification_dispatch import emit_recommendation

    now = now or int(time.time())
    day = _utc_day(now)

    async with AsyncSessionLocal() as session:
        # (a) users reachable + opted-in. NULL pref = opted-in (legacy row), matching
        # the dispatcher's isnot(False) channel filter and the shipped iOS toggles.
        opted_in = set(
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

        # (b) users with a genuinely fresh mix to announce.
        with_mix = set(
            (
                await session.execute(
                    select(Mix.user_id).where(Mix.kind == "session", Mix.state == "active").distinct()
                )
            )
            .scalars()
            .all()
        )
        audience = sorted(opted_in & with_mix)  # sorted → deterministic emit order
        if not audience:
            return {"users": 0, "notified": 0, "reason": "no_fresh_mixes"}

        deliveries = 0
        for uid in audience:
            deliveries += await emit_recommendation(
                session,
                uid,
                title="Your Daily Mix is ready",
                body="Fresh picks based on what you've been playing.",
                dedup_key=f"reco:daily:{uid}:{day}",
                now=now,
            )
        await session.commit()

    # Low-latency push (mirrors new_media + the download emit).
    if deliveries:
        from app.services.notification_dispatch import dispatch_pending

        async with AsyncSessionLocal() as dispatch_session:
            await dispatch_pending(dispatch_session)

    logger.info("Daily reco notify: %d/%d user(s) notified for %s", deliveries, len(audience), day)
    return {"users": len(audience), "notified": deliveries, "day": day}
