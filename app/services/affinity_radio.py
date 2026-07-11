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
# Genre / mood gate helpers
#
# Pure audio-embedding nearness spans genres (an energetic instrumental
# electronic seed sits nearest energetic instrumental rap/pop beats that share
# its production). The gate re-prioritises the neighbours that also share the
# seed's *genre family* and *mood*, so results stay close but feel like the same
# kind of music. Everything below is pure/deterministic so it is unit-testable
# without a DB or FAISS index.
# ---------------------------------------------------------------------------

# Broad genre families keyed by substring. Order matters: the first family with
# any matching substring wins, so less-ambiguous families (hip-hop, rock, r&b …)
# are checked before the broad "electronic" bucket, which itself precedes "pop".
# Labels are lower-cased before matching; the map is multilingual on purpose
# (the dev library carries French labels: "Électronique", "Alternatif et Indé").
_GENRE_FAMILY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("hiphop", ("hip hop", "hip-hop", "hiphop", "rap", "trap", "drill", "grime", "boom bap")),
    ("rnb", ("r&b", "rnb", "r and b", "rhythm and blues", "soul", "funk", "motown", "neo-soul")),
    ("reggae", ("reggae", "dancehall", "ska")),
    ("classical", ("classiq", "classical", "orchestr", "baroque", "opera", "choral", "symphon", "concerto")),
    ("jazz", ("jazz", "blues", "swing", "bebop", "bossa")),
    ("latin", ("latin", "reggaeton", "salsa", "bachata", "cumbia", "flamenco")),
    ("folk_country", ("folk", "country", "americana", "bluegrass", "singer-songwriter", "singer songwriter")),
    (
        "rock",
        (
            "rock",
            "metal",
            "punk",
            "grunge",
            "hardcore",
            "shoegaze",
            "emo",
            "indie",
            "indé",
            "alternatif",
            "alternative",
            "post-rock",
            "post rock",
        ),
    ),
    (
        "electronic",
        (
            "electro",
            "électro",
            "electronic",
            "dance",
            "house",
            "techno",
            "trance",
            "edm",
            "dubstep",
            "drum and bass",
            "drum & bass",
            "dnb",
            "d&b",
            "synth",
            "ambient",
            "downtempo",
            "future",
            "garage",
            "concrèt",
            "concret",
            "idm",
            "chillwave",
            "vaporwave",
            "breakbeat",
            "hardstyle",
            "lo-fi",
            "lofi",
            "trip hop",
            "trip-hop",
            "chillout",
            "chill-out",
        ),
    ),
    ("pop", ("pop", "k-pop", "kpop", "j-pop")),
    ("film", ("film", "soundtrack", "score", "cinematic", "ost")),
)

# Fixed order for the mood vector built from EffNet mood tags.
_MOOD_LABELS: tuple[str, ...] = ("happy", "sad", "aggressive", "relaxed", "party")


def _genre_family(genre: str | None) -> str | None:
    """Map a raw (possibly multilingual) genre label to a broad family, or None."""
    if not genre:
        return None
    g = genre.lower()
    for family, keywords in _GENRE_FAMILY_KEYWORDS:
        if any(kw in g for kw in keywords):
            return family
    return None


