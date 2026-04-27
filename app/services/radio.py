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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Playlist, PlaylistTrack, TrackFeatures, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (non-tunable constants)
# ---------------------------------------------------------------------------

_BATCH_OVERFETCH = 5  # multiplier for candidate overfetch
_ARTIST_MAX_CONSECUTIVE = 2  # max tracks from same artist in a row


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
    **context,
) -> RadioSession:
    """
    Create and initialize a radio session from a seed.

    seed_type: "track" | "artist" | "playlist"
    seed_value: track_id, artist name, or playlist_id
    """
    from app.services import faiss_index

    session = RadioSession(
        session_id=str(uuid.uuid4()),
        user_id=user_id,
        seed_type=seed_type,
        seed_value=seed_value,
        device_type=context.get("device_type"),
        output_type=context.get("output_type"),
        location_label=context.get("location_label"),
        hour_of_day=context.get("hour_of_day"),
        day_of_week=context.get("day_of_week"),
    )

    if seed_type == "track":
        session.seed_track_ids = [seed_value]
        # Get display name
        row = await db.execute(
            select(TrackFeatures.title, TrackFeatures.artist).where(TrackFeatures.track_id == seed_value)
        )
        meta = row.first()
        if meta and meta.title:
            session.seed_display_name = f"{meta.artist} — {meta.title}" if meta.artist else meta.title

        # Seed embedding is the track's own embedding
        session.seed_embedding = faiss_index.get_embedding(seed_value)

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

    # Pull toward liked tracks
    for vec, weight in attract_vecs:
        drift += vec.astype(np.float64) * weight * radio_cfg.feedback_weight

    # Push away from skipped/disliked tracks
    for vec, weight in repel_vecs:
        drift -= vec.astype(np.float64) * weight * radio_cfg.feedback_weight * 0.5

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
    from app.services import collab_filter, faiss_index, lastfm_candidates, session_embeddings
    from app.services.ranker import score_candidates
    from app.services.reranker import get_last_rerank_actions, rerank

    s = get_session(session_id)
    if s is None:
        return None

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
    if collab_filter.is_ready():
        raw = collab_filter.get_cf_candidates(s.user_id, k=80)
        _add(
            [
                {"track_id": tid, "score": score * radio_cfg.source_cf, "source": "radio_cf"}
                for tid, score in raw
                if tid not in exclude
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

    if not candidates:
        return []

    # --- Boost liked-similar and penalize skip-similar ---
    if s.liked or s.skipped:
        _apply_session_feedback_boost(candidates, s)

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
    scored = [(tid, ranker_score * 0.6 + retrieval_scores.get(tid, 0.0) * 0.4) for tid, ranker_score in scored]
    scored.sort(key=lambda x: x[1], reverse=True)

    # Capture pre-rerank ordering for the audit before reranking shuffles it.
    raw_score_by_tid = {tid: float(score) for tid, score in scored}
    pre_rerank_pos_by_tid = {tid: i for i, (tid, _) in enumerate(scored)}

    # --- Rerank for diversity ---
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
    reranked = _enforce_no_consecutive_artist(reranked, db, s)

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
                col: round(float(feat_result["features"][idx][ci]), 4)
                for ci, col in enumerate(FEATURE_COLUMNS)
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


def _enforce_no_consecutive_artist(
    ranked: list[tuple[str, float]],
    db: AsyncSession,
    s: RadioSession,
) -> list[tuple[str, float]]:
    """
    Reorder to avoid more than _ARTIST_MAX_CONSECUTIVE tracks from the
    same artist in a row (includes the session's recent history).
    """
    # This is a best-effort reorder — we don't have artist info cached,
    # so we use the FAISS-indexed file paths (same approach as reranker).
    # For now, keep the ranker output as-is — the global reranker already
    # applies artist diversity. We can refine later if needed.
    return ranked
