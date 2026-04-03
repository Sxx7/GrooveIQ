"""
GrooveIQ – Post-ranking reranker (Phase 4, Step 8).

Applied after the LightGBM ranker, before returning results.
Enforces diversity constraints and applies business rules:

  1. Artist diversity — max 2 tracks from the same "artist" in top 10
  2. Anti-repetition — suppress tracks played in the last 2 hours
  3. Skip suppression — demote tracks early-skipped >2 times in last 24h
  4. Freshness boost — +10% score uplift for tracks the user has never played
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures, TrackInteraction

logger = logging.getLogger(__name__)

# Max tracks from the same artist in the top N positions.
_ARTIST_DIVERSITY_TOP_N = 10
_ARTIST_MAX_PER_TOP = 2

# Anti-repetition window.
_REPEAT_WINDOW_S = 2 * 3600  # 2 hours

# Freshness boost multiplier for never-played tracks.
_FRESHNESS_BOOST = 0.10

# Skip suppression: demote tracks early-skipped >N times in 24h.
_SKIP_THRESHOLD = 2
_SKIP_DEMOTE_FACTOR = 0.5


def _extract_artist(file_path: Optional[str]) -> str:
    """
    Heuristic: extract artist from file path.
    Assumes /library/Artist/Album/Track.mp3 layout.
    Falls back to parent directory name.
    """
    if not file_path:
        return "__unknown__"
    parts = file_path.replace("\\", "/").split("/")
    # Find the artist directory: typically 2 levels above the file.
    if len(parts) >= 3:
        return parts[-3]  # artist
    if len(parts) >= 2:
        return parts[-2]
    return "__unknown__"


async def rerank(
    ranked: List[Tuple[str, float]],
    user_id: str,
    session: AsyncSession,
) -> List[Tuple[str, float]]:
    """
    Apply diversity and business-rule filters to ranked candidates.

    Args:
        ranked: list of (track_id, score) sorted descending.
        user_id: user for interaction lookups.
        session: DB session.

    Returns:
        Reranked list of (track_id, score).
    """
    if not ranked:
        return ranked

    track_ids = [tid for tid, _ in ranked]
    score_map = {tid: score for tid, score in ranked}

    # --- Load data needed for all rules ---
    now = int(time.time())

    # Track features (for artist extraction).
    feat_result = await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.file_path)
        .where(TrackFeatures.track_id.in_(track_ids))
    )
    path_map = {row.track_id: row.file_path for row in feat_result.all()}

    # Interactions for this user.
    inter_result = await session.execute(
        select(TrackInteraction)
        .where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.track_id.in_(track_ids),
        )
    )
    inter_map: Dict[str, TrackInteraction] = {
        i.track_id: i for i in inter_result.scalars().all()
    }

    # --- Rule 1: Freshness boost (never-played tracks get score uplift) ---
    for tid in track_ids:
        if tid not in inter_map:
            score_map[tid] = score_map[tid] * (1.0 + _FRESHNESS_BOOST)

    # --- Rule 2: Skip suppression (early-skipped >2 times recently → demote) ---
    cutoff_24h = now - 86_400
    for tid in track_ids:
        inter = inter_map.get(tid)
        if inter and inter.early_skip_count > _SKIP_THRESHOLD:
            if inter.last_played_at and inter.last_played_at >= cutoff_24h:
                score_map[tid] = score_map[tid] * _SKIP_DEMOTE_FACTOR

    # --- Rule 3: Anti-repetition (suppress tracks played in last 2h) ---
    recently_played: Set[str] = set()
    cutoff_repeat = now - _REPEAT_WINDOW_S
    for tid in track_ids:
        inter = inter_map.get(tid)
        if inter and inter.last_played_at and inter.last_played_at >= cutoff_repeat:
            recently_played.add(tid)

    # --- Rebuild sorted list with updated scores, excluding recently played ---
    adjusted = [
        (tid, score_map[tid])
        for tid in track_ids
        if tid not in recently_played
    ]
    adjusted.sort(key=lambda x: x[1], reverse=True)

    # --- Rule 4: Artist diversity in top N ---
    artist_map = {tid: _extract_artist(path_map.get(tid)) for tid in track_ids}
    result = _enforce_artist_diversity(adjusted, artist_map)

    return result


def _enforce_artist_diversity(
    ranked: List[Tuple[str, float]],
    artist_map: Dict[str, str],
) -> List[Tuple[str, float]]:
    """
    Ensure no more than _ARTIST_MAX_PER_TOP tracks from the same artist
    appear in the first _ARTIST_DIVERSITY_TOP_N positions of the output.

    Excess tracks are pushed past position N, preserving relative order.
    """
    # Two-pass: first fill the top N slots respecting diversity, then append the rest.
    accepted: List[Tuple[str, float]] = []
    deferred: List[Tuple[str, float]] = []
    artist_count: Counter = Counter()

    for tid, score in ranked:
        artist = artist_map.get(tid, "__unknown__")
        if len(accepted) < _ARTIST_DIVERSITY_TOP_N:
            if artist_count[artist] < _ARTIST_MAX_PER_TOP:
                accepted.append((tid, score))
                artist_count[artist] += 1
            else:
                deferred.append((tid, score))
        else:
            deferred.append((tid, score))

    return accepted + deferred
