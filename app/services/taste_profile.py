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
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import (
    ListenEvent,
    ListenSession,
    TrackFeatures,
    TrackInteraction,
    User,
)
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# Audio feature columns to aggregate into preferences.
_AUDIO_FEATURES = [
    "bpm",
    "energy",
    "danceability",
    "valence",
    "acousticness",
    "instrumentalness",
    "loudness",
]

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
                    update(User).where(User.user_id == user_id).values(taste_profile=profile, profile_updated_at=now)
                )
                updated += 1

            await session.commit()

        if updated > 0 or skipped > 0:
            logger.info(
                "Taste profile builder complete",
                extra={"users_updated": updated, "users_skipped": skipped},
            )

    return {"users_updated": updated, "users_skipped": skipped}


async def _build_profile(session: AsyncSession, user_id: str) -> dict[str, Any] | None:
    """
    Build the full taste profile for one user.

    Returns None if the user has no interactions AND no external data
    (Last.fm / onboarding) to seed from.
    """
    # Fetch all interactions for this user, ordered by satisfaction_score desc.
    result = await session.execute(
        select(TrackInteraction)
        .where(TrackInteraction.user_id == user_id)
        .order_by(TrackInteraction.satisfaction_score.desc())
    )
    interactions = result.scalars().all()

    if not interactions:
        # No local interactions — try to build a seed profile from
        # Last.fm cache and/or onboarding preferences.
        user_result = await session.execute(select(User).where(User.user_id == user_id))
        user = user_result.scalar_one_or_none()
        if user:
            seed = await build_seed_profile(session, user)
            if seed:
                return seed
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
    tp_cfg = get_config().taste_profile
    top_tracks = [
        {"track_id": inter.track_id, "score": round(inter.satisfaction_score or 0, 4)}
        for inter, _ in weighted_interactions[: tp_cfg.top_tracks_limit]
    ]

    # --- Audio preferences (weighted by satisfaction * recency) ---
    track_ids = [inter.track_id for inter in interactions]
    features_map = await _fetch_track_features(session, track_ids)
    audio_prefs = _compute_audio_preferences(weighted_interactions, features_map)

    # --- Multi-timescale audio preferences (short=7d, medium=30d, long=all-time) ---
    timescale_profiles = _compute_timescale_audio_preferences(interactions, features_map, now)

    # --- Mood preferences ---
    mood_prefs = _compute_mood_preferences(weighted_interactions, features_map)

    # --- Key preferences ---
    key_prefs = _compute_key_preferences(weighted_interactions, features_map)

    # --- Session-based patterns ---
    session_stats = await _compute_session_stats(session, user_id)

    # --- Time and device patterns (from sessions) ---
    time_patterns = await _compute_time_patterns(session, user_id)
    device_patterns = await _compute_device_patterns(session, user_id)

    # --- Output, context-type, and location patterns (from events) ---
    output_patterns = await _compute_output_patterns(session, user_id)
    context_type_patterns = await _compute_context_type_patterns(session, user_id)
    location_patterns = await _compute_location_patterns(session, user_id)

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
        "avg_completion": (round(sum(completions) / len(completions), 4) if completions else None),
        "total_plays": total_plays,
        "active_days": len(active_days_set),
        "listening_since": first_played,
    }

    # --- Popularity preference (niche vs mainstream tendency) ---
    # Measures whether user tends toward popular or niche tracks.
    # 0.0 = strongly niche, 0.5 = neutral, 1.0 = strongly mainstream.
    popularity_pref = await _compute_popularity_preference(session, user_id, interactions)

    profile = {
        "top_tracks": top_tracks,
        "behaviour": behaviour,
        "popularity_preference": popularity_pref,
        "updated_at": now,
    }

    if audio_prefs:
        profile["audio_preferences"] = audio_prefs
    if timescale_profiles:
        profile["timescale_audio"] = timescale_profiles
    if mood_prefs:
        profile["mood_preferences"] = mood_prefs
    if key_prefs:
        profile["key_preferences"] = key_prefs
    if time_patterns:
        profile["time_patterns"] = time_patterns
    if device_patterns:
        profile["device_patterns"] = device_patterns
    if output_patterns:
        profile["output_patterns"] = output_patterns
    if context_type_patterns:
        profile["context_type_patterns"] = context_type_patterns
    if location_patterns:
        profile["location_patterns"] = location_patterns

    # --- Enrich with Last.fm data and onboarding preferences ---
    user_result = await session.execute(select(User).where(User.user_id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        _enrich_with_lastfm(profile, user.lastfm_cache, len(interactions))
        _enrich_with_onboarding(profile, user.onboarding_preferences, len(interactions))

    return profile


async def _fetch_track_features(session: AsyncSession, track_ids: list[str]) -> dict[str, TrackFeatures]:
    """Fetch TrackFeatures for a list of track_ids."""
    if not track_ids:
        return {}

    # Post-#37, every TrackInteraction.track_id / ListenEvent.track_id is the
    # internal GrooveIQ id (a stable file-path hash). A plain track_id JOIN
    # is sufficient — the legacy OR-on-external_track_id is no longer needed.
    features_map: dict[str, TrackFeatures] = {}
    for i in range(0, len(track_ids), 500):
        batch = track_ids[i : i + 500]
        result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(batch)))
        for tf in result.scalars().all():
            features_map[tf.track_id] = tf

    return features_map


