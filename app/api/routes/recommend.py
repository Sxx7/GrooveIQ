"""
GrooveIQ – Recommendation endpoint (Phase 4).

GET /v1/recommend/{user_id} — returns ranked track candidates
from content-based, collaborative filtering, and heuristic sources.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import check_user_access, require_api_key
from app.db.session import get_session
from app.models.db import ListenEvent, TrackFeatures, TrackInteraction, User

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
    output_type: str = Query(None, description="Audio output: headphones, speaker, bluetooth_speaker, car_audio, built_in, airplay"),
    context_type: str = Query(None, description="Listening context: playlist, album, radio, search, home_shelf"),
    location_label: str = Query(None, description="Semantic location: home, work, gym, commute"),
    hour_of_day: int = Query(None, ge=0, le=23, description="Client's local hour (0-23)"),
    day_of_week: int = Query(None, ge=1, le=7, description="Client's local day of week (1=Mon, 7=Sun)"),
    genre: str = Query(None, description="Filter candidates by genre (case-insensitive substring match)"),
    mood: str = Query(None, description="Filter candidates by mood tag label (e.g. happy, sad, aggressive). Requires confidence > 0.3"),
    debug: bool = Query(False, description="Include debug info: candidates by source, feature vectors, reranker actions"),
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    from app.core.config import settings as _settings

    # Debug mode is development-only — it exposes model internals and
    # feature vectors that could leak user taste profile information.
    if debug and _settings.APP_ENV != "development":
        raise HTTPException(
            status_code=403,
            detail="Debug mode is only available when APP_ENV=development.",
        )

    # Per-user authorization check.
    check_user_access(_key, user_id)

    # Verify user exists.
    result = await session.execute(
        select(User.user_id).where(User.user_id == user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Not found.")

    # Verify seed track if provided.
    if seed_track_id:
        result = await session.execute(
            select(TrackFeatures.track_id).where(TrackFeatures.track_id == seed_track_id)
        )
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
        feat_q = await session.execute(
            select(TrackFeatures).where(TrackFeatures.track_id.in_(cand_ids))
        )
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
                    isinstance(tag, dict)
                    and tag.get("label") == mood
                    and tag.get("confidence", 0) > 0.3
                    for tag in tags
                ):
                    continue
            filtered.append(c)
        candidates = filtered

    model_version = _get_model_version()

    if not candidates:
        # Diagnose why there are no candidates so the client can show a useful message.
        from sqlalchemy import func as sa_func

        track_count = (await session.execute(
            select(sa_func.count()).select_from(TrackFeatures)
        )).scalar() or 0

        if track_count == 0:
            reason = "no_library"
        else:
            interaction_count = (await session.execute(
                select(sa_func.count()).select_from(TrackInteraction)
                .where(TrackInteraction.user_id == user_id)
            )).scalar() or 0

            if interaction_count == 0:
                reason = "no_history"
            else:
                user_row = (await session.execute(
                    select(User.taste_profile).where(User.user_id == user_id)
                )).scalar_one_or_none()
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
        user_id, candidate_ids, session,
        hour_of_day=hour_of_day, day_of_week=day_of_week,
        device_type=device_type, output_type=output_type,
        context_type=context_type, location_label=location_label,
    )
    score_map = {tid: score for tid, score in scored}
    source_map = {c["track_id"]: c["source"] for c in candidates}

    if debug:
        debug_data["pre_rerank"] = [{"track_id": tid, "score": round(score, 4), "position": i} for i, (tid, score) in enumerate(scored)]

    # --- Step 2: Rerank for diversity / business rules ---
    from app.services.reranker import rerank
    reranked = await rerank(
        scored, user_id, session,
        device_type=device_type, output_type=output_type,
        collect_actions=debug,
    )
    reranked = reranked[:limit]

    if debug:
        from app.services.reranker import get_last_rerank_actions
        debug_data["reranker_actions"] = get_last_rerank_actions()
        # Build feature vectors for debug output
        from app.services.feature_eng import build_features, FEATURE_COLUMNS
        feat_result_debug = await build_features(
            user_id, [tid for tid, _ in reranked], session,
            hour_of_day=hour_of_day, day_of_week=day_of_week,
            device_type=device_type, output_type=output_type,
            context_type=context_type, location_label=location_label,
        )
        feature_vectors = {}
        for idx, tid in enumerate(feat_result_debug["track_ids"]):
            feature_vectors[tid] = {col: round(float(feat_result_debug["features"][idx][ci]), 4) for ci, col in enumerate(FEATURE_COLUMNS)}
        debug_data["feature_vectors"] = feature_vectors

    # Fetch track metadata for response.
    final_ids = [tid for tid, _ in reranked]
    feat_result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids))
    )
    feat_map = {t.track_id: t for t in feat_result.scalars().all()}

    # Generate a request_id for impression tracking.
    request_id = str(uuid.uuid4())

    # Log reco_impression events for feedback loop (include context for future training).
    now = int(time.time())
    for i, (tid, score) in enumerate(reranked):
        session.add(ListenEvent(
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
        ))
    await session.commit()

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
            track_data.update({
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
            })
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
    result = await session.execute(
        select(User.user_id).where(User.user_id == user_id)
    )
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
        stream_q = (
            select(ListenEvent.request_id, ListenEvent.track_id)
            .where(
                ListenEvent.user_id == user_id,
                ListenEvent.request_id.in_(request_ids),
                ListenEvent.event_type.in_(["play_start", "play_end"]),
            )
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
    "/stats/model",
    summary="Get recommendation model stats and evaluation metrics",
    description="Returns ranker training info, latest offline evaluation metrics, and impression-to-stream stats.",
)
async def get_model_stats(
    _key: str = Depends(require_api_key),
):
    from app.services.evaluation import get_model_report
    return await get_model_report()
