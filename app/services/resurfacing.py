"""GrooveIQ – Recently-engaged "resurfacing" heat (the cross-surface special-track loop).

A track the user has *just engaged with* — replayed, seeked back into, finished, liked, or
deliberately searched out and played in full — earns a short-lived, time-decayed "heat" so it
keeps resurfacing across radio, Discover and Library for a while (the "special track that keeps
coming back"). Heat fades if the user stops
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
from collections import defaultdict

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

# --- Special-tracks confirmation loop (the two-card candidate→confirmed split, GrooveIQ#139) ---
# Library shows two resurfacing cards: "Special tracks" (stage-1 candidates on a tryout) and
# "Keep listening" (stage-2 confirmed favourites). A single seek-back / replay nominates a track
# as a CANDIDATE — a deliberately low bar ("rewind, that was good"). It graduates to CONFIRMED on
# one notch more intent: an explicit vote (played from the Special card, or liked) confirms
# immediately; the organic path waits for a clear repeat so a one-off doesn't self-promote. A
# candidate shown repeatedly but never touched is dropped (the ignore-gate), and that drop also
# stops it spreading cross-surface (the reranker honours the same gate — immediate spread, #139).
SPECIAL_TRACKS_SURFACE = "library:special_tracks"
_CONFIRM_REPEAT_MIN = 2  # repeat/replay count that organically confirms a candidate
_IGNORE_GATE_LIMIT = 3  # Special-card impressions with no play (since last engagement) → drop


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


def _is_candidate_triggered(inter) -> bool:
    """Whether a track is eligible for the Special-tracks tryout at all — it must have been
    nominated by the candidate signal itself: a seek-back, a back-to-back replay, or a
    deliberately-searched track the user listened to in full. Search is a strong but
    ambiguous intent signal (the user went looking for it — but maybe on a friend's rec they
    won't actually keep), so a searched full-listen earns a tryout, not instant trust."""
    return inter.total_seekbk > 0 or inter.repeat_count > 0 or (getattr(inter, "search_play_count", 0) or 0) > 0


def _is_confirmed(inter, confirmed_via_card: set[str]) -> bool:
    """Whether a candidate has graduated to the Keep-listening ("confirmed") card — one clear
    vote beyond the seek-back/replay that nominated it: played from the Special card (the strong,
    explicit signal), liked, or organically returned to (≥``_CONFIRM_REPEAT_MIN`` replays).

    A passive full-listen is deliberately NOT a graduation signal. An ordinary 2nd play would
    otherwise auto-confirm a track before the tryout ("Special tracks") card could ever show it —
    which is exactly why the candidate card came back empty in prod (most seek-back/replay
    nominees were already full-listened twice). Graduation must be a real vote (played-from-card /
    like) or a clear organic repeat; a searched full-listen still only earns a *tryout* via
    :func:`_is_candidate_triggered`, never instant trust."""
    return (
        inter.track_id in confirmed_via_card
        or inter.like_count > 0
        or inter.repeat_count >= _CONFIRM_REPEAT_MIN
    )


async def _suppressed_until(user_id: str, db: AsyncSession) -> dict[str, int]:
    """Latest ``suppress`` event timestamp per track for this user (the dismiss signal)."""
    result = await db.execute(
        select(ListenEvent.track_id, func.max(ListenEvent.timestamp))
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "suppress")
        .group_by(ListenEvent.track_id)
    )
    return {row[0]: int(row[1]) for row in result.all() if row[1] is not None}


async def _special_card_signals(
    user_id: str, db: AsyncSession, last_played: dict[str, int]
) -> tuple[set[str], dict[str, int]]:
    """Special-tracks card feedback derived from the impression/play log.

    Returns ``(confirmed_via_card, ignored_count)``:
      * ``confirmed_via_card`` — tracks the user *played from the Special card*: an impression on
        :data:`SPECIAL_TRACKS_SURFACE` whose ``request_id`` later carried a play of the same
        track (the strong, explicit confirm).
      * ``ignored_count`` — per track, Special-card impressions that did NOT lead to such a play,
        counted only since the track's last engagement (``last_played``), so any fresh seek-back /
        replay / play resets it back toward the ignore-gate.
    """
    imp_result = await db.execute(
        select(ListenEvent.track_id, ListenEvent.request_id, ListenEvent.timestamp).where(
            ListenEvent.user_id == user_id,
            ListenEvent.event_type == "reco_impression",
            ListenEvent.surface == SPECIAL_TRACKS_SURFACE,
            ListenEvent.request_id.isnot(None),
        )
    )
    impressions = imp_result.all()
    if not impressions:
        return set(), {}

    request_ids = {row[1] for row in impressions}
    play_result = await db.execute(
        select(ListenEvent.request_id, ListenEvent.track_id).where(
            ListenEvent.user_id == user_id,
            ListenEvent.event_type.in_(("play_start", "play_end")),
            ListenEvent.request_id.in_(request_ids),
        )
    )
    played_pairs = {(row[0], row[1]) for row in play_result.all()}

    confirmed_via_card: set[str] = set()
    ignored_count: dict[str, int] = defaultdict(int)
    for track_id, request_id, ts in impressions:
        if (request_id, track_id) in played_pairs:
            confirmed_via_card.add(track_id)
        elif ts > last_played.get(track_id, 0):
            ignored_count[track_id] += 1
    return confirmed_via_card, dict(ignored_count)