def _compute_audio_preferences(
    weighted_interactions: list,
    features_map: dict[str, TrackFeatures],
) -> dict[str, Any] | None:
    """
    Compute weighted mean and std of audio features.

    Weight = satisfaction_score * recency_weight.  Tracks without features
    or with null values for a feature are excluded from that feature's stats.
    """
    # Accumulate weighted values per feature.
    feature_values: dict[str, list[tuple]] = {f: [] for f in _AUDIO_FEATURES}

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


def _compute_timescale_audio_preferences(
    interactions: list,
    features_map: dict[str, TrackFeatures],
    now: int,
) -> dict[str, Any] | None:
    """
    Compute audio preference means at three timescales:
      - short (7 days)  — current mood/phase
      - medium (30 days) — medium-term preferences
      - long (365 days)  — core musical identity

    Returns {short: {bpm: mean, energy: mean, ...}, medium: {...}, long: {...}}
    Only includes features with enough weighted data.
    """
    tp_cfg = get_config().taste_profile
    timescales = {
        "short": tp_cfg.timescale_short_days * 86_400,
        "medium": settings.TASTE_PROFILE_DECAY_DAYS * 86_400,
        "long": tp_cfg.timescale_long_days * 86_400,
    }
    result: dict[str, dict[str, float]] = {}

    for scale_name, decay_seconds in timescales.items():
        weighted = []
        for inter in interactions:
            last_played = inter.last_played_at or inter.updated_at
            age = max(now - last_played, 0)
            weight = math.exp(-age / decay_seconds) if decay_seconds > 0 else 1.0
            weighted.append((inter, weight))

        # Compute weighted means for key audio features.
        scale_prefs: dict[str, float] = {}
        for feat in _AUDIO_FEATURES:
            total_w = 0.0
            total_v = 0.0
            for inter, recency_weight in weighted:
                tf = features_map.get(inter.track_id)
                if tf is None:
                    continue
                val = getattr(tf, feat, None)
                if val is None:
                    continue
                satisfaction = max(min(inter.satisfaction_score or 0.5, 1.0), 0.0)
                w = recency_weight * (0.1 + satisfaction)
                total_w += w
                total_v += val * w
            if total_w > 1e-9:
                scale_prefs[feat] = round(total_v / total_w, 4)

        if scale_prefs:
            result[scale_name] = scale_prefs

    return result if result else None


def _compute_mood_preferences(
    weighted_interactions: list,
    features_map: dict[str, TrackFeatures],
) -> dict[str, float] | None:
    """
    Aggregate mood tag preferences across tracks, weighted by satisfaction * recency.
    """
    mood_scores: dict[str, float] = defaultdict(float)
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
    return {label: round(score / total_weight, 4) for label, score in sorted(mood_scores.items(), key=lambda x: -x[1])}


def _compute_key_preferences(
    weighted_interactions: list,
    features_map: dict[str, TrackFeatures],
) -> dict[str, float] | None:
    """Aggregate key/mode preferences."""
    key_scores: dict[str, float] = defaultdict(float)
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

    return {label: round(score / total_weight, 4) for label, score in sorted(key_scores.items(), key=lambda x: -x[1])}


