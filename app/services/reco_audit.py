"""
GrooveIQ – Recommendation audit & replay service.

Persists every /v1/recommend (and radio batch) request along with the
candidate pool, ranker scores, reranker actions, and feature vectors.
Powers the dashboard "Audit" sub-tab and the offline replay endpoint
that re-scores past requests with the current ranker / config.

Writes are fire-and-forget from the caller's perspective: the route
hands us a complete payload dict, we open our own session and persist.
This keeps the recommend endpoint's hot path free of audit latency.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.db import (
    RecommendationCandidateAudit,
    RecommendationRequestAudit,
    TrackFeatures,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


async def write_audit(
    *,
    request_id: str,
    user_id: str,
    surface: str,
    seed_track_id: str | None,
    context_id: str | None,
    request_context: dict[str, Any],
    model_version: str,
    config_version: int,
    duration_ms: int,
    limit_requested: int,
    candidates_by_source: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
    session: AsyncSession | None = None,
) -> bool:
    """Persist one request audit + its candidates.

    Idempotent: if a request_id row already exists, this is a no-op.
    Returns True if a new audit was written.

    When ``session`` is None, opens its own session via AsyncSessionLocal —
    this is the fire-and-forget path used by the recommend route.
    """
    if not settings.RECO_AUDIT_ENABLED:
        return False

    if session is None:
        async with AsyncSessionLocal() as own_session:
            try:
                wrote = await _write_audit_inner(
                    own_session,
                    request_id=request_id,
                    user_id=user_id,
                    surface=surface,
                    seed_track_id=seed_track_id,
                    context_id=context_id,
                    request_context=request_context,
                    model_version=model_version,
                    config_version=config_version,
                    duration_ms=duration_ms,
                    limit_requested=limit_requested,
                    candidates_by_source=candidates_by_source,
                    candidate_rows=candidate_rows,
                )
                await own_session.commit()
                return wrote
            except Exception:
                await own_session.rollback()
                logger.exception("reco_audit.write_audit failed (request_id=%s)", request_id)
                return False
    else:
        return await _write_audit_inner(
            session,
            request_id=request_id,
            user_id=user_id,
            surface=surface,
            seed_track_id=seed_track_id,
            context_id=context_id,
            request_context=request_context,
            model_version=model_version,
            config_version=config_version,
            duration_ms=duration_ms,
            limit_requested=limit_requested,
            candidates_by_source=candidates_by_source,
            candidate_rows=candidate_rows,
        )


async def _write_audit_inner(
    session: AsyncSession,
    *,
    request_id: str,
    user_id: str,
    surface: str,
    seed_track_id: str | None,
    context_id: str | None,
    request_context: dict[str, Any],
    model_version: str,
    config_version: int,
    duration_ms: int,
    limit_requested: int,
    candidates_by_source: dict[str, Any],
    candidate_rows: list[dict[str, Any]],
) -> bool:
    # Idempotency: skip if already persisted.
    existing = await session.execute(
        select(RecommendationRequestAudit.request_id).where(RecommendationRequestAudit.request_id == request_id)
    )
    if existing.scalar_one_or_none() is not None:
        return False

    cap = settings.RECO_AUDIT_MAX_CANDIDATES
    sorted_rows = sorted(candidate_rows, key=lambda r: r.get("raw_score") or 0.0, reverse=True)
    persisted_rows = sorted_rows[:cap]

    request_row = RecommendationRequestAudit(
        request_id=request_id,
        user_id=user_id,
        created_at=int(time.time()),
        surface=surface,
        seed_track_id=seed_track_id,
        context_id=context_id,
        model_version=model_version,
        config_version=config_version,
        request_context=request_context or {},
        candidates_total=len(candidate_rows),
        candidates_by_source=candidates_by_source or {},
        duration_ms=duration_ms,
        limit_requested=limit_requested,
    )
    session.add(request_row)

    for row in persisted_rows:
        session.add(
            RecommendationCandidateAudit(
                request_id=request_id,
                track_id=row["track_id"],
                sources=row.get("sources") or [],
                raw_score=float(row.get("raw_score") or 0.0),
                pre_rerank_position=int(row.get("pre_rerank_position", -1)),
                final_score=row.get("final_score"),
                final_position=row.get("final_position"),
                shown=bool(row.get("shown", False)),
                reranker_actions=row.get("reranker_actions") or [],
                feature_vector=row.get("feature_vector") or {},
            )
        )
    return True


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_requests(
    session: AsyncSession,
    user_id: str | None = None,
    *,
    surface: str | None = None,
    limit: int = 50,
    offset: int = 0,
    since: int | None = None,
) -> list[dict[str, Any]]:
    """Paginated list of recent audit summaries."""
    q = select(RecommendationRequestAudit)
    if user_id:
        q = q.where(RecommendationRequestAudit.user_id == user_id)
    if surface:
        q = q.where(RecommendationRequestAudit.surface == surface)
    if since is not None:
        q = q.where(RecommendationRequestAudit.created_at >= since)
    q = q.order_by(RecommendationRequestAudit.created_at.desc()).limit(limit).offset(offset)
    rows = (await session.execute(q)).scalars().all()
    if not rows:
        return []

    # Look up "top track" (final_position=0) for each request — single batched query.
    request_ids = [r.request_id for r in rows]
    top_q = await session.execute(
        select(
            RecommendationCandidateAudit.request_id,
            RecommendationCandidateAudit.track_id,
        ).where(
            RecommendationCandidateAudit.request_id.in_(request_ids),
            RecommendationCandidateAudit.final_position == 0,
        )
    )
    top_track_ids: dict[str, str] = {r.request_id: r.track_id for r in top_q.all()}

    # Fetch metadata for those track_ids in one shot.
    track_ids = list(set(top_track_ids.values()))
    feat_map: dict[str, TrackFeatures] = {}
    if track_ids:
        feat_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(track_ids)))
        feat_map = {t.track_id: t for t in feat_q.scalars().all()}

    out: list[dict[str, Any]] = []
    for r in rows:
        top_id = top_track_ids.get(r.request_id)
        top_track = None
        if top_id:
            tf = feat_map.get(top_id)
            top_track = {
                "track_id": top_id,
                "title": tf.title if tf else None,
                "artist": tf.artist if tf else None,
            }
        out.append(
            {
                "request_id": r.request_id,
                "user_id": r.user_id,
                "created_at": r.created_at,
                "surface": r.surface,
                "seed_track_id": r.seed_track_id,
                "context_id": r.context_id,
                "model_version": r.model_version,
                "config_version": r.config_version,
                "candidates_total": r.candidates_total,
                "candidates_by_source": r.candidates_by_source or {},
                "duration_ms": r.duration_ms,
                "limit_requested": r.limit_requested,
                "top_track": top_track,
            }
        )
    return out


async def get_request(
    session: AsyncSession,
    request_id: str,
) -> dict[str, Any] | None:
    """Return full request audit + every persisted candidate (sorted by final_position)."""
    q = (
        select(RecommendationRequestAudit)
        .where(RecommendationRequestAudit.request_id == request_id)
        .options(selectinload(RecommendationRequestAudit.candidates))
    )
    row = (await session.execute(q)).scalar_one_or_none()
    if row is None:
        return None

    # Enrich candidates with track metadata.
    cand_track_ids = list({c.track_id for c in row.candidates})
    feat_map: dict[str, TrackFeatures] = {}
    if cand_track_ids:
        feat_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id.in_(cand_track_ids)))
        feat_map = {t.track_id: t for t in feat_q.scalars().all()}

    def _sort_key(c: RecommendationCandidateAudit) -> tuple[int, int, float]:
        # Shown tracks first, then by final_position, then by pre_rerank_position.
        # final_position=None goes to the bottom.
        fp = c.final_position if c.final_position is not None else 9_999_999
        return (0 if c.shown else 1, fp, -float(c.raw_score or 0.0))

    candidates_sorted = sorted(row.candidates, key=_sort_key)
    candidates_out: list[dict[str, Any]] = []
    for c in candidates_sorted:
        tf = feat_map.get(c.track_id)
        candidates_out.append(
            {
                "track_id": c.track_id,
                "sources": c.sources or [],
                "raw_score": float(c.raw_score or 0.0),
                "pre_rerank_position": c.pre_rerank_position,
                "final_score": float(c.final_score) if c.final_score is not None else None,
                "final_position": c.final_position,
                "shown": bool(c.shown),
                "reranker_actions": c.reranker_actions or [],
                "feature_vector": c.feature_vector or {},
                "title": tf.title if tf else None,
                "artist": tf.artist if tf else None,
            }
        )

    return {
        "request_id": row.request_id,
        "user_id": row.user_id,
        "created_at": row.created_at,
        "surface": row.surface,
        "seed_track_id": row.seed_track_id,
        "context_id": row.context_id,
        "model_version": row.model_version,
        "config_version": row.config_version,
        "request_context": row.request_context or {},
        "candidates_total": row.candidates_total,
        "candidates_by_source": row.candidates_by_source or {},
        "duration_ms": row.duration_ms,
        "limit_requested": row.limit_requested,
        "candidates": candidates_out,
    }


async def get_candidate(
    session: AsyncSession,
    request_id: str,
    track_id: str,
) -> dict[str, Any] | None:
    """Single candidate's full audit (feature vector, sources, reranker actions)."""
    q = select(RecommendationCandidateAudit).where(
        RecommendationCandidateAudit.request_id == request_id,
        RecommendationCandidateAudit.track_id == track_id,
    )
    c = (await session.execute(q)).scalar_one_or_none()
    if c is None:
        return None

    tf_q = await session.execute(select(TrackFeatures).where(TrackFeatures.track_id == track_id))
    tf = tf_q.scalar_one_or_none()

    return {
        "track_id": c.track_id,
        "sources": c.sources or [],
        "raw_score": float(c.raw_score or 0.0),
        "pre_rerank_position": c.pre_rerank_position,
        "final_score": float(c.final_score) if c.final_score is not None else None,
        "final_position": c.final_position,
        "shown": bool(c.shown),
        "reranker_actions": c.reranker_actions or [],
        "feature_vector": c.feature_vector or {},
        "title": tf.title if tf else None,
        "artist": tf.artist if tf else None,
    }


