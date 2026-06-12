"""
GrooveIQ – Radio session service.

Replicates YouTube Music's "Radio" function: a stateful, infinite queue
that seeds from a track, artist, or playlist and adapts in real-time
based on in-session behavior (skips, likes, dislikes).

Architecture:
  - RadioSession objects live in-memory (ephemeral, like pipeline state)
  - Each session maintains: seed embedding, played set, skip/like history,
    a drifting "taste vector" that blends seed similarity with feedback
  - Candidate retrieval weights seed similarity more heavily (~50%) than
    in global recs, and the in-session feedback loop re-weights on every
    /next call without needing a full pipeline run

Design decisions:
  - Sessions expire after 4h of inactivity (configurable)
  - Max 50 concurrent sessions (oldest evicted on overflow)
  - Each /next call generates a fresh batch, not a pre-computed queue,
    so feedback from the previous batch influences the next one immediately
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ListenEvent, Playlist, PlaylistTrack, TrackFeatures, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (non-tunable constants)
# ---------------------------------------------------------------------------

_BATCH_OVERFETCH = 5  # multiplier for candidate overfetch
_ARTIST_MAX_CONSECUTIVE = 2  # max tracks from same artist in a row

# Proven-set thresholds for the radio_proven recall source (single-user regime):
# a track is "proven" for this user if liked, played enough, or high-satisfaction.
# Crowd-free — purely the user's own behaviour. Distinct from the confidence
# model's mu/sigma "proven" (used by the novelty filter).
_PROVEN_MIN_PLAYS = 3
_PROVEN_MIN_SATISFACTION = 0.6

# Graded cross-session repeat cooldown (de-rank recently/often-served tracks).
# A demotion floored so favourites cool down and return rather than vanish; a
# no-op when there is no recent radio serve history. Strength is dial-driven
# (PresetConfig.cooldown_alpha). See docs/RECO_ALGORITHM_AUDIT.md §8.
_COOLDOWN_WINDOW_H = 48.0  # only look back this far for serve history
_COOLDOWN_HALFLIFE_H = 18.0  # recency decay half-life
_COOLDOWN_SERVES_N = 3  # serves at which the frequency term saturates
_COOLDOWN_FLOOR = 0.5  # strongest possible demotion multiplier

# ---------------------------------------------------------------------------
# Discovery dial (Chunk 7) — radio honours the same posture as /v1/recommend.
# ---------------------------------------------------------------------------

# Map radio's own retrieval sources onto the per-source weight keys the dial
# interpolates (those keys are named for the global-recommend sources). A
# familiar posture boosts the seed/CF/artist anchors and damps the exploratory
# Last.fm source; a discovery posture does the reverse. ``radio_drift`` (the
# feedback-adaptive core) has no analog and is deliberately never re-weighted by
# the dial. An empty multiplier map (balanced / default) is a no-op.
_RADIO_SOURCE_DIAL_KEY: dict[str, str] = {
    "radio_seed": "content_profile",
    "radio_content": "content",
    "radio_skipgram": "session_skipgram",
    "radio_lastfm": "lastfm_similar",
    "radio_cf": "cf",
    "radio_artist": "artist_recall",
}

# How strongly the dial scales the radio drift step. The drift step (how far each
# like/skip moves the taste vector) scales linearly around the balanced anchor so
# the default posture is unchanged, the familiar end drifts less (hugs the seed),
# and the deep-discovery end drifts more (roams faster on feedback).
_DRIFT_DIAL_GAIN = 1.0


def _dial_drift_scale(discovery: float) -> float:
    """Multiplier on the drift step for a dial position (1.0 at the balanced anchor).

    Anchored at the configured ``balanced`` dial position so the default radio
    (discovery == balanced anchor) keeps today's drift behaviour byte-for-byte.
    Clamped at 0 so an extreme dial can never invert the step direction.
    """
    balanced = float(get_config().modes.dial_anchors.get("balanced", 0.3))
    return max(0.0, 1.0 + _DRIFT_DIAL_GAIN * (discovery - balanced))


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class RadioFeedback:
    """A single in-session feedback signal."""

    track_id: str
    action: str  # "skip", "like", "dislike"
    timestamp: float = field(default_factory=time.time)


@dataclass
class RadioSession:
    """Stateful radio session."""

    session_id: str
    user_id: str
    seed_type: str  # "track", "artist", "playlist"
    seed_value: str  # track_id, artist name, or playlist_id
    seed_track_ids: list[str] = field(default_factory=list)  # resolved seed tracks

    # Embeddings
    seed_embedding: np.ndarray | None = None  # anchor point (never changes)
    drift_embedding: np.ndarray | None = None  # shifts with feedback

    # Session state
    played: list[str] = field(default_factory=list)  # ordered play history
    played_set: set[str] = field(default_factory=set)  # fast lookup
    skipped: set[str] = field(default_factory=set)
    liked: set[str] = field(default_factory=set)
    disliked: set[str] = field(default_factory=set)
    feedback_log: list[RadioFeedback] = field(default_factory=list)

    # Discovery-dial posture (0=familiar … 1=deep discovery). Default 0.3 =
    # balanced, so a session created without a dial value behaves like today.
    # Updatable on each /next call. Sets the baseline that feedback drifts around.
    discovery: float = 0.3

    # Context (updatable on each /next call)
    device_type: str | None = None
    output_type: str | None = None
    context_type: str = "radio"
    location_label: str | None = None
    hour_of_day: int | None = None
    day_of_week: int | None = None

    # Metadata
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    total_served: int = 0

    # Display info
    seed_display_name: str | None = None  # "Artist Name" or "Track Title" for UI


# ---------------------------------------------------------------------------
# Session store (in-memory, thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_sessions: dict[str, RadioSession] = {}


def _evict_expired() -> None:
    """Remove expired sessions (caller must hold _lock)."""
    now = time.time()
    expired = [sid for sid, s in _sessions.items() if now - s.last_active > get_config().radio.session_ttl_hours * 3600]
    for sid in expired:
        del _sessions[sid]


def _evict_oldest() -> None:
    """Remove oldest session if at capacity (caller must hold _lock)."""
    if len(_sessions) >= get_config().radio.max_sessions:
        oldest_sid = min(_sessions, key=lambda s: _sessions[s].last_active)
        del _sessions[oldest_sid]


def get_session(session_id: str) -> RadioSession | None:
    """Retrieve a radio session by ID."""
    with _lock:
        _evict_expired()
        s = _sessions.get(session_id)
        if s:
            s.last_active = time.time()
        return s


def store_session(session: RadioSession) -> None:
    """Store a radio session."""
    with _lock:
        _evict_expired()
        _evict_oldest()
        _sessions[session.session_id] = session


def remove_session(session_id: str) -> bool:
    """Remove a radio session. Returns True if it existed."""
    with _lock:
        return _sessions.pop(session_id, None) is not None


def list_sessions(user_id: str | None = None) -> list[dict[str, Any]]:
    """List active radio sessions, optionally filtered by user."""
    with _lock:
        _evict_expired()
        results = []
        for s in _sessions.values():
            if user_id and s.user_id != user_id:
                continue
            results.append(
                {
                    "session_id": s.session_id,
                    "user_id": s.user_id,
                    "seed_type": s.seed_type,
                    "seed_value": s.seed_value,
                    "seed_display_name": s.seed_display_name,
                    "total_served": s.total_served,
                    "tracks_played": len(s.played),
                    "tracks_skipped": len(s.skipped),
                    "tracks_liked": len(s.liked),
                    "created_at": int(s.created_at),
                    "last_active": int(s.last_active),
                }
            )
        return results


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


async def create_radio_session(
    user_id: str,
    seed_type: str,
    seed_value: str,
    db: AsyncSession,
    discovery: float = 0.3,
    **context,
) -> RadioSession:
    """
    Create and initialize a radio session from a seed.

    seed_type: "track" | "artist" | "playlist"
    seed_value: track_id, artist name, or playlist_id
    discovery: discovery-dial posture (0=familiar … 1=deep discovery, 0.3=balanced).
    """
    from app.services import faiss_index

    session = RadioSession(
        session_id=str(uuid.uuid4()),
        user_id=user_id,
        seed_type=seed_type,
        seed_value=seed_value,
        discovery=discovery,
        device_type=context.get("device_type"),
        output_type=context.get("output_type"),
        location_label=context.get("location_label"),
        hour_of_day=context.get("hour_of_day"),
        day_of_week=context.get("day_of_week"),
    )

    if seed_type == "track":
        # seed_value may be an internal track_id or a per-backend external ID
        # (typically media_server_id from an iOS client). FAISS and
        # seed_track_ids are keyed by internal track_id, so resolve here.
        row = await db.execute(
            select(TrackFeatures.track_id, TrackFeatures.title, TrackFeatures.artist).where(
                or_(
                    TrackFeatures.track_id == seed_value,
                    TrackFeatures.media_server_id == seed_value,
                )
            )
        )
        meta = row.first()
        internal_tid = meta.track_id if meta else seed_value
        session.seed_track_ids = [internal_tid]
        if meta and meta.title:
            session.seed_display_name = f"{meta.artist} — {meta.title}" if meta.artist else meta.title

        # Seed embedding is the track's own embedding
        session.seed_embedding = faiss_index.get_embedding(internal_tid)

    elif seed_type == "artist":
        # Find all tracks by this artist
        result = await db.execute(
            select(TrackFeatures.track_id).where(TrackFeatures.artist.ilike(f"%{seed_value}%")).limit(200)
        )
        session.seed_track_ids = [r[0] for r in result.all()]
        session.seed_display_name = seed_value

        # Seed embedding is the centroid of the artist's tracks
        if session.seed_track_ids:
            session.seed_embedding = faiss_index.get_centroid(session.seed_track_ids)

    elif seed_type == "playlist":
        # Load playlist tracks
        result = await db.execute(
            select(PlaylistTrack.track_id)
            .where(PlaylistTrack.playlist_id == int(seed_value))
            .order_by(PlaylistTrack.position)
        )
        session.seed_track_ids = [r[0] for r in result.all()]

        # Get playlist name for display
        pl_row = await db.execute(select(Playlist.name).where(Playlist.id == int(seed_value)))
        pl_name = pl_row.scalar_one_or_none()
        session.seed_display_name = pl_name or f"Playlist #{seed_value}"

        # Seed embedding is the centroid of playlist tracks
        if session.seed_track_ids:
            session.seed_embedding = faiss_index.get_centroid(session.seed_track_ids)

    # Initialize drift embedding to match seed
    if session.seed_embedding is not None:
        session.drift_embedding = session.seed_embedding.copy()

    store_session(session)
    return session


# ---------------------------------------------------------------------------
# Feedback processing
# ---------------------------------------------------------------------------


def record_feedback(session_id: str, track_id: str, action: str) -> bool:
    """
    Record in-session feedback and update the drift embedding.

    action: "skip" | "like" | "dislike"
    Returns True if the session exists and feedback was recorded.
    """

    s = get_session(session_id)
    if s is None:
        return False

    fb = RadioFeedback(track_id=track_id, action=action)
    s.feedback_log.append(fb)

    if action == "skip":
        s.skipped.add(track_id)
    elif action == "like":
        s.liked.add(track_id)
        s.disliked.discard(track_id)  # un-dislike if previously disliked
    elif action == "dislike":
        s.disliked.add(track_id)
        s.liked.discard(track_id)

    # Update drift embedding based on feedback
    _update_drift_embedding(s)
    return True


def _update_drift_embedding(s: RadioSession) -> None:
    """
    Recompute the drift embedding from feedback signals.

    Liked tracks pull the drift vector toward them.
    Skipped/disliked tracks push it away.
    More recent feedback has stronger influence.
    """
    from app.services import faiss_index

    if s.seed_embedding is None:
        return

    # Collect weighted feedback vectors
    attract_vecs: list[tuple[np.ndarray, float]] = []
    repel_vecs: list[tuple[np.ndarray, float]] = []

    n = len(s.feedback_log)
    for i, fb in enumerate(s.feedback_log):
        emb = faiss_index.get_embedding(fb.track_id)
        if emb is None:
            continue
        # More recent feedback gets higher weight
        radio_cfg = get_config().radio
        recency = radio_cfg.feedback_decay ** (n - 1 - i)

        if fb.action == "like":
            attract_vecs.append((emb, recency * radio_cfg.feedback_like_weight))
        elif fb.action == "dislike":
            repel_vecs.append((emb, recency * radio_cfg.feedback_dislike_weight))
        elif fb.action == "skip":
            repel_vecs.append((emb, recency * radio_cfg.feedback_skip_weight))

    if not attract_vecs and not repel_vecs:
        return

    # Start from seed
    radio_cfg = get_config().radio
    drift = s.seed_embedding.copy().astype(np.float64)

    # Discovery dial sets the baseline drift magnitude: familiar hugs the seed
    # (small steps), deep-discovery roams (large steps). Feedback still moves the
    # vector at every posture — the dial only scales how far each signal pushes.
    drift_scale = _dial_drift_scale(s.discovery)

    # Pull toward liked tracks
    for vec, weight in attract_vecs:
        drift += vec.astype(np.float64) * weight * radio_cfg.feedback_weight * drift_scale

    # Push away from skipped/disliked tracks
    for vec, weight in repel_vecs:
        drift -= vec.astype(np.float64) * weight * radio_cfg.feedback_weight * 0.5 * drift_scale

    # Normalize
    norm = np.linalg.norm(drift)
    if norm > 1e-9:
        drift = (drift / norm).astype(np.float32)
        s.drift_embedding = drift


# ---------------------------------------------------------------------------
# Candidate generation (radio-specific)
# ---------------------------------------------------------------------------


async def get_next_tracks(
    session_id: str,
    count: int,
    db: AsyncSession,
    *,
    collect_audit: bool = False,
) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """
    Generate the next batch of tracks for a radio session.

    Returns None if session not found.
    Returns list of track dicts with metadata.

    When ``collect_audit=True``, returns ``(tracks, audit_data)`` instead.
    The audit_data dict carries everything the audit service needs:
      - ``candidate_rows``: per-track audit data (sources, raw/final scores,
        positions, reranker actions, feature vectors)
      - ``candidates_by_source``: count by source
    """
    from app.services import candidate_gen, collab_filter, faiss_index, lastfm_candidates, session_embeddings
    from app.services.modes import resolve_dial
    from app.services.ranker import score_candidates
    from app.services.request_config import apply_overrides
    from app.services.reranker import get_last_rerank_actions, rerank

    s = get_session(session_id)
    if s is None:
        return None

    # Resolve the session's dial posture into a request-scoped config override
    # (read pre-override, exactly like the recommend handler). The override is
    # applied around the modes-reading call sites below — the source-weight /
    # novelty filter on the candidate pool and the reranker's acquisition term.
    dial = resolve_dial(s.discovery, None, get_config().modes)

    # Build exclusion set: already played + disliked in this session
    exclude = set(s.played_set) | s.disliked

    # Also exclude globally disliked tracks
    disliked_result = await db.execute(
        select(TrackInteraction.track_id).where(
            TrackInteraction.user_id == s.user_id,
            (TrackInteraction.dislike_count > 0) | (TrackInteraction.early_skip_count > 2),
        )
    )
    exclude |= {row[0] for row in disliked_result.all()}

    overfetch = count * _BATCH_OVERFETCH
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set(exclude)

    # When collecting audit data, track every source a track surfaced from
    # (a candidate may match multiple sources but only enters `candidates`
    # once via the dedup below).
    sources_by_tid: dict[str, list[str]] = {} if collect_audit else None

    def _add(items: list[dict[str, Any]]) -> None:
        for c in items:
            tid = c["track_id"]
            if sources_by_tid is not None:
                sources_by_tid.setdefault(tid, []).append(c["source"])
            if tid not in seen:
                seen.add(tid)
                candidates.append(c)

    radio_cfg = get_config().radio

    # --- Source 1: FAISS similarity to drift embedding (primary, weighted high) ---
    if faiss_index.is_ready() and s.drift_embedding is not None:
        results = faiss_index.search(s.drift_embedding, k=overfetch, exclude_ids=exclude)
        _add(
            [
                {"track_id": tid, "score": score * radio_cfg.source_drift, "source": "radio_drift"}
                for tid, score in results
            ]
        )

    # --- Source 2: FAISS similarity to seed embedding (anchor) ---
    if faiss_index.is_ready() and s.seed_embedding is not None and s.drift_embedding is not s.seed_embedding:
        results = faiss_index.search(s.seed_embedding, k=overfetch // 2, exclude_ids=exclude)
        _add(
            [
                {"track_id": tid, "score": score * radio_cfg.source_seed, "source": "radio_seed"}
                for tid, score in results
            ]
        )

    # --- Source 3: Seed track direct similarity (for track seeds) ---
    if s.seed_type == "track" and faiss_index.is_ready():
        for seed_tid in s.seed_track_ids[:3]:
            results = faiss_index.search_by_track_id(seed_tid, k=50, exclude_ids=exclude)
            _add(
                [
                    {"track_id": tid, "score": score * radio_cfg.source_content, "source": "radio_content"}
                    for tid, score in results
                ]
            )

    # --- Source 4: Session skip-gram (behavioral co-occurrence) ---
    if session_embeddings.is_ready() and s.seed_track_ids:
        for seed_tid in s.seed_track_ids[:5]:
            raw = session_embeddings.get_similar_tracks(seed_tid, k=50, exclude_ids=exclude)
            _add(
                [
                    {"track_id": tid, "score": score * radio_cfg.source_skipgram, "source": "radio_skipgram"}
                    for tid, score in raw
                ]
            )

    # --- Source 5: Last.fm similar tracks ---
    if lastfm_candidates.is_ready() and s.seed_track_ids:
        for seed_tid in s.seed_track_ids[:5]:
            raw = lastfm_candidates.get_similar_for_track(seed_tid, k=50, exclude_ids=exclude)
            _add(
                [
                    {"track_id": tid, "score": score * radio_cfg.source_lastfm, "source": "radio_lastfm"}
                    for tid, score in raw
                ]
            )

    # --- Source 6: CF (collaborative filtering) ---
    # Gated on a real crowd: with too few users the ALS factors collapse to
    # per-user popularity (seed-unaware), so CF becomes noise/leak rather than
    # signal. On a single-user instance this is a no-op; it auto-enables once the
    # instance has enough users. See docs/RECO_ALGORITHM_AUDIT.md §8.5.
    if collab_filter.is_ready() and collab_filter.has_crowd():
        raw = collab_filter.get_cf_candidates(s.user_id, k=80)
        _add(
            [
                {"track_id": tid, "score": score * radio_cfg.source_cf, "source": "radio_cf"}
                for tid, score in raw
                if tid not in exclude
            ]
        )

    # --- Source 8: Proven recall (proven set ∩ seed neighbourhood) ---
    # The crowd-free replacement for cross-user CF on small instances: the user's
    # own high-completion / liked / repeatedly-played tracks that are ALSO
    # acoustically near the seed. Weighted by the dial's proven_recall_mult (high
    # at the familiar end, 0 at deep), so 'Familiar' surfaces what the user knows
    # *that still fits the seed* instead of a seed-unaware global favourite.
    proven_mult = float(dial.overrides.get("modes", {}).get("active", {}).get("proven_recall_mult", 0.0))
    if proven_mult > 0.0 and faiss_index.is_ready():
        anchor_emb = s.drift_embedding if s.drift_embedding is not None else s.seed_embedding
        if anchor_emb is not None:
            proven_ids = await _get_proven_set(s.user_id, db)
            if proven_ids:
                results = faiss_index.search(anchor_emb, k=overfetch * 2, exclude_ids=exclude)
                _add(
                    [
                        {
                            "track_id": tid,
                            "score": score * radio_cfg.source_seed * proven_mult,
                            "source": "radio_proven",
                        }
                        for tid, score in results
                        if tid in proven_ids
                    ]
                )

    # --- Source 7: Artist recall (same artist tracks for artist seeds) ---
    if s.seed_type == "artist" and s.seed_track_ids:
        # Include more tracks from the seeded artist
        _add(
            [
                {"track_id": tid, "score": radio_cfg.source_artist, "source": "radio_artist"}
                for tid in s.seed_track_ids
                if tid not in seen
            ]
        )

    # --- Discovery dial: per-source weight tilt + proven-novelty filter ---
    # Both are gated so the default (balanced) posture is a no-op. The dial's
    # interpolated per-source multipliers (keyed by recommend-source name) are
    # mapped onto radio's analogous sources, and at the discovery end the user's
    # proven set is excluded from the pool (radio already excludes `played`).
    with apply_overrides(dial.overrides):
        active = get_config().modes.active
        if active.source_weight_mult:
            for c in candidates:
                key = _RADIO_SOURCE_DIAL_KEY.get(c["source"])
                if key:
                    c["score"] *= active.source_weight_mult.get(key, 1.0)
        if active.novelty_filter:
            candidates = await candidate_gen.apply_novelty_filter(candidates, s.user_id, db, active)

    if not candidates:
        return []

    # --- Boost liked-similar and penalize skip-similar ---
    if s.liked or s.skipped:
        _apply_session_feedback_boost(candidates, s)

    # --- Graded cross-session repeat cooldown (dial-driven) ---
    # Demotes tracks recently/often served to this user in radio so a fresh
    # session off the same seed doesn't re-serve the same cluster. Floored, so a
    # beloved track cools down and returns rather than being banned. No-op when
    # cooldown_alpha == 0 or there is no recent radio serve history.
    cooldown_alpha = float(dial.overrides.get("modes", {}).get("active", {}).get("cooldown_alpha", 0.0))
    if cooldown_alpha > 0.0:
        await _apply_repeat_cooldown(candidates, s.user_id, db, cooldown_alpha)

    # Sort and trim candidates
    candidates.sort(key=lambda c: c["score"], reverse=True)
    candidates = candidates[:overfetch]

    candidate_ids = [c["track_id"] for c in candidates]
    retrieval_scores = {c["track_id"]: c["score"] for c in candidates}

    # --- Score with LightGBM ranker ---
    scored = await score_candidates(
        s.user_id,
        candidate_ids,
        db,
        hour_of_day=s.hour_of_day,
        day_of_week=s.day_of_week,
        device_type=s.device_type,
        output_type=s.output_type,
        context_type="radio",
        location_label=s.location_label,
    )
    source_map = {c["track_id"]: c["source"] for c in candidates}

    # Blend ranker score with retrieval score.  When the ranker has no model
    # (fallback mode), satisfaction_score is 0.0 for unplayed tracks, which
    # kills all differentiation.  Blending preserves the FAISS similarity
    # ordering while still respecting any ranker signal that exists.
    #
    # The blend weight is dial-driven (PresetConfig.ranker_blend on the resolved
    # preset, read off the whitelisted override): familiar pushes it up so the
    # retention/completion ranker dominates and the user's proven tracks (which
    # carry real satisfaction labels) float up, while unproven tracks score ~0;
    # deep pushes it down so retrieval/novelty leads. Default 0.6 == today.
    blend = float(dial.overrides.get("modes", {}).get("active", {}).get("ranker_blend", 0.6))
    scored = [(tid, ranker_score * blend + retrieval_scores.get(tid, 0.0) * (1.0 - blend)) for tid, ranker_score in scored]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Capture pre-rerank ordering for the audit before reranking shuffles it.
    raw_score_by_tid = {tid: float(score) for tid, score in scored}
    pre_rerank_pos_by_tid = {tid: i for i, (tid, _) in enumerate(scored)}

    # --- Rerank for diversity (under the dial override so the acquisition term
    # — additive +kappa*sigma - lambda_proven*[is_proven] — reflects the posture).
    with apply_overrides(dial.overrides):
        reranked = await rerank(
            scored,
            s.user_id,
            db,
            device_type=s.device_type,
            output_type=s.output_type,
            collect_actions=collect_audit,
        )
    rerank_actions = get_last_rerank_actions() if collect_audit else []

    # Additional radio-specific filtering: enforce no consecutive same-artist
    reranked = await _enforce_no_consecutive_artist(reranked, db, s)

    final_score_by_tid = {tid: float(score) for tid, score in reranked}
    final_pos_by_tid = {tid: i for i, (tid, _) in enumerate(reranked)}

    # Take the requested count
    final = reranked[:count]
    shown_tids: set[str] = {tid for tid, _ in final}

    # Fetch track metadata
    final_ids = [tid for tid, _ in final]
    feat_result = await db.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids)))
    feat_map = {t.track_id: t for t in feat_result.scalars().all()}

    # Build response and update session state
    tracks = []
    for i, (tid, score) in enumerate(final):
        s.played.append(tid)
        s.played_set.add(tid)
        s.total_served += 1

        tf = feat_map.get(tid)
        # `tid` is the internal GrooveIQ track_id (a stable hash of the file
        # path; never rewritten by sync). The client receives both the
        # internal id and the media_server_id so it can hand the latter to
        # Navidrome for playback.
        track_data = {
            "position": i,
            "track_id": tid,
            "source": source_map.get(tid, "unknown"),
            "score": round(score, 4),
        }
        if tf:
            track_data.update(
                {
                    "title": tf.title,
                    "artist": tf.artist,
                    "album": tf.album,
                    "genre": tf.genre,
                    "bpm": tf.bpm,
                    "key": tf.key,
                    "mode": tf.mode,
                    "energy": tf.energy,
                    "danceability": tf.danceability,
                    "valence": tf.valence,
                    "mood_tags": tf.mood_tags,
                    "duration": tf.duration,
                    "media_server_id": tf.media_server_id,
                }
            )
        tracks.append(track_data)

    if not collect_audit:
        return tracks

    # --- Build audit payload ---
    audit_track_ids = list(raw_score_by_tid.keys())  # all candidates that made it past scoring
    candidates_by_source: dict[str, int] = {}
    for src_list in sources_by_tid.values():
        for src in src_list:
            candidates_by_source[src] = candidates_by_source.get(src, 0) + 1

    actions_by_tid: dict[str, list[dict[str, Any]]] = {}
    for action in rerank_actions:
        tid = action.get("track_id")
        if tid:
            actions_by_tid.setdefault(tid, []).append(action)

    # Build feature vectors for the persisted candidates (cap by config).
    from app.core.config import settings as _settings
    from app.services.feature_eng import FEATURE_COLUMNS, build_features

    cap = _settings.RECO_AUDIT_MAX_CANDIDATES
    ranked_by_raw = sorted(raw_score_by_tid.items(), key=lambda x: x[1], reverse=True)
    top_n = [tid for tid, _ in ranked_by_raw[:cap]]
    audit_persist_ids = list({*top_n, *shown_tids})

    feature_vectors: dict[str, dict[str, float]] = {}
    if audit_persist_ids:
        feat_result = await build_features(
            s.user_id,
            audit_persist_ids,
            db,
            hour_of_day=s.hour_of_day,
            day_of_week=s.day_of_week,
            device_type=s.device_type,
            output_type=s.output_type,
            context_type="radio",
            location_label=s.location_label,
        )
        for idx, tid in enumerate(feat_result["track_ids"]):
            feature_vectors[tid] = {
                col: round(float(feat_result["features"][idx][ci]), 4) for ci, col in enumerate(FEATURE_COLUMNS)
            }

    candidate_rows: list[dict[str, Any]] = []
    for tid in audit_persist_ids:
        candidate_rows.append(
            {
                "track_id": tid,
                "sources": sources_by_tid.get(tid, []),
                "raw_score": raw_score_by_tid.get(tid, 0.0),
                "pre_rerank_position": pre_rerank_pos_by_tid.get(tid, -1),
                "final_score": final_score_by_tid.get(tid),
                "final_position": final_pos_by_tid.get(tid) if tid in final_pos_by_tid else None,
                "shown": tid in shown_tids,
                "reranker_actions": actions_by_tid.get(tid, []),
                "feature_vector": feature_vectors.get(tid, {}),
            }
        )

    audit_data = {
        "candidate_rows": candidate_rows,
        "candidates_by_source": candidates_by_source,
        "candidates_total": len(audit_track_ids),
    }
    return tracks, audit_data


async def _get_proven_set(user_id: str, db: AsyncSession) -> set[str]:
    """The user's proven track_ids: liked, played >= _PROVEN_MIN_PLAYS, or high satisfaction.

    Crowd-free "known/loved" set for the radio_proven recall source — distinct from
    the confidence model's mu/sigma "proven" that the novelty filter uses.
    """
    result = await db.execute(
        select(TrackInteraction.track_id).where(
            TrackInteraction.user_id == user_id,
            or_(
                TrackInteraction.like_count > 0,
                TrackInteraction.play_count >= _PROVEN_MIN_PLAYS,
                TrackInteraction.satisfaction_score >= _PROVEN_MIN_SATISFACTION,
            ),
        )
    )
    return {row[0] for row in result.all()}


async def _apply_repeat_cooldown(
    candidates: list[dict[str, Any]],
    user_id: str,
    db: AsyncSession,
    alpha: float,
) -> None:
    """Demote candidates by how recently/often they were served to the user in radio.

    ``penalty = max(_COOLDOWN_FLOOR, 1 - alpha * recency_decay * frequency)`` where
    recency halves every ``_COOLDOWN_HALFLIFE_H`` and frequency saturates at
    ``_COOLDOWN_SERVES_N`` serves within ``_COOLDOWN_WINDOW_H``. Reads radio serve
    history from listen_events; a no-op for candidates with no recent serves.
    """
    if not candidates:
        return
    now = int(time.time())
    cutoff = now - int(_COOLDOWN_WINDOW_H * 3600)
    cand_ids = [c["track_id"] for c in candidates]

    result = await db.execute(
        select(
            ListenEvent.track_id,
            func.count().label("serves"),
            func.max(ListenEvent.timestamp).label("last_ts"),
        )
        .where(
            ListenEvent.user_id == user_id,
            ListenEvent.track_id.in_(cand_ids),
            ListenEvent.timestamp >= cutoff,
            or_(ListenEvent.context_type == "radio", ListenEvent.surface == "radio"),
        )
        .group_by(ListenEvent.track_id)
    )
    history = {row.track_id: (int(row.serves), int(row.last_ts)) for row in result.all()}
    if not history:
        return

    half_life_s = _COOLDOWN_HALFLIFE_H * 3600.0
    for c in candidates:
        hist = history.get(c["track_id"])
        if not hist:
            continue
        serves, last_ts = hist
        recency = 0.5 ** (max(0, now - last_ts) / half_life_s)
        frequency = min(1.0, serves / _COOLDOWN_SERVES_N)
        penalty = max(_COOLDOWN_FLOOR, 1.0 - alpha * recency * frequency)
        c["score"] *= penalty


def _apply_session_feedback_boost(
    candidates: list[dict[str, Any]],
    s: RadioSession,
) -> None:
    """
    Adjust candidate scores based on similarity to liked/skipped tracks
    within this radio session.
    """
    from app.services import faiss_index

    if not faiss_index.is_ready():
        return

    # Compute centroid of liked tracks
    liked_centroid = faiss_index.get_centroid(list(s.liked)) if s.liked else None
    # Compute centroid of skipped/disliked tracks
    skip_ids = list(s.skipped | s.disliked)
    skip_centroid = faiss_index.get_centroid(skip_ids) if skip_ids else None

    for c in candidates:
        emb = faiss_index.get_embedding(c["track_id"])
        if emb is None:
            continue

        # Boost tracks similar to liked tracks
        if liked_centroid is not None:
            sim = float(np.dot(emb, liked_centroid))
            if sim > 0.5:
                c["score"] *= 1.0 + (sim - 0.5) * 0.4  # up to +20% boost

        # Penalize tracks similar to skipped tracks
        if skip_centroid is not None:
            sim = float(np.dot(emb, skip_centroid))
            if sim > 0.7:
                c["score"] *= max(0.3, 1.0 - (sim - 0.7) * 1.5)  # up to -45% penalty


async def _enforce_no_consecutive_artist(
    ranked: list[tuple[str, float]],
    db: AsyncSession,
    s: RadioSession,
) -> list[tuple[str, float]]:
    """
    Reorder to avoid more than _ARTIST_MAX_CONSECUTIVE tracks from the same artist
    in a row, preserving score order as much as possible (a "spread" reorder).

    Best-effort: tracks whose artist is unknown are treated as distinct so they're
    never deferred. Score order is otherwise honoured — a track is only pushed back
    when placing it would exceed the consecutive-artist cap.
    """
    if len(ranked) <= _ARTIST_MAX_CONSECUTIVE:
        return ranked

    ids = [tid for tid, _ in ranked]
    rows = await db.execute(select(TrackFeatures.track_id, TrackFeatures.artist).where(TrackFeatures.track_id.in_(ids)))
    artist_of = {r.track_id: (r.artist or "") for r in rows.all()}

    remaining = list(ranked)
    result: list[tuple[str, float]] = []
    recent_artists: list[str] = []  # artists of the tracks already placed
    while remaining:
        pick_idx = 0  # fallback: everyone left is the blocked artist -> take the top
        for i, (tid, _) in enumerate(remaining):
            artist = artist_of.get(tid, "")
            # Block only if the same artist already fills the last N placed slots.
            if artist and recent_artists[-_ARTIST_MAX_CONSECUTIVE:].count(artist) >= _ARTIST_MAX_CONSECUTIVE:
                continue
            pick_idx = i
            break
        tid, score = remaining.pop(pick_idx)
        result.append((tid, score))
        recent_artists.append(artist_of.get(tid, ""))
    return result
