"""
GrooveIQ – Phase-A confidence proxy (mu / sigma) for the discovery dial.

The discovery dial is a UCB-style acquisition function over two per-(user,
track) quantities:

  * ``mu``    — predicted engagement: how likely the user is to *not* skip /
                to complete this track.  High ``mu`` = a safe bet.
  * ``sigma`` — uncertainty about ``mu``.  Low ``sigma`` = the estimate is
                well-evidenced and can be trusted.

A track is "proven" when ``mu`` is high *and* ``sigma`` is low — i.e. the
model is confident the user will enjoy it.  Crucially this is **not** the
same as "played before": a never-heard track sitting deep inside a cluster
of the user's loved tracks can be proven, while a track played once and
skipped is not.  That distinction is impossible to express with a raw
play-count and is the whole point of this module.

Phase A (this file) computes both quantities from signals that already
exist — no new model:

  * ``mu``    blends the track's own engagement history (satisfaction score
              + completion vs early-skip ratio) with engagement *inherited*
              from the user's nearest tracks in EffNet embedding space, plus
              an optional ranker base score.
  * ``sigma`` decreases with *evidence density* — the user's own recency-
              weighted plays on the track (``personal_evidence``) plus the
              weighted count of the user's high-``mu`` tracks within an
              embedding radius (``neighbour_evidence``).

Phase B will swap the internals for a calibrated skip-head (``mu``) and
quantile-regression uncertainty (``sigma``) behind this same interface.

The function is pure and deterministic given its inputs: interactions and
the embedding index are injected, there is no global RNG, and the only
time reference (recency) is passed in explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.models.algorithm_config_schema import PresetConfig
from app.services.algorithm_config import get_config

# Seconds per day — recency decay works in day units.
_SECONDS_PER_DAY = 86_400.0


class EmbeddingIndex(Protocol):
    """Minimal embedding-store interface used by the confidence proxy.

    Satisfied by ``app.services.faiss_index.effnet_index`` (and trivially by
    a test fake).  Only ``get_embedding`` is needed — neighbour evidence is
    computed by direct cosine to the user's own tracks, not ANN search, so
    the result is exact and deterministic.
    """

    def get_embedding(self, track_id: str) -> np.ndarray | None: ...


@dataclass(frozen=True)
class InteractionEvidence:
    """The slice of a ``TrackInteraction`` row the confidence proxy needs.

    Decoupled from the ORM so the function stays pure and unit-testable.
    Build one per heard track; ``from_obj`` reads the matching attributes off
    a ``TrackInteraction`` (or any duck-typed object).
    """

    play_count: int = 0
    full_listen_count: int = 0
    early_skip_count: int = 0
    skip_count: int = 0
    like_count: int = 0
    repeat_count: int = 0
    avg_completion: float | None = None
    satisfaction_score: float | None = None
    last_played_at: int | None = None

    @classmethod
    def from_obj(cls, obj: object) -> InteractionEvidence:
        """Build from a ``TrackInteraction`` row (attribute access, no ORM import)."""

        def g(name: str, default):
            val = getattr(obj, name, default)
            return default if val is None else val

        return cls(
            play_count=g("play_count", 0),
            full_listen_count=g("full_listen_count", 0),
            early_skip_count=g("early_skip_count", 0),
            skip_count=g("skip_count", 0),
            like_count=g("like_count", 0),
            repeat_count=g("repeat_count", 0),
            avg_completion=getattr(obj, "avg_completion", None),
            satisfaction_score=getattr(obj, "satisfaction_score", None),
            last_played_at=getattr(obj, "last_played_at", None),
        )


@dataclass(frozen=True)
class ConfidenceParams:
    """Tunable knobs for the Phase-A proxy (code-level; not yet versioned config).

    Defaults are deliberately conservative; Phase B folds the calibrated
    equivalents into the modes config.
    """

    neighbour_radius: float = 0.5
    """Min cosine (in normalised EffNet space) for a track to count as a neighbour."""
    sigma_evidence_scale: float = 0.5
    """Higher -> evidence drives sigma down faster (sigma = 1 / (1 + scale * evidence))."""
    model_prior_weight: float = 1.0
    """Weight of the optional ranker base score in the mu blend (in 'plays' units)."""
    neutral_prior_mu: float = 0.5
    """mu returned when a candidate has no personal, neighbour, or model signal."""
    recency_halflife_days: float = 30.0
    """Half-life for recency-weighting personal evidence (only applied when ``now`` given)."""


@dataclass(frozen=True)
class ConfidenceScore:
    """Per-(user, track) confidence estimate."""

    mu: float
    sigma: float
    is_proven: bool
    evidence: float
    personal_evidence: float
    neighbour_evidence: float


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _personal_mu(iv: InteractionEvidence) -> float | None:
    """Own-history engagement estimate from satisfaction score + completion/skip.

    Returns ``None`` when the track has no usable own-history signal.
    """
    base = iv.satisfaction_score  # already per-user normalised to [0, 1]

    own: float | None = None
    if iv.play_count > 0:
        if iv.avg_completion is not None:
            comp = iv.avg_completion
        else:
            comp = iv.full_listen_count / iv.play_count
        skip_ratio = min(1.0, iv.early_skip_count / iv.play_count)
        own = _clip01(comp - 0.5 * skip_ratio)

    if base is None and own is None:
        return None
    if base is None:
        return own
    if own is None:
        return _clip01(base)
    return _clip01(0.5 * base + 0.5 * own)


def _personal_evidence(iv: InteractionEvidence, now: int | None, halflife_days: float) -> float:
    """Engagement volume on the track, optionally decayed by recency."""
    volume = iv.full_listen_count + 0.5 * iv.play_count + iv.like_count + iv.repeat_count
    if volume <= 0:
        return 0.0
    if now is not None and iv.last_played_at is not None and halflife_days > 0:
        age_days = max(0.0, (now - iv.last_played_at) / _SECONDS_PER_DAY)
        volume *= float(np.exp(-np.log(2.0) * age_days / halflife_days))
    return float(volume)


def _radius_weight(cos: float, radius: float) -> float:
    """Ramp from 0 at ``radius`` to 1 at cosine 1.0; 0 below ``radius``."""
    if cos <= radius:
        return 0.0
    denom = 1.0 - radius
    if denom <= 1e-9:
        return 1.0 if cos >= 1.0 else 0.0
    return _clip01((cos - radius) / denom)


def compute_confidence(
    user_id: str,
    candidate_track_ids: list[str],
    *,
    interactions: dict[str, InteractionEvidence],
    faiss_index: EmbeddingIndex,
    base_scores: dict[str, float] | None = None,
    preset: PresetConfig | None = None,
    now: int | None = None,
    params: ConfidenceParams | None = None,
) -> dict[str, ConfidenceScore]:
    """Compute ``mu`` / ``sigma`` / ``is_proven`` for each candidate track.

    Args:
        user_id: the user the estimate is for (used only for clarity/logging).
        candidate_track_ids: tracks to score.
        interactions: the user's per-track history, keyed by track_id. Tracks
            present here are "heard" and seed the personal + neighbour signals.
        faiss_index: embedding store (e.g. ``faiss_index.effnet_index``).
        base_scores: optional ranker-predicted satisfaction per track (a point
            estimate). Anchors ``mu`` but, having no evidence, does not lower
            ``sigma`` — so a high-``mu`` model guess is not automatically proven.
        preset: thresholds for the proven gate. Defaults to the active config's
            ``balanced`` preset when omitted.
        now: current unix time, for recency-weighting personal evidence. When
            ``None``, no recency decay is applied (keeps tests deterministic).
        params: proxy tunables; defaults used when omitted.

    Returns:
        dict mapping each candidate track_id to its ``ConfidenceScore``.
    """
    p = params or ConfidenceParams()
    preset = preset or get_config().modes.balanced

    # Pre-compute per-heard-track personal mu + embedding for neighbour lookups.
    # Only tracks with both a usable mu and an embedding can lend evidence.
    neighbour_pool: list[tuple[str, float, np.ndarray]] = []
    personal_mu_cache: dict[str, float | None] = {}
    for tid, iv in interactions.items():
        pmu = _personal_mu(iv)
        personal_mu_cache[tid] = pmu
        emb = faiss_index.get_embedding(tid)
        if pmu is not None and emb is not None:
            neighbour_pool.append((tid, pmu, emb))

    results: dict[str, ConfidenceScore] = {}
    for cand in candidate_track_ids:
        iv = interactions.get(cand)

        # Personal signal (own history on this exact track).
        personal_mu = personal_mu_cache.get(cand) if iv is not None else None
        personal_ev = _personal_evidence(iv, now, p.recency_halflife_days) if iv is not None else 0.0

        # Neighbour signal: inherit engagement from the user's nearby tracks.
        neighbour_mu: float | None = None
        neighbour_ev = 0.0
        cand_emb = faiss_index.get_embedding(cand)
        if cand_emb is not None and neighbour_pool:
            num = 0.0
            den = 0.0
            for tid, pmu, emb in neighbour_pool:
                if tid == cand:
                    continue
                cos = float(np.dot(cand_emb, emb))
                w = _radius_weight(cos, p.neighbour_radius)
                if w <= 0.0:
                    continue
                num += w * pmu
                den += w
                # Evidence counts *high-mu* neighbours: a nearby loved track
                # contributes ~its mu; a nearby skipped track contributes ~0.
                neighbour_ev += w * max(pmu, 0.0)
            if den > 0.0:
                neighbour_mu = num / den

        # Optional model prior (ranker point estimate).
        model_mu = base_scores.get(cand) if base_scores else None

        # Blend mu over whatever signals are present, weighting personal and
        # neighbour terms by their evidence and the model term by a fixed prior.
        terms: list[tuple[float, float]] = []
        if model_mu is not None:
            terms.append((p.model_prior_weight, _clip01(float(model_mu))))
        if personal_mu is not None and personal_ev > 0.0:
            terms.append((personal_ev, personal_mu))
        if neighbour_mu is not None and neighbour_ev > 0.0:
            terms.append((neighbour_ev, neighbour_mu))

        if terms:
            total_w = sum(w for w, _ in terms)
            mu = _clip01(sum(w * m for w, m in terms) / total_w)
        else:
            mu = p.neutral_prior_mu

        evidence = personal_ev + neighbour_ev
        sigma = 1.0 / (1.0 + p.sigma_evidence_scale * evidence)
        is_proven = mu >= preset.proven_mu_min and sigma <= preset.proven_sigma_max

        results[cand] = ConfidenceScore(
            mu=mu,
            sigma=sigma,
            is_proven=is_proven,
            evidence=evidence,
            personal_evidence=personal_ev,
            neighbour_evidence=neighbour_ev,
        )

    return results


def proven_set(scores: dict[str, ConfidenceScore]) -> set[str]:
    """Convenience: the set of track_ids whose ``ConfidenceScore.is_proven`` is True."""
    return {tid for tid, s in scores.items() if s.is_proven}


async def load_user_evidence(session, user_id: str, *, limit: int = 500) -> dict[str, InteractionEvidence]:
    """Load a user's per-track interaction evidence, most-played first.

    DB-backed convenience for callers (reranker / candidate_gen) that need to
    seed the neighbour pool and personal signals.  The pure
    :func:`compute_confidence` takes this dict as its ``interactions`` argument,
    so the math itself stays dependency-free and unit-testable.

    Ordered by ``play_count`` (never null) so the most-engaged tracks — the ones
    that anchor the proven set and the neighbour density — are kept under the cap.
    """
    from sqlalchemy import select

    from app.models.db import TrackInteraction

    result = await session.execute(
        select(TrackInteraction)
        .where(TrackInteraction.user_id == user_id)
        .order_by(TrackInteraction.play_count.desc())
        .limit(limit)
    )
    return {row.track_id: InteractionEvidence.from_obj(row) for row in result.scalars().all()}
