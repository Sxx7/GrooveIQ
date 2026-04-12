"""
GrooveIQ – Track interaction scoring service.

Materialises TrackInteraction rows: one per (user, track) pair.
Runs incrementally — only processes events newer than the last run.

The satisfaction_score is a weighted combination of engagement signals,
designed as the training label for the future ranking model.

Weights (tunable):
  - Full listen (completion ≥ 0.8 or dwell ≥ 30s):  +1.0
  - Partial listen (mid-skip, 2s–30s):               +0.2
  - Early skip (<2s dwell):                           -0.5
  - Explicit like:                                    +2.0
  - Explicit dislike:                                 -2.0
  - Repeat:                                           +1.5
  - Playlist add:                                     +1.5
  - Queue add:                                        +0.5
  - Heavy seeking (>2 seeks in one play):             -0.3

The raw score is then normalised per-user to [0, 1] using min-max scaling
so that different users (heavy skipper vs passive listener) are comparable.

Edge cases:
  - Users with only 1 track interaction → score defaults to 0.5
  - Tracks never fully played → still scored from partial signals
  - Events without dwell_ms → classified by event_type + value alone
  - Division by zero everywhere guarded
  - Incremental: uses last_event_id high-water mark per (user, track)
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# Process in chunks.
_EVENT_CHUNK_SIZE = 5000


def _get_scoring_weights():
    """Read current scoring weights from the active algorithm config."""
    cfg = get_config().track_scoring
    return cfg


def _get_context_skip_weights() -> dict[str, float]:
    """Build context_type → early-skip weight map from config."""
    cfg = get_config().track_scoring
    return {
        "playlist": cfg.w_early_skip_playlist,
        "album": cfg.w_early_skip_playlist,
        "radio": cfg.w_early_skip_radio,
        "search": cfg.w_early_skip_radio,
        "home_shelf": cfg.w_early_skip,
    }


async def run_track_scoring() -> dict:
    """
    Main entry point.  Processes all un-scored events.

    Returns summary: {interactions_created, interactions_updated, events_processed}.
    """
    async with AsyncSessionLocal() as session:
        # Global high-water mark: the minimum of per-pair last_event_id
        # is not useful for incremental.  Instead, find the overall max
        # event_id across all interactions, then fetch events above that.
        # But some (user, track) pairs may not exist yet.  So we use
        # the minimum possible: 0 if no interactions exist at all,
        # otherwise the max last_event_id gives us an upper bound on
        # what's been processed — but new pairs might have older events.
        #
        # Correct approach: fetch ALL events above the global min last_event_id
        # across interactions (or 0), then for each (user, track) pair filter
        # to events above that pair's last_event_id.
        result = await session.execute(select(func.coalesce(func.min(TrackInteraction.last_event_id), 0)))
        global_min_hwm = result.scalar_one()

        # But if there are NO interactions yet, we need all events.
        # If there ARE interactions, some (user, track) pairs might be new.
        # The safest incremental approach: use a separate "scoring watermark"
        # that tracks the last fully processed event id.  For simplicity,
        # we track per-(user, track) via last_event_id and use global_min
        # as the chunk start.
        #
        # Optimisation: if all existing pairs have the same hwm (common
        # after a full run), global_min == that hwm and we skip processed events.

        total_created = 0
        total_updated = 0
        total_events = 0
        hwm = global_min_hwm

        while True:
            events = await _fetch_events_after(session, hwm, _EVENT_CHUNK_SIZE)
            if not events:
                break

            created, updated = await _process_events(session, events)
            total_created += created
            total_updated += updated
            total_events += len(events)
            hwm = events[-1].id
            await session.commit()

        # After all events processed, normalise satisfaction scores per user.
        if total_events > 0:
            await _normalise_scores(session)
            await session.commit()

            logger.info(
                "Track scoring complete",
                extra={
                    "events_processed": total_events,
                    "interactions_created": total_created,
                    "interactions_updated": total_updated,
                },
            )

    return {
        "interactions_created": total_created,
        "interactions_updated": total_updated,
        "events_processed": total_events,
    }


async def _fetch_events_after(session: AsyncSession, after_id: int, limit: int) -> Sequence:
    """Fetch events ordered by id, excluding reco_impression (not a user action)."""
    result = await session.execute(
        select(ListenEvent)
        .where(
            ListenEvent.id > after_id,
            ListenEvent.event_type != "reco_impression",
        )
        .order_by(ListenEvent.id)
        .limit(limit)
    )
    return result.scalars().all()


async def _process_events(session: AsyncSession, events: Sequence) -> tuple[int, int]:
    """
    Aggregate events into TrackInteraction rows.

    Returns (created, updated).
    """
    # Group events by (user_id, track_id).
    by_pair: dict[tuple[str, str], list] = defaultdict(list)
    for ev in events:
        by_pair[(ev.user_id, ev.track_id)].append(ev)

    # Fetch existing interactions for these pairs in bulk.
    pairs = list(by_pair.keys())
    existing = await _fetch_existing_interactions(session, pairs)

    created = 0
    updated = 0
    now = int(time.time())

    for (user_id, track_id), pair_events in by_pair.items():
        interaction = existing.get((user_id, track_id))

        # Filter to only events above this pair's high-water mark.
        if interaction is not None:
            pair_events = [e for e in pair_events if e.id > interaction.last_event_id]
            if not pair_events:
                continue

        delta = _compute_delta(pair_events)

        if interaction is None:
            # Create new interaction.
            row = TrackInteraction(
                user_id=user_id,
                track_id=track_id,
                play_count=delta["play_count"],
                skip_count=delta["skip_count"],
                like_count=delta["like_count"],
                dislike_count=delta["dislike_count"],
                repeat_count=delta["repeat_count"],
                playlist_add_count=delta["playlist_add_count"],
                queue_add_count=delta["queue_add_count"],
                total_dwell_ms=delta["total_dwell_ms"],
                avg_completion=delta["avg_completion"],
                early_skip_count=delta["early_skip_count"],
                mid_skip_count=delta["mid_skip_count"],
                full_listen_count=delta["full_listen_count"],
                total_seekfwd=delta["total_seekfwd"],
                total_seekbk=delta["total_seekbk"],
                first_played_at=delta["first_ts"],
                last_played_at=delta["last_ts"],
                satisfaction_score=_raw_satisfaction(delta),
                last_event_id=delta["max_event_id"],
                updated_at=now,
            )
            session.add(row)
            created += 1
        else:
            # Merge delta into existing.
            merged = _merge_interaction(interaction, delta)
            await session.execute(
                update(TrackInteraction)
                .where(TrackInteraction.id == interaction.id)
                .values(
                    play_count=merged["play_count"],
                    skip_count=merged["skip_count"],
                    like_count=merged["like_count"],
                    dislike_count=merged["dislike_count"],
                    repeat_count=merged["repeat_count"],
                    playlist_add_count=merged["playlist_add_count"],
                    queue_add_count=merged["queue_add_count"],
                    total_dwell_ms=merged["total_dwell_ms"],
                    avg_completion=merged["avg_completion"],
                    early_skip_count=merged["early_skip_count"],
                    mid_skip_count=merged["mid_skip_count"],
                    full_listen_count=merged["full_listen_count"],
                    total_seekfwd=merged["total_seekfwd"],
                    total_seekbk=merged["total_seekbk"],
                    first_played_at=merged["first_played_at"],
                    last_played_at=merged["last_played_at"],
                    satisfaction_score=merged["satisfaction_score"],
                    last_event_id=merged["last_event_id"],
                    updated_at=now,
                )
            )
            updated += 1

    return created, updated


async def _fetch_existing_interactions(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> dict[tuple[str, str], TrackInteraction]:
    """Bulk-fetch existing TrackInteraction rows for a list of (user, track) pairs."""
    if not pairs:
        return {}

    # Build OR conditions for each pair.  For large pair sets this could be
    # slow, but _EVENT_CHUNK_SIZE bounds the number of unique pairs per chunk.
    from sqlalchemy import and_, or_

    conditions = [and_(TrackInteraction.user_id == uid, TrackInteraction.track_id == tid) for uid, tid in pairs]
    result = await session.execute(select(TrackInteraction).where(or_(*conditions)))
    rows = result.scalars().all()
    return {(r.user_id, r.track_id): r for r in rows}


def _compute_delta(events: list) -> dict:
    """
    Compute aggregate counts from a batch of events for one (user, track) pair.
    """
    play_count = 0
    skip_count = 0
    like_count = 0
    dislike_count = 0
    repeat_count = 0
    playlist_add_count = 0
    queue_add_count = 0
    total_dwell_ms = 0
    has_dwell = False
    completions: list[float] = []
    early_skip_count = 0
    mid_skip_count = 0
    full_listen_count = 0
    total_seekfwd = 0
    total_seekbk = 0
    # Context-modulated skip penalty: sum of per-skip weights (replaces flat count * weight).
    context_skip_penalty = 0.0
    first_ts: int | None = None
    last_ts: int | None = None
    max_event_id = 0

    for ev in events:
        max_event_id = max(max_event_id, ev.id)

        if first_ts is None or ev.timestamp < first_ts:
            first_ts = ev.timestamp
        if last_ts is None or ev.timestamp > last_ts:
            last_ts = ev.timestamp

        et = ev.event_type

        if et == "play_start":
            play_count += 1

        if et == "play_end":
            completion = ev.value  # 0.0–1.0 or None
            if completion is not None:
                completions.append(completion)

            # Classify listen depth.
            dwell = ev.dwell_ms
            ctx = getattr(ev, "context_type", None)
            ctx_skip_map = _get_context_skip_weights()
            cfg = _get_scoring_weights()
            skip_w = ctx_skip_map.get(ctx, cfg.w_early_skip) if ctx else cfg.w_early_skip
            if dwell is not None:
                has_dwell = True
                total_dwell_ms += dwell
                if dwell < cfg.early_skip_ms:
                    early_skip_count += 1
                    context_skip_penalty += skip_w
                elif dwell < cfg.mid_skip_ms:
                    mid_skip_count += 1
                    # Mid-skips get half the context penalty of early skips.
                    context_skip_penalty += skip_w * 0.4
                else:
                    full_listen_count += 1
            elif completion is not None:
                # No dwell_ms — fall back to completion ratio.
                if completion >= 0.8:
                    full_listen_count += 1
                elif completion >= 0.1:
                    mid_skip_count += 1
                    context_skip_penalty += skip_w * 0.4
                else:
                    early_skip_count += 1
                    context_skip_penalty += skip_w

            # Check for heavy seeking on this play.
            (ev.num_seekfwd or 0) + (ev.num_seekbk or 0)
            total_seekfwd += ev.num_seekfwd or 0
            total_seekbk += ev.num_seekbk or 0

        elif et == "skip":
            skip_count += 1
            ctx = getattr(ev, "context_type", None)
            ctx_skip_map = _get_context_skip_weights()
            cfg = _get_scoring_weights()
            skip_w = ctx_skip_map.get(ctx, cfg.w_early_skip) if ctx else cfg.w_early_skip
            # A skip event without a preceding play_end: classify by value (seconds elapsed).
            if ev.value is not None:
                elapsed_ms = int(ev.value * 1000)
                if elapsed_ms < cfg.early_skip_ms:
                    early_skip_count += 1
                    context_skip_penalty += skip_w
                elif elapsed_ms < cfg.mid_skip_ms:
                    mid_skip_count += 1
                    context_skip_penalty += skip_w * 0.4

        elif et == "like":
            like_count += 1
        elif et == "dislike":
            dislike_count += 1
        elif et == "repeat":
            repeat_count += 1
        elif et == "playlist_add":
            playlist_add_count += 1
        elif et == "queue_add":
            queue_add_count += 1
        elif et in ("seek_forward", "seek_back"):
            if et == "seek_forward":
                total_seekfwd += 1
            else:
                total_seekbk += 1

    avg_completion = sum(completions) / len(completions) if completions else None

    return {
        "play_count": play_count,
        "skip_count": skip_count,
        "like_count": like_count,
        "dislike_count": dislike_count,
        "repeat_count": repeat_count,
        "playlist_add_count": playlist_add_count,
        "queue_add_count": queue_add_count,
        "total_dwell_ms": total_dwell_ms if has_dwell else None,
        "avg_completion": avg_completion,
        "early_skip_count": early_skip_count,
        "mid_skip_count": mid_skip_count,
        "full_listen_count": full_listen_count,
        "total_seekfwd": total_seekfwd,
        "total_seekbk": total_seekbk,
        "context_skip_penalty": context_skip_penalty,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "max_event_id": max_event_id,
    }


def _raw_satisfaction(delta: dict) -> float:
    """
    Compute a raw (un-normalised) satisfaction score from aggregated counts.

    Skip penalty uses context-modulated weights when available (skips in
    radio/search are weakly negative, skips mid-playlist are strongly negative).
    Falls back to flat weights when context_skip_penalty is absent.
    """
    cfg = _get_scoring_weights()
    score = 0.0
    score += delta["full_listen_count"] * cfg.w_full_listen
    # Use context-modulated skip penalty when available.  The penalty
    # already accounts for both early and mid skips weighted by context_type,
    # so we only add the mid-listen positive signal on top.
    ctx_penalty = delta.get("context_skip_penalty")
    if ctx_penalty is not None and ctx_penalty != 0.0:
        score += ctx_penalty  # negative, context-weighted
        score += delta["mid_skip_count"] * cfg.w_mid_listen  # partial positive
    else:
        # Fallback: flat weights (no context info on events).
        score += delta["early_skip_count"] * cfg.w_early_skip
        score += delta["mid_skip_count"] * cfg.w_mid_listen
    score += delta["like_count"] * cfg.w_like
    score += delta["dislike_count"] * cfg.w_dislike
    score += delta["repeat_count"] * cfg.w_repeat
    score += delta["playlist_add_count"] * cfg.w_playlist_add
    score += delta["queue_add_count"] * cfg.w_queue_add

    # Penalise heavy seeking (proportional to total seeks above threshold).
    total_seeks = delta["total_seekfwd"] + delta["total_seekbk"]
    plays = max(delta["play_count"], 1)
    seeks_per_play = total_seeks / plays
    if seeks_per_play > cfg.heavy_seek_threshold:
        score += (seeks_per_play - cfg.heavy_seek_threshold) * cfg.w_heavy_seek

    return score


def _merge_interaction(existing: TrackInteraction, delta: dict) -> dict:
    """
    Merge a new delta into an existing TrackInteraction's values.

    Returns a dict of the merged values ready for UPDATE.
    """
    new_play_count = existing.play_count + delta["play_count"]
    new_skip_count = existing.skip_count + delta["skip_count"]
    new_like_count = existing.like_count + delta["like_count"]
    new_dislike_count = existing.dislike_count + delta["dislike_count"]
    new_repeat_count = existing.repeat_count + delta["repeat_count"]
    new_playlist_add_count = existing.playlist_add_count + delta["playlist_add_count"]
    new_queue_add_count = existing.queue_add_count + delta["queue_add_count"]
    new_early_skip_count = existing.early_skip_count + delta["early_skip_count"]
    new_mid_skip_count = existing.mid_skip_count + delta["mid_skip_count"]
    new_full_listen_count = existing.full_listen_count + delta["full_listen_count"]
    new_total_seekfwd = existing.total_seekfwd + delta["total_seekfwd"]
    new_total_seekbk = existing.total_seekbk + delta["total_seekbk"]

    # Merge dwell.
    if delta["total_dwell_ms"] is not None:
        new_total_dwell = (existing.total_dwell_ms or 0) + delta["total_dwell_ms"]
    else:
        new_total_dwell = existing.total_dwell_ms

    # Merge avg_completion as weighted average.
    old_compl = existing.avg_completion
    new_compl = delta["avg_completion"]
    old_plays = existing.play_count
    new_plays = delta["play_count"]
    if old_compl is not None and new_compl is not None:
        total = old_plays + new_plays
        merged_compl = (old_compl * old_plays + new_compl * new_plays) / total if total > 0 else None
    elif new_compl is not None:
        merged_compl = new_compl
    else:
        merged_compl = old_compl

    # Temporal.
    first_played = existing.first_played_at
    if delta["first_ts"] is not None:
        if first_played is None or delta["first_ts"] < first_played:
            first_played = delta["first_ts"]
    last_played = existing.last_played_at
    if delta["last_ts"] is not None:
        if last_played is None or delta["last_ts"] > last_played:
            last_played = delta["last_ts"]

    # Recompute raw satisfaction from merged totals.
    # Context skip penalty: we can only accumulate the new delta's penalty
    # (the existing interaction was already computed with its own penalties).
    # On merge we fall back to flat weights for the full history — context
    # modulation improves incrementally as new events arrive with context.
    merged_delta = {
        "full_listen_count": new_full_listen_count,
        "mid_skip_count": new_mid_skip_count,
        "early_skip_count": new_early_skip_count,
        "like_count": new_like_count,
        "dislike_count": new_dislike_count,
        "repeat_count": new_repeat_count,
        "playlist_add_count": new_playlist_add_count,
        "queue_add_count": new_queue_add_count,
        "total_seekfwd": new_total_seekfwd,
        "total_seekbk": new_total_seekbk,
        "play_count": new_play_count,
        "context_skip_penalty": delta.get("context_skip_penalty", 0.0),
    }

    return {
        "play_count": new_play_count,
        "skip_count": new_skip_count,
        "like_count": new_like_count,
        "dislike_count": new_dislike_count,
        "repeat_count": new_repeat_count,
        "playlist_add_count": new_playlist_add_count,
        "queue_add_count": new_queue_add_count,
        "total_dwell_ms": new_total_dwell,
        "avg_completion": merged_compl,
        "early_skip_count": new_early_skip_count,
        "mid_skip_count": new_mid_skip_count,
        "full_listen_count": new_full_listen_count,
        "total_seekfwd": new_total_seekfwd,
        "total_seekbk": new_total_seekbk,
        "first_played_at": first_played,
        "last_played_at": last_played,
        "satisfaction_score": _raw_satisfaction(merged_delta),
        "last_event_id": max(existing.last_event_id, delta["max_event_id"]),
    }


async def _normalise_scores(session: AsyncSession) -> None:
    """
    Min-max normalise satisfaction_score per user to [0, 1].

    Users with only one interaction get score 0.5.
    Users where min == max (all same score) get score 0.5.
    """
    # Get per-user min/max of raw satisfaction scores.
    result = await session.execute(
        select(
            TrackInteraction.user_id,
            func.min(TrackInteraction.satisfaction_score).label("min_score"),
            func.max(TrackInteraction.satisfaction_score).label("max_score"),
            func.count(TrackInteraction.id).label("cnt"),
        ).group_by(TrackInteraction.user_id)
    )
    user_stats = result.all()

    for user_id, min_score, max_score, cnt in user_stats:
        if cnt <= 1 or min_score is None or max_score is None:
            # Single interaction or no scores: default to 0.5.
            await session.execute(
                update(TrackInteraction).where(TrackInteraction.user_id == user_id).values(satisfaction_score=0.5)
            )
            continue

        score_range = max_score - min_score
        if score_range < 1e-9:
            # All interactions have the same raw score.
            await session.execute(
                update(TrackInteraction).where(TrackInteraction.user_id == user_id).values(satisfaction_score=0.5)
            )
            continue

        # Normalise: (score - min) / (max - min).
        await session.execute(
            update(TrackInteraction)
            .where(TrackInteraction.user_id == user_id)
            .values(satisfaction_score=((TrackInteraction.satisfaction_score - min_score) / score_range))
        )
