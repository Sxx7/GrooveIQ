"""
GrooveIQ – Affinity radio session service.

A deliberately *pure* counterpart to the adaptive radio (``app/services/radio.py``):
a stateful, infinite queue seeded from a track, artist, or playlist that serves the
**sonically nearest library tracks to the fixed seed** and nothing else.

Where radio blends ~10 candidate sources, drifts its taste vector on feedback, and
re-scores everything through the LightGBM ranker + diversity reranker, affinity radio
does none of that. Each ``/next`` is a single FAISS nearest-neighbour query against the
*original* seed embedding (which never moves), with everything already served this session
— plus the user's disliked tracks, and (by default) everything they've already heard —
excluded. The result is a stream that spirals gently outward from the seed through the
user's library, always as close as possible, never repeating.

This is the "I just want more of this exact vibe, and surface stuff I haven't heard"
surface. It is intentionally isolated from the personalization loop:

  - No drift / feedback adaptation — the anchor is the seed, full stop.
  - No ranker / reranker — pure cosine order is the contract.
  - No ``reco_impression`` logging and no reco-audit writes — so surfacing a large
    neighbourhood the user may not play can't inject false-negative training signal
    into the main ranker.

Architecture mirrors radio's session store: in-memory, thread-safe, TTL + capacity
eviction (reusing the ``radio`` config knobs so there's nothing new to tune).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Playlist, PlaylistTrack, TrackFeatures, TrackInteraction
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AffinitySession:
    """A pure-similarity ("affinity") radio session.

    The seed embedding is the immutable anchor; ``served_set`` accumulates everything
    handed out so successive ``/next`` calls walk outward through the neighbourhood
    without repeating.
    """

    session_id: str
    user_id: str
    seed_type: str  # "track" | "artist" | "playlist"
    seed_value: str  # track_id / media_server_id, artist name, or playlist_id

    seed_track_ids: list[str] = field(default_factory=list)  # resolved seed tracks (excluded from results)
    seed_embedding: np.ndarray | None = None  # anchor — never changes
    seed_display_name: str | None = None

    # When True (default) already-heard tracks are excluded, so every result is
    # both close *and* new-to-the-user. When False, only disliked/served/seed
    # tracks are excluded (pure "more like this", heard-or-not).
    unheard_only: bool = True

    # Session state
    served: list[str] = field(default_factory=list)  # ordered served history
    served_set: set[str] = field(default_factory=set)  # fast exclusion lookup
    total_served: int = 0

    # Metadata
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Session store (in-memory, thread-safe) — mirrors radio.py, separate store.
# TTL / capacity reuse the `radio` config group so there's nothing new to tune.
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_sessions: dict[str, AffinitySession] = {}


def _evict_expired() -> None:
    """Remove expired sessions (caller must hold _lock)."""
    now = time.time()
    ttl = get_config().radio.session_ttl_hours * 3600
    expired = [sid for sid, s in _sessions.items() if now - s.last_active > ttl]
    for sid in expired:
        del _sessions[sid]


def _evict_oldest() -> None:
    """Remove oldest session if at capacity (caller must hold _lock)."""
    if len(_sessions) >= get_config().radio.max_sessions:
        oldest_sid = min(_sessions, key=lambda s: _sessions[s].last_active)
        del _sessions[oldest_sid]


def get_session(session_id: str) -> AffinitySession | None:
    """Retrieve an affinity session by ID (refreshes its activity clock)."""
    with _lock:
        _evict_expired()
        s = _sessions.get(session_id)
        if s:
            s.last_active = time.time()
        return s


def store_session(session: AffinitySession) -> None:
    with _lock:
        _evict_expired()
        _evict_oldest()
        _sessions[session.session_id] = session


def remove_session(session_id: str) -> bool:
    """Remove a session. Returns True if it existed."""
    with _lock:
        return _sessions.pop(session_id, None) is not None


def list_sessions(user_id: str | None = None) -> list[dict[str, Any]]:
    """List active affinity sessions, optionally filtered by user."""
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
                    "unheard_only": s.unheard_only,
                    "total_served": s.total_served,
                    "created_at": int(s.created_at),
                    "last_active": int(s.last_active),
                }
            )
        return results


# ---------------------------------------------------------------------------
# Session creation
# ---------------------------------------------------------------------------


async def create_affinity_session(
    user_id: str,
    seed_type: str,
    seed_value: str,
    db: AsyncSession,
    unheard_only: bool = True,
) -> AffinitySession:
    """Create and initialise an affinity session from a seed.

    seed_type: "track" | "artist" | "playlist"
    seed_value: internal track_id or media_server_id, artist name, or playlist_id.

    The seed embedding is resolved exactly as radio does (track embedding, or the
    FAISS centroid of an artist's / playlist's tracks) but is then frozen — it is the
    permanent anchor for every ``/next`` in this session.
    """
    from app.services import faiss_index

    session = AffinitySession(
        session_id=str(uuid.uuid4()),
        user_id=user_id,
        seed_type=seed_type,
        seed_value=seed_value,
        unheard_only=unheard_only,
    )

    if seed_type == "track":
        # seed_value may be an internal track_id or a per-backend external ID
        # (typically media_server_id from an iOS client). FAISS + seed_track_ids
        # are keyed by internal track_id, so resolve here (mirrors radio.py).
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
        session.seed_embedding = faiss_index.get_embedding(internal_tid)

    elif seed_type == "artist":
        result = await db.execute(
            select(TrackFeatures.track_id).where(TrackFeatures.artist.ilike(f"%{seed_value}%")).limit(200)
        )
        session.seed_track_ids = [r[0] for r in result.all()]
        session.seed_display_name = seed_value
        if session.seed_track_ids:
            session.seed_embedding = faiss_index.get_centroid(session.seed_track_ids)

    elif seed_type == "playlist":
        result = await db.execute(
            select(PlaylistTrack.track_id)
            .where(PlaylistTrack.playlist_id == int(seed_value))
            .order_by(PlaylistTrack.position)
        )
        session.seed_track_ids = [r[0] for r in result.all()]
        pl_row = await db.execute(select(Playlist.name).where(Playlist.id == int(seed_value)))
        pl_name = pl_row.scalar_one_or_none()
        session.seed_display_name = pl_name or f"Playlist #{seed_value}"
        if session.seed_track_ids:
            session.seed_embedding = faiss_index.get_centroid(session.seed_track_ids)

    store_session(session)
    return session


# ---------------------------------------------------------------------------
# Exclusion set
# ---------------------------------------------------------------------------


async def _get_exclusion_set(user_id: str, db: AsyncSession, unheard_only: bool) -> set[str]:
    """Tracks to keep out of results: always the user's disliked tracks, plus — when
    ``unheard_only`` — everything they've already played.

    Computed fresh on every ``/next`` (a single indexed ``TrackInteraction`` query) so a
    track the user played mid-session can't reappear. The seed's own tracks and this
    session's served set are excluded by the caller.
    """
    conditions = [TrackInteraction.dislike_count > 0]
    if unheard_only:
        conditions.append(TrackInteraction.play_count > 0)
        conditions.append(TrackInteraction.last_played_at.isnot(None))

    result = await db.execute(
        select(TrackInteraction.track_id).where(
            TrackInteraction.user_id == user_id,
            or_(*conditions),
        )
    )
    return {row[0] for row in result.all()}


# ---------------------------------------------------------------------------
# Next batch
# ---------------------------------------------------------------------------


async def get_next_tracks(session_id: str, count: int, db: AsyncSession) -> list[dict[str, Any]] | None:
    """Return the next ``count`` nearest unheard tracks to the fixed seed.

    Returns ``None`` if the session doesn't exist / has expired, ``[]`` if the seed has
    no usable embedding, and otherwise a list of track dicts (fewer than ``count`` means
    the reachable neighbourhood is exhausted). Pure cosine order — no reranking.
    """
    s = get_session(session_id)
    if s is None:
        return None
    if s.seed_embedding is None:
        return []

    from app.services import faiss_index

    exclude = set(s.served_set)
    exclude.update(s.seed_track_ids)
    exclude.update(await _get_exclusion_set(s.user_id, db, s.unheard_only))

    # search() already sorts by descending cosine and sizes its internal fetch_k to
    # survive the exclusion set, returning at most `count` surviving neighbours.
    results = faiss_index.search(s.seed_embedding, k=count, exclude_ids=exclude)
    if not results:
        return []

    final_ids = [tid for tid, _ in results]
    score_by_tid = {tid: score for tid, score in results}

    feat_result = await db.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids)))
    feat_map = {t.track_id: t for t in feat_result.scalars().all()}

    tracks: list[dict[str, Any]] = []
    for i, tid in enumerate(final_ids):
        tf = feat_map.get(tid)
        s.served.append(tid)
        s.served_set.add(tid)
        s.total_served += 1

        track_data: dict[str, Any] = {
            "position": i,
            "track_id": tid,
            "similarity": round(score_by_tid.get(tid, 0.0), 4),
        }
        if tf:
            track_data.update(
                {
                    "media_server_id": tf.media_server_id,
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

    return tracks
