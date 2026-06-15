"""GrooveIQ – Recently-engaged "resurfacing" heat (the cross-surface special-track loop).

A track the user has *just engaged with* — replayed, seeked back into, finished, or liked —
earns a short-lived, time-decayed "heat" so it keeps resurfacing across radio, Discover and
Library for a while (the "special track that keeps coming back"). Heat fades if the user stops
engaging, and a track that keeps earning it eventually crosses into the proven set (graduation)
— so the loop is self-limiting. Crowd-free: purely this user's own recent behaviour. Any client
sees the same set, because the heat is computed server-side from the shared interaction store.

The reranker reads :func:`engagement_heat` to boost the hottest candidate cross-surface; the
``GET /v1/users/{uid}/resurfacing`` endpoint reads :func:`get_resurfacing_tracks` to let a client
render a "Keep listening" card; ``POST .../suppress`` writes a ``suppress`` event that this module
honours (until the user engages again).
"""

from __future__ import annotations

import time

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ListenEvent, TrackInteraction

# Heat halves every ~5 days; engagement older than the window earns nothing.
_HALF_LIFE_S = 5 * 86_400.0
_RECENCY_WINDOW_S = 21 * 86_400

# Engagement-intensity weights (per signal). Seek-back is deliberately modest — it is our own
# signal, not a documented major-platform one (decision D2). Each count is capped so a single
# obsessive day can't peg a track at max heat on one signal alone.
_W_LIKE = 1.0
_W_REPEAT = 0.8
_W_FULL_LISTEN = 0.5
_W_SEEKBK = 0.25
_W_SKIP_PENALTY = 0.6
_INTENSITY_SCALE = 2.0  # divides the raw weighted sum toward ~[0, 1] before recency decay


def engagement_intensity(inter: TrackInteraction) -> float:
    """The un-decayed strength of the user's recent engagement with one track, ~[0, 1+].

    A weighted, per-signal-capped sum of the positive signals (like / replay / full-listen /
    seek-back) minus a skip penalty. 0 when net-negative (the user sampled and rejected it).
    """
    raw = (
        _W_LIKE * min(inter.like_count, 2)
        + _W_REPEAT * min(inter.repeat_count, 3)
        + _W_FULL_LISTEN * min(inter.full_listen_count, 3)
        + _W_SEEKBK * min(inter.total_seekbk, 4)
        - _W_SKIP_PENALTY * (inter.skip_count + inter.early_skip_count)
    )
    return max(0.0, raw / _INTENSITY_SCALE)


def engagement_heat(inter: TrackInteraction, now: int) -> float:
    """Time-decayed engagement heat in [0, 1]. 0 when never recently engaged or net-negative."""
    if not inter.last_played_at:
        return 0.0
    age = now - inter.last_played_at
    if age < 0 or age > _RECENCY_WINDOW_S:
        return 0.0
    intensity = engagement_intensity(inter)
    if intensity <= 0.0:
        return 0.0
    recency = 0.5 ** (age / _HALF_LIFE_S)
    return min(1.0, intensity * recency)


async def _suppressed_until(user_id: str, db: AsyncSession) -> dict[str, int]:
    """Latest ``suppress`` event timestamp per track for this user (the dismiss signal)."""
    result = await db.execute(
        select(ListenEvent.track_id, func.max(ListenEvent.timestamp))
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "suppress")
        .group_by(ListenEvent.track_id)
    )
    return {row[0]: int(row[1]) for row in result.all() if row[1] is not None}


async def get_resurfacing_tracks(
    user_id: str,
    db: AsyncSession,
    *,
    limit: int = 20,
    min_heat: float = 0.05,
) -> list[tuple[str, float]]:
    """The user's currently-"hot" tracks as ``(track_id, heat)``, highest first.

    Excludes tracks the user suppressed since their last engagement (re-engaging after a
    suppress brings a track back — the suppress only applies to engagement up to that point).
    """
    now = int(time.time())
    cutoff = now - _RECENCY_WINDOW_S
    result = await db.execute(
        select(TrackInteraction).where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.last_played_at.isnot(None),
            TrackInteraction.last_played_at >= cutoff,
            or_(
                TrackInteraction.repeat_count > 0,
                TrackInteraction.total_seekbk > 0,
                TrackInteraction.like_count > 0,
                TrackInteraction.full_listen_count > 0,
            ),
        )
    )
    interactions = result.scalars().all()
    suppressed = await _suppressed_until(user_id, db)

    scored: list[tuple[str, float]] = []
    for inter in interactions:
        sup_ts = suppressed.get(inter.track_id)
        if sup_ts is not None and (inter.last_played_at or 0) <= sup_ts:
            continue  # suppressed and not re-engaged since
        heat = engagement_heat(inter, now)
        if heat >= min_heat:
            scored.append((inter.track_id, heat))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
