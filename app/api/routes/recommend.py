"""
GrooveIQ – Recommendation endpoint (Phase 4).

GET /v1/recommend/{user_id} — returns ranked track candidates
from content-based, collaborative filtering, and heuristic sources.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import check_user_access, require_admin, require_api_key
from app.db.session import get_session
from app.models.db import CoverArtCache, ListenEvent, TrackFeatures, TrackInteraction, User

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_model_version() -> str:
    from app.services.ranker import get_model_version

    return get_model_version() or "phase4-candidate-gen-v1"


@router.get(
    "/recommend/{user_id}",
    summary="Get track recommendations for a user",
    description="""
Returns a ranked list of recommended tracks for the given user.

Candidates are generated from multiple sources:
- **content**: FAISS-based acoustic similarity (from user profile or seed track)
- **content_profile**: acoustic similarity from user's taste centroid
- **cf**: collaborative filtering ("users who liked X also liked Y")
- **artist_recall**: tracks from recently listened artists
- **popular**: globally popular tracks (fallback)

Optionally provide a `seed_track_id` to bias results toward a specific track.

Each result includes the source tag for debugging.
A `reco_impression` event is logged for feedback loop training.
""",
)
async def get_recommendations(
    user_id: str,
    seed_track_id: str = Query(None, description="Optional seed track to bias content candidates"),
    limit: int = Query(25, ge=1, le=100),
    # Context params (all optional — client sends what it knows).
    device_type: str = Query(None, description="Device class: mobile, desktop, speaker, car, web"),
    output_type: str = Query(
        None, description="Audio output: headphones, speaker, bluetooth_speaker, car_audio, built_in, airplay"
    ),
    context_type: str = Query(None, description="Listening context: playlist, album, radio, search, home_shelf"),
    location_label: str = Query(None, description="Semantic location: home, work, gym, commute"),
    hour_of_day: int = Query(None, ge=0, le=23, description="Client's local hour (0-23)"),
    day_of_week: int = Query(None, ge=1, le=7, description="Client's local day of week (1=Mon, 7=Sun)"),
    genre: str = Query(None, description="Filter candidates by genre (case-insensitive substring match)"),
    mood: str = Query(
        None, description="Filter candidates by mood tag label (e.g. happy, sad, aggressive). Requires confidence > 0.3"
    ),
    debug: bool = Query(
        False, description="Include debug info: candidates by source, feature vectors, reranker actions"
    ),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    request_t0 = time.monotonic()

    # Debug mode exposes model internals and feature vectors — restrict
    # to admin API keys regardless of environment.
    if debug:
        require_admin(_key)

    # Per-user authorization check.
    check_user_access(_key, user_id)

    # Verify user exists.
    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Verify seed track if provided.
    if seed_track_id:
        result = await session.execute(select(TrackFeatures.track_id).where(TrackFeatures.track_id == seed_track_id))
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Not found.")

    # Generate candidates — over-fetch more when filtering by genre/mood.
    from app.services.candidate_gen import get_candidates

    overfetch = limit * 6 if (genre or mood) else limit * 3
    candidates = await get_candidates(
        user_id=user_id,
        seed_track_id=seed_track_id,
        k=overfetch,
        session=session,
    )

    # Filter candidates by genre/mood if requested.
    if candidates and (genre or mood):
        cand_ids = [c["track_id"] for c in candidates]
        feat_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(cand_ids)))
        feat_lookup = {t.track_id: t for t in feat_q.scalars().all()}

        filtered = []
        for c in candidates:
            tf = feat_lookup.get(c["track_id"])
            if not tf:
                continue
            if genre and not (tf.genre and genre.lower() in tf.genre.lower()):
                continue
            if mood:
                tags = tf.mood_tags if isinstance(tf.mood_tags, list) else []
                if not any(
                    isinstance(tag, dict) and tag.get("label") == mood and tag.get("confidence", 0) > 0.3
                    for tag in tags
                ):
                    continue
            filtered.append(c)
        candidates = filtered

    model_version = _get_model_version()

    if not candidates:
        # Diagnose why there are no candidates so the client can show a useful message.
        from sqlalchemy import func as sa_func

        track_count = (await session.execute(select(sa_func.count()).select_from(TrackFeatures))).scalar() or 0

        if track_count == 0:
            reason = "no_library"
        else:
            interaction_count = (
                await session.execute(
                    select(sa_func.count()).select_from(TrackInteraction).where(TrackInteraction.user_id == user_id)
                )
            ).scalar() or 0

            if interaction_count == 0:
                reason = "no_history"
            else:
                user_row = (
                    await session.execute(select(User.taste_profile).where(User.user_id == user_id))
                ).scalar_one_or_none()
                reason = "no_taste_profile" if not user_row else "no_candidates"

        return {
            "request_id": str(uuid.uuid4()),
            "tracks": [],
            "model_version": model_version,
            "user_id": user_id,
            "seed_track_id": seed_track_id,
            "reason": reason,
        }

    candidate_ids = [c["track_id"] for c in candidates]

    # Build per-candidate source attribution (a candidate may surface from
    # multiple sources — we keep them all for the audit).
    sources_by_tid: dict[str, list[str]] = {}
    for c in candidates:
        sources_by_tid.setdefault(c["track_id"], []).append(c["source"])
    # Counts per source for the audit summary.
    candidates_by_source_counts: dict[str, int] = {}
    for src_list in sources_by_tid.values():
        for s in src_list:
            candidates_by_source_counts[s] = candidates_by_source_counts.get(s, 0) + 1

    # Collect debug data if requested.
    debug_data = None
    if debug:
        from collections import defaultdict

        candidates_by_source = defaultdict(list)
        for c in candidates:
            candidates_by_source[c["source"]].append({"track_id": c["track_id"], "score": round(c["score"], 4)})
        debug_data = {
            "candidates_by_source": dict(candidates_by_source),
            "total_candidates": len(candidates),
        }

    # --- Step 1: Score candidates with LightGBM ranker (or fallback) ---
    from app.services.ranker import score_candidates

    scored = await score_candidates(
        user_id,
        candidate_ids,
        session,
        hour_of_day=hour_of_day,
        day_of_week=day_of_week,
        device_type=device_type,
        output_type=output_type,
        context_type=context_type,
        location_label=location_label,
    )
    raw_score_by_tid = {tid: float(score) for tid, score in scored}
    pre_rerank_pos_by_tid = {tid: i for i, (tid, _) in enumerate(scored)}
    source_map = {c["track_id"]: c["source"] for c in candidates}

    if debug:
        debug_data["pre_rerank"] = [
            {"track_id": tid, "score": round(score, 4), "position": i} for i, (tid, score) in enumerate(scored)
        ]

    # --- Step 2: Rerank for diversity / business rules ---
    # Always collect rerank actions so the audit can record them, regardless
    # of debug mode.  This is cheap (just appends to a list).
    from app.services.reranker import get_last_rerank_actions, rerank

    reranked_full = await rerank(
        scored,
        user_id,
        session,
        device_type=device_type,
        output_type=output_type,
        collect_actions=True,
    )
    rerank_actions = get_last_rerank_actions()
    reranked = reranked_full[:limit]
    final_score_by_tid = {tid: float(score) for tid, score in reranked_full}
    final_pos_by_tid = {tid: i for i, (tid, _) in enumerate(reranked_full)}
    shown_tids = {tid for tid, _ in reranked}

    # Group reranker actions by track for the audit (so each candidate row
    # carries the actions that affected it).
    actions_by_tid: dict[str, list[dict]] = {}
    for action in rerank_actions:
        tid = action.get("track_id")
        if tid:
            actions_by_tid.setdefault(tid, []).append(action)

    # --- Build feature vectors for the candidates we'll persist in the audit.
    # Persist the top-N by raw_score (capped by RECO_AUDIT_MAX_CANDIDATES).
    # We always do this so the audit has a complete picture, but we cap the
    # set so a 500-candidate request doesn't write 500 feature vectors.
    audit_track_ids: list[str] = []
    feature_vectors: dict[str, dict] = {}
    if settings.RECO_AUDIT_ENABLED or debug:
        from app.services.feature_eng import FEATURE_COLUMNS, build_features

        cap = settings.RECO_AUDIT_MAX_CANDIDATES
        # Pick top-N by raw_score, but always include shown tracks.
        ranked_by_raw = sorted(raw_score_by_tid.items(), key=lambda x: x[1], reverse=True)
        top_n = [tid for tid, _ in ranked_by_raw[:cap]]
        audit_track_ids = list({*top_n, *shown_tids})

        feat_result = await build_features(
            user_id,
            audit_track_ids,
            session,
            hour_of_day=hour_of_day,
            day_of_week=day_of_week,
            device_type=device_type,
            output_type=output_type,
            context_type=context_type,
            location_label=location_label,
        )
        for idx, tid in enumerate(feat_result["track_ids"]):
            feature_vectors[tid] = {
                col: round(float(feat_result["features"][idx][ci]), 4) for ci, col in enumerate(FEATURE_COLUMNS)
            }

    if debug:
        debug_data["reranker_actions"] = rerank_actions
        debug_data["feature_vectors"] = {tid: feature_vectors.get(tid, {}) for tid, _ in reranked}

    # Fetch track metadata for response.
    final_ids = [tid for tid, _ in reranked]
    feat_meta_result = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids)))
    feat_map = {t.track_id: t for t in feat_meta_result.scalars().all()}

    # Generate a request_id for impression tracking.
    request_id = str(uuid.uuid4())

    # Log reco_impression events for feedback loop (include context for future training).
    now = int(time.time())
    for i, (tid, score) in enumerate(reranked):
        session.add(
            ListenEvent(
                user_id=user_id,
                track_id=tid,
                event_type="reco_impression",
                surface="recommend_api",
                position=i,
                request_id=request_id,
                model_version=model_version,
                device_type=device_type,
                output_type=output_type,
                context_type=context_type,
                location_label=location_label,
                hour_of_day=hour_of_day,
                day_of_week=day_of_week,
                timestamp=now,
            )
        )
    await session.commit()

    # --- Fire-and-forget audit write ---
    if settings.RECO_AUDIT_ENABLED:
        from app.services.algorithm_config import get_config_version
        from app.services.reco_audit import write_audit

        candidate_rows = []
        for tid in audit_track_ids:
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
        duration_ms = int((time.monotonic() - request_t0) * 1000)
        request_context_payload = {
            "device_type": device_type,
            "output_type": output_type,
            "context_type": context_type,
            "location_label": location_label,
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
            "genre": genre,
            "mood": mood,
        }
        asyncio.create_task(
            write_audit(
                request_id=request_id,
                user_id=user_id,
                surface="recommend_api",
                seed_track_id=seed_track_id,
                context_id=None,
                request_context=request_context_payload,
                model_version=model_version,
                config_version=get_config_version(),
                duration_ms=duration_ms,
                limit_requested=limit,
                candidates_by_source=candidates_by_source_counts,
                candidate_rows=candidate_rows,
            )
        )

    # Build response.
    tracks = []
    for i, (tid, score) in enumerate(reranked):
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

    response = {
        "request_id": request_id,
        "model_version": model_version,
        "user_id": user_id,
        "seed_track_id": seed_track_id,
        "context": {
            "hour_of_day": hour_of_day,
            "day_of_week": day_of_week,
            "device_type": device_type,
            "output_type": output_type,
            "context_type": context_type,
            "location_label": location_label,
        },
        "tracks": tracks,
    }
    if debug and debug_data:
        response["debug"] = debug_data
    return response


@router.get(
    "/recommend/{user_id}/history",
    summary="Get recommendation history for a user",
    description="Returns past recommendation impressions with whether the user streamed the recommended track.",
)
async def get_recommendation_history(
    user_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from sqlalchemy import func as sa_func

    check_user_access(_key, user_id)

    # Verify user exists.
    result = await session.execute(select(User.user_id).where(User.user_id == user_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Count impressions
    count_q = select(sa_func.count()).select_from(
        select(ListenEvent.id)
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "reco_impression")
        .subquery()
    )
    total = (await session.execute(count_q)).scalar() or 0

    # Get impression events, most recent first
    q = (
        select(ListenEvent)
        .where(ListenEvent.user_id == user_id, ListenEvent.event_type == "reco_impression")
        .order_by(ListenEvent.timestamp.desc())
        .offset(offset)
        .limit(limit)
    )
    impressions = (await session.execute(q)).scalars().all()

    if not impressions:
        return {"total": total, "history": []}

    # Check which impressions led to streams (play_start/play_end with same request_id)
    request_ids = list({i.request_id for i in impressions if i.request_id})
    streamed_set = set()
    if request_ids:
        stream_q = select(ListenEvent.request_id, ListenEvent.track_id).where(
            ListenEvent.user_id == user_id,
            ListenEvent.request_id.in_(request_ids),
            ListenEvent.event_type.in_(["play_start", "play_end"]),
        )
        stream_rows = (await session.execute(stream_q)).all()
        streamed_set = {(r[0], r[1]) for r in stream_rows}

    # Get track metadata for all impression track_ids
    track_ids = list({i.track_id for i in impressions})
    feat_q = select(TrackFeatures).where(TrackFeatures.track_id.in_(track_ids))
    feat_map = {t.track_id: t for t in (await session.execute(feat_q)).scalars().all()}

    return {
        "total": total,
        "history": [
            {
                "timestamp": imp.timestamp,
                "track_id": imp.track_id,
                "position": imp.position,
                "request_id": imp.request_id,
                "model_version": imp.model_version,
                "streamed": (imp.request_id, imp.track_id) in streamed_set if imp.request_id else False,
                "title": feat_map[imp.track_id].title if imp.track_id in feat_map else None,
                "artist": feat_map[imp.track_id].artist if imp.track_id in feat_map else None,
                "bpm": feat_map[imp.track_id].bpm if imp.track_id in feat_map else None,
                "energy": feat_map[imp.track_id].energy if imp.track_id in feat_map else None,
            }
            for imp in impressions
        ],
    }


@router.get(
    "/recommend/{user_id}/artists",
    summary="Get recommended artists for a user",
    description="""
