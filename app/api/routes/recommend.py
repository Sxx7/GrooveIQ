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

from app.core.security import require_api_key
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
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(require_api_key),
):
    # Verify user exists.
    result = await session.execute(
        select(User.user_id).where(User.user_id == user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")

    # Verify seed track if provided.
    if seed_track_id:
        result = await session.execute(
            select(TrackFeatures.track_id).where(TrackFeatures.track_id == seed_track_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail=f"Seed track '{seed_track_id}' not found.")

    # Generate candidates.
    from app.services.candidate_gen import get_candidates
    candidates = await get_candidates(
        user_id=user_id,
        seed_track_id=seed_track_id,
        k=limit * 3,  # over-fetch for re-ranking
        session=session,
    )

    model_version = _get_model_version()

    if not candidates:
        return {"request_id": str(uuid.uuid4()), "tracks": [], "model_version": model_version}

    candidate_ids = [c["track_id"] for c in candidates]

    # --- Step 1: Score candidates with LightGBM ranker (or fallback) ---
    from app.services.ranker import score_candidates
    scored = await score_candidates(user_id, candidate_ids, session)
    score_map = {tid: score for tid, score in scored}
    source_map = {c["track_id"]: c["source"] for c in candidates}

    # --- Step 2: Rerank for diversity / business rules ---
    from app.services.reranker import rerank
    reranked = await rerank(scored, user_id, session)
    reranked = reranked[:limit]

    # Fetch track metadata for response.
    final_ids = [tid for tid, _ in reranked]
    feat_result = await session.execute(
        select(TrackFeatures).where(TrackFeatures.track_id.in_(final_ids))
    )
    feat_map = {t.track_id: t for t in feat_result.scalars().all()}

    # Generate a request_id for impression tracking.
    request_id = str(uuid.uuid4())

    # Log reco_impression events for feedback loop.
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
                "file_path": tf.file_path,
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

    return {
        "request_id": request_id,
        "model_version": model_version,
        "user_id": user_id,
        "seed_track_id": seed_track_id,
        "tracks": tracks,
    }


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

    # Verify user exists.
    result = await session.execute(
        select(User.user_id).where(User.user_id == user_id)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail=f"User '{user_id}' not found.")

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
                "file_path": feat_map[imp.track_id].file_path if imp.track_id in feat_map else None,
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