async def _compute_popularity_preference(session: AsyncSession, user_id: str, interactions: list) -> float:
    """
    Compute user's preference for popular vs niche tracks.

    Compares the user's listened tracks' global popularity against the
    library median. Returns 0.0 (strongly niche) to 1.0 (strongly mainstream).
    """
    if not interactions:
        return 0.5

    # Get global play counts for user's tracks.
    user_track_ids = [i.track_id for i in interactions]
    result = await session.execute(
        select(
            TrackInteraction.track_id,
            func.sum(TrackInteraction.play_count).label("total"),
        )
        .where(TrackInteraction.track_id.in_(user_track_ids))
        .group_by(TrackInteraction.track_id)
    )
    user_track_pops = {row.track_id: row.total or 0 for row in result.all()}

    # Get library-wide median popularity.
    all_pop_result = await session.execute(
        select(func.sum(TrackInteraction.play_count).label("total")).group_by(TrackInteraction.track_id)
    )
    all_pops = sorted([row.total or 0 for row in all_pop_result.all()])

    if not all_pops:
        return 0.5

    median_pop = all_pops[len(all_pops) // 2] if all_pops else 1

    # Weighted average popularity of user's tracks (weighted by satisfaction).
    total_w = 0.0
    total_pop = 0.0
    for inter in interactions:
        w = max(inter.satisfaction_score or 0.5, 0.1)
        pop = user_track_pops.get(inter.track_id, 0)
        total_w += w
        total_pop += w * pop

    if total_w < 1e-9:
        return 0.5

    user_avg_pop = total_pop / total_w

    # Normalize: ratio of user's avg popularity to library median.
    # Sigmoid-like mapping to [0, 1].
    if median_pop < 1:
        return 0.5
    ratio = user_avg_pop / median_pop
    # ratio > 1 = mainstream, < 1 = niche. Map via tanh to [0, 1].
    import math

    return round(0.5 + 0.5 * math.tanh(ratio - 1.0), 4)


async def _compute_session_stats(session: AsyncSession, user_id: str) -> dict[str, Any]:
    """Compute aggregate session statistics for a user."""
    result = await session.execute(
        select(
            func.avg(ListenSession.track_count).label("avg_tc"),
            func.avg(ListenSession.skip_rate).label("avg_sr"),
            func.avg(ListenSession.duration_s).label("avg_dur"),
            func.count(ListenSession.id).label("cnt"),
        ).where(ListenSession.user_id == user_id)
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


async def _compute_time_patterns(session: AsyncSession, user_id: str) -> dict[str, float] | None:
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

    return {str(r.hour_of_day): round(r.cnt / total, 4) for r in rows}


async def _compute_device_patterns(session: AsyncSession, user_id: str) -> dict[str, float] | None:
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

    return {r.dominant_device_type: round(r.cnt / total, 4) for r in rows}


async def _compute_output_patterns(session: AsyncSession, user_id: str) -> dict[str, float] | None:
    """Fraction of events per audio output type (headphones, speaker, etc.)."""
    result = await session.execute(
        select(
            ListenEvent.output_type,
            func.count(ListenEvent.id).label("cnt"),
        )
        .where(
            ListenEvent.user_id == user_id,
            ListenEvent.output_type.isnot(None),
        )
        .group_by(ListenEvent.output_type)
    )
    rows = result.all()
    if not rows:
        return None

    total = sum(r.cnt for r in rows)
    if total == 0:
        return None

    return {r.output_type: round(r.cnt / total, 4) for r in rows}


async def _compute_context_type_patterns(session: AsyncSession, user_id: str) -> dict[str, float] | None:
    """Fraction of sessions per listening context type (playlist, album, radio, etc.)."""
    result = await session.execute(
        select(
            ListenSession.dominant_context_type,
            func.count(ListenSession.id).label("cnt"),
        )
        .where(
            ListenSession.user_id == user_id,
            ListenSession.dominant_context_type.isnot(None),
        )
        .group_by(ListenSession.dominant_context_type)
    )
    rows = result.all()
    if not rows:
        return None

    total = sum(r.cnt for r in rows)
    if total == 0:
        return None

    return {r.dominant_context_type: round(r.cnt / total, 4) for r in rows}


async def _compute_location_patterns(session: AsyncSession, user_id: str) -> dict[str, float] | None:
    """Fraction of events per location label (home, work, gym, commute, etc.)."""
    result = await session.execute(
        select(
            ListenEvent.location_label,
            func.count(ListenEvent.id).label("cnt"),
        )
        .where(
            ListenEvent.user_id == user_id,
            ListenEvent.location_label.isnot(None),
        )
        .group_by(ListenEvent.location_label)
    )
    rows = result.all()
    if not rows:
        return None

    total = sum(r.cnt for r in rows)
    if total == 0:
        return None

    return {r.location_label: round(r.cnt / total, 4) for r in rows}


# ---------------------------------------------------------------------------
# Last.fm enrichment
# ---------------------------------------------------------------------------


def _enrich_with_lastfm(
    profile: dict[str, Any],
    lastfm_cache: dict[str, Any] | None,
    interaction_count: int,
) -> None:
    """
    Enrich a taste profile with Last.fm cached data.

    Last.fm data provides genre/artist preferences that may not be fully
    captured by local listening alone.  The influence decays as local
    interactions grow — at 500+ interactions, Last.fm contributes <10%.
    """
    if not lastfm_cache:
        return

    # Weight: high when few local interactions, fading as local data grows.
    # At 0 interactions: 1.0, at 100: ~0.37, at 500: ~0.007
    tp_cfg = get_config().taste_profile
    lastfm_weight = math.exp(-interaction_count / tp_cfg.lastfm_decay_interactions)
    if lastfm_weight < tp_cfg.enrichment_min_weight:
        return  # negligible, skip computation

    # --- Genre preferences from Last.fm ---
    genres = lastfm_cache.get("genres")
    if genres and isinstance(genres, dict):
        total = sum(genres.values()) or 1
        lastfm_genres = {g: round(c / total, 4) for g, c in genres.items()}
        existing = profile.get("lastfm_genres")
        if not existing:
            profile["lastfm_genres"] = lastfm_genres

    # --- Top artists from Last.fm (overall period) ---
    top_artists = lastfm_cache.get("top_artists", {}).get("overall", [])
    if top_artists:
        profile["lastfm_top_artists"] = [
            {
                "name": a.get("name", ""),
                "playcount": int(a.get("playcount", 0)),
            }
            for a in top_artists[:30]
            if a.get("name")
        ]

    # --- Loved tracks from Last.fm ---
    loved = lastfm_cache.get("loved_tracks", [])
    if loved:
        profile["lastfm_loved_tracks"] = [
            {
                "artist": t.get("artist", {}).get("name", "")
                if isinstance(t.get("artist"), dict)
                else str(t.get("artist", "")),
                "title": t.get("name", ""),
            }
            for t in loved[:30]
            if t.get("name")
        ]

    profile["lastfm_weight"] = round(lastfm_weight, 4)


# ---------------------------------------------------------------------------
# Onboarding enrichment
# ---------------------------------------------------------------------------


def _enrich_with_onboarding(
    profile: dict[str, Any],
    onboarding: dict[str, Any] | None,
    interaction_count: int,
) -> None:
    """
    Enrich a taste profile with explicit onboarding preferences.

    Onboarding influence decays faster than Last.fm — it's meant for
    cold-start only.  At 200+ interactions, onboarding is negligible.
    """
    if not onboarding:
        return

    tp_cfg = get_config().taste_profile
    onboarding_weight = math.exp(-interaction_count / tp_cfg.onboarding_decay_interactions)
    if onboarding_weight < tp_cfg.enrichment_min_weight:
        return

    # Blend explicit audio preferences if the user specified them.
    audio_prefs = profile.get("audio_preferences", {})
    if onboarding.get("energy_preference") is not None and "energy" in audio_prefs:
        local_mean = audio_prefs["energy"]["mean"]
        blended = local_mean * (1 - onboarding_weight) + onboarding["energy_preference"] * onboarding_weight
        audio_prefs["energy"]["mean"] = round(blended, 4)
    if onboarding.get("danceability_preference") is not None and "danceability" in audio_prefs:
        local_mean = audio_prefs["danceability"]["mean"]
        blended = local_mean * (1 - onboarding_weight) + onboarding["danceability_preference"] * onboarding_weight
        audio_prefs["danceability"]["mean"] = round(blended, 4)

    # Blend mood preferences.
    if onboarding.get("mood_preferences"):
        existing_moods = profile.get("mood_preferences", {})
        for mood in onboarding["mood_preferences"]:
            mood_lower = mood.lower()
            existing_val = existing_moods.get(mood_lower, 0.0)
            # Boost the onboarded mood by blending in a synthetic score.
            blended = existing_val * (1 - onboarding_weight) + 0.5 * onboarding_weight
            existing_moods[mood_lower] = round(blended, 4)
        if existing_moods:
            profile["mood_preferences"] = existing_moods

    # Add context/device preferences from onboarding.
    if onboarding.get("listening_contexts"):
        existing = profile.get("context_type_patterns", {})
        if not existing:
            n = len(onboarding["listening_contexts"])
            profile["context_type_patterns"] = {ctx: round(1.0 / n, 4) for ctx in onboarding["listening_contexts"]}

    if onboarding.get("device_types"):
        existing = profile.get("device_patterns", {})
        if not existing:
            n = len(onboarding["device_types"])
            profile["device_patterns"] = {dev: round(1.0 / n, 4) for dev in onboarding["device_types"]}

    profile["onboarding_weight"] = round(onboarding_weight, 4)


# ---------------------------------------------------------------------------
# Seed profile builder (cold-start from Last.fm + onboarding only)
# ---------------------------------------------------------------------------


async def build_seed_profile(
    session: AsyncSession,
    user: User,
) -> dict[str, Any] | None:
    """
    Build a minimal taste profile for a user with zero local interactions,
    using Last.fm cache and/or onboarding preferences.

    This gives the recommendation system something to work with before
    the user has generated any local listening events.

    Returns None if neither Last.fm nor onboarding data is available.
    """
    lastfm_cache = user.lastfm_cache
    onboarding = user.onboarding_preferences

    if not lastfm_cache and not onboarding:
        return None

    now = int(time.time())
    profile: dict[str, Any] = {
        "top_tracks": [],
        "behaviour": {
            "avg_session_tracks": None,
            "avg_skip_rate": None,
            "avg_completion": None,
            "total_plays": 0,
            "active_days": 0,
            "listening_since": None,
        },
        "popularity_preference": 0.5,
        "updated_at": now,
        "seed_source": [],
    }

    # --- Seed from onboarding ---
    if onboarding:
        profile["seed_source"].append("onboarding")

        if onboarding.get("energy_preference") is not None or onboarding.get("danceability_preference") is not None:
            audio = {}
            if onboarding.get("energy_preference") is not None:
                audio["energy"] = {"mean": onboarding["energy_preference"], "std": 0.15}
            if onboarding.get("danceability_preference") is not None:
                audio["danceability"] = {"mean": onboarding["danceability_preference"], "std": 0.15}
            profile["audio_preferences"] = audio

        if onboarding.get("mood_preferences"):
            n = len(onboarding["mood_preferences"])
            profile["mood_preferences"] = {m.lower(): round(1.0 / n, 4) for m in onboarding["mood_preferences"]}

        if onboarding.get("listening_contexts"):
            n = len(onboarding["listening_contexts"])
            profile["context_type_patterns"] = {ctx: round(1.0 / n, 4) for ctx in onboarding["listening_contexts"]}

        if onboarding.get("device_types"):
            n = len(onboarding["device_types"])
            profile["device_patterns"] = {dev: round(1.0 / n, 4) for dev in onboarding["device_types"]}

        # Match favourite tracks/artists against library to build audio prefs.
        if onboarding.get("favourite_tracks"):
            from sqlalchemy import or_

            # Onboarding may carry either internal track_ids or media_server_ids
            # (an iOS client typically only knows the latter at first-launch).
            result = await session.execute(
                select(TrackFeatures).where(
                    or_(
                        TrackFeatures.track_id.in_(onboarding["favourite_tracks"]),
                        TrackFeatures.media_server_id.in_(onboarding["favourite_tracks"]),
                    )
                )
            )
            tracks = result.scalars().all()
            if tracks:
                profile["top_tracks"] = [
                    {"track_id": t.track_id, "score": 1.0}
                    for t in tracks[: get_config().taste_profile.top_tracks_limit]
                ]
                # Compute audio preferences from favourite tracks.
                audio = profile.get("audio_preferences", {})
                for feat in _AUDIO_FEATURES:
                    vals = [getattr(t, feat) for t in tracks if getattr(t, feat, None) is not None]
                    if vals:
                        mean = sum(vals) / len(vals)
                        audio[feat] = {"mean": round(mean, 4), "std": 0.15}
                profile["audio_preferences"] = audio

        if onboarding.get("favourite_artists"):
            from sqlalchemy import func as sa_func

            artist_tracks = []
            for artist in onboarding["favourite_artists"]:
                result = await session.execute(
                    select(TrackFeatures).where(sa_func.lower(TrackFeatures.artist).contains(artist.lower())).limit(20)
                )
                artist_tracks.extend(result.scalars().all())
            if artist_tracks:
                audio = profile.get("audio_preferences", {})
                for feat in _AUDIO_FEATURES:
                    vals = [getattr(t, feat) for t in artist_tracks if getattr(t, feat, None) is not None]
                    if vals:
                        mean = sum(vals) / len(vals)
                        # Only set if not already seeded from favourite tracks.
                        if feat not in audio:
                            audio[feat] = {"mean": round(mean, 4), "std": 0.15}
                profile["audio_preferences"] = audio

        profile["onboarding_weight"] = 1.0

    # --- Seed from Last.fm ---
    if lastfm_cache:
        profile["seed_source"].append("lastfm")

        # Match Last.fm top tracks to library for audio preferences.
        lastfm_top = lastfm_cache.get("top_tracks", {}).get("overall", [])
        if lastfm_top:
            # Try to match by artist + title.
            matched_features = []
            for lt in lastfm_top[:30]:
                title = lt.get("name", "")
                artist = lt.get("artist", {})
                artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
                if not title or not artist_name:
                    continue
                result = await session.execute(
                    select(TrackFeatures)
                    .where(
                        func.lower(TrackFeatures.title) == title.lower(),
                        func.lower(TrackFeatures.artist).contains(artist_name.lower()),
                    )
                    .limit(1)
                )
                tf = result.scalar_one_or_none()
                if tf:
                    matched_features.append(tf)

            if matched_features:
                # Compute audio prefs from matched Last.fm tracks.
                audio = profile.get("audio_preferences", {})
                for feat in _AUDIO_FEATURES:
                    vals = [getattr(t, feat) for t in matched_features if getattr(t, feat, None) is not None]
                    if vals:
                        mean = sum(vals) / len(vals)
                        if feat in audio:
                            # Blend with onboarding if both present.
                            existing = audio[feat]["mean"]
                            audio[feat]["mean"] = round((existing + mean) / 2, 4)
                        else:
                            audio[feat] = {"mean": round(mean, 4), "std": 0.15}
                profile["audio_preferences"] = audio

                # Add matched Last.fm tracks to top_tracks.
                existing_ids = {t["track_id"] for t in profile["top_tracks"]}
                for tf in matched_features:
                    if tf.track_id not in existing_ids:
                        profile["top_tracks"].append({"track_id": tf.track_id, "score": 0.8})
                profile["top_tracks"] = profile["top_tracks"][: get_config().taste_profile.top_tracks_limit]

        # Genres from Last.fm.
        genres = lastfm_cache.get("genres")
        if genres and isinstance(genres, dict):
            total = sum(genres.values()) or 1
            profile["lastfm_genres"] = {g: round(c / total, 4) for g, c in genres.items()}

        # Top artists from Last.fm.
        top_artists = lastfm_cache.get("top_artists", {}).get("overall", [])
        if top_artists:
            profile["lastfm_top_artists"] = [
                {"name": a.get("name", ""), "playcount": int(a.get("playcount", 0))}
                for a in top_artists[:30]
                if a.get("name")
            ]

        # Loved tracks from Last.fm.
        loved = lastfm_cache.get("loved_tracks", [])
        if loved:
            profile["lastfm_loved_tracks"] = [
                {
                    "artist": t.get("artist", {}).get("name", "")
                    if isinstance(t.get("artist"), dict)
                    else str(t.get("artist", "")),
                    "title": t.get("name", ""),
                }
                for t in loved[:30]
                if t.get("name")
            ]

        profile["lastfm_weight"] = 1.0

    return (
        profile
        if (profile.get("audio_preferences") or profile.get("lastfm_genres") or profile.get("top_tracks"))
        else None
    )
