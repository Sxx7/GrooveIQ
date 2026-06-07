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

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.algorithm_config_schema import PRESET_NAMES
from app.models.db import ListenEvent, TrackFeatures, TrackInteraction, shown_impression_clause
from app.services.feature_eng import build_features
from app.services.ranker import get_model_stats as _ranker_stats

logger = logging.getLogger(__name__)

# Cached latest evaluation results.
_last_eval: dict[str, Any] = {}

# Cached latest per-dial-bucket evaluation (Chunk 10).
_last_dial_eval: dict[str, Any] = {}

# Skip vs play event types used for the proven-set skip-rate diagnostic.
_SKIP_EVENT_TYPES: tuple[str, ...] = ("skip",)
_PLAY_EVENT_TYPES: tuple[str, ...] = ("play_start",)


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
            await session.execute(
                select(func.count(ListenEvent.id)).where(
                    ListenEvent.event_type == "reco_impression", shown_impression_clause()
                )
            )
        ).scalar_one()

        if imp_count == 0:
            return {"impressions": 0, "streams": 0, "i2s_rate": None}

        # Get distinct request_ids that have impressions.
        imp_requests = (
            await session.execute(
                select(func.count(func.distinct(ListenEvent.request_id))).where(
                    ListenEvent.event_type == "reco_impression",
                    ListenEvent.request_id.isnot(None),
                    shown_impression_clause(),
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
                        .where(ListenEvent.event_type == "reco_impression", shown_impression_clause())
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


# ---------------------------------------------------------------------------
# Per-dial-bucket list-quality metrics (Chunk 10)
# ---------------------------------------------------------------------------
#
# NDCG measures whether a ranking matches held-out satisfaction, but it cannot
# tell whether the discovery dial is doing its job: a "deep discovery" mix that
# scores fine on held-out plays might still be recycling the same popular
# favourites.  These metrics measure the *shape* of each dial bucket's output —
# how novel, how varied, how much of the catalog it reaches — plus a
# familiar-end sanity check that the user's "proven" set really is low-skip.
#
# All four are pure list functions (unit-tested directly); the orchestrator
# below runs the live pipeline per preset for a bounded user sample and
# aggregates them into per-bucket numbers surfaced on the model-stats endpoint.


def mean_inverse_popularity(track_ids: list[str], popularity: dict[str, int]) -> float | None:
    """Novelty as mean ``1 / (1 + global_play_count)`` over the list.

    A track nobody has played scores 1.0; a heavily played track approaches 0.
    Higher = a more obscure / novel list.  Returns ``None`` for an empty list.
    """
    if not track_ids:
        return None
    return round(float(np.mean([1.0 / (1.0 + max(0, popularity.get(t, 0))) for t in track_ids])), 4)


def pct_never_played(track_ids: list[str], popularity: dict[str, int]) -> float | None:
    """Fraction of the list with zero global plays (genuinely new to everyone)."""
    if not track_ids:
        return None
    return round(sum(1 for t in track_ids if popularity.get(t, 0) <= 0) / len(track_ids), 4)


def intra_list_diversity(track_ids: list[str], embeddings: dict[str, np.ndarray | None]) -> float | None:
    """Mean pairwise cosine *distance* (``1 - cos``) over tracks with embeddings.

    Identical tracks → 0.0; orthogonal → 1.0.  Vectors are L2-normalised here so
    the caller can pass raw embeddings.  Returns ``None`` when fewer than two
    tracks have a usable embedding.
    """
    vecs: list[np.ndarray] = []
    for t in track_ids:
        emb = embeddings.get(t)
        if emb is None:
            continue
        v = np.asarray(emb, dtype=np.float64).ravel()
        norm = float(np.linalg.norm(v))
        if norm < 1e-9:
            continue
        vecs.append(v / norm)
    if len(vecs) < 2:
        return None
    total = 0.0
    pairs = 0
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            total += 1.0 - float(np.dot(vecs[i], vecs[j]))
            pairs += 1
    return round(total / pairs, 4) if pairs else None


def catalog_coverage(recommended_ids: set[str], catalog_size: int) -> float | None:
    """Fraction of the catalog reached by the union of recommendations."""
    if catalog_size <= 0:
        return None
    return round(min(len(recommended_ids), catalog_size) / catalog_size, 4)


def skip_rate_on_set(skips: dict[str, int], plays: dict[str, int], target_ids: set[str]) -> float | None:
    """Skip rate restricted to ``target_ids``: ``skips / (skips + plays)``.

    Returns ``None`` when there is no skip-or-play activity on the set.
    """
    s = sum(skips.get(t, 0) for t in target_ids)
    p = sum(plays.get(t, 0) for t in target_ids)
    denom = s + p
    if denom <= 0:
        return None
    return round(s / denom, 4)


def _avg(values: list[float | None]) -> float | None:
    """Mean of the non-null values, rounded; ``None`` when all are null/empty."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return round(float(np.mean(vals)), 4)


async def _load_dial_eval_context(session, max_users: int) -> tuple[int, dict[str, int], list[str]]:
    """Catalog size, global popularity, and the most-active sample users."""
    catalog_size = (await session.execute(select(func.count()).select_from(TrackFeatures))).scalar() or 0

    pop_rows = (
        await session.execute(
            select(TrackInteraction.track_id, func.sum(TrackInteraction.play_count)).group_by(TrackInteraction.track_id)
        )
    ).all()
    popularity = {tid: int(total or 0) for tid, total in pop_rows}

    user_rows = (
        await session.execute(
            select(TrackInteraction.user_id, func.sum(TrackInteraction.play_count).label("plays"))
            .group_by(TrackInteraction.user_id)
            .order_by(func.sum(TrackInteraction.play_count).desc())
            .limit(max(1, max_users))
        )
    ).all()
    user_ids = [uid for uid, _ in user_rows]
    return int(catalog_size), popularity, user_ids


async def _proven_skip_rate(session, user_ids: list[str], faiss_index, now: int) -> tuple[float | None, int]:
    """Skip rate on each sample user's proven set, aggregated over real events.

    The proven set is computed exactly like the dial does (``confidence`` proxy),
    so this answers "when we call a track *proven*, does the user actually keep
    listening?".  Returns ``(rate, proven_pair_count)``; the rate is ``None`` when
    there is no proven track or no skip/play activity on the proven set.
    """
    from app.services import confidence

    proven_pairs: set[tuple[str, str]] = set()
    for uid in user_ids:
        evidence = await confidence.load_user_evidence(session, uid)
        if not evidence:
            continue
        scores = confidence.compute_confidence(
            uid, list(evidence.keys()), interactions=evidence, faiss_index=faiss_index, now=now
        )
        for tid in confidence.proven_set(scores):
            proven_pairs.add((uid, tid))

    if not proven_pairs:
        return None, 0

    uids = {u for u, _ in proven_pairs}
    tids = {t for _, t in proven_pairs}
    event_rows = (
        await session.execute(
            select(ListenEvent.user_id, ListenEvent.track_id, ListenEvent.event_type).where(
                ListenEvent.user_id.in_(uids),
                ListenEvent.track_id.in_(tids),
                ListenEvent.event_type.in_(list(_SKIP_EVENT_TYPES) + list(_PLAY_EVENT_TYPES)),
            )
        )
    ).all()

    skips: dict[str, int] = {}
    plays: dict[str, int] = {}
    for uid, tid, event_type in event_rows:
        if (uid, tid) not in proven_pairs:
            continue
        if event_type in _SKIP_EVENT_TYPES:
            skips[tid] = skips.get(tid, 0) + 1
        else:
            plays[tid] = plays.get(tid, 0) + 1

    return skip_rate_on_set(skips, plays, tids), len(proven_pairs)


async def _default_generate(user_id: str, dial, limit: int, session) -> list[str]:
    """Run the live candidate-gen → rank → rerank pipeline under the dial override.

    Mirrors ``generate_recommendation_payload`` but returns only the final
    track-id list — the dial overrides are applied around candidate-gen and
    rerank, so this produces the same ordering the serving endpoint would.
    """
    from app.services.candidate_gen import get_candidates
    from app.services.ranker import score_candidates
    from app.services.request_config import apply_overrides
    from app.services.reranker import rerank

    with apply_overrides(dial.overrides):
        candidates = await get_candidates(user_id=user_id, k=max(limit * 4, limit), session=session)
    if not candidates:
        return []
    candidate_ids = [c["track_id"] for c in candidates]
    scored = await score_candidates(user_id, candidate_ids, session)
    if not scored:
        return []
    with apply_overrides(dial.overrides):
        reranked = await rerank(scored, user_id, session)
    return [tid for tid, _ in reranked[:limit]]


async def evaluate_dial_modes(
    *,
    generate=None,
    faiss_index=None,
    user_ids: list[str] | None = None,
    limit: int = 25,
    max_users: int = 8,
    now: int | None = None,
) -> dict[str, Any]:
    """Measure novelty / coverage / diversity for each discovery-dial preset.

    For every named preset it generates a recommendation list for a bounded
    sample of users (read-only — no impressions, no audit), then aggregates the
    list-quality metrics per bucket.  The familiar bucket also carries the
    proven-set skip-rate diagnostic.  The result is cached in ``_last_dial_eval``
    and surfaced via :func:`get_model_report`.

    ``generate`` and ``faiss_index`` are injectable for testing; the defaults
    run the real pipeline and read the live EffNet index.
    """
    from app.services import modes as modes_svc
    from app.services.algorithm_config import get_config

    if faiss_index is None:
        from app.services.faiss_index import effnet_index

        faiss_index = effnet_index
    now = int(time.time()) if now is None else now
    gen = generate or _default_generate
    modes_cfg = get_config().modes

    async with AsyncSessionLocal() as session:
        catalog_size, popularity, discovered_users = await _load_dial_eval_context(session, max_users)
        if user_ids is None:
            user_ids = discovered_users

        if not user_ids or catalog_size <= 0:
            result = {
                "error": "insufficient_data",
                "buckets": {},
                "users_evaluated": len(user_ids or []),
                "catalog_size": catalog_size,
            }
            _set_last_dial_eval(result, now)
            return result

        # Familiar-end diagnostic — independent of which preset generated a list.
        try:
            proven_rate, proven_count = await _proven_skip_rate(session, user_ids, faiss_index, now)
        except Exception:
            logger.warning("proven-set skip-rate computation failed", exc_info=True)
            proven_rate, proven_count = None, 0

        buckets: dict[str, Any] = {}
        for preset_name in PRESET_NAMES:
            dial = modes_svc.resolve_dial(None, preset_name, modes_cfg)
            union_ids: set[str] = set()
            novelty_per_user: list[float | None] = []
            never_played_per_user: list[float | None] = []
            diversity_per_user: list[float | None] = []

            for uid in user_ids:
                try:
                    ranked = await gen(uid, dial, limit, session)
                except Exception:
                    logger.warning("dial generate failed (user=%s, preset=%s)", uid, preset_name, exc_info=True)
                    ranked = []
                ranked = list(ranked)[:limit]
                if not ranked:
                    continue
                union_ids.update(ranked)
                novelty_per_user.append(mean_inverse_popularity(ranked, popularity))
                never_played_per_user.append(pct_never_played(ranked, popularity))
                embs = {t: faiss_index.get_embedding(t) for t in ranked}
                diversity_per_user.append(intra_list_diversity(ranked, embs))

            bucket: dict[str, Any] = {
                "novelty": _avg(novelty_per_user),
                "pct_never_played": _avg(never_played_per_user),
                "intra_list_diversity": _avg(diversity_per_user),
                "catalog_coverage": catalog_coverage(union_ids, catalog_size),
                "tracks_seen": len(union_ids),
                "users_evaluated": len(user_ids),
            }
            if preset_name == "familiar":
                # The proven set is what familiar leans on — surface its skip-rate here.
                bucket["proven_skip_rate"] = proven_rate
                bucket["proven_set_size"] = proven_count
            buckets[preset_name] = bucket

    result = {
        "buckets": buckets,
        "catalog_size": catalog_size,
        "users_evaluated": len(user_ids),
        "limit": limit,
        "proven_skip_rate": proven_rate,
        "proven_set_size": proven_count,
    }
    _set_last_dial_eval(result, now)
    return result


def _set_last_dial_eval(result: dict[str, Any], now: int) -> None:
    global _last_dial_eval
    _last_dial_eval = {**result, "evaluated_at": now}


async def get_dial_mode_report() -> dict[str, Any] | None:
    """Return the per-dial metrics, refreshing them lazily within a TTL.

    Bounded (``RECO_DIAL_EVAL_MAX_USERS``) and admin-only via the calling
    endpoint; a failure never propagates — the report degrades to the last
    cached value (or ``None``).  Set ``RECO_DIAL_EVAL_ENABLED=false`` to disable
    the auto-refresh and only ever serve a previously computed result.
    """
    if not settings.RECO_DIAL_EVAL_ENABLED:
        return _last_dial_eval or None

    now = int(time.time())
    ttl = max(0, settings.RECO_DIAL_EVAL_TTL_MINUTES) * 60
    cached_at = _last_dial_eval.get("evaluated_at", 0) if _last_dial_eval else 0
    if _last_dial_eval and ttl > 0 and (now - cached_at) < ttl:
        return _last_dial_eval

    try:
        await evaluate_dial_modes(
            limit=settings.RECO_DIAL_EVAL_LIMIT,
            max_users=settings.RECO_DIAL_EVAL_MAX_USERS,
            now=now,
        )
    except Exception:
        logger.warning("dial-mode evaluation failed", exc_info=True)

    return _last_dial_eval or None


async def get_model_report() -> dict[str, Any]:
    """Full model report: ranker stats + latest eval + impression + dial metrics."""
    ranker = _ranker_stats()
    impressions = await get_impression_stats()
    dial_modes = await get_dial_mode_report()

    return {
        "ranker": ranker,
        "latest_evaluation": _last_eval if _last_eval else None,
        "impressions": impressions,
        "dial_modes": dial_modes,
    }
