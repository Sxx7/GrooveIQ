"""
GrooveIQ — Artist recommendation service (L1 content + L2 ranker roll-up).

Turns the legacy play-count/recency heuristic behind
``GET /v1/recommend/{user}/artists`` into an algorithm-driven blend that reuses
the two strongest assets we already have: the 64-dim audio embeddings (FAISS)
and the trained track ranker. **No new model is trained** — both signals are
computed per-call from infrastructure that already exists:

  * ``content_score``  — cosine of the artist's audio centroid against the
    user's taste centroid (FAISS). "Does this artist *sound* like what I like?"
  * ``ranker_rollup``  — top-k mean of the existing track ranker's learned
    scores over the artist's in-library tracks. "Do I rate their tracks highly?"
  * ``lastfm_signal``  — Last.fm similar/top match (borrowed CF).
  * ``history_score``  — the legacy play-count/recency/satisfaction heuristic,
    folded in as one input rather than the whole ranking.

The four terms are blended with weights from the versioned ``artist_reco``
config group, shifted by the request ``mode`` (familiar | balanced | discover).
Every term degrades gracefully: no embeddings → content term is 0; no ranker →
the ranker falls back to satisfaction; neither available → the blend reduces to
the legacy heuristic, so a fresh/un-analysed library still returns something.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import numpy as np
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.db import TrackFeatures, TrackInteraction, User
from app.services import faiss_index, ranker
from app.services.algorithm_config import get_config
from app.services.artist_meta import _normalize
from app.services.candidate_gen import _get_user_top_track_ids

logger = logging.getLogger(__name__)

MODES = ("familiar", "balanced", "discover")
DEFAULT_MODE = "discover"

# Per-mode multipliers applied to the artist_reco weights at serving time.
# ``discover`` leans on audio content + Last.fm discovery; ``familiar`` leans on
# the ranker roll-up and proven listening history; ``balanced`` is neutral.
_MODE_MULT: dict[str, dict[str, float]] = {
    "familiar": {"content": 0.6, "ranker": 1.4, "lastfm": 0.7, "history": 1.5},
    "balanced": {"content": 1.0, "ranker": 1.0, "lastfm": 1.0, "history": 1.0},
    "discover": {"content": 1.6, "ranker": 0.7, "lastfm": 1.4, "history": 0.4},
}

# Cap the number of in-library candidate tracks fed to the ranker in one batch
# so a huge library can't blow up a single request (history candidates first).
_MAX_ROLLUP_TRACKS = 4000

# How many Last.fm seed artists to expand (mirrors the legacy endpoint).
_LASTFM_SEED_LIMIT = 8


def _coerce_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in MODES else DEFAULT_MODE


def _top_k_mean(values: list[float], k: int) -> float:
    """Mean of the top-k values — rewards a few strong tracks over one outlier
    or a long tail of filler. Empty → 0.0."""
    if not values:
        return 0.0
    top = sorted(values, reverse=True)[: max(1, k)]
    return float(sum(top) / len(top))


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


async def recommend_artists(
    session: AsyncSession,
    user_id: str,
    *,
    mode: str = DEFAULT_MODE,
    limit: int = 25,
    include_discovery: bool = True,
) -> dict[str, Any]:
    """
    Rank artists for ``user_id`` by blending content, learned-ranking, Last.fm,
    and history signals. Returns ``{"mode", "generated_at", "artists": [...]}``;
    the route layer adds ``user_id``/``total`` and the image + top-track
    enrichment. Each artist dict carries ``sources``/``reasons``/``signals`` for
    "Because you listen to X"-style badges.
    """
    mode = _coerce_mode(mode)
    cfg = get_config().artist_reco
    now = time.time()

    taste = await _load_taste_profile(session, user_id)

    # --- Candidate set: legacy history + Last.fm sources -------------------
    artist_map: dict[str, dict[str, Any]] = {}
    await _add_history_candidates(session, user_id, artist_map, now)
    await _add_lastfm_candidates(artist_map, taste)

    # --- Library presence + per-artist library track ids (one scan) -------
    artist_tracks, audio_map = await _build_artist_index(session)
    for norm, a in artist_map.items():
        if norm in artist_tracks:
            a["in_library"] = True
            a.setdefault("track_count", len(artist_tracks[norm]))
            if not a.get("track_count"):
                a["track_count"] = len(artist_tracks[norm])

    # --- L1 content: user taste centroid + FAISS discovery ----------------
    user_centroid = None
    if faiss_index.is_ready():
        top_ids = _get_user_top_track_ids(taste)
        if top_ids:
            user_centroid = faiss_index.get_centroid(top_ids)

    if include_discovery and user_centroid is not None:
        await _add_faiss_discovery_candidates(
            session, user_id, user_centroid, artist_map, artist_tracks, cfg.discovery_faiss_k
        )

    # content_score for every in-library candidate (centroid vs taste).
    if user_centroid is not None:
        for norm, a in artist_map.items():
            if not a.get("in_library"):
                continue
            if a.get("content_score") is not None:
                continue  # already set by the discovery pass
            tids = artist_tracks.get(norm) or []
            c_a = faiss_index.get_centroid(tids) if tids else None
            if c_a is not None:
                sim = float(np.dot(c_a, user_centroid))
                a["content_score"] = _clamp01((sim + 1.0) / 2.0)

    # --- L2 ranker roll-up (one batched scoring call) ---------------------
    await _add_ranker_rollup(session, user_id, artist_map, artist_tracks, cfg.rollup_top_k)

    # --- Blend, reasons, signals ------------------------------------------
    m = _MODE_MULT[mode]
    wc = cfg.w_content * m["content"]
    wr = cfg.w_ranker * m["ranker"]
    wl = cfg.w_lastfm * m["lastfm"]
    wh = cfg.w_history * m["history"]
    denom = (wc + wr + wl + wh) or 1.0

    results: list[dict[str, Any]] = []
    for norm, a in artist_map.items():
        content = a.get("content_score") or 0.0
        rollup = a.get("ranker_rollup") or 0.0
        lastfm = a.get("lastfm_signal") or 0.0
        history = a.get("history_score") or 0.0

        score = (wc * content + wr * rollup + wl * lastfm + wh * history) / denom

        sources: list[str] = []
        if history > 0:
            sources.append("listening")
        if content > 0:
            sources.append("taste_centroid")
        if rollup > 0:
            sources.append("ranker_rollup")
        if lastfm > 0:
            sources.append(a.get("_lastfm_source", "lastfm_similar"))

        reasons: list[str] = []
        if content >= cfg.content_reason_threshold:
            reasons.append("sounds like your taste")
        if rollup >= cfg.ranker_reason_threshold:
            reasons.append("you rate their tracks highly")
        if a.get("similar_to"):
            reasons.append(f"similar to {a['similar_to'][0]}")
        if a.get("in_library") and (a.get("plays") or 0) == 0 and content >= cfg.content_reason_threshold:
            reasons.append("in your library, rarely played")

        out = {
            "name": a["name"],
            "score": round(score, 4),
            "source": a.get("source", "listening"),
            "sources": sources or [a.get("source", "listening")],
            "reasons": reasons,
            "signals": {
                "content_score": round(content, 4),
                "ranker_rollup": round(rollup, 4),
                "lastfm_match": round(lastfm, 4),
                "history_score": round(history, 4),
                "play_count": a.get("plays") or 0,
                "avg_satisfaction": round(a.get("avg_satisfaction") or 0.0, 4),
            },
            "in_library": bool(a.get("in_library")),
            "plays": a.get("plays") or 0,
            "likes": a.get("likes") or 0,
            "track_count": a.get("track_count") or 0,
            "avg_satisfaction": round(a.get("avg_satisfaction") or 0.0, 4),
            "last_played": a.get("last_played"),
            "audio": audio_map.get(norm) or a.get("audio"),
        }
        # Preserve optional legacy fields when present.
        for opt in ("similar_to", "mbid", "lastfm_playcount"):
            if a.get(opt) is not None:
                out[opt] = a[opt]
        results.append(out)

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"mode": mode, "generated_at": int(now), "artists": results[:limit]}


# ---------------------------------------------------------------------------
# Candidate sources
# ---------------------------------------------------------------------------


async def _load_taste_profile(session: AsyncSession, user_id: str) -> dict[str, Any]:
    row = (await session.execute(select(User.taste_profile).where(User.user_id == user_id))).scalar_one_or_none()
    return row or {}


async def _add_history_candidates(
    session: AsyncSession, user_id: str, artist_map: dict[str, dict[str, Any]], now: float
) -> None:
    """Source 1: local listening, aggregated to artist level (legacy heuristic
    as ``history_score``)."""
    q = (
        select(
            TrackFeatures.artist,
            sa_func.sum(TrackInteraction.play_count).label("plays"),
            sa_func.sum(TrackInteraction.like_count).label("likes"),
            sa_func.avg(TrackInteraction.satisfaction_score).label("avg_satisfaction"),
            sa_func.count(TrackInteraction.track_id).label("track_count"),
            sa_func.max(TrackInteraction.last_played_at).label("last_played"),
        )
        .join(TrackFeatures, TrackFeatures.track_id == TrackInteraction.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackFeatures.artist.isnot(None),
            TrackFeatures.artist != "",
        )
        .group_by(TrackFeatures.artist)
        .having(sa_func.sum(TrackInteraction.play_count) > 0)
        .order_by(sa_func.avg(TrackInteraction.satisfaction_score).desc())
        .limit(200)
    )
    rows = (await session.execute(q)).all()
    for r in rows:
        name = (r.artist or "").strip()
        if not name:
            continue
        norm = _normalize(name)
        recency = 1.0
        if r.last_played:
            days_ago = (now - r.last_played) / 86400
            recency = math.exp(-days_ago / 60)  # 60-day half-life
        history = (r.avg_satisfaction or 0) * 0.5 + min((r.plays or 0) / 50, 1.0) * 0.3 + recency * 0.2
        artist_map[norm] = {
            "name": name,
            "source": "listening",
            "history_score": round(history, 4),
            "plays": r.plays or 0,
            "likes": r.likes or 0,
            "track_count": r.track_count or 0,
            "avg_satisfaction": r.avg_satisfaction or 0.0,
            "last_played": r.last_played,
            "in_library": True,
        }


async def _add_lastfm_candidates(artist_map: dict[str, dict[str, Any]], taste: dict[str, Any]) -> None:
    """Sources 2 & 3: Last.fm similar (borrowed CF) and the user's Last.fm top
    artists. Best-effort — a Last.fm failure never breaks the endpoint."""
    # Source 3 first (cheap, from cached profile): Last.fm top artists.
    for entry in taste.get("lastfm_top_artists", []) or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        norm = _normalize(name)
        pc = entry.get("playcount", 0) or 0
        top_signal = min(pc / 500, 1.0)
        if norm in artist_map:
            artist_map[norm].setdefault("lastfm_playcount", pc)
            existing = artist_map[norm].get("lastfm_signal") or 0.0
            artist_map[norm]["lastfm_signal"] = max(existing, top_signal)
            artist_map[norm].setdefault("_lastfm_source", "lastfm_top")
            continue
        artist_map[norm] = {
            "name": name,
            "source": "lastfm_top",
            "_lastfm_source": "lastfm_top",
            "lastfm_signal": top_signal,
            "lastfm_playcount": pc,
            "history_score": 0.0,
            "in_library": False,
            "plays": 0,
            "likes": 0,
            "track_count": 0,
        }

    # Source 2: Last.fm similar artists (expand the strongest seeds).
    if not settings.LASTFM_API_KEY:
        return
    seeds = sorted(
        (a for a in artist_map.values() if a.get("history_score")),
        key=lambda a: a.get("history_score", 0.0),
        reverse=True,
    )[:_LASTFM_SEED_LIMIT]
    if not seeds:
        return

    from app.services.discovery import LastFmClient as DiscoveryLastFm

    lfm = DiscoveryLastFm(settings.LASTFM_API_KEY)
    try:
        tasks = [lfm.get_similar_artists(a["name"], limit=15) for a in seeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for seed_idx, result in enumerate(results):
            if isinstance(result, Exception):
                continue
            seed_name = seeds[seed_idx]["name"]
            for sim in result:
                sim_name = (sim.get("name") or "").strip()
                if not sim_name:
                    continue
                sim_norm = _normalize(sim_name)
                match = float(sim.get("match", 0) or 0)
                if sim_norm in artist_map:
                    a = artist_map[sim_norm]
                    a["lastfm_signal"] = max(a.get("lastfm_signal") or 0.0, match)
                    a.setdefault("_lastfm_source", "lastfm_similar")
                    a.setdefault("similar_to", [])
                    if seed_name not in a["similar_to"]:
                        a["similar_to"].append(seed_name)
                    continue
                artist_map[sim_norm] = {
                    "name": sim_name,
                    "source": "lastfm_similar",
                    "_lastfm_source": "lastfm_similar",
                    "lastfm_signal": match,
                    "similar_to": [seed_name],
                    "mbid": sim.get("mbid"),
                    "history_score": 0.0,
                    "in_library": False,
                    "plays": 0,
                    "likes": 0,
                    "track_count": 0,
                }
    except Exception as exc:
        logger.warning("artist_reco: Last.fm similar expansion failed: %s", exc)
    finally:
        await lfm.close()


async def _add_faiss_discovery_candidates(
    session: AsyncSession,
    user_id: str,
    user_centroid: np.ndarray,
    artist_map: dict[str, dict[str, Any]],
    artist_tracks: dict[str, list[str]],
    discovery_k: int,
) -> None:
    """The content analogue of Last.fm similar: nearest library tracks to the
    user's taste centroid, excluding anything they've already interacted with,
    grouped by artist. Surfaces in-library artists that *sound* like the user's
    taste but sit in the long tail (low/zero plays)."""
    interacted = await _interacted_track_ids(session, user_id)
    neighbours = faiss_index.search(user_centroid, k=discovery_k, exclude_ids=interacted)
    if not neighbours:
        return

    nbr_ids = [tid for tid, _ in neighbours]
    score_by_id = {tid: s for tid, s in neighbours}
    rows = (
        await session.execute(
            select(TrackFeatures.track_id, TrackFeatures.artist).where(TrackFeatures.track_id.in_(nbr_ids))
        )
    ).all()

    by_artist: dict[str, dict[str, Any]] = {}
    for track_id, artist in rows:
        if not artist:
            continue
        norm = _normalize(artist)
        slot = by_artist.setdefault(norm, {"name": artist.strip(), "sims": []})
        slot["sims"].append(score_by_id.get(track_id, 0.0))

    for norm, slot in by_artist.items():
        content = _clamp01((float(np.mean(slot["sims"])) + 1.0) / 2.0)
        if norm in artist_map:
            # Already a candidate — just contribute the content signal.
            artist_map[norm].setdefault("content_score", content)
            continue
        artist_map[norm] = {
            "name": slot["name"],
            "source": "taste_centroid",
            "content_score": content,
            "history_score": 0.0,
            "in_library": True,
            "plays": 0,
            "likes": 0,
            "track_count": len(artist_tracks.get(norm) or []),
        }


# ---------------------------------------------------------------------------
# L2 ranker roll-up
# ---------------------------------------------------------------------------


async def _add_ranker_rollup(
    session: AsyncSession,
    user_id: str,
    artist_map: dict[str, dict[str, Any]],
    artist_tracks: dict[str, list[str]],
    top_k: int,
) -> None:
    """Score every in-library candidate artist's tracks in ONE ranker call,
    then set ``ranker_rollup`` = top-k mean of that artist's track scores."""
    # Collect in-library candidate tracks (history candidates first), capped.
    batch: list[str] = []
    seen: set[str] = set()
    ordered_norms = [n for n, a in artist_map.items() if a.get("in_library")]
    for norm in ordered_norms:
        for tid in artist_tracks.get(norm) or []:
            if tid in seen:
                continue
            seen.add(tid)
            batch.append(tid)
            if len(batch) >= _MAX_ROLLUP_TRACKS:
                break
        if len(batch) >= _MAX_ROLLUP_TRACKS:
            break
    if not batch:
        return

    try:
        scored = await ranker.score_candidates(user_id, batch, session)
    except Exception as exc:
        logger.warning("artist_reco: ranker roll-up failed: %s", exc)
        return
    score_by_id = {tid: _clamp01(float(s)) for tid, s in scored}

    for norm in ordered_norms:
        vals = [score_by_id[t] for t in (artist_tracks.get(norm) or []) if t in score_by_id]
        if vals:
            artist_map[norm]["ranker_rollup"] = round(_top_k_mean(vals, top_k), 4)


