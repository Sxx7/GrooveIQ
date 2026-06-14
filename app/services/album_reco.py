"""
GrooveIQ — Album recommendation service (library-only roll-up).

Albums are *not* a separately-modelled entity (they aren't at Spotify either —
public technical accounts describe the recommender at the track level, with
albums/artists rendered as presentation surfaces). So this is an aggregation
layer over the existing track signals, **not** a parallel ML pipeline.

Library tracks are grouped by ``(album_artist or artist, album)`` — there is no
MusicBrainz album id in the schema, and a string key is adequate for a personal
library. Each album is scored by blending:

  * ``ranker_rollup`` — top-k mean of the track ranker's learned scores over the
    album's tracks ("you rate this album's tracks highly").
  * ``coverage``      — owned tracks / total album tracks ("you have/play the
    whole album"), estimated from ``max(track_number)``.
  * ``freshness``     — a gentle "rediscover this" boost for albums you haven't
    played in a while.
  * ``content_score`` — cosine of the album's audio centroid against your taste
    centroid (audio coherence vs taste).

Weights come from the versioned ``album_reco`` config group and shift with the
request ``mode`` (familiar | balanced | discover). This pass is **library-only**
— discovery / acquire handles (Fill Library surfacing) are intentionally not
wired here; ``acquire`` is always ``null``.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import TrackFeatures, TrackInteraction, User
from app.services import faiss_index, ranker
from app.services.algorithm_config import get_config
from app.services.artist_meta import _normalize
from app.services.candidate_gen import _get_user_top_track_ids

logger = logging.getLogger(__name__)

MODES = ("familiar", "balanced", "discover")
DEFAULT_MODE = "discover"

# Per-mode multipliers applied to the album_reco weights at serving time.
_MODE_MULT: dict[str, dict[str, float]] = {
    "familiar": {"content": 0.6, "ranker": 1.3, "coverage": 1.2, "fresh": 0.7},
    "balanced": {"content": 1.0, "ranker": 1.0, "coverage": 1.0, "fresh": 1.0},
    "discover": {"content": 1.6, "ranker": 0.8, "coverage": 0.8, "fresh": 1.3},
}

_LN2 = math.log(2.0)
_MAX_ROLLUP_TRACKS = 4000
_REPRESENTATIVE_LIMIT = 5


def _coerce_mode(mode: str | None) -> str:
    m = (mode or DEFAULT_MODE).strip().lower()
    return m if m in MODES else DEFAULT_MODE


def _top_k_mean(values: list[float], k: int) -> float:
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


async def recommend_albums(
    session: AsyncSession,
    user_id: str,
    *,
    mode: str = DEFAULT_MODE,
    limit: int = 25,
) -> dict[str, Any]:
    """
    Rank in-library albums for ``user_id``. Returns
    ``{"mode", "generated_at", "albums": [...]}``; the route layer adds
    ``user_id``/``total`` and resolves cover art for the top results.
    """
    mode = _coerce_mode(mode)
    cfg = get_config().album_reco
    now = time.time()

    taste = await _load_taste_profile(session, user_id)

    # --- Group library tracks into albums ---------------------------------
    albums = await _group_albums(session)
    if not albums:
        return {"mode": mode, "generated_at": int(now), "albums": []}

    # --- Per-track user interactions (satisfaction / last played) ----------
    interactions = await _user_interactions(session, user_id)

    # --- L2 ranker roll-up: one batched scoring call -----------------------
    score_by_id = await _score_album_tracks(session, user_id, albums, interactions)

    # --- User taste centroid for L1 content --------------------------------
    user_centroid = None
    if faiss_index.is_ready():
        top_ids = _get_user_top_track_ids(taste)
        if top_ids:
            user_centroid = faiss_index.get_centroid(top_ids)

    # --- Blend weights -----------------------------------------------------
    m = _MODE_MULT[mode]
    wc = cfg.w_content * m["content"]
    wr = cfg.w_ranker * m["ranker"]
    wcov = cfg.w_coverage * m["coverage"]
    wf = cfg.w_fresh * m["fresh"]
    denom = (wc + wr + wcov + wf) or 1.0

    results: list[dict[str, Any]] = []
    for alb in albums.values():
        tids = alb["track_ids"]
        if len(tids) < cfg.min_album_tracks:
            continue

        # ranker roll-up (k = album size, capped by config)
        rank_vals = [score_by_id[t] for t in tids if t in score_by_id]
        k = min(cfg.rollup_top_k, len(tids))
        rollup = _top_k_mean(rank_vals, k) if rank_vals else 0.0

        # coverage = owned / total (max track_number as a proxy for album size)
        max_tn = alb["max_track_number"]
        total = max_tn if max_tn and max_tn >= len(tids) else len(tids)
        coverage = _clamp01(len(tids) / total) if total else 1.0

        # freshness = rediscover boost for long-unplayed albums (0 if never played)
        last_played = alb["last_played"]
        days_since = None
        freshness = 0.0
        if last_played:
            days_since = (now - last_played) / 86400
            freshness = _clamp01(1.0 - math.exp(-_LN2 * days_since / cfg.fresh_halflife_days))

        # content coherence vs taste
        content = 0.0
        if user_centroid is not None:
            c_b = faiss_index.get_centroid(tids)
            if c_b is not None:
                content = _clamp01((float(np.dot(c_b, user_centroid)) + 1.0) / 2.0)

        score = (wc * content + wr * rollup + wcov * coverage + wf * freshness) / denom

        sources: list[str] = ["ranker_rollup"] if rollup > 0 else []
        if content > 0:
            sources.append("taste_centroid")
        if coverage >= 0.999:
            sources.append("coverage")
        if freshness > 0:
            sources.append("freshness")

        reasons: list[str] = []
        if rollup >= 0.6:
            reasons.append("high-scoring across the whole album")
        if coverage >= 0.999:
            reasons.append("you have the whole album")
        if days_since is not None and days_since >= cfg.fresh_halflife_days:
            months = int(days_since / 30)
            reasons.append(f"not played in {months} month{'s' if months != 1 else ''}")
        if content >= 0.6:
            reasons.append("sounds like your taste")

        results.append(
            {
                "album": alb["album"],
                "album_artist": alb["album_artist"],
                "score": round(score, 4),
                "in_library": True,
                "library_track_count": len(tids),
                "completeness": round(coverage, 4),
                "sources": sources or ["coverage"],
                "reasons": reasons,
                "signals": {
                    "ranker_rollup": round(rollup, 4),
                    "coverage": round(coverage, 4),
                    "days_since_last_play": int(days_since) if days_since is not None else None,
                    "content_score": round(content, 4),
                },
                "audio_profile": alb["audio"],
                "representative_tracks": _representative_tracks(alb, score_by_id, interactions),
                "cover_url": None,  # resolved by the route for the top-N
                "acquire": None,  # library-only pass — no discovery/acquire handle yet
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    return {"mode": mode, "generated_at": int(now), "albums": results[:limit]}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_taste_profile(session: AsyncSession, user_id: str) -> dict[str, Any]:
    row = (await session.execute(select(User.taste_profile).where(User.user_id == user_id))).scalar_one_or_none()
    return row or {}


async def _group_albums(session: AsyncSession) -> dict[tuple[str, str], dict[str, Any]]:
    """Group every library track with a non-empty album into
    ``(norm_album_artist, norm_album) -> album record``."""
    rows = (
        await session.execute(
            select(
                TrackFeatures.track_id,
                TrackFeatures.artist,
                TrackFeatures.album_artist,
                TrackFeatures.album,
                TrackFeatures.track_number,
                TrackFeatures.title,
                TrackFeatures.media_server_id,
                TrackFeatures.energy,
                TrackFeatures.valence,
                TrackFeatures.danceability,
                TrackFeatures.bpm,
            ).where(TrackFeatures.album.isnot(None), TrackFeatures.album != "")
        )
    ).all()

    albums: dict[tuple[str, str], dict[str, Any]] = {}
    accum: dict[tuple[str, str], dict[str, list]] = {}
    for r in rows:
        album = (r.album or "").strip()
        if not album:
            continue
        display_artist = (r.album_artist or r.artist or "").strip()
        key = (_normalize(display_artist), _normalize(album))
        rec = albums.get(key)
        if rec is None:
            rec = {
                "album": album,
                "album_artist": display_artist or None,
                "track_ids": [],
                "max_track_number": 0,
                "last_played": None,
                "tracks": [],  # per-track {track_id,title,media_server_id} for representatives
            }
            albums[key] = rec
            accum[key] = {"energy": [], "valence": [], "danceability": [], "bpm": []}
        rec["track_ids"].append(r.track_id)
        rec["tracks"].append({"track_id": r.track_id, "title": r.title, "media_server_id": r.media_server_id})
        if r.track_number and r.track_number > rec["max_track_number"]:
            rec["max_track_number"] = r.track_number
        for field in ("energy", "valence", "danceability", "bpm"):
            val = getattr(r, field)
            if val is not None:
                accum[key][field].append(val)

    for key, rec in albums.items():
        a = accum[key]
        rec["audio"] = {
            "energy": round(float(np.mean(a["energy"])), 3) if a["energy"] else None,
            "valence": round(float(np.mean(a["valence"])), 3) if a["valence"] else None,
            "danceability": round(float(np.mean(a["danceability"])), 3) if a["danceability"] else None,
            "bpm": round(float(np.mean(a["bpm"])), 1) if a["bpm"] else None,
        }
    return albums


async def _user_interactions(session: AsyncSession, user_id: str) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                TrackInteraction.track_id,
                TrackInteraction.satisfaction_score,
                TrackInteraction.last_played_at,
                TrackInteraction.play_count,
            ).where(TrackInteraction.user_id == user_id)
        )
    ).all()
    return {
        r.track_id: {
            "satisfaction": r.satisfaction_score or 0.0,
            "last_played": r.last_played_at,
            "play_count": r.play_count or 0,
        }
        for r in rows
    }


async def _score_album_tracks(
    session: AsyncSession,
    user_id: str,
    albums: dict[tuple[str, str], dict[str, Any]],
    interactions: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Score every album's tracks in ONE ranker call. Albums with the most
    listening are batched first so favourites get scored before the cap. Also
    fills each album's ``last_played`` (max over its tracks).

    On a library larger than ``_MAX_ROLLUP_TRACKS`` album tracks, the long tail
    of never-played albums is left out of the batch and falls back to
    ``ranker_rollup=0``. That degradation is aligned with the modes: ``familiar``
    up-weights the ranker and wants exactly the high-play albums (scored first),
    while ``discover`` rides ``content``/``freshness`` rather than the ranker, so
    the cap costs little where it bites. The cap bounds per-request latency
    (``build_features`` runs inside ``score_candidates``)."""

    # Order albums by total play count (favourites first) and accumulate tracks.
    def _album_plays(rec: dict[str, Any]) -> int:
        return sum(interactions.get(t, {}).get("play_count", 0) for t in rec["track_ids"])

    ordered = sorted(albums.values(), key=_album_plays, reverse=True)

    batch: list[str] = []
    seen: set[str] = set()
    for rec in ordered:
        # set last_played while we iterate
        lp = None
        for t in rec["track_ids"]:
            tlp = interactions.get(t, {}).get("last_played")
            if tlp and (lp is None or tlp > lp):
                lp = tlp
        rec["last_played"] = lp
        for t in rec["track_ids"]:
            if t in seen:
                continue
            seen.add(t)
            if len(batch) < _MAX_ROLLUP_TRACKS:
                batch.append(t)

    if not batch:
        return {}
    try:
        scored = await ranker.score_candidates(user_id, batch, session)
    except Exception as exc:
        logger.warning("album_reco: ranker roll-up failed: %s", exc)
        return {}
    return {tid: _clamp01(float(s)) for tid, s in scored}


def _representative_tracks(
    alb: dict[str, Any],
    score_by_id: dict[str, float],
    interactions: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Top tracks of the album by ranker score, falling back to satisfaction."""

    def _key(t: dict[str, Any]) -> float:
        tid = t["track_id"]
        if tid in score_by_id:
            return score_by_id[tid]
        return interactions.get(tid, {}).get("satisfaction", 0.0)

    ordered = sorted(alb["tracks"], key=_key, reverse=True)[:_REPRESENTATIVE_LIMIT]
    return [{"track_id": t["track_id"], "title": t["title"], "media_server_id": t["media_server_id"]} for t in ordered]