async def boostable_heat_target(user_id: str, db: AsyncSession, inter_map: dict, now: int) -> tuple[str, float] | None:
    """The single hottest track the user is currently into that we may still spread
    cross-surface — not suppressed, and not a dropped (ignore-gated) Special-card candidate.

    Powers the reranker's ``recently_engaged_boost`` so the immediate cross-surface spread
    (GrooveIQ#139) honours the user's drops and dismissals. Reuses the caller's already-loaded
    ``inter_map`` (``{track_id: TrackInteraction}``), so it adds only the suppress +
    impression/play lookups, not another interaction scan.
    """
    if not inter_map:
        return None
    suppressed = await _suppressed_until(user_id, db)
    last_played = {tid: (inter.last_played_at or 0) for tid, inter in inter_map.items()}
    confirmed_via_card, ignored_count = await _special_card_signals(user_id, db, last_played)

    best: tuple[str, float] | None = None
    for tid, inter in inter_map.items():
        sup_ts = suppressed.get(tid)
        if sup_ts is not None and (inter.last_played_at or 0) <= sup_ts:
            continue  # suppressed and not re-engaged since
        if ignored_count.get(tid, 0) >= _IGNORE_GATE_LIMIT and not _is_confirmed(inter, confirmed_via_card):
            continue  # a candidate the user keeps ignoring — stop spreading it cross-surface
        heat = engagement_heat(inter, now)
        if heat <= 0.0:
            continue
        if best is None or heat > best[1]:
            best = (tid, heat)
    return best


async def get_resurfacing_tracks(
    user_id: str,
    db: AsyncSession,
    *,
    limit: int = 20,
    min_heat: float = 0.05,
    stage: str | None = None,
    apply_ignore_gate: bool = False,
) -> list[tuple[str, float]]:
    """The user's currently-"hot" tracks as ``(track_id, heat)``, highest first.

    Excludes tracks the user suppressed since their last engagement (re-engaging after a
    suppress brings a track back — the suppress only applies to engagement up to that point).

    ``stage`` selects which resurfacing card to fill (GrooveIQ#139):
      * ``None`` — every hot track (legacy / internal callers).
      * ``"confirmed"`` — the *Keep listening* card: hot tracks the user has embraced (played
        from the Special card, liked, or returned to ≥2×).
      * ``"candidate"`` — the *Special tracks* card: hot tracks nominated by a seek-back / replay
        / searched full-listen that are not yet confirmed and not yet dropped by the ignore-gate.

    ``apply_ignore_gate`` additionally drops still-unconfirmed candidates the user keeps ignoring
    on the Special card (even at ``stage=None``), so cross-surface spread — e.g. radio's
    resurfacing injection — honours a drop the same way the card does.
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
                # Searched-and-fully-played candidates (a strict subset of full_listen_count
                # today, but kept explicit so the nominator survives a looser search bar).
                TrackInteraction.search_play_count > 0,
            ),
        )
    )
    interactions = result.scalars().all()
    suppressed = await _suppressed_until(user_id, db)

    confirmed_via_card: set[str] = set()
    ignored_count: dict[str, int] = {}
    need_signals = stage is not None or apply_ignore_gate
    if need_signals:
        last_played = {i.track_id: (i.last_played_at or 0) for i in interactions}
        confirmed_via_card, ignored_count = await _special_card_signals(user_id, db, last_played)

    scored: list[tuple[str, float]] = []
    for inter in interactions:
        sup_ts = suppressed.get(inter.track_id)
        if sup_ts is not None and (inter.last_played_at or 0) <= sup_ts:
            continue  # suppressed and not re-engaged since
        heat = engagement_heat(inter, now)
        if heat < min_heat:
            continue
        if need_signals:
            confirmed = _is_confirmed(inter, confirmed_via_card)
            if stage == "confirmed" and not confirmed:
                continue
            if stage == "candidate" and (confirmed or not _is_candidate_triggered(inter)):
                continue
            # Ignore-gate: a still-unconfirmed candidate shown repeatedly with no play is dropped —
            # from the Special card (stage="candidate") and from cross-surface spread
            # (apply_ignore_gate) — so a drop on the card stops the track everywhere (#139).
            if (
                (stage == "candidate" or apply_ignore_gate)
                and not confirmed
                and ignored_count.get(inter.track_id, 0) >= _IGNORE_GATE_LIMIT
            ):
                continue
        scored.append((inter.track_id, heat))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]