# ---------------------------------------------------------------------------
# Library lookups
# ---------------------------------------------------------------------------


async def _build_artist_index(
    session: AsyncSession,
) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """One library scan → ``(norm_artist -> [track_id], norm_artist -> mean audio)``.

    Merges what used to be two full-table scans (track index + a GROUP BY audio
    aggregate) into a single pass; the averaging moves to Python, which is cheap
    for a personal library.
    """
    rows = (
        await session.execute(
            select(
                TrackFeatures.track_id,
                TrackFeatures.artist,
                TrackFeatures.energy,
                TrackFeatures.danceability,
                TrackFeatures.valence,
                TrackFeatures.bpm,
            ).where(TrackFeatures.artist.isnot(None))
        )
    ).all()

    tracks: dict[str, list[str]] = {}
    acc: dict[str, dict[str, list[float]]] = {}
    for track_id, artist, energy, danceability, valence, bpm in rows:
        if not artist:
            continue
        norm = _normalize(artist)
        tracks.setdefault(norm, []).append(track_id)
        a = acc.setdefault(norm, {"energy": [], "danceability": [], "valence": [], "bpm": []})
        if energy is not None:
            a["energy"].append(energy)
        if danceability is not None:
            a["danceability"].append(danceability)
        if valence is not None:
            a["valence"].append(valence)
        if bpm is not None:
            a["bpm"].append(bpm)

    audio: dict[str, dict[str, Any]] = {}
    for norm, a in acc.items():
        audio[norm] = {
            "energy": round(float(np.mean(a["energy"])), 3) if a["energy"] else None,
            "danceability": round(float(np.mean(a["danceability"])), 3) if a["danceability"] else None,
            "valence": round(float(np.mean(a["valence"])), 3) if a["valence"] else None,
            "bpm": round(float(np.mean(a["bpm"])), 1) if a["bpm"] else None,
        }
    return tracks, audio


async def _interacted_track_ids(session: AsyncSession, user_id: str) -> set[str]:
    rows = (
        await session.execute(select(TrackInteraction.track_id).where(TrackInteraction.user_id == user_id))
    ).all()
    return {r.track_id for r in rows}
