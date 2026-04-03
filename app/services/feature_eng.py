"""
GrooveIQ – Feature engineering for the ranking model (Phase 4, Step 6).

Builds a feature matrix (one row per candidate track) that combines:
  - User-side features (from taste_profile)
  - Track-side features (from track_features)
  - Interaction features (from track_interactions for this user×track)
  - Context features (time of day, day of week)

Missing values are filled with sensible defaults so the model always
receives a fixed-width feature vector.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures, TrackInteraction, User

# Feature columns in deterministic order (model input contract).
FEATURE_COLUMNS = [
    # Track-side
    "bpm", "energy", "danceability", "valence", "loudness",
    "instrumentalness", "duration",
    # Track popularity
    "track_popularity",
    # User-track interaction
    "satisfaction_score", "play_count", "skip_count",
    "recency_days", "avg_completion",
    "has_prior_interaction",
    # Preference deltas
    "delta_bpm", "delta_energy", "delta_danceability", "delta_valence",
    # Mood match
    "mood_match_score",
    # Time-of-day affinity
    "time_affinity",
    # Context (cyclic encoding)
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
]

NUM_FEATURES = len(FEATURE_COLUMNS)


async def build_features(
    user_id: str,
    candidate_track_ids: List[str],
    session: AsyncSession,
    hour_of_day: Optional[int] = None,
    day_of_week: Optional[int] = None,
) -> Dict[str, np.ndarray]:
    """
    Build feature vectors for a list of candidate tracks.

    Returns:
        {
            "track_ids": list of track_id strings (same order as rows),
            "features": np.ndarray of shape (n_candidates, NUM_FEATURES),
        }
    """
    if not candidate_track_ids:
        return {"track_ids": [], "features": np.empty((0, NUM_FEATURES), dtype=np.float32)}

    # --- Load user taste profile ---
    user_result = await session.execute(
        select(User.taste_profile).where(User.user_id == user_id)
    )
    taste_profile = user_result.scalar_one_or_none() or {}
    audio_prefs = taste_profile.get("audio_preferences", {})
    mood_prefs = taste_profile.get("mood_preferences", {})
    time_patterns = taste_profile.get("time_patterns", {})

    # --- Load track features in bulk ---
    feat_result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id.in_(candidate_track_ids))
    )
    feat_map: Dict[str, TrackFeatures] = {
        t.track_id: t for t in feat_result.scalars().all()
    }

    # --- Load interactions for this user × candidates ---
    inter_result = await session.execute(
        select(TrackInteraction).where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.track_id.in_(candidate_track_ids),
        )
    )
    inter_map: Dict[str, TrackInteraction] = {
        i.track_id: i for i in inter_result.scalars().all()
    }

    # --- Track popularity (total play_count across all users) ---
    from sqlalchemy import func
    pop_result = await session.execute(
        select(
            TrackInteraction.track_id,
            func.sum(TrackInteraction.play_count).label("total"),
        )
        .where(TrackInteraction.track_id.in_(candidate_track_ids))
        .group_by(TrackInteraction.track_id)
    )
    pop_map = {row.track_id: row.total or 0 for row in pop_result.all()}

    # --- Context features ---
    now = time.time()
    if hour_of_day is None:
        hour_of_day = time.localtime(int(now)).tm_hour
    if day_of_week is None:
        day_of_week = time.localtime(int(now)).tm_wday + 1  # 1=Mon

    hour_sin = math.sin(2 * math.pi * hour_of_day / 24)
    hour_cos = math.cos(2 * math.pi * hour_of_day / 24)
    dow_sin = math.sin(2 * math.pi * day_of_week / 7)
    dow_cos = math.cos(2 * math.pi * day_of_week / 7)

    time_affinity_val = float(time_patterns.get(str(hour_of_day), 0.0))

    # --- Build feature rows ---
    rows: List[np.ndarray] = []
    valid_ids: List[str] = []

    for tid in candidate_track_ids:
        tf = feat_map.get(tid)
        if tf is None:
            continue  # skip candidates without track features

        inter = inter_map.get(tid)
        has_inter = 1.0 if inter else 0.0

        # Track-side
        bpm = tf.bpm or 120.0
        energy = tf.energy or 0.5
        danceability = tf.danceability or 0.5
        valence = tf.valence or 0.5
        loudness = tf.loudness or -8.0
        instrumentalness = tf.instrumentalness or 0.0
        duration = tf.duration or 200.0

        # Popularity
        track_pop = float(pop_map.get(tid, 0))

        # Interaction
        satisfaction = float(inter.satisfaction_score or 0.5) if inter else 0.0
        play_count = float(inter.play_count) if inter else 0.0
        skip_count = float(inter.skip_count) if inter else 0.0
        avg_completion = float(inter.avg_completion or 0.0) if inter else 0.0

        recency = 0.0
        if inter and inter.last_played_at:
            recency = (now - inter.last_played_at) / 86_400  # days

        # Preference deltas
        delta_bpm = abs(bpm - audio_prefs.get("bpm", {}).get("mean", bpm))
        delta_energy = abs(energy - audio_prefs.get("energy", {}).get("mean", energy))
        delta_dance = abs(danceability - audio_prefs.get("danceability", {}).get("mean", danceability))
        delta_valence = abs(valence - audio_prefs.get("valence", {}).get("mean", valence))

        # Mood match: dot product of user mood prefs and track mood tags
        mood_score = 0.0
        if mood_prefs and tf.mood_tags:
            for tag in tf.mood_tags:
                if isinstance(tag, dict):
                    label = tag.get("label", "")
                    conf = tag.get("confidence", 0.0)
                    mood_score += mood_prefs.get(label, 0.0) * conf

        row = np.array([
            bpm, energy, danceability, valence, loudness,
            instrumentalness, duration,
            track_pop,
            satisfaction, play_count, skip_count,
            recency, avg_completion,
            has_inter,
            delta_bpm, delta_energy, delta_dance, delta_valence,
            mood_score,
            time_affinity_val,
            hour_sin, hour_cos, dow_sin, dow_cos,
        ], dtype=np.float32)

        rows.append(row)
        valid_ids.append(tid)

    if not rows:
        return {"track_ids": [], "features": np.empty((0, NUM_FEATURES), dtype=np.float32)}

    features = np.stack(rows)
    # Replace any NaN with 0.
    features = np.nan_to_num(features, nan=0.0)

    return {"track_ids": valid_ids, "features": features}


async def build_training_data(session: AsyncSession) -> Dict[str, Any]:
    """
    Build training data from all track_interactions.

    Returns:
        {
            "features": np.ndarray (n_samples, NUM_FEATURES),
            "labels": np.ndarray (n_samples,),
            "groups": np.ndarray — number of candidates per user (for LGBMRanker),
            "n_samples": int,
        }
    """
    # Load all interactions grouped by user.
    result = await session.execute(
        select(TrackInteraction)
        .order_by(TrackInteraction.user_id, TrackInteraction.satisfaction_score.desc())
    )
    interactions = result.scalars().all()

    if not interactions:
        return {"features": np.empty((0, NUM_FEATURES)), "labels": np.array([]), "groups": np.array([]), "n_samples": 0}

    # Group by user.
    from collections import defaultdict
    by_user: Dict[str, List] = defaultdict(list)
    for inter in interactions:
        by_user[inter.user_id].append(inter)

    all_features: List[np.ndarray] = []
    all_labels: List[float] = []
    groups: List[int] = []

    for user_id, user_inters in by_user.items():
        track_ids = [i.track_id for i in user_inters]
        result = await build_features(user_id, track_ids, session)

        if result["features"].shape[0] == 0:
            continue

        # Labels = satisfaction_score for each track in the same order.
        label_map = {i.track_id: float(i.satisfaction_score or 0.5) for i in user_inters}
        labels = np.array([label_map.get(tid, 0.5) for tid in result["track_ids"]], dtype=np.float32)

        all_features.append(result["features"])
        all_labels.append(labels)
        groups.append(len(result["track_ids"]))

    if not all_features:
        return {"features": np.empty((0, NUM_FEATURES)), "labels": np.array([]), "groups": np.array([]), "n_samples": 0}

    features = np.vstack(all_features)
    labels = np.concatenate(all_labels)

    return {
        "features": features,
        "labels": labels,
        "groups": np.array(groups, dtype=np.int32),
        "n_samples": features.shape[0],
    }