def _mood_vec(mood_tags: Any) -> np.ndarray | None:
    """Build a fixed-order mood vector from a track's ``mood_tags`` JSON.

    ``mood_tags`` is a list of ``{"label", "confidence"}`` dicts. Returns a
    5-dim float array in ``_MOOD_LABELS`` order, or None when unusable.
    """
    if not mood_tags or not isinstance(mood_tags, list):
        return None
    conf: dict[str, float] = {}
    for m in mood_tags:
        if isinstance(m, dict) and "label" in m:
            try:
                conf[str(m["label"]).lower()] = float(m.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
    if not conf:
        return None
    vec = np.array([conf.get(lbl, 0.0) for lbl in _MOOD_LABELS], dtype=np.float32)
    if float(np.linalg.norm(vec)) < 1e-9:
        return None
    return vec


def _mood_cos(a: np.ndarray | None, b: np.ndarray | None) -> float:
    """Cosine similarity of two mood vectors; neutral 0.5 when either is missing."""
    if a is None or b is None:
        return 0.5
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.5
    return float(np.dot(a, b) / (na * nb))


def _gate_and_rank(
    pool: list[tuple[str, float, str | None, np.ndarray | None]],
    seed_families: set[str],
    seed_mood: np.ndarray | None,
    cfg: Any,
    count: int,
) -> list[tuple[str, float]]:
    """Blend embedding cosine with genre-family + mood match, then take top ``count``.

    ``pool`` is ``(track_id, emb_sim, genre_family, mood_vec)`` in descending
    embedding-cosine order. Returns ``(track_id, emb_sim)`` pairs ordered by the
    blended score — ``emb_sim`` is preserved so callers still report the true
    cosine-to-seed as ``similarity``. Pure function: no I/O.
    """
    scored: list[tuple[str, float, float, float]] = []  # tid, emb_sim, genre_match, score
    for tid, emb_sim, fam, mood in pool:
        genre_match = 1.0 if (fam is not None and fam in seed_families) else 0.0
        mood_sim = _mood_cos(mood, seed_mood)
        score = cfg.w_embedding * emb_sim + cfg.w_genre * genre_match + cfg.w_mood * mood_sim
        scored.append((tid, emb_sim, genre_match, score))

    use_hard = cfg.hard_gate and bool(seed_families)
    if use_hard:
        same = [s for s in scored if s[2] >= 1.0]
        # Only enforce the hard filter when enough same-family candidates survive,
        # otherwise a niche seed's batch would run dry — fall back to soft.
        if len(same) >= cfg.hard_min_results:
            ranked = same
        else:
            use_hard = False
            ranked = scored
    else:
        ranked = scored

    if not use_hard and cfg.genre_soft_penalty < 1.0 and seed_families:
        # Soft gate: down-weight off-family candidates (never fully unless penalty==0).
        ranked = [
            (tid, emb, gm, score if gm >= 1.0 else score * cfg.genre_soft_penalty) for tid, emb, gm, score in ranked
        ]

    ranked.sort(key=lambda x: x[3], reverse=True)
    return [(tid, emb) for tid, emb, _gm, _sc in ranked[:count]]


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

    # Genre/mood gate anchor (computed once from the seed tracks, then frozen).
    # seed_genre_families: broad families the seed belongs to; seed_mood: mean
    # EffNet mood vector. Both feed the gate in get_next_tracks.
    seed_genre_families: set[str] = field(default_factory=set)
    seed_mood: np.ndarray | None = None

    # Per-session gate override: None -> use the algorithm-config default,
    # True/False -> force the gate on/off for this session (for A/B by ear).
    gate_override: bool | None = None

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


async def _compute_seed_profile(db: AsyncSession, track_ids: list[str]) -> tuple[set[str], np.ndarray | None]:
    """Derive the gate anchor from the seed tracks: the set of genre families they
    span and their mean EffNet mood vector. One indexed query; pure otherwise.
    """
    if not track_ids:
        return set(), None
    result = await db.execute(
        select(TrackFeatures.genre, TrackFeatures.mood_tags).where(TrackFeatures.track_id.in_(track_ids))
    )
    families: set[str] = set()
    mood_vecs: list[np.ndarray] = []
    for genre, mood_tags in result.all():
        fam = _genre_family(genre)
        if fam:
            families.add(fam)
        mv = _mood_vec(mood_tags)
        if mv is not None:
            mood_vecs.append(mv)
    mood = np.mean(mood_vecs, axis=0).astype(np.float32) if mood_vecs else None
    return families, mood


def gate_active(session: AffinitySession) -> bool:
    """Whether the genre/mood gate will run for this session's ``/next`` calls.

    The per-session override wins over the config default, and the gate is only
    active when the seed actually has a genre family or mood anchor to gate on.
    """
    cfg = get_config().affinity
    enabled = session.gate_override if session.gate_override is not None else cfg.gate_enabled
    return bool(enabled) and (bool(session.seed_genre_families) or session.seed_mood is not None)


async def create_affinity_session(
    user_id: str,
    seed_type: str,
    seed_value: str,
    db: AsyncSession,
    unheard_only: bool = True,
    gate: bool | None = None,
) -> AffinitySession:
    """Create and initialise an affinity session from a seed.

    seed_type: "track" | "artist" | "playlist"
    seed_value: internal track_id or media_server_id, artist name, or playlist_id.
    gate: per-session genre/mood gate override (None -> config default).

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
        gate_override=gate,
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

    # Freeze the genre/mood gate anchor alongside the embedding anchor.
    session.seed_genre_families, session.seed_mood = await _compute_seed_profile(db, session.seed_track_ids)

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

    cfg = get_config().affinity

    exclude = set(s.served_set)
    exclude.update(s.seed_track_ids)
    exclude.update(await _get_exclusion_set(s.user_id, db, s.unheard_only))

    if gate_active(s):
        # Oversample the neighbourhood, then re-prioritise by genre family + mood.
        fetch_k = max(count, count * cfg.oversample)
        pool = faiss_index.search(s.seed_embedding, k=fetch_k, exclude_ids=exclude)
        if not pool:
            return []
        pool_ids = [tid for tid, _ in pool]
        emb_by_tid = {tid: score for tid, score in pool}
        feat_result = await db.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(pool_ids)))
        feat_map = {t.track_id: t for t in feat_result.scalars().all()}
        gate_pool = [
            (tid, emb_by_tid[tid], _genre_family(feat_map[tid].genre), _mood_vec(feat_map[tid].mood_tags))
            for tid in pool_ids
            if tid in feat_map
        ]
        selected = _gate_and_rank(gate_pool, s.seed_genre_families, s.seed_mood, cfg, count)
        final_ids = [tid for tid, _ in selected]
        score_by_tid = {tid: emb for tid, emb in selected}
    else:
        # Pure cosine order. search() already sorts by descending cosine and sizes
        # its internal fetch_k to survive the exclusion set, returning at most
        # `count` surviving neighbours.
        results = faiss_index.search(s.seed_embedding, k=count, exclude_ids=exclude)
        if not results:
            return []
        final_ids = [tid for tid, _ in results]
        score_by_tid = {tid: score for tid, score in results}
        feat_result = await db.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids)))
        feat_map = {t.track_id: t for t in feat_result.scalars().all()}

    if not final_ids:
        return []

    tracks: list[dict[str, Any]] = []
    for tid in final_ids:
        tf = feat_map.get(tid)
        s.served.append(tid)
        s.served_set.add(tid)
        s.total_served += 1

        # Skip tracks with no streamable media_server_id (unplayable on the media
        # server). Still marked served above so the outward spiral won't re-pick
        # them. The FAISS index already excludes null-msid tracks, so this is a
        # defensive guard; `position` is assigned post-skip to stay contiguous.
        if tf is None or not tf.media_server_id:
            continue
        track_data: dict[str, Any] = {
            "position": len(tracks),
            "track_id": tid,
            "similarity": round(score_by_tid.get(tid, 0.0), 4),
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
        tracks.append(track_data)

    return tracks