Returns a ranked list of recommended artists for the given user,
derived from listening behavior, taste profile, and Last.fm similarity.

Sources:
- **listening**: Artists from the user's most-played/liked tracks, ranked by
  aggregate satisfaction and recency.
- **lastfm_similar**: Similar artists to the user's top artists via Last.fm API.
- **lastfm_top**: User's Last.fm top artists (if connected).

Each artist includes aggregate audio stats from their library tracks and
the user's listening history with that artist (if any).
""",
)
async def get_recommended_artists(
    user_id: str,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    check_user_access(_key, user_id)

    # --- Verify user & load taste profile ---
    user_row = (await session.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
    if user_row is None:
        raise HTTPException(status_code=404, detail="User not found.")

    taste = user_row.taste_profile or {}
    [t["track_id"] for t in taste.get("top_tracks", [])]

    # --- Source 1: Local listening (artist aggregation from interactions) ---
    from sqlalchemy import desc
    from sqlalchemy import func as sa_func

    # Join interactions with features to get artist + satisfaction data
    q = (
        select(
            TrackFeatures.artist,
            sa_func.sum(TrackInteraction.play_count).label("plays"),
            sa_func.sum(TrackInteraction.like_count).label("likes"),
            sa_func.avg(TrackInteraction.satisfaction_score).label("avg_satisfaction"),
            sa_func.count(TrackInteraction.track_id).label("track_count"),
            sa_func.max(TrackInteraction.last_played_at).label("last_played"),
            sa_func.avg(TrackFeatures.energy).label("avg_energy"),
            sa_func.avg(TrackFeatures.danceability).label("avg_danceability"),
            sa_func.avg(TrackFeatures.valence).label("avg_valence"),
            sa_func.avg(TrackFeatures.bpm).label("avg_bpm"),
        )
        .join(TrackFeatures, TrackFeatures.track_id == TrackInteraction.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackFeatures.artist.isnot(None),
            TrackFeatures.artist != "",
        )
        .group_by(TrackFeatures.artist)
        .having(sa_func.sum(TrackInteraction.play_count) > 0)
        .order_by(desc("avg_satisfaction"))
        .limit(200)
    )
    rows = (await session.execute(q)).all()

    # Build artist dict: normalized_name -> data
    import time as _time

    now = _time.time()
    artist_map = {}
    for r in rows:
        name = r.artist.strip()
        if not name:
            continue
        norm = name.lower()
        # Recency factor: more recent = higher boost
        recency = 1.0
        if r.last_played:
            days_ago = (now - r.last_played) / 86400
            import math

            recency = math.exp(-days_ago / 60)  # 60-day half-life

        score = (r.avg_satisfaction or 0) * 0.5 + min((r.plays or 0) / 50, 1.0) * 0.3 + recency * 0.2
        artist_map[norm] = {
            "name": name,
            "score": round(score, 4),
            "source": "listening",
            "plays": r.plays or 0,
            "likes": r.likes or 0,
            "track_count": r.track_count or 0,
            "avg_satisfaction": round(r.avg_satisfaction or 0, 4),
            "last_played": r.last_played,
            "audio": {
                "energy": round(r.avg_energy, 3) if r.avg_energy is not None else None,
                "danceability": round(r.avg_danceability, 3) if r.avg_danceability is not None else None,
                "valence": round(r.avg_valence, 3) if r.avg_valence is not None else None,
                "bpm": round(r.avg_bpm, 1) if r.avg_bpm is not None else None,
            },
            "in_library": True,
        }

    # --- Source 2: Last.fm similar artists ---
    if settings.LASTFM_API_KEY:
        from app.services.discovery import LastFmClient as DiscoveryLastFm

        # Pick top seed artists (top by score from local listening)
        seed_artists = sorted(artist_map.values(), key=lambda a: a["score"], reverse=True)[:8]
        if seed_artists:
            lfm = DiscoveryLastFm(settings.LASTFM_API_KEY)
            try:
                import asyncio

                tasks = [lfm.get_similar_artists(a["name"], limit=15) for a in seed_artists]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for seed_idx, result in enumerate(results):
                    if isinstance(result, Exception):
                        continue
                    seed_name = seed_artists[seed_idx]["name"]
                    for sim in result:
                        sim_name = sim.get("name", "").strip()
                        if not sim_name:
                            continue
                        sim_norm = sim_name.lower()
                        match_score = float(sim.get("match", 0))
                        # If already known from listening, boost; don't replace
                        if sim_norm in artist_map:
                            existing = artist_map[sim_norm]
                            existing["score"] = round(existing["score"] + match_score * 0.15, 4)
                            if "similar_to" not in existing:
                                existing["similar_to"] = []
                            existing["similar_to"].append(seed_name)
                            continue
                        # New artist discovery
                        if sim_norm not in artist_map:
                            artist_map[sim_norm] = {
                                "name": sim_name,
                                "score": round(match_score * 0.6, 4),
                                "source": "lastfm_similar",
                                "similar_to": [seed_name],
                                "mbid": sim.get("mbid"),
                                "in_library": False,
                                "plays": 0,
                                "likes": 0,
                                "track_count": 0,
                            }
            finally:
                await lfm.close()

    # --- Source 3: Last.fm top artists (from cached profile) ---
    lastfm_top = taste.get("lastfm_top_artists", [])
    for entry in lastfm_top:
        name = entry.get("name", "").strip()
        if not name:
            continue
        norm = name.lower()
        if norm in artist_map:
            # Already present — tag it
            if artist_map[norm].get("source") != "listening":
                artist_map[norm]["lastfm_playcount"] = entry.get("playcount", 0)
            continue
        artist_map[norm] = {
            "name": name,
            "score": round(min((entry.get("playcount", 0) or 0) / 500, 1.0) * 0.4, 4),
            "source": "lastfm_top",
            "lastfm_playcount": entry.get("playcount", 0),
            "in_library": False,
            "plays": 0,
            "likes": 0,
            "track_count": 0,
        }

    # --- Check library presence for non-listening artists ---
    non_library = [n for n, a in artist_map.items() if not a.get("in_library")]
    if non_library:
        # Batch check which of these artists have tracks in the library
        lib_q = (
            select(TrackFeatures.artist, sa_func.count().label("cnt"))
            .where(TrackFeatures.artist.isnot(None))
            .group_by(TrackFeatures.artist)
        )
        lib_rows = (await session.execute(lib_q)).all()
        lib_norm = {r.artist.strip().lower(): r.cnt for r in lib_rows if r.artist}
        for norm, a in artist_map.items():
            if not a.get("in_library") and norm in lib_norm:
                a["in_library"] = True
                a["track_count"] = lib_norm[norm]

    # --- Rank and return ---
    ranked = sorted(artist_map.values(), key=lambda a: a["score"], reverse=True)[:limit]

    # --- Batch-fetch artist images from cover_art_cache ---
    import re

    _STRIP_RE = re.compile(r"[^\w\s]", re.UNICODE)

    def _cover_norm(s: str) -> str:
        n = s.strip().lower()
        if n.startswith("the "):
            n = n[4:]
        n = _STRIP_RE.sub("", n)
        return " ".join(n.split())

    artist_norms = {_cover_norm(a["name"]): a["name"] for a in ranked}
    img_map: dict[str, str] = {}
    if artist_norms:
        img_q = await session.execute(
            select(CoverArtCache.artist_norm, CoverArtCache.url).where(
                CoverArtCache.artist_norm.in_(list(artist_norms.keys())),
                CoverArtCache.title_norm == "",
            )
        )
        img_map = {r.artist_norm: r.url for r in img_q.all() if r.url}

    # Resolve any artists still without an image by hitting the download backend
    # (#56). resolve_artist_image reads the same cover_art_cache table first, so
    # any negative-cached entries inside the TTL skip the upstream call. We
    # parallelise across fresh sessions because AsyncSession is single-threaded;
    # cap the fan-out so a cold-cache page load doesn't fire dozens of upstream
    # calls at once.
    _ARTIST_IMG_RESOLVE_CAP = 12
    missing_artist_names = [
        artist_norms[n] for n in artist_norms.keys() if n not in img_map
    ][:_ARTIST_IMG_RESOLVE_CAP]
    if missing_artist_names:
        import asyncio

        from app.db.session import AsyncSessionLocal
        from app.services.cover_art import resolve_artist_image

        async def _resolve_one(name: str) -> tuple[str, str | None]:
            try:
                async with AsyncSessionLocal() as own_session:
                    url = await resolve_artist_image(own_session, name)
                    await own_session.commit()
            except Exception:
                url = None
            return _cover_norm(name), url

        results = await asyncio.gather(
            *[_resolve_one(n) for n in missing_artist_names],
            return_exceptions=False,
        )
        for norm, url in results:
            if url:
                img_map[norm] = url

    for a in ranked:
        a["image_url"] = img_map.get(_cover_norm(a["name"]))

    # --- Fetch per-user top tracks for each artist ---
    artist_names = [a["name"] for a in ranked]
    top_tracks_q = (
        select(
            TrackFeatures.artist,
            TrackFeatures.track_id,
            TrackFeatures.title,
            TrackFeatures.album,
            TrackFeatures.duration,
            TrackInteraction.satisfaction_score,
            TrackInteraction.play_count,
        )
        .join(TrackInteraction, TrackInteraction.track_id == TrackFeatures.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackFeatures.artist.in_(artist_names),
        )
        .order_by(TrackInteraction.satisfaction_score.desc())
    )
    top_rows = (await session.execute(top_tracks_q)).all()

    # Group by artist, take top 5 per artist
    from collections import defaultdict

    tracks_by_artist: dict[str, list] = defaultdict(list)
    for r in top_rows:
        if len(tracks_by_artist[r.artist]) < 5:
            tracks_by_artist[r.artist].append(
                {
                    "track_id": r.track_id,
                    "title": r.title,
                    "album": r.album,
                    "duration": r.duration,
                    "satisfaction_score": round(r.satisfaction_score, 4) if r.satisfaction_score else 0,
                    "play_count": r.play_count or 0,
                }
            )

    for a in ranked:
        a["top_tracks"] = tracks_by_artist.get(a["name"], [])

    return {
        "user_id": user_id,
        "total": len(ranked),
        "artists": ranked,
    }


@router.get(
    "/stats/model",
    summary="Get recommendation model stats and evaluation metrics",
    description="Returns ranker training info, latest offline evaluation metrics, and impression-to-stream stats.",
)
async def get_model_stats(
    _key: str = Depends(require_api_key),
):
    from app.core.security import require_admin

    require_admin(_key)
    from app.services.evaluation import get_model_report

    return await get_model_report()
