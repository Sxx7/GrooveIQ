"""
GrooveIQ – Candidate generation service (Phase 4).

Merges multiple retrieval sources into a single candidate set for ranking:

  1. Content-based (FAISS) — acoustically similar tracks
  2. Collaborative filtering — "users who liked X also liked Y"
  3. Heuristic recall — recently played artists, popular tracks

Each candidate is tagged with its source for debugging and future
model training (learning which sources contribute most).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.db import TrackFeatures, TrackInteraction, User
from app.services import collab_filter, faiss_index, lastfm_candidates, sasrec, session_embeddings
from app.services.algorithm_config import get_config

logger = logging.getLogger(__name__)


def _get_user_top_track_ids(taste_profile: dict, limit: int = 20) -> list[str]:
    """Extract top track IDs from a user's taste profile."""
    top_tracks = taste_profile.get("top_tracks", [])
    return [t["track_id"] for t in top_tracks[:limit] if "track_id" in t]


async def get_content_candidates(
    seed_track_id: str,
    k: int = 200,
    exclude_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """
    FAISS-based content candidates from a single seed track.

    Returns list of {"track_id": str, "score": float, "source": "content"}.
    """
    results = faiss_index.search_by_track_id(
        seed_track_id,
        k=k,
        exclude_ids=exclude_ids,
    )
    return [{"track_id": tid, "score": score, "source": "content"} for tid, score in results]


async def get_content_candidates_for_user(
    user_id: str,
    k: int = 200,
    exclude_ids: set[str] | None = None,
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """
    FAISS candidates from the user's taste profile centroid.

    Computes the mean embedding of the user's top tracks, then
    queries FAISS for the nearest neighbours to that centroid.
    """
    if session is not None:
        result = await session.execute(select(User.taste_profile).where(User.user_id == user_id))
        row = result.scalar_one_or_none()
    else:
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(User.taste_profile).where(User.user_id == user_id))
            row = result.scalar_one_or_none()

    if not row:
        return []

    top_ids = _get_user_top_track_ids(row)
    if not top_ids:
        return []

    centroid = faiss_index.get_centroid(top_ids)
    if centroid is None:
        return []

    results = faiss_index.search(centroid, k=k, exclude_ids=exclude_ids)
    return [{"track_id": tid, "score": score, "source": "content_profile"} for tid, score in results]


async def _get_disliked_track_ids(user_id: str, session: AsyncSession) -> set[str]:
    """
    Tracks the user has disliked or early-skipped more than 2 times.
    These are filtered out of all candidate sources.
    """
    result = await session.execute(
        select(TrackInteraction.track_id).where(
            TrackInteraction.user_id == user_id,
            (TrackInteraction.dislike_count > 0) | (TrackInteraction.early_skip_count > 2),
        )
    )
    return {row[0] for row in result.all()}


async def _get_recently_skipped_ids(user_id: str, session: AsyncSession) -> set[str]:
    """Tracks the user skipped in the last 24h."""
    cutoff = int(time.time()) - 86_400
    result = await session.execute(
        select(TrackInteraction.track_id).where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.last_played_at >= cutoff,
            TrackInteraction.early_skip_count > 0,
        )
    )
    return {row[0] for row in result.all()}


async def _get_popular_tracks(session: AsyncSession, k: int = 50) -> list[dict[str, Any]]:
    """
    Heuristic recall: tracks with the most interactions in the last 30 days.
    """
    cutoff = int(time.time()) - 30 * 86_400
    result = await session.execute(
        select(
            TrackInteraction.track_id,
            func.sum(TrackInteraction.play_count).label("total_plays"),
        )
        .where(TrackInteraction.last_played_at >= cutoff)
        .group_by(TrackInteraction.track_id)
        .order_by(func.sum(TrackInteraction.play_count).desc())
        .limit(k)
    )
    rows = result.all()

    cfg = get_config().candidate_sources
    return [{"track_id": row.track_id, "score": cfg.popular, "source": "popular"} for row in rows]


async def _get_recently_played_artist_tracks(user_id: str, session: AsyncSession, k: int = 50) -> list[dict[str, Any]]:
    """
    Heuristic recall: tracks from artists the user listened to in the last 7 days.
    Uses file_path heuristic (parent directory = artist/album) since we don't
    store artist metadata separately.
    """
    cutoff = int(time.time()) - 7 * 86_400
    # Get recently interacted track IDs.
    result = await session.execute(
        select(TrackInteraction.track_id)
        .where(
            TrackInteraction.user_id == user_id,
            TrackInteraction.last_played_at >= cutoff,
        )
        .order_by(TrackInteraction.satisfaction_score.desc())
        .limit(20)
    )
    recent_track_ids = [row[0] for row in result.all()]

    if not recent_track_ids:
        return []

    # Get file paths for these tracks to extract artist dirs.
    result = await session.execute(select(TrackFeatures.file_path).where(TrackFeatures.track_id.in_(recent_track_ids)))
    paths = [row[0] for row in result.all() if row[0]]

    if not paths:
        return []

    # Extract parent directories (assumed to be artist or artist/album).
    import os

    artist_dirs = set()
    for p in paths:
        parts = p.split(os.sep)
        if len(parts) >= 2:
            artist_dirs.add(parts[-2])  # album or artist directory

    if not artist_dirs:
        return []

    # Find other tracks in the same directories.
    conditions = [TrackFeatures.file_path.contains(d) for d in list(artist_dirs)[:10]]
    from sqlalchemy import or_

    result = await session.execute(
        select(TrackFeatures.track_id)
        .where(
            or_(*conditions),
            TrackFeatures.track_id.notin_(recent_track_ids),
        )
        .limit(k)
    )
    tracks = [row[0] for row in result.all()]

    cfg = get_config().candidate_sources
    return [{"track_id": tid, "score": cfg.artist_recall, "source": "artist_recall"} for tid in tracks]


