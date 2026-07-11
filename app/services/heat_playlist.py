"""GrooveIQ – "Heat playlist": the user's currently-hottest tracks.

Heat = recent play intensity (base) + a succession bonus for tracks played
back-to-back / on repeat in short succession. Deliberately isolated from
``resurfacing.py`` (the "Keep listening" surface): this is a NEW service with
its own ``get_config().heat`` group, computed on-demand, read-only, no model,
no schema migration.

Why not reuse resurfacing's heat? Its ``engagement_intensity`` caps
``full_listen_count`` at 3, so a track played 48× and one played 3× tie —
which flattens exactly the ranking a "Heat playlist" needs. Here the base uses
``log1p`` of the recency-decayed *event* counts, so a heavily-played track
cleanly outranks a lightly-played one with no cap. And because the app sends
almost no ``repeat``/``replay`` events, "succession" is DERIVED from the
``play_start`` stream (same-track adjacency within a short gap), not from
``TrackInteraction.repeat_count``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ListenEvent
from app.services.algorithm_config import get_config

_DAY_S = 86_400.0
# One indexed query covers plays, full-listens, and skips; ordered once, walked once.
_PLAY_STREAM_TYPES = ("play_start", "play_end", "skip")


@dataclass
class _Acc:
    """Per-track accumulator over the recent play stream."""

    d_play: float = 0.0  # Σ recency-decay over play_start events
    d_full: float = 0.0  # Σ recency-decay over full-listen play_end events
    d_skip: float = 0.0  # Σ recency-decay over skip signals
    d_succ: float = 0.0  # Σ recency-decay over succession adjacencies
    n_play: int = 0  # raw play count (min_plays floor + "played N× recently" chip)
    n_full: int = 0
    n_succ: int = 0  # raw succession count (the "on repeat" chip)
    last_ts: int = 0  # most-recent play_start ts (hero recency + freshness chip)


def _decay(now: int, ts: int, half_life_s: float) -> float:
    age = now - ts
    if age < 0:
        age = 0
    return 0.5 ** (age / half_life_s)


def _is_full_listen(value, dwell_ms, cfg) -> bool:
    """Whether a play_end represents a full listen. dwell-first (the accurate
    signal); ``value`` is a completion FRACTION in [0, 1] — a legacy elapsed-seconds
    value (> 1) is ignored here and left to the dwell branch, so a short legacy play
    is never mislabelled a full listen (mirrors track_scoring after c110691)."""
    if dwell_ms is not None and dwell_ms >= cfg.full_listen_ms:
        return True
    return value is not None and value <= 1.0 and value >= cfg.full_listen_completion


def _is_early_skip(value, dwell_ms, cfg) -> bool:
    """Whether a play_end / skip represents a genuine early abandon — a heat
    *penalty* — as opposed to a tap-next after hearing most of the track (which must
    NOT penalise heat; heavy-rotation tracks emit exactly those). dwell-first, then
    the completion fraction; unknown → not a penalty."""
    if _is_full_listen(value, dwell_ms, cfg):
        return False
    if value is not None and value <= 1.0:
        return value < cfg.skip_completion
    # No usable completion fraction (missing, or legacy seconds-form): fall back to
    # dwell — short and not a full listen → an early skip; otherwise unknown.
    return dwell_ms is not None and dwell_ms < cfg.full_listen_ms


async def get_heat_tracks(
    db: AsyncSession,
    user_id: str,
    *,
    limit: int = 60,
    now: int | None = None,
) -> tuple[list[tuple[str, float, dict]], bool]:
    """The user's currently-hottest tracks + whether Heat is hero-eligible.

    Returns ``(ranked, is_hero_eligible)`` where ``ranked`` is a list of
    ``(track_id, heat, signals)`` sorted by heat descending, truncated to
    ``limit``. ``signals`` carries the per-track breakdown that powers the
    reason chips. ``is_hero_eligible`` is computed over the FULL qualifying set
    (before truncation) so a small ``limit`` never suppresses it.

    Read-only: writes no ``listen_events``.
    """
    cfg = get_config().heat
    if not cfg.enabled:
        return [], False

    now = int(now if now is not None else time.time())
    half_life_s = cfg.half_life_days * _DAY_S
    gap_s = cfg.succession_gap_minutes * 60.0
    cutoff = now - cfg.window_days * int(_DAY_S)

    rows = (
        await db.execute(
            select(
                ListenEvent.track_id,
                ListenEvent.event_type,
                ListenEvent.timestamp,
                ListenEvent.value,
                ListenEvent.dwell_ms,
            )
            .where(
                ListenEvent.user_id == user_id,
                ListenEvent.event_type.in_(_PLAY_STREAM_TYPES),
                ListenEvent.timestamp >= cutoff,
            )
            .order_by(ListenEvent.timestamp.asc(), ListenEvent.id.asc())
        )
    ).all()

    accs: dict[str, _Acc] = {}
    # Succession lives on the play_start stream only: a play is "in succession"
    # when the immediately-preceding play (globally, in time order) was the same
    # track within a small gap. play_end must NOT advance the stream, or every
    # listen's play_start->play_end pair would be a false same-track adjacency.
    prev_track: str | None = None
    prev_ts: int = 0

    for track_id, etype, ts, value, dwell_ms in rows:
        acc = accs.get(track_id)
        if acc is None:
            acc = accs[track_id] = _Acc()
        w = _decay(now, ts, half_life_s)

        if etype == "play_start":
            acc.d_play += w
            acc.n_play += 1
            if ts > acc.last_ts:
                acc.last_ts = ts
            if prev_track == track_id and (ts - prev_ts) <= gap_s:
                acc.d_succ += w
                acc.n_succ += 1
            prev_track, prev_ts = track_id, ts
        elif etype == "play_end":
            if _is_full_listen(value, dwell_ms, cfg):
                acc.d_full += w
                acc.n_full += 1
            elif _is_early_skip(value, dwell_ms, cfg):
                acc.d_skip += w
        elif etype == "skip":
            # Only a genuine early abandon is a heat penalty. A skip after hearing
            # most of the track is a tap-next, not a rejection — counting it would
            # penalise exactly the heavy-rotation tracks Heat exists to surface.
            if _is_early_skip(value, dwell_ms, cfg):
                acc.d_skip += w

    scored: list[tuple[str, float, dict]] = []
    hero_count = 0
    hero_cutoff = now - cfg.hero_window_days * int(_DAY_S)

    for tid, acc in accs.items():
        if acc.n_play < cfg.min_plays:
            continue
        base = (
            cfg.w_play * math.log1p(acc.d_play)
            + cfg.w_full_listen * math.log1p(acc.d_full)
            - cfg.w_skip_penalty * math.log1p(acc.d_skip)
        )
        base = max(0.0, base)
        succ_bonus = cfg.w_succession * math.log1p(acc.d_succ)
        heat = base + succ_bonus
        if heat < cfg.min_heat:
            continue

        days_since = (now - acc.last_ts) / _DAY_S if acc.last_ts else None
        signals = {
            "plays": acc.n_play,
            "full_listens": acc.n_full,
            "skips": round(acc.d_skip, 3),
            "succession_count": acc.n_succ,
            "succession_score": round(acc.d_succ, 3),
            "base": round(base, 4),
            "succession_bonus": round(succ_bonus, 4),
            "last_played_at": acc.last_ts,
            "days_since": round(days_since, 2) if days_since is not None else None,
            "recency": round(_decay(now, acc.last_ts, half_life_s), 3) if acc.last_ts else 0.0,
        }
        scored.append((tid, heat, signals))

        if heat >= cfg.hot_threshold and acc.last_ts >= hero_cutoff:
            hero_count += 1

    scored.sort(key=lambda x: x[1], reverse=True)
    is_hero_eligible = hero_count >= cfg.hero_min_tracks
    return scored[:limit], is_hero_eligible


def heat_reason(signals: dict) -> str:
    """A short reason chip for a heat row (mirrors resurfacing's ``reason`` string style)."""
    if signals.get("succession_count", 0) >= 2:
        return "on repeat"
    plays = signals.get("plays", 0)
    if plays >= 2:
        return f"played {plays}× recently"
    return "heating up"