async def get_stats(session: AsyncSession) -> dict[str, Any]:
    """Aggregate stats for the audit dashboard panel."""
    cutoff_30d = int(time.time()) - 30 * 86_400
    req_count_q = await session.execute(
        select(func.count())
        .select_from(RecommendationRequestAudit)
        .where(RecommendationRequestAudit.created_at >= cutoff_30d)
    )
    cand_count_q = await session.execute(select(func.count()).select_from(RecommendationCandidateAudit))
    total_req_q = await session.execute(select(func.count()).select_from(RecommendationRequestAudit))

    req_30d = req_count_q.scalar() or 0
    total_cand = cand_count_q.scalar() or 0
    total_req = total_req_q.scalar() or 0

    # Rough storage estimate: ~5 KB per candidate row + ~1 KB per request.
    storage_bytes = total_cand * 5_000 + total_req * 1_000

    return {
        "total_requests_30d": req_30d,
        "total_requests_all": total_req,
        "total_candidates_all": total_cand,
        "storage_bytes_estimate": storage_bytes,
        "retention_days": settings.RECO_AUDIT_RETENTION_DAYS,
        "max_candidates_per_request": settings.RECO_AUDIT_MAX_CANDIDATES,
        "enabled": settings.RECO_AUDIT_ENABLED,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


async def purge_old(session: AsyncSession, retention_days: int) -> int:
    """Delete audits older than retention_days. Returns rows deleted."""
    cutoff = int(time.time()) - retention_days * 86_400
    # Candidates are removed by FK ON DELETE CASCADE.
    result = await session.execute(
        delete(RecommendationRequestAudit).where(RecommendationRequestAudit.created_at < cutoff)
    )
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


async def replay_request(
    session: AsyncSession,
    request_id: str,
    mode: str = "rerank_only",
) -> dict[str, Any] | None:
    """
    Re-rank a persisted request's candidate pool with the current ranker
    + reranker config and return rank deltas vs the original ranking.

    rerank_only: re-score persisted feature vectors with the current model,
                 then re-rerank.  Cheap; answers "did the latest tuning help?".
    full:        reconstruct the original request context and re-run the
                 full pipeline (candidate gen → features → rank → rerank)
                 with current models and config.

    Pure read — does not persist anything.
    """
    from app.services.algorithm_config import get_config_version
    from app.services.feature_eng import FEATURE_COLUMNS, build_features
    from app.services.ranker import get_model_version, score_candidates
    from app.services.reranker import rerank

    if mode not in ("rerank_only", "full"):
        raise ValueError("mode must be 'rerank_only' or 'full'")

    detail = await get_request(session, request_id)
    if detail is None:
        return None

    user_id = detail["user_id"]
    request_context = detail["request_context"] or {}
    original_candidates = detail["candidates"]

    # Build {track_id: original_position} from the persisted ranking.
    original_positions: dict[str, int | None] = {c["track_id"]: c["final_position"] for c in original_candidates}
    original_scores: dict[str, float | None] = {c["track_id"]: c["final_score"] for c in original_candidates}
    titles: dict[str, str | None] = {c["track_id"]: c.get("title") for c in original_candidates}
    artists: dict[str, str | None] = {c["track_id"]: c.get("artist") for c in original_candidates}

    if mode == "rerank_only":
        # Re-score using persisted feature vectors with the current model.
        # If no model is loaded, this falls back to satisfaction_score —
        # same as the live serve path.
        import numpy as np

        from app.services.ranker import _model

        track_ids = [c["track_id"] for c in original_candidates]
        # Reconstruct feature matrix from persisted dicts in FEATURE_COLUMNS order.
        feature_rows = []
        valid_track_ids: list[str] = []
        for c in original_candidates:
            fv = c.get("feature_vector") or {}
            if not fv:
                continue
            row = [float(fv.get(col, 0.0) or 0.0) for col in FEATURE_COLUMNS]
            feature_rows.append(row)
            valid_track_ids.append(c["track_id"])

        if not feature_rows:
            return {
                "request_id": request_id,
                "mode": mode,
                "original_model_version": detail["model_version"],
                "original_config_version": detail["config_version"],
                "new_model_version": get_model_version() or detail["model_version"],
                "new_config_version": get_config_version(),
                "rank_deltas": [],
                "summary": {
                    "avg_abs_delta": 0.0,
                    "top10_overlap": 0.0,
                    "kendall_tau": None,
                    "new_top10_tracks": [],
                    "dropped_top10_tracks": [],
                    "candidates_compared": 0,
                },
            }

        features = np.asarray(feature_rows, dtype=np.float32)
        if _model is not None:
            scores = _model.predict(features).tolist()
        else:
            sat_idx = FEATURE_COLUMNS.index("satisfaction_score")
            scores = features[:, sat_idx].tolist()

        scored = list(zip(valid_track_ids, scores))
        scored.sort(key=lambda x: x[1], reverse=True)
    else:
        # Full replay: rebuild feature vectors live and re-score with the current model.
        track_ids = [c["track_id"] for c in original_candidates]
        feat_result = await build_features(
            user_id,
            track_ids,
            session,
            hour_of_day=request_context.get("hour_of_day"),
            day_of_week=request_context.get("day_of_week"),
            device_type=request_context.get("device_type"),
            output_type=request_context.get("output_type"),
            context_type=request_context.get("context_type"),
            location_label=request_context.get("location_label"),
        )
        scored = await score_candidates(
            user_id,
            feat_result["track_ids"],
            session,
            hour_of_day=request_context.get("hour_of_day"),
            day_of_week=request_context.get("day_of_week"),
            device_type=request_context.get("device_type"),
            output_type=request_context.get("output_type"),
            context_type=request_context.get("context_type"),
            location_label=request_context.get("location_label"),
        )

    # Re-rerank with current rules and limit.
    reranked = await rerank(
        scored,
        user_id,
        session,
        device_type=request_context.get("device_type"),
        output_type=request_context.get("output_type"),
    )
    limit_requested = detail["limit_requested"] or 25
    reranked = reranked[:limit_requested]

    new_positions: dict[str, int] = {tid: i for i, (tid, _) in enumerate(reranked)}
    new_scores: dict[str, float] = {tid: float(score) for tid, score in reranked}

    # Build rank deltas for every candidate considered, sorted by best of
    # (original_position, new_position).
    all_track_ids = set(original_positions.keys()) | set(new_positions.keys())
    deltas: list[dict[str, Any]] = []
    for tid in all_track_ids:
        op = original_positions.get(tid)
        np_ = new_positions.get(tid)
        delta = None
        if op is not None and np_ is not None:
            delta = op - np_
        deltas.append(
            {
                "track_id": tid,
                "title": titles.get(tid),
                "artist": artists.get(tid),
                "original_position": op,
                "new_position": np_,
                "delta": delta,
                "original_score": original_scores.get(tid),
                "new_score": new_scores.get(tid),
            }
        )

    # Sort: shown-in-either-ranking first, then by min(positions).
    def _sort_key(d: dict[str, Any]) -> tuple[int, int]:
        op = d["original_position"]
        np_ = d["new_position"]
        if op is None and np_ is None:
            return (2, 9_999_999)
        if op is None:
            return (1, np_)
        if np_ is None:
            return (1, op)
        return (0, min(op, np_))

    deltas.sort(key=_sort_key)

    # Summary metrics.
    paired_deltas = [d["delta"] for d in deltas if d["delta"] is not None]
    avg_abs_delta = sum(abs(x) for x in paired_deltas) / len(paired_deltas) if paired_deltas else 0.0

    original_top10 = {tid for tid, op in original_positions.items() if op is not None and op < 10}
    new_top10 = {tid for tid, np_ in new_positions.items() if np_ < 10}
    overlap = len(original_top10 & new_top10) / 10.0 if (original_top10 or new_top10) else 0.0
    new_in_top10 = sorted(new_top10 - original_top10)
    dropped_from_top10 = sorted(original_top10 - new_top10)

    # Kendall's tau on the intersection — pairwise concordance of rankings.
    kendall_tau = _kendall_tau(original_positions, new_positions)

    return {
        "request_id": request_id,
        "mode": mode,
        "original_model_version": detail["model_version"],
        "original_config_version": detail["config_version"],
        "new_model_version": get_model_version() or detail["model_version"],
        "new_config_version": get_config_version(),
        "rank_deltas": deltas,
        "summary": {
            "avg_abs_delta": round(avg_abs_delta, 3),
            "top10_overlap": round(overlap, 3),
            "kendall_tau": kendall_tau,
            "new_top10_tracks": new_in_top10,
            "dropped_top10_tracks": dropped_from_top10,
            "candidates_compared": len(paired_deltas),
        },
    }


def _kendall_tau(
    original_positions: dict[str, int | None],
    new_positions: dict[str, int],
) -> float | None:
    """Kendall's tau over the intersection of tracks ranked in both lists."""
    intersect = [tid for tid, op in original_positions.items() if op is not None and tid in new_positions]
    n = len(intersect)
    if n < 2:
        return None
    concordant = 0
    discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            ti, tj = intersect[i], intersect[j]
            oi, oj = original_positions[ti], original_positions[tj]
            ni, nj = new_positions[ti], new_positions[tj]
            if (oi - oj) * (ni - nj) > 0:
                concordant += 1
            elif (oi - oj) * (ni - nj) < 0:
                discordant += 1
    total_pairs = n * (n - 1) / 2
    if total_pairs == 0:
        return None
    return round((concordant - discordant) / total_pairs, 4)
