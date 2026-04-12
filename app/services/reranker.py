"""
GrooveIQ – Post-ranking reranker (Phase 4, Step 8).

Applied after the LightGBM ranker, before returning results.
Enforces diversity constraints and applies business rules:

  1. Artist diversity — max 2 tracks from the same "artist" in top 10
  2. Anti-repetition — suppress tracks played in the last 2 hours
  3. Skip suppression — demote tracks early-skipped >2 times in last 24h
  4. Freshness boost — +10% score uplift for tracks the user has never played
  5. Exploration slots — reserve ~15% of slots for low-interaction tracks with
     score noise proportional to uncertainty (Thompson Sampling-inspired)
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)


def _extract_artist(file_path: str | None) -> str:
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
    ranked: list[tuple[str, float]],
    user_id: str,
    session: AsyncSession,
    device_type: str | None = None,
    output_type: str | None = None,
    collect_actions: bool = False,
) -> list[tuple[str, float]]:
    """
    Apply diversity and business-rule filters to ranked candidates.

    Args:
        ranked: list of (track_id, score) sorted descending.
        user_id: user for interaction lookups.
        session: DB session.
        device_type: current device class (mobile, desktop, speaker, car, web).
        output_type: current audio output (headphones, speaker, car_audio, etc.).

    Returns:
        Reranked list of (track_id, score).
    """
    if not ranked:
        return ranked

    track_ids = [tid for tid, _ in ranked]
    score_map = {tid: score for tid, score in ranked}

    # --- Load data needed for all rules ---
    now = int(time.time())
    actions: list[dict] = [] if collect_actions else None

    # Track features (for artist extraction and context-aware rules).
    feat_result = await session.execute(
        select(TrackFeatures.track_id, TrackFeatures.file_path, TrackFeatures.duration).where(
            TrackFeatures.track_id.in_(track_ids)
        )
    )
    feat_rows = feat_result.all()
    path_map = {row.track_id: row.file_path for row in feat_rows}
    duration_map = {row.track_id: row.duration for row in feat_rows}

    # Interactions for this user.
    inter_result = await session.execute(
        select(TrackInteraction).where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.track_id.in_(track_ids),
        )
    )
    inter_map: dict[str, TrackInteraction] = {i.track_id: i for i in inter_result.scalars().all()}

    cfg = get_config().reranker

    # --- Rule 1: Freshness boost (never-played tracks get score uplift) ---
    for tid in track_ids:
        if tid not in inter_map:
            old = score_map[tid]
            score_map[tid] = old * (1.0 + cfg.freshness_boost)
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "freshness_boost",
                        "score_before": round(old, 4),
                        "score_after": round(score_map[tid], 4),
                    }
                )

    # --- Rule 2: Skip suppression (early-skipped >threshold times recently → demote) ---
    cutoff_24h = now - 86_400
    for tid in track_ids:
        inter = inter_map.get(tid)
        if inter and inter.early_skip_count > cfg.skip_threshold:
            if inter.last_played_at and inter.last_played_at >= cutoff_24h:
                old = score_map[tid]
                score_map[tid] = old * cfg.skip_demote_factor
                if actions is not None:
                    actions.append(
                        {
                            "track_id": tid,
                            "action": "skip_suppression",
                            "score_before": round(old, 4),
                            "score_after": round(score_map[tid], 4),
                        }
                    )

    # --- Rule 3: Anti-repetition (suppress tracks played recently) ---
    recently_played: set[str] = set()
    cutoff_repeat = now - int(cfg.repeat_window_hours * 3600)
    for tid in track_ids:
        inter = inter_map.get(tid)
        if inter and inter.last_played_at and inter.last_played_at >= cutoff_repeat:
            recently_played.add(tid)
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "anti_repetition_exclude",
                        "reason": f"played_within_{cfg.repeat_window_hours}h",
                    }
                )

    # --- Rule 5: Context-aware — suppress short tracks in car/speaker mode ---
    short_tracks: set[str] = set()
    is_car_or_speaker = device_type in ("car", "speaker") or output_type in (
        "car_audio",
        "bluetooth_speaker",
        "speaker",
    )
    if is_car_or_speaker:
        for tid in track_ids:
            dur = duration_map.get(tid)
            if dur is not None and dur < cfg.min_duration_car:
                short_tracks.add(tid)
                if actions is not None:
                    actions.append({"track_id": tid, "action": "short_track_exclude", "duration": dur})

    # --- Rebuild sorted list with updated scores, excluding filtered tracks ---
    excluded = recently_played | short_tracks
    adjusted = [(tid, score_map[tid]) for tid in track_ids if tid not in excluded]
    adjusted.sort(key=lambda x: x[1], reverse=True)

    # --- Rule 4: Exploration slots (before artist diversity so diversity is final) ---
    adjusted = _inject_exploration_slots(adjusted, inter_map, actions=actions)

    # --- Rule 5: Artist diversity in top N (applied last to guarantee constraint) ---
    artist_map = {tid: _extract_artist(path_map.get(tid)) for tid in track_ids}
    result = _enforce_artist_diversity(adjusted, artist_map, actions=actions)

    if collect_actions:
        # Attach actions to result via module-level cache
        _last_rerank_actions[:] = actions
    return result


# Module-level cache for last rerank actions (for debug mode).
_last_rerank_actions: list[dict] = []


def get_last_rerank_actions() -> list[dict]:
    """Return the actions from the most recent rerank call (debug mode)."""
    return list(_last_rerank_actions)


def _enforce_artist_diversity(
    ranked: list[tuple[str, float]],
    artist_map: dict[str, str],
    actions: list[dict] | None = None,
) -> list[tuple[str, float]]:
    """
    Ensure no more than _ARTIST_MAX_PER_TOP tracks from the same artist
    appear in the first _ARTIST_DIVERSITY_TOP_N positions of the output.

    Excess tracks are pushed past position N, preserving relative order.
    """
    cfg = get_config().reranker
    # Two-pass: first fill the top N slots respecting diversity, then append the rest.
    accepted: list[tuple[str, float]] = []
    deferred: list[tuple[str, float]] = []
    artist_count: Counter = Counter()

    for tid, score in ranked:
        artist = artist_map.get(tid, "__unknown__")
        if len(accepted) < cfg.artist_diversity_top_n:
            if artist_count[artist] < cfg.artist_max_per_top:
                accepted.append((tid, score))
                artist_count[artist] += 1
            else:
                deferred.append((tid, score))
                if actions is not None:
                    actions.append(
                        {
                            "track_id": tid,
                            "action": "artist_diversity_demote",
                            "from_position": len(accepted) + len(deferred) - 1,
                            "to_position": cfg.artist_diversity_top_n + len(deferred) - 1,
                        }
                    )
        else:
            deferred.append((tid, score))

    return accepted + deferred


def _inject_exploration_slots(
    ranked: list[tuple[str, float]],
    inter_map: dict[str, TrackInteraction],
    actions: list[dict] | None = None,
) -> list[tuple[str, float]]:
    """
    Reserve ~15% of recommendation slots for under-explored tracks.

    Tracks with few interactions have high uncertainty — they might be
    great but the model lacks data.  We inject them into the final list
    by adding score noise proportional to 1/sqrt(play_count + 1),
    inspired by Thompson Sampling.

    Exploration tracks are pulled from the bottom half of the ranked
    list (tracks the model scored lower but hasn't seen enough data on)
    and placed into evenly-spaced "exploration slots" in the output.
    """
    cfg = get_config().reranker
    n = len(ranked)
    if n < 5:
        return ranked

    n_explore = max(1, int(n * cfg.exploration_fraction))

    # Split: top half = exploitation pool, bottom half = exploration pool.
    split = n // 2
    exploit_pool = list(ranked[:split])
    explore_pool = list(ranked[split:])

    # Score exploration candidates with uncertainty noise.
    scored_explore: list[tuple[str, float, float]] = []  # (tid, original, noisy)
    for tid, score in explore_pool:
        plays = inter_map[tid].play_count if tid in inter_map else 0
        # More noise for tracks with fewer plays.
        noise = random.gauss(0, 1) * cfg.exploration_noise_scale / math.sqrt(plays + 1)
        noisy_score = score + abs(noise)  # bias upward to give them a chance
        scored_explore.append((tid, score, noisy_score))

    # Pick top exploration candidates by noisy score.
    scored_explore.sort(key=lambda x: x[2], reverse=True)
    explore_picks = [(tid, orig) for tid, orig, _ in scored_explore[:n_explore]]
    if actions is not None:
        for tid, orig, noisy in scored_explore[:n_explore]:
            actions.append({"track_id": tid, "action": "exploration_slot", "noise_added": round(noisy - orig, 4)})

    # Interleave: place exploration tracks at evenly-spaced positions.
    # E.g. for 25 results and 4 explore slots → positions ~6, 12, 18, 24.
    result: list[tuple[str, float]] = []
    explore_ids = {tid for tid, _ in explore_picks}
    # Remove explore picks from exploit pool if they somehow overlap.
    exploit_pool = [(tid, s) for tid, s in exploit_pool if tid not in explore_ids]
    # Also remove from the remaining explore pool.
    remaining = [(tid, s) for tid, s in explore_pool if tid not in explore_ids]
    # Full non-explore list in original rank order.
    non_explore = exploit_pool + remaining

    if not explore_picks:
        return ranked

    # Calculate spacing: spread explore tracks evenly across the output.
    total_out = len(non_explore) + len(explore_picks)
    spacing = total_out / (len(explore_picks) + 1)

    explore_iter = iter(explore_picks)
    non_explore_iter = iter(non_explore)
    next_explore_pos = spacing

    for i in range(total_out):
        if i >= next_explore_pos - 0.5:
            ex = next(explore_iter, None)
            if ex is not None:
                result.append(ex)
                next_explore_pos += spacing
                continue
        ne = next(non_explore_iter, None)
        if ne is not None:
            result.append(ne)
        else:
            # Drain remaining explore picks.
            ex = next(explore_iter, None)
            if ex is not None:
                result.append(ex)

    return result