async def get_candidates(
    user_id: str,
    seed_track_id: str | None = None,
    k: int = 200,
    *,
    session: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """
    Merged candidate retrieval from all sources.

    Args:
        user_id: the user to generate candidates for.
        seed_track_id: optional seed track for content-based retrieval.
        k: max number of candidates to return.
        session: existing DB session (required for SQLite to avoid pool exhaustion).

    Returns:
        List of {"track_id", "score", "source"} dicts, deduplicated,
        with disliked/skipped tracks removed.
    """
    if session is None:
        async with AsyncSessionLocal() as session:
            return await _get_candidates_impl(user_id, seed_track_id, k, session)
    return await _get_candidates_impl(user_id, seed_track_id, k, session)


async def _get_candidates_impl(
    user_id: str,
    seed_track_id: str | None,
    k: int,
    session: AsyncSession,
) -> list[dict[str, Any]]:
    cfg = get_config().candidate_sources

    # Build exclusion set.
    disliked = await _get_disliked_track_ids(user_id, session)
    recently_skipped = await _get_recently_skipped_ids(user_id, session)
    exclude = disliked | recently_skipped

    # Source 1: Content-based (FAISS).
    content_candidates: list[dict[str, Any]] = []
    if faiss_index.is_ready():
        if seed_track_id:
            raw = await get_content_candidates(
                seed_track_id,
                k=100,
                exclude_ids=exclude,
            )
            content_candidates = [
                {"track_id": c["track_id"], "score": c["score"] * cfg.content, "source": "content"} for c in raw
            ]
        else:
            raw = await get_content_candidates_for_user(
                user_id,
                k=100,
                exclude_ids=exclude,
                session=session,
            )
            content_candidates = [
                {"track_id": c["track_id"], "score": c["score"] * cfg.content_profile, "source": "content_profile"}
                for c in raw
            ]

    # Source 2: Collaborative filtering.
    cf_candidates: list[dict[str, Any]] = []
    if collab_filter.is_ready():
        raw = collab_filter.get_cf_candidates(user_id, k=100)
        cf_candidates = [
            {"track_id": tid, "score": score * cfg.cf, "source": "cf"} for tid, score in raw if tid not in exclude
        ]

    # Source 3: Session skip-gram embeddings (behavioral co-occurrence).
    session_emb_candidates: list[dict[str, Any]] = []
    if session_embeddings.is_ready():
        if seed_track_id:
            raw = session_embeddings.get_similar_tracks(
                seed_track_id,
                k=100,
                exclude_ids=exclude,
            )
            session_emb_candidates = [
                {"track_id": tid, "score": score * cfg.session_skipgram, "source": "session_skipgram"}
                for tid, score in raw
            ]
        else:
            # Use user's top tracks as centroid for session-based retrieval.
            user_result = await session.execute(select(User.taste_profile).where(User.user_id == user_id))
            tp = user_result.scalar_one_or_none()
            if tp:
                top_ids = _get_user_top_track_ids(tp, limit=20)
                if top_ids:
                    raw = session_embeddings.get_similar_to_tracks(
                        top_ids,
                        k=100,
                        exclude_ids=exclude,
                    )
                    session_emb_candidates = [
                        {"track_id": tid, "score": score * cfg.session_skipgram, "source": "session_skipgram"}
                        for tid, score in raw
                    ]

    # Source 4: Last.fm similar tracks (external CF from millions of users).
    lastfm_sim_candidates: list[dict[str, Any]] = []
    if lastfm_candidates.is_ready():
        if seed_track_id:
            raw = lastfm_candidates.get_similar_for_track(
                seed_track_id,
                k=100,
                exclude_ids=exclude,
            )
        else:
            # Use user's top tracks for merged similar-track retrieval.
            user_result = await session.execute(select(User.taste_profile).where(User.user_id == user_id))
            tp = user_result.scalar_one_or_none()
            top_ids = _get_user_top_track_ids(tp, limit=20) if tp else []
            raw = (
                lastfm_candidates.get_similar_for_user(
                    top_ids,
                    k=100,
                    exclude_ids=exclude,
                )
                if top_ids
                else []
            )

        lastfm_sim_candidates = [
            {"track_id": tid, "score": score * cfg.lastfm_similar, "source": "lastfm_similar"}
            for tid, score in raw
            if tid not in exclude
        ]

    # Source 5: SASRec sequential predictions (transformer next-track).
    sasrec_candidates: list[dict[str, Any]] = []
    if sasrec.is_ready():
        raw = sasrec.get_top_predictions(user_id, k=100, exclude_ids=exclude)
        sasrec_candidates = [
            {"track_id": tid, "score": max(score * cfg.sasrec, 0.1), "source": "sasrec"}
            for tid, score in raw
            if tid not in exclude
        ]

    # Source 6: Heuristic recall.
    popular = await _get_popular_tracks(session, k=50)
    artist_recall = await _get_recently_played_artist_tracks(user_id, session, k=50)

    # Merge and deduplicate (first occurrence wins — preserves source priority).
    seen: set[str] = set()
    merged: list[dict[str, Any]] = []

    for candidate_list in [
        content_candidates,
        cf_candidates,
        session_emb_candidates,
        sasrec_candidates,
        lastfm_sim_candidates,
        artist_recall,
        popular,
    ]:
        for c in candidate_list:
            tid = c["track_id"]
            if tid in seen or tid in exclude:
                continue
            seen.add(tid)
            merged.append(c)

    # Sort by score descending, return up to k.
    merged.sort(key=lambda c: c["score"], reverse=True)
    return merged[:k]
