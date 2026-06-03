"""
GrooveIQ – Tests for the Phase-A confidence proxy (Chunk 3).

Synthetic fixtures (a fake embedding index — no DB, no faiss) assert the
intended ordering of mu / sigma / is_proven across the four canonical
scenarios, plus determinism and the key "proven without play-count"
behaviour: a never-heard track inside a cluster of loved tracks can be
proven, while a track played once and skipped cannot.
"""

from __future__ import annotations

import numpy as np

from app.models.algorithm_config_schema import get_defaults
from app.services.confidence import (
    ConfidenceParams,
    InteractionEvidence,
    compute_confidence,
    proven_set,
)

# balanced preset thresholds: proven_mu_min=0.6, proven_sigma_max=0.3
BALANCED = get_defaults().modes.balanced


class _FakeIndex:
    """Minimal EmbeddingIndex: stores L2-normalised vectors in a dict."""

    def __init__(self, embeddings: dict[str, list[float]]):
        self._e: dict[str, np.ndarray] = {}
        for tid, vec in embeddings.items():
            v = np.asarray(vec, dtype=np.float32)
            n = float(np.linalg.norm(v))
            self._e[tid] = (v / n) if n > 0 else v

    def get_embedding(self, track_id: str) -> np.ndarray | None:
        return self._e.get(track_id)


def _loved(**overrides) -> InteractionEvidence:
    base = dict(
        play_count=15,
        full_listen_count=15,
        early_skip_count=0,
        avg_completion=0.95,
        satisfaction_score=0.9,
    )
    base.update(overrides)
    return InteractionEvidence(**base)


def _loved_cluster(n: int, axis: int = 0) -> tuple[dict[str, InteractionEvidence], dict[str, list[float]]]:
    """n loved tracks clustered tightly around a single axis direction."""
    interactions: dict[str, InteractionEvidence] = {}
    embs: dict[str, list[float]] = {}
    for i in range(n):
        vec = [0.0, 0.0, 0.0, 0.0]
        vec[axis] = 1.0
        vec[(axis + 1) % 4] = 0.01 * i  # tiny perturbation -> cosine ~1 within cluster
        interactions[f"L{i}"] = _loved()
        embs[f"L{i}"] = vec
    return interactions, embs


def test_heavily_played_high_completion_is_proven():
    interactions = {
        "T_played": InteractionEvidence(
            play_count=20,
            full_listen_count=20,
            early_skip_count=0,
            avg_completion=0.95,
            satisfaction_score=0.9,
            like_count=2,
        )
    }
    idx = _FakeIndex({"T_played": [1, 0, 0, 0]})

    out = compute_confidence("u", ["T_played"], interactions=interactions, faiss_index=idx, preset=BALANCED)
    s = out["T_played"]

    assert s.mu >= 0.8
    assert s.sigma <= 0.2
    assert s.is_proven is True


def test_unheard_in_loved_cluster_can_be_proven():
    """The key behaviour vs play_count: a never-heard track surrounded by the
    user's loved tracks is proven despite zero personal plays."""
    interactions, embs = _loved_cluster(8)
    embs["C_new"] = [1.0, 0.03, 0.0, 0.0]  # sits inside the loved cluster
    idx = _FakeIndex(embs)

    out = compute_confidence("u", ["C_new"], interactions=interactions, faiss_index=idx, preset=BALANCED)
    s = out["C_new"]

    assert "C_new" not in interactions  # genuinely never heard
    assert s.personal_evidence == 0.0
    assert s.neighbour_evidence > 0.0
    assert s.mu >= 0.6
    assert s.sigma <= 0.3
    assert s.is_proven is True


def test_played_once_then_skipped_low_mu_not_proven():
    interactions = {
        "T_skip": InteractionEvidence(
            play_count=1,
            full_listen_count=0,
            early_skip_count=1,
            avg_completion=0.02,
            satisfaction_score=0.08,
        )
    }
    idx = _FakeIndex({"T_skip": [1, 0, 0, 0]})

    out = compute_confidence("u", ["T_skip"], interactions=interactions, faiss_index=idx, preset=BALANCED)
    s = out["T_skip"]

    assert s.mu < 0.3
    assert s.is_proven is False


