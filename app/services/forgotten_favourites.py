"""
GrooveIQ — Forgotten-favourites recommendation service (track-level, library-only).

Surfaces individual *tracks* the user demonstrably loved at some point but hasn't
played in a long time — the "I forgot how much I liked this" surface. Like
``artist_reco`` / ``album_reco`` this is a read-only aggregation over the
existing ``track_interactions`` signals, **not** a parallel ML pipeline.

Each qualifying track is scored **multiplicatively**::

    score = affinity * dormancy

so a track only surfaces when it is *both* a proven favourite *and* dormant. A
weighted sum would leak in two wrong shapes — a beloved track played yesterday
(high affinity, low dormancy) and a mediocre track untouched for years (low
affinity, high dormancy) — neither of which is a "forgotten favourite".

  * ``affinity`` — how much the user loved it: a blend of the per-user
    normalised ``satisfaction_score``, a like/repeat boost, and a play-count
    saturation term. Weights come from the ``forgotten_favourites`` config group.
  * ``dormancy`` — how long since the last play, via the same exponential ramp
    the album "rediscover" boost uses: ``1 - exp(-ln2 * days_since / halflife)``.

Qualification gates (all from config) keep it favourites-only: the track must
have been played (``last_played_at`` set), have ``play_count >= min_play_count``,
``satisfaction_score >= min_satisfaction``, and have been dormant for at least
``min_dormancy_days``. Never-played tracks are intentionally excluded — those are
the *new-discovery* surface (reranker freshness boost), not forgotten favourites.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

_LN2 = math.log(2.0)


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


async def recommend_forgotten_favourites(
    session: AsyncSession,
    user_id: str,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """
    Rank dormant favourites for ``user_id``. Returns
    ``{"generated_at", "tracks": [...]}``; the route layer adds
    ``user_id``/``total``. Each track carries ``sources``/``reasons``/``signals``
    for "you loved this — last played N months ago"-style badges.
    """
    cfg = get_config().forgotten_favourites
    now = time.time()
    cutoff = now - cfg.min_dormancy_days * 86400

    # Push every qualification gate into SQL — the (user_id, satisfaction_score)
    # index covers the hot predicates and the dormant favourites are a small slice
    # of the interaction table.
    q = (
        select(
            TrackInteraction.track_id,
            TrackInteraction.satisfaction_score,
            TrackInteraction.last_played_at,
            TrackInteraction.play_count,
            TrackInteraction.like_count,
            TrackInteraction.repeat_count,
            TrackFeatures.title,
            TrackFeatures.artist,
            TrackFeatures.album,
            TrackFeatures.media_server_id,
            TrackFeatures.duration,
        )
        .join(TrackFeatures, TrackFeatures.track_id == TrackInteraction.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.last_played_at.isnot(None),
            TrackInteraction.last_played_at <= cutoff,
            TrackInteraction.play_count >= cfg.min_play_count,
            TrackInteraction.satisfaction_score >= cfg.min_satisfaction,
        )
    )
    rows = (await session.execute(q)).all()
    if not rows:
        return {"generated_at": int(now), "tracks": []}

    # affinity weights (normalised by their sum so the blend stays in [0, 1]).
    ws, wl, wp = cfg.w_satisfaction, cfg.w_likes, cfg.w_plays
    denom = (ws + wl + wp) or 1.0

    results: list[dict[str, Any]] = []
    for r in rows:
        last_played = r.last_played_at
        days_since = (now - last_played) / 86400

        sat_term = _clamp01(r.satisfaction_score or 0.0)
        like_term = _clamp01(((r.like_count or 0) + (r.repeat_count or 0)) / cfg.likes_saturation)
        plays_term = _clamp01((r.play_count or 0) / cfg.plays_saturation)
        affinity = (ws * sat_term + wl * like_term + wp * plays_term) / denom

        dormancy = _clamp01(1.0 - math.exp(-_LN2 * days_since / cfg.dormancy_halflife_days))
        score = affinity * dormancy

        sources: list[str] = ["satisfaction"]
        if (r.like_count or 0) + (r.repeat_count or 0) > 0:
            sources.append("likes")
        if (r.play_count or 0) > 0:
            sources.append("plays")

        reasons: list[str] = []
        if sat_term >= 0.7:
            reasons.append("you loved this")
        elif sat_term >= cfg.min_satisfaction:
            reasons.append("you used to play this a lot")
        if (r.like_count or 0) > 0:
            reasons.append("you liked it")
        elif (r.repeat_count or 0) > 0:
            reasons.append("you put it on repeat")
        months = int(days_since / 30)
        if months >= 1:
            reasons.append(f"not played in {months} month{'s' if months != 1 else ''}")
        else:
            reasons.append(f"not played in {int(days_since)} days")

        results.append(
            {
                "track_id": r.track_id,
                "title": r.title,
                "artist": r.artist,
                "album": r.album,
                "media_server_id": r.media_server_id,
                "duration": r.duration,
                "score": round(score, 4),
                "sources": sources,
                "reasons": reasons,
                "signals": {
                    "affinity": round(affinity, 4),
                    "dormancy": round(dormancy, 4),
                    "satisfaction": round(sat_term, 4),
                    "days_since_last_play": int(days_since),
                    "play_count": r.play_count or 0,
                    "like_count": r.like_count or 0,
                    "repeat_count": r.repeat_count or 0,
                },
                "last_played_at": last_played,
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"generated_at": int(now), "tracks": results[:limit]}
