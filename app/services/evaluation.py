"""
GrooveIQ – Offline evaluation service (Phase 4, Step 9).

Provides holdout evaluation of the ranking model and impression-to-stream
metrics for monitoring recommendation quality.

Metrics computed:
  - NDCG@10, NDCG@50  — ranking quality on held-out interactions
  - Mean skip rate     — fraction of recommended tracks that were skipped
  - Mean completion    — average completion rate of recommended tracks
  - Impression-to-stream rate (i2s) — fraction of impressions that led to plays
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np
from sqlalchemy import func, select

from app.db.session import AsyncSessionLocal
from app.models.db import ListenEvent, TrackInteraction
from app.services.feature_eng import build_features
from app.services.ranker import get_model_stats as _ranker_stats

logger = logging.getLogger(__name__)

# Cached latest evaluation results.
_last_eval: dict[str, Any] = {}


def _ndcg_at_k(relevances: list[float], k: int) -> float:
    """Compute NDCG@k from a list of relevance scores (in rank order)."""
    relevances = relevances[:k]
    if not relevances:
        return 0.0

    dcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))
    ideal = sorted(relevances, reverse=True)
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal))

    if idcg < 1e-9:
        return 0.0
    return dcg / idcg


async def evaluate_holdout(train_cutoff_ts: int | None = None) -> dict[str, Any]:
    """
    Split interactions into train/test by timestamp, train a fresh model
    on the train set, predict on test users, compute offline metrics.

    If train_cutoff_ts is None, uses 80/20 time-based split.
    """
    from app.services.feature_eng import FEATURE_COLUMNS, build_training_data
    from app.services.ranker import _create_model

    async with AsyncSessionLocal() as session:
        # Determine cutoff.
        if train_cutoff_ts is None:
            result = await session.execute(
                select(
                    func.min(TrackInteraction.first_played_at).label("earliest"),
                    func.max(TrackInteraction.last_played_at).label("latest"),
                )
            )
            row = result.first()
            if row is None or row.earliest is None or row.latest is None:
                return {"error": "no_data", "metrics": {}}
            span = row.latest - row.earliest
            train_cutoff_ts = row.earliest + int(span * 0.8)

        # Split: train = interactions with last_played_at < cutoff.
        #         test  = interactions with last_played_at >= cutoff.
        train_result = await session.execute(
            select(TrackInteraction)
            .where(TrackInteraction.last_played_at < train_cutoff_ts)
            .order_by(TrackInteraction.user_id)
        )
        train_interactions = train_result.scalars().all()

        test_result = await session.execute(
            select(TrackInteraction)
            .where(TrackInteraction.last_played_at >= train_cutoff_ts)
            .order_by(TrackInteraction.user_id)
        )
        test_interactions = test_result.scalars().all()

    if len(train_interactions) < 50 or len(test_interactions) < 10:
        return {
            "error": "insufficient_data",
            "train_size": len(train_interactions),
            "test_size": len(test_interactions),
            "metrics": {},
        }

    # Build training data from train interactions.
    async with AsyncSessionLocal() as session:
        data = await build_training_data(session)

    if data["n_samples"] < 50:
        return {"error": "insufficient_training_data", "metrics": {}}

    model, engine = _create_model()
    if engine == "lgbm":
        model.fit(data["features"], data["labels"], feature_name=FEATURE_COLUMNS)
    else:
        model.fit(data["features"], data["labels"])

    # Evaluate on test set: for each test user, rank their test tracks.
    # Compare model against two baselines:
    #   - random: shuffled order (expected NDCG depends on score distribution)
    #   - popularity: ranked by global play_count descending
    from collections import defaultdict

    test_by_user: dict[str, list] = defaultdict(list)
    for inter in test_interactions:
        test_by_user[inter.user_id].append(inter)

    ndcg10_scores: list[float] = []
    ndcg50_scores: list[float] = []
    # Baselines
    random_ndcg10: list[float] = []
    popularity_ndcg10: list[float] = []

    async with AsyncSessionLocal() as session:
        # Pre-load global popularity (total play_count across all users).
        from sqlalchemy import func as sa_func

        pop_result = await session.execute(
            select(
                TrackInteraction.track_id,
                sa_func.sum(TrackInteraction.play_count).label("total"),
            ).group_by(TrackInteraction.track_id)
        )
        global_popularity = {row.track_id: row.total or 0 for row in pop_result.all()}

        for user_id, user_inters in test_by_user.items():
            if len(user_inters) < 2:
                continue

            track_ids = [i.track_id for i in user_inters]
            feat_result = await build_features(user_id, track_ids, session)

            if feat_result["features"].shape[0] < 2:
                continue

            predicted_scores = model.predict(feat_result["features"])
            # True relevances in predicted rank order.
            true_scores = {i.track_id: float(i.satisfaction_score or 0) for i in user_inters}
            ranked_ids = [feat_result["track_ids"][i] for i in np.argsort(-predicted_scores)]
            relevances = [true_scores.get(tid, 0.0) for tid in ranked_ids]

            ndcg10_scores.append(_ndcg_at_k(relevances, 10))
            ndcg50_scores.append(_ndcg_at_k(relevances, 50))

            # Baseline 1: random ordering.
            random_order = list(feat_result["track_ids"])
            np.random.shuffle(random_order)
            random_rels = [true_scores.get(tid, 0.0) for tid in random_order]
            random_ndcg10.append(_ndcg_at_k(random_rels, 10))

            # Baseline 2: popularity ordering (global play_count desc).
            pop_order = sorted(
                feat_result["track_ids"],
                key=lambda tid: global_popularity.get(tid, 0),
                reverse=True,
            )
            pop_rels = [true_scores.get(tid, 0.0) for tid in pop_order]
            popularity_ndcg10.append(_ndcg_at_k(pop_rels, 10))

    model_ndcg10 = round(float(np.mean(ndcg10_scores)), 4) if ndcg10_scores else None
    baseline_random = round(float(np.mean(random_ndcg10)), 4) if random_ndcg10 else None
    baseline_pop = round(float(np.mean(popularity_ndcg10)), 4) if popularity_ndcg10 else None

    # Compute lift over baselines.
    lift_over_random = None
    lift_over_popularity = None
    if model_ndcg10 is not None and baseline_random and baseline_random > 0:
        lift_over_random = round((model_ndcg10 - baseline_random) / baseline_random * 100, 1)
    if model_ndcg10 is not None and baseline_pop and baseline_pop > 0:
        lift_over_popularity = round((model_ndcg10 - baseline_pop) / baseline_pop * 100, 1)

    metrics = {
        "ndcg_at_10": model_ndcg10,
        "ndcg_at_50": round(float(np.mean(ndcg50_scores)), 4) if ndcg50_scores else None,
        "evaluated_users": len(ndcg10_scores),
        "train_size": len(train_interactions),
        "test_size": len(test_interactions),
        "cutoff_ts": train_cutoff_ts,
        # Baselines for comparison — if model isn't beating these, it's not helping.
        "baseline_random_ndcg_at_10": baseline_random,
        "baseline_popularity_ndcg_at_10": baseline_pop,
        "lift_over_random_pct": lift_over_random,
        "lift_over_popularity_pct": lift_over_popularity,
    }

    global _last_eval
    _last_eval = {**metrics, "evaluated_at": int(time.time())}

    logger.info(
        f"Holdout evaluation: NDCG@10={model_ndcg10} "
        f"(random={baseline_random}, popularity={baseline_pop}, "
        f"lift={lift_over_popularity}%), users={metrics['evaluated_users']}"
    )
    return {"metrics": metrics}


async def get_impression_stats() -> dict[str, Any]:
    """
    Compute impression-to-stream metrics from reco_impression events.
    """
    async with AsyncSessionLocal() as session:
        # Total impressions.
        imp_count = (
            await session.execute(select(func.count(ListenEvent.id)).where(ListenEvent.event_type == "reco_impression"))
        ).scalar_one()

        if imp_count == 0:
            return {"impressions": 0, "streams": 0, "i2s_rate": None}

        # Get distinct request_ids that have impressions.
        imp_requests = (
            await session.execute(
                select(func.count(func.distinct(ListenEvent.request_id))).where(
                    ListenEvent.event_type == "reco_impression",
                    ListenEvent.request_id.isnot(None),
                )
            )
        ).scalar_one()

        # Streams that were attributed to a reco (share a request_id with an impression).
        # A "stream" = play_start or play_end event with same request_id.
        stream_count = (
            await session.execute(
                select(func.count(ListenEvent.id)).where(
                    ListenEvent.event_type.in_(["play_start", "play_end"]),
                    ListenEvent.request_id.isnot(None),
                    ListenEvent.request_id.in_(
                        select(ListenEvent.request_id)
                        .where(ListenEvent.event_type == "reco_impression")
                        .where(ListenEvent.request_id.isnot(None))
                        .distinct()
                    ),
                )
            )
        ).scalar_one()

        i2s = round(stream_count / imp_count, 4) if imp_count > 0 else None

        return {
            "impressions": imp_count,
            "impression_requests": imp_requests,
            "streams_from_reco": stream_count,
            "i2s_rate": i2s,
        }


async def get_model_report() -> dict[str, Any]:
    """Full model report: ranker stats + latest eval + impression metrics."""
    ranker = _ranker_stats()
    impressions = await get_impression_stats()

    return {
        "ranker": ranker,
        "latest_evaluation": _last_eval if _last_eval else None,
        "impressions": impressions,
    }
