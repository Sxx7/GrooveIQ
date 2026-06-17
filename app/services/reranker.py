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
from app.services import confidence, faiss_index
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# T3 (discovery-dial): play count at which a track counts as fully "familiar" for
# the continuous novelty penalty. ~8 plays ≈ a track the user clearly knows and
# returns to; below that, familiarity ramps linearly so lightly-heard tracks
# aren't over-penalised.
_NOVELTY_FULL_PLAYS = 8.0


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
    rng: random.Random | None = None,
) -> list[tuple[str, float]]:
    """
    Apply diversity and business-rule filters to ranked candidates.

    Args:
        ranked: list of (track_id, score) sorted descending.
        user_id: user for interaction lookups.
        session: DB session.
        device_type: current device class (mobile, desktop, speaker, car, web).
        output_type: current audio output (headphones, speaker, car_audio, etc.).
        rng: optional RNG for the (stochastic) exploration slots. Defaults to the
            global ``random`` module — i.e. unchanged behaviour. Pass a seeded
            ``random.Random`` for deterministic tests.

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

    # --- Discovery-dial acquisition (additive, gated) ---
    # Apply the UCB-style adjustment on top of today's ranker score:
    #     adj = ranker_score + kappa*sigma - lambda_proven*[is_proven]
    # `modes.active` is the dial-resolved preset (a no-op by default). The gate
    # below is *structural*, not float-luck: when both coefficients are zero
    # (familiar / balanced / default) confidence is never computed and score_map
    # is left exactly as the ranker produced it — so the default path is
    # byte-for-byte unchanged. mu/sigma never replace the base ordering signal.
    preset = get_config().modes.active
    if preset.kappa > 0.0 or preset.lambda_proven > 0.0:
        evidence = await confidence.load_user_evidence(session, user_id)
        for tid, inter in inter_map.items():
            evidence.setdefault(tid, confidence.InteractionEvidence.from_obj(inter))
        conf = confidence.compute_confidence(
            user_id,
            track_ids,
            interactions=evidence,
            faiss_index=faiss_index.effnet_index,
            base_scores=dict(score_map),
            preset=preset,
        )
        for tid in track_ids:
            cs = conf.get(tid)
            if cs is None:
                continue
            old = score_map[tid]
            score_map[tid] = old + preset.kappa * cs.sigma - preset.lambda_proven * (1.0 if cs.is_proven else 0.0)
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "acquisition",
                        "score_before": round(old, 4),
                        "score_after": round(score_map[tid], 4),
                        "sigma": round(cs.sigma, 4),
                        "is_proven": cs.is_proven,
                    }
                )

    # --- Discovery-dial novelty penalty (continuous familiarity demotion, gated) ---
    # Distinct from lambda_proven (binary, thresholded on the confidence mu/sigma):
    # this demotes tracks *smoothly* by how much THIS user has actually played them,
    # so the familiar cluster sinks proportionally and novel tracks can surface past
    # it. Uses raw play history (not the confidence model, which can misclassify a
    # heavy favourite as un-proven). Off (=0) for familiar/balanced — default path
    # is left exactly as the ranker produced it.
    if preset.novelty_weight > 0.0:
        for tid in track_ids:
            inter = inter_map.get(tid)
            plays = inter.play_count if inter else 0
            if plays <= 0:
                continue
            familiarity = min(1.0, plays / _NOVELTY_FULL_PLAYS)
            old = score_map[tid]
            score_map[tid] = old - preset.novelty_weight * familiarity
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "novelty_penalty",
                        "score_before": round(old, 4),
                        "score_after": round(score_map[tid], 4),
                        "familiarity": round(familiarity, 4),
                    }
                )

    # --- Discovery-dial familiarity boost (continuous proven uplift, gated) ---
    # The positive mirror of the novelty penalty above: at the *familiar* end we
    # actively up-rank tracks THIS user has played, proportional to play count, so
    # the proven cluster rises to the top instead of merely "not being excluded".
    # Crowd-free — it reads the user's own play history, not any CF signal. Gated
    # on familiarity_weight > 0 (only familiar sets it), so balanced/discovery/deep
    # and the default path are byte-for-byte unchanged.
    if preset.familiarity_weight > 0.0:
        for tid in track_ids:
            inter = inter_map.get(tid)
            plays = inter.play_count if inter else 0
            if plays <= 0:
                continue
            familiarity = min(1.0, plays / _NOVELTY_FULL_PLAYS)
            old = score_map[tid]
            score_map[tid] = old + preset.familiarity_weight * familiarity
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "familiarity_boost",
                        "score_before": round(old, 4),
                        "score_after": round(score_map[tid], 4),
                        "familiarity": round(familiarity, 4),
                    }
                )

    # --- Recently-engaged resurfacing boost (cross-surface "special track", capped at one) ---
    # A track the user just replayed / seeked-back / finished / liked earns time-decayed heat
    # (app.services.resurfacing). Boost only the single hottest such candidate so it resurfaces
    # across radio / Discover / Library without flooding one batch — like a single inserted rec.
    # `boostable_heat_target` reuses the inter_map already loaded above but honours suppress + the
    # Special-card ignore-gate, so the immediate cross-surface spread (GrooveIQ#139) never keeps
    # boosting a track the user dismissed or repeatedly ignored. Gated on cfg.recently_engaged_boost.
    #
    # Posture gate (GrooveIQ#P2): twin of radio.py's Source-10 resurfacing gate. Suppress the
    # boost on exploratory postures (discovery / deep_discovery) so a Discover-launched radio — or
    # the discovery /recommend tier — isn't re-led by the user's hottest proven track. `preset` is
    # the dial-resolved posture (modes.active under apply_overrides); keyed on the same novelty
    # levers as radio so familiar / balanced still boost and discovery / deep do not.
    _exploratory_posture = bool(preset.novelty_filter) or bool(preset.require_interaction)
    if cfg.recently_engaged_boost > 0.0 and inter_map and not _exploratory_posture:
        from app.services.resurfacing import boostable_heat_target

        target = await boostable_heat_target(user_id, session, inter_map, now)
        if target is not None:
            tid, heat = target
            old = score_map[tid]
            score_map[tid] = old + cfg.recently_engaged_boost * heat
            if actions is not None:
                actions.append(
                    {
                        "track_id": tid,
                        "action": "recently_engaged_boost",
                        "score_before": round(old, 4),
                        "score_after": round(score_map[tid], 4),
                        "heat": round(heat, 4),
                    }
                )

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
    adjusted = _inject_exploration_slots(adjusted, inter_map, actions=actions, rng=rng)

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
    rng: random.Random | None = None,
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

    _rng = rng if rng is not None else random
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
        noise = _rng.gauss(0, 1) * cfg.exploration_noise_scale / math.sqrt(plays + 1)
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
