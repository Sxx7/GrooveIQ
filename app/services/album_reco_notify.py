"""GrooveIQ – album recommendation notifications for the in-app Activity feed.

The missing producer for the "album recommendations" rows of the unified Activity
feed. Runs daily and, for each opted-in user, emits a few ``recommendation``
events — one per top-ranked in-library album — into the generic outbox. Each event
carries the album's cover + identity in ``data`` so the feed renders a rich,
tappable row; the push path forwards only the routing whitelist, so a burst of
album events collapses (via digest + the daily budget) into at most one sparse
"new recommendations" push. Reusing ``event_type="recommendation"`` means prefs
gating (``notif_recommendations``), anti-spam, and Apprise routing are all
inherited — no dispatch/relay changes.

Feed density vs push sparseness: this producer deliberately emits several album
rows (``_MAX_ALBUMS``) so the FEED is useful. Keeping the PUSH sparse is the
dispatcher's job — with digest + daily-budget on (the shipped config) the burst is
one push. A per-(user, album) dedup key makes re-runs idempotent, so the feed
accretes only genuinely new album picks (a given album notifies a user once).

Cost note: ``recommend_albums`` does a per-user library roll-up + ranker pass, so
this is O(opted-in users) roll-ups — fine at the self-hosted ~5–10 users/instance
scale. Mirrors :mod:`app.services.reco_notify`: own session, idempotent, inline
dispatch at the end.
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

# How many top album rows to emit per user per run — the feed-density knob. Kept
# small so the daily digest/budget collapses the burst into one sparse push.
_MAX_ALBUMS = 3


def _album_dedup_key(user_id: str, artist: str | None, album: str) -> str:
    """Per-(user, album) key in the reco namespace (distinct from the media key,
    so an album reco never collides with a new-release / download of the same
    album). Makes re-runs idempotent — a given album notifies a user at most once."""
    a = (artist or "").strip().lower()
    b = album.strip().lower()
    return f"reco:album:{user_id}:{a}|{b}"


async def notify_album_recommendations(now: int | None = None) -> dict[str, Any]:
    """Emit up to ``_MAX_ALBUMS`` album ``recommendation`` events per opted-in
    user, then dispatch inline. Opens its own session; idempotent via the
    per-(user, album) dedup key (a re-run only adds genuinely new albums)."""
    if not (settings.NOTIFY_RECOMMENDATIONS_ENABLED and settings.push_enabled):
        return {"skipped": "disabled"}

    from app.services.album_reco import recommend_albums
    from app.services.cover_art import resolve_cover_art
    from app.services.notification_dispatch import emit_recommendation

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
        users_with_albums = 0
        for uid in opted_in:
            try:
                result = await recommend_albums(session, uid, mode="discover", limit=_MAX_ALBUMS)
            except Exception as exc:  # a bad user shouldn't sink the whole batch
                logger.warning("album reco failed for %s: %s", uid, exc)
                continue
            albums = result.get("albums") or []
            if not albums:
                continue
            users_with_albums += 1
            for alb in albums[:_MAX_ALBUMS]:
                album = alb.get("album") or ""
                if not album:
                    continue
                artist = alb.get("album_artist")
                cover = await resolve_cover_art(session, artist or "", album)
                rep = alb.get("representative_tracks") or []
                msid = next((t.get("media_server_id") for t in rep if t.get("media_server_id")), None)
                reasons = alb.get("reasons") or []
                deliveries += await emit_recommendation(
                    session,
                    uid,
                    title="An album you might like",
                    body=f"{album} — {artist}" if artist else album,
                    dedup_key=_album_dedup_key(uid, artist, album),
                    data_extra={
                        "kind": "album",  # iOS discriminates album vs playlist/mix rows on this
                        "cover_url": cover,
                        "album": album,
                        "album_artist": artist,
                        "media_server_id": msid,
                        "reason": reasons[0] if reasons else None,
                    },
                    now=now,
                )
        await session.commit()

    # Low-latency push (mirrors reco_notify + the download emit).
    if deliveries:
        from app.services.notification_dispatch import dispatch_pending

        async with AsyncSessionLocal() as dispatch_session:
            await dispatch_pending(dispatch_session)

    logger.info(
        "Album reco notify: %d new album row(s) for %d/%d user(s)",
        deliveries,
        users_with_albums,
        len(opted_in),
    )
    return {"users": len(opted_in), "with_albums": users_with_albums, "notified": deliveries}