def test_isolated_unheard_far_from_cluster_high_sigma_not_proven():
    interactions, embs = _loved_cluster(8)
    embs["C_far"] = [0.0, 0.0, 0.0, 1.0]  # orthogonal to the loved cluster
    idx = _FakeIndex(embs)

    out = compute_confidence("u", ["C_far"], interactions=interactions, faiss_index=idx, preset=BALANCED)
    s = out["C_far"]

    assert s.neighbour_evidence == 0.0
    assert s.sigma >= 0.7
    assert s.is_proven is False


def test_neighbour_density_monotonically_lowers_sigma():
    """More loved neighbours within radius -> stronger evidence -> lower sigma."""
    sparse_int, sparse_emb = _loved_cluster(2)
    sparse_emb["C"] = [1.0, 0.03, 0.0, 0.0]
    dense_int, dense_emb = _loved_cluster(8)
    dense_emb["C"] = [1.0, 0.03, 0.0, 0.0]

    s_sparse = compute_confidence(
        "u", ["C"], interactions=sparse_int, faiss_index=_FakeIndex(sparse_emb), preset=BALANCED
    )["C"]
    s_dense = compute_confidence(
        "u", ["C"], interactions=dense_int, faiss_index=_FakeIndex(dense_emb), preset=BALANCED
    )["C"]

    assert s_dense.neighbour_evidence > s_sparse.neighbour_evidence
    assert s_dense.sigma < s_sparse.sigma


def test_base_score_anchors_mu_but_no_evidence_blocks_proven():
    """A high ranker point estimate raises mu, but with no evidence sigma stays
    high — so the track is NOT proven. Confidence != point estimate."""
    idx = _FakeIndex({"C": [1, 0, 0, 0]})

    out = compute_confidence("u", ["C"], interactions={}, faiss_index=idx, base_scores={"C": 0.95}, preset=BALANCED)
    s = out["C"]

    assert s.mu >= 0.8  # anchored by the model prior
    assert s.sigma >= 0.7  # but unevidenced
    assert s.is_proven is False


def test_unknown_track_with_no_signal_returns_neutral_prior():
    idx = _FakeIndex({})  # no embeddings at all
    params = ConfidenceParams()

    out = compute_confidence("u", ["ghost"], interactions={}, faiss_index=idx, preset=BALANCED, params=params)
    s = out["ghost"]

    assert s.mu == params.neutral_prior_mu
    assert s.evidence == 0.0
    assert s.sigma == 1.0
    assert s.is_proven is False


def test_determinism_same_inputs_same_outputs():
    interactions, embs = _loved_cluster(5)
    embs["C"] = [1.0, 0.02, 0.0, 0.0]
    idx = _FakeIndex(embs)

    a = compute_confidence("u", ["C"], interactions=interactions, faiss_index=idx, preset=BALANCED)["C"]
    b = compute_confidence("u", ["C"], interactions=interactions, faiss_index=idx, preset=BALANCED)["C"]

    assert a == b


def test_preset_defaults_to_balanced_when_none():
    """Omitting `preset` reads the active config's balanced thresholds."""
    interactions = {
        "T": InteractionEvidence(
            play_count=20, full_listen_count=20, early_skip_count=0, avg_completion=0.95, satisfaction_score=0.9
        )
    }
    idx = _FakeIndex({"T": [1, 0, 0, 0]})

    default_preset = compute_confidence("u", ["T"], interactions=interactions, faiss_index=idx)["T"]
    explicit_balanced = compute_confidence("u", ["T"], interactions=interactions, faiss_index=idx, preset=BALANCED)["T"]

    assert default_preset == explicit_balanced
    assert default_preset.is_proven is True


def test_proven_set_helper():
    interactions, embs = _loved_cluster(8)
    embs["C_in"] = [1.0, 0.03, 0.0, 0.0]
    embs["C_out"] = [0.0, 0.0, 0.0, 1.0]
    idx = _FakeIndex(embs)

    scores = compute_confidence("u", ["C_in", "C_out"], interactions=interactions, faiss_index=idx, preset=BALANCED)
    proven = proven_set(scores)

    assert "C_in" in proven
    assert "C_out" not in proven
