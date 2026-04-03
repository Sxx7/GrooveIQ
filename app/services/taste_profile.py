"""
GrooveIQ – User taste profile builder.

Populates User.taste_profile (JSON) with aggregated preferences
derived from TrackInteraction + TrackFeatures.

The taste profile drives:
  - Feature deltas in the ranking model (user pref vs candidate track)
  - Cold-start recommendations (match new users to popular tracks in their range)
  - Playlist seeding (pick seeds that match the user's typical vibe)

Profile structure:
{
    "audio_preferences": {
        "bpm":          {"mean": 120.5, "std": 15.2},
        "energy":       {"mean": 0.72, "std": 0.11},
        "danceability": {"mean": 0.65, "std": 0.13},
        "valence":      {"mean": 0.58, "std": 0.14},
        "acousticness": {"mean": 0.30, "std": 0.20},
        "instrumentalness": {"mean": 0.15, "std": 0.18},
        "loudness":     {"mean": -8.5, "std": 3.2}
    },
    "mood_preferences": {
        "happy": 0.45,      # average confidence-weighted frequency
        "relaxed": 0.30,
        ...
    },
    "key_preferences": {
        "C major": 0.12,
        "G major": 0.10,
        ...
    },
    "top_tracks": [
        {"track_id": "abc", "score": 0.95},
        ...  # top 50 by satisfaction_score
    ],
    "time_patterns": {
        "0": 0.01, "1": 0.005, ..., "23": 0.03   # fraction of plays per hour
    },
    "device_patterns": {
        "mobile": 0.60, "desktop": 0.35, "speaker": 0.05
    },
    "behaviour": {
        "avg_session_tracks": 12.5,
        "avg_skip_rate": 0.18,
        "avg_completion": 0.72,
        "total_plays": 1234,
        "active_days": 45,
        "listening_since": 1700000000
    },
    "updated_at": 1712000000
}

Edge cases:
  - Users with no interactions → profile set to null, skipped
  - Users with interactions but no analysed tracks → audio_preferences omitted
  - Tracks missing specific features → excluded from that feature's stats
  - Exponential decay: recent plays weighted more than old ones
  - Division by zero guarded everywhere
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import (
    ListenSession,
    TrackFeatures,
    TrackInteraction,
    User,
)

logger = logging.getLogger(__name__)

# Audio feature columns to aggregate into preferences.
_AUDIO_FEATURES = [
    "bpm", "energy", "danceability", "valence",
    "acousticness", "instrumentalness", "loudness",
]

# Top-N tracks to include in profile.
_TOP_TRACKS_LIMIT = 50

# Process users in batches.
_USER_BATCH_SIZE = 100


async def run_taste_profile_builder() -> dict:
    """
    Main entry point.  Rebuilds taste profiles for all active users
    who have TrackInteraction data.

    Returns summary: {users_updated, users_skipped}.
    """
    async with AsyncSessionLocal() as session:
        # Fetch all active users who have at least one interaction.
        result = await session.execute(
            select(User.user_id)
            .where(User.is_active == True)  # noqa: E712
            .order_by(User.user_id)
        )
        all_user_ids = [row[0] for row in result.all()]

        updated = 0
        skipped = 0

        # Process in batches.
        for i in range(0, len(all_user_ids), _USER_BATCH_SIZE):
            batch = all_user_ids[i : i + _USER_BATCH_SIZE]
            for user_id in batch:
                profile = await _build_profile(session, user_id)
                if profile is None:
                    skipped += 1
                    continue

                now = int(time.time())
                await session.execute(
                    update(User)
                    .where(User.user_id == user_id)
                    .values(taste_profile=profile, profile_updated_at=now)
                )
                updated += 1

            await session.commit()

        if updated > 0 or skipped > 0:
            logger.info(
                "Taste profile builder complete",
                extra={"users_updated": updated, "users_skipped": skipped},
            )

    return {"users_updated": updated, "users_skipped": skipped}


async def _build_profile(
    session: AsyncSession, user_id: str
) -> Optional[Dict[str, Any]]:
    """
    Build the full taste profile for one user.

    Returns None if the user has no interactions.
    """
    # Fetch all interactions for this user, ordered by satisfaction_score desc.
    result = await session.execute(
        select(TrackInteraction)
        .where(TrackInteraction.user_id == user_id)
        .order_by(TrackInteraction.satisfaction_score.desc())
    )
    interactions = result.scalars().all()

    if not interactions:
        return None

    now = int(time.time())
    decay_seconds = settings.TASTE_PROFILE_DECAY_DAYS * 86_400

    # Compute recency weights: exp(-(now - last_played) / decay).
    weighted_interactions = []
    for inter in interactions:
        last_played = inter.last_played_at or inter.updated_at
        age = max(now - last_played, 0)
        weight = math.exp(-age / decay_seconds) if decay_seconds > 0 else 1.0
        weighted_interactions.append((inter, weight))

    # --- Top tracks ---
    top_tracks = [
        {"track_id": inter.track_id, "score": round(inter.satisfaction_score or 0, 4)}
        for inter, _ in weighted_interactions[:_TOP_TRACKS_LIMIT]
    ]

    # --- Audio preferences (weighted by satisfaction * recency) ---
    track_ids = [inter.track_id for inter in interactions]
    features_map = await _fetch_track_features(session, track_ids)
    audio_prefs = _compute_audio_preferences(weighted_interactions, features_map)

    # --- Mood preferences ---
    mood_prefs = _compute_mood_preferences(weighted_interactions, features_map)

    # --- Key preferences ---
    key_prefs = _compute_key_preferences(weighted_interactions, features_map)

    # --- Session-based patterns ---
    session_stats = await _compute_session_stats(session, user_id)

    # --- Time and device patterns (from sessions) ---
    time_patterns = await _compute_time_patterns(session, user_id)
    device_patterns = await _compute_device_patterns(session, user_id)

    # --- Behaviour summary ---
    total_plays = sum(i.play_count for i in interactions)
    total_skips = sum(i.skip_count for i in interactions)
    completions = [i.avg_completion for i in interactions if i.avg_completion is not None]
    first_played = min(
        (i.first_played_at for i in interactions if i.first_played_at is not None),
        default=None,
    )

    # Count distinct days with activity.
    active_days_set = set()
    for inter in interactions:
        if inter.last_played_at:
            # Day granularity (UTC).
            active_days_set.add(inter.last_played_at // 86_400)
        if inter.first_played_at:
            active_days_set.add(inter.first_played_at // 86_400)

    behaviour = {
        "avg_session_tracks": session_stats.get("avg_track_count"),
        "avg_skip_rate": round(total_skips / max(total_plays, 1), 4),
        "avg_completion": (
            round(sum(completions) / len(completions), 4) if completions else None
        ),
        "total_plays": total_plays,
        "active_days": len(active_days_set),
        "listening_since": first_played,
    }

    profile = {
        "top_tracks": top_tracks,
        "behaviour": behaviour,
        "updated_at": now,
    }

    if audio_prefs:
        profile["audio_preferences"] = audio_prefs
    if mood_prefs:
        profile["mood_preferences"] = mood_prefs
    if key_prefs:
        profile["key_preferences"] = key_prefs
    if time_patterns:
        profile["time_patterns"] = time_patterns
    if device_patterns:
        profile["device_patterns"] = device_patterns

    return profile


async def _fetch_track_features(
    session: AsyncSession, track_ids: List[str]
) -> Dict[str, TrackFeatures]:
    """Fetch TrackFeatures for a list of track_ids."""
    if not track_ids:
        return {}

    # Batch in groups of 500 to avoid huge IN clauses.
    features_map: Dict[str, TrackFeatures] = {}
    for i in range(0, len(track_ids), 500):
        batch = track_ids[i : i + 500]
        result = await session.execute(
            select(TrackFeatures).where(TrackFeatures.track_id.in_(batch))
        )
        for tf in result.scalars().all():
            features_map[tf.track_id] = tf

    return features_map


def _compute_audio_preferences(
    weighted_interactions: list,
    features_map: Dict[str, TrackFeatures],
) -> Optional[Dict[str, Any]]:
    """
    Compute weighted mean and std of audio features.

    Weight = satisfaction_score * recency_weight.  Tracks without features
    or with null values for a feature are excluded from that feature's stats.
    """
    # Accumulate weighted values per feature.
    feature_values: Dict[str, List[tuple]] = {f: [] for f in _AUDIO_FEATURES}

    for inter, recency_weight in weighted_interactions:
        tf = features_map.get(inter.track_id)
        if tf is None:
            continue

        # Combined weight: recency * satisfaction (clamp satisfaction to [0, 1]).
        satisfaction = max(min(inter.satisfaction_score or 0.5, 1.0), 0.0)
        w = recency_weight * (0.1 + satisfaction)  # floor of 0.1 so disliked tracks still contribute

        for feat in _AUDIO_FEATURES:
            val = getattr(tf, feat, None)
            if val is not None:
                feature_values[feat].append((val, w))

    if not any(feature_values.values()):
        return None

    prefs = {}
    for feat, values in feature_values.items():
        if not values:
            continue

        total_w = sum(w for _, w in values)
        if total_w < 1e-9:
            continue

        wmean = sum(v * w for v, w in values) / total_w
        wvar = sum(w * (v - wmean) ** 2 for v, w in values) / total_w
        wstd = math.sqrt(max(wvar, 0))

        prefs[feat] = {
            "mean": round(wmean, 4),
            "std": round(wstd, 4),
        }

    return prefs if prefs else None


def _compute_mood_preferences(
    weighted_interactions: list,
    features_map: Dict[str, TrackFeatures],
) -> Optional[Dict[str, float]]:
    """
    Aggregate mood tag preferences across tracks, weighted by satisfaction * recency.
    """
    mood_scores: Dict[str, float] = defaultdict(float)
    total_weight = 0.0

    for inter, recency_weight in weighted_interactions:
        tf = features_map.get(inter.track_id)
        if tf is None or not tf.mood_tags:
            continue

        satisfaction = max(min(inter.satisfaction_score or 0.5, 1.0), 0.0)
        w = recency_weight * (0.1 + satisfaction)
        total_weight += w

        for tag in tf.mood_tags:
            if isinstance(tag, dict):
                label = tag.get("label", "")
                conf = tag.get("confidence", 0.0)
                mood_scores[label] += w * conf

    if total_weight < 1e-9 or not mood_scores:
        return None

    # Normalise to fractions that sum to ~1.
    return {
        label: round(score / total_weight, 4)
        for label, score in sorted(mood_scores.items(), key=lambda x: -x[1])
    }


def _compute_key_preferences(
    weighted_interactions: list,
    features_map: Dict[str, TrackFeatures],
) -> Optional[Dict[str, float]]:
    """Aggregate key/mode preferences."""
    key_scores: Dict[str, float] = defaultdict(float)
    total_weight = 0.0

    for inter, recency_weight in weighted_interactions:
        tf = features_map.get(inter.track_id)
        if tf is None or tf.key is None or tf.mode is None:
            continue

        satisfaction = max(min(inter.satisfaction_score or 0.5, 1.0), 0.0)
        w = recency_weight * (0.1 + satisfaction)
        total_weight += w

        key_label = f"{tf.key} {tf.mode}"
        key_scores[key_label] += w

    if total_weight < 1e-9 or not key_scores:
        return None

    return {
        label: round(score / total_weight, 4)
        for label, score in sorted(key_scores.items(), key=lambda x: -x[1])
    }


async def _compute_session_stats(
    session: AsyncSession, user_id: str
) -> Dict[str, Any]:
    """Compute aggregate session statistics for a user."""
    result = await session.execute(
        select(
            func.avg(ListenSession.track_count).label("avg_tc"),
            func.avg(ListenSession.skip_rate).label("avg_sr"),
            func.avg(ListenSession.duration_s).label("avg_dur"),
            func.count(ListenSession.id).label("cnt"),
        )
        .where(ListenSession.user_id == user_id)
    )
    row = result.first()
    if row is None or row.cnt == 0:
        return {}

    return {
        "avg_track_count": round(row.avg_tc, 2) if row.avg_tc else None,
        "avg_skip_rate": round(row.avg_sr, 4) if row.avg_sr else None,
        "avg_duration_s": round(row.avg_dur, 1) if row.avg_dur else None,
        "session_count": row.cnt,
    }


async def _compute_time_patterns(
    session: AsyncSession, user_id: str
) -> Optional[Dict[str, float]]:
    """Fraction of sessions per hour-of-day."""
    result = await session.execute(
        select(
            ListenSession.hour_of_day,
            func.count(ListenSession.id).label("cnt"),
        )
        .where(
            ListenSession.user_id == user_id,
            ListenSession.hour_of_day.isnot(None),
        )
        .group_by(ListenSession.hour_of_day)
    )
    rows = result.all()
    if not rows:
        return None

    total = sum(r.cnt for r in rows)
    if total == 0:
        return None

    return {
        str(r.hour_of_day): round(r.cnt / total, 4)
        for r in rows
    }


async def _compute_device_patterns(
    session: AsyncSession, user_id: str
) -> Optional[Dict[str, float]]:
    """Fraction of sessions per device type."""
    result = await session.execute(
        select(
            ListenSession.dominant_device_type,
            func.count(ListenSession.id).label("cnt"),
        )
        .where(
            ListenSession.user_id == user_id,
            ListenSession.dominant_device_type.isnot(None),
        )
        .group_by(ListenSession.dominant_device_type)
    )
    rows = result.all()
    if not rows:
        return None

    total = sum(r.cnt for r in rows)
    if total == 0:
        return None

    return {
        r.dominant_device_type: round(r.cnt / total, 4)
        for r in rows
    }
