"""
GrooveIQ — Tests for CLAP-derived mood scores (#98).

These tests stub out the CLAP text encoder so they don't require the
~50 MB ONNX model on disk. The text encoder is exercised by the
existing test_clap_setup.py / live tests on the dev server.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from app.services import clap_mood


@pytest.fixture(autouse=True)
def _reset_label_cache():
    """Each test starts with a fresh label-embedding cache."""
    clap_mood._reset_for_tests()
    yield
    clap_mood._reset_for_tests()


def _stub_label_embeddings(monkeypatch, embeddings: dict[str, np.ndarray] | None) -> None:
    """Bypass the real CLAP text encoder by injecting label embeddings directly."""

    def _fake_get():
        return embeddings

    monkeypatch.setattr(clap_mood, "_get_label_embeddings", _fake_get)


def _unit_vectors(dim: int = 512) -> dict[str, np.ndarray]:
    """Five mutually-orthogonal-ish unit vectors, one per mood label.

    Each label's vector has a 1.0 in a distinct component and zeros elsewhere,
    then is L2-normalised (already unit-norm). Lets us craft an audio
    embedding that scores arbitrarily high on any chosen label.
    """
    out = {}
    for i, label in enumerate(clap_mood._MOOD_LABELS):
        v = np.zeros(dim, dtype=np.float32)
        v[i] = 1.0
        out[label] = v
    return out


# ---------------------------------------------------------------------------
# Score mapping
# ---------------------------------------------------------------------------


class TestComputeMoodScores:
    def test_perfect_match_scores_one(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # Audio identical to the "happy" label vector → cosine 1.0 → score 1.0.
        scores = clap_mood.compute_mood_scores_from_clap(labels["happy"])
        assert scores is not None
        assert scores["happy"] == 1.0
        # Orthogonal labels score 0.5 (cosine 0.0 → (0+1)/2).
        assert scores["sad"] == 0.5
        assert scores["aggressive"] == 0.5
        assert scores["relaxed"] == 0.5
        assert scores["party"] == 0.5

    def test_anti_correlated_scores_zero(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # Audio that's the negation of the happy vector → cosine -1.0 → score 0.
        anti_happy = -labels["happy"]
        scores = clap_mood.compute_mood_scores_from_clap(anti_happy)
        assert scores is not None
        assert scores["happy"] == 0.0
        # Orthogonal labels still 0.5.
        assert scores["sad"] == 0.5

    def test_partial_match_maps_linearly(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # 0.5 along happy axis, rest from another orthogonal axis →
        # cosine 0.5 with happy → score 0.75. Build from scratch so the
        # vector is unit-norm regardless of which dim we pick.
        v = np.zeros(512, dtype=np.float32)
        v[0] = 0.5  # happy axis
        v[10] = float(np.sqrt(1 - 0.25))  # orthogonal-to-everything-we-care-about
        scores = clap_mood.compute_mood_scores_from_clap(v)
        assert scores is not None
        # cosine(audio, happy) = 0.5 → (0.5 + 1) / 2 = 0.75
        assert scores["happy"] == 0.75

    def test_returns_none_on_empty_or_zero_audio(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        assert clap_mood.compute_mood_scores_from_clap(np.array([], dtype=np.float32)) is None
        assert clap_mood.compute_mood_scores_from_clap(np.zeros(512, dtype=np.float32)) is None
        assert clap_mood.compute_mood_scores_from_clap(None) is None

    def test_returns_none_on_dim_mismatch(self, monkeypatch):
        labels = _unit_vectors(dim=512)
        _stub_label_embeddings(monkeypatch, labels)

        # 256-dim audio but labels are 512-dim → can't compute, return None.
        assert clap_mood.compute_mood_scores_from_clap(np.ones(256, dtype=np.float32) / 16) is None

    def test_returns_none_when_label_embeddings_unavailable(self, monkeypatch):
        _stub_label_embeddings(monkeypatch, None)
        assert clap_mood.compute_mood_scores_from_clap(np.ones(512, dtype=np.float32) / np.sqrt(512)) is None

    def test_normalises_non_unit_audio_defensively(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # 5x the happy vector → still cosine 1.0 with happy after normalisation.
        scores = clap_mood.compute_mood_scores_from_clap(labels["happy"] * 5.0)
        assert scores is not None
        assert scores["happy"] == 1.0

    def test_scores_rounded_to_three_decimals(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        v = np.zeros(512, dtype=np.float32)
        v[0] = 0.333  # happy axis
        v[10] = float(np.sqrt(1 - 0.333**2))
        scores = clap_mood.compute_mood_scores_from_clap(v)
        assert scores is not None
        for label, score in scores.items():
            assert isinstance(score, float)
            assert round(score, 3) == score, (label, score)


# ---------------------------------------------------------------------------
# Valence composition
# ---------------------------------------------------------------------------


class TestDeriveValence:
    def test_neutral_lands_near_half(self):
        # All labels at 0.5 (orthogonal audio) →
        # 0.4*0.5 + 0.3*0.5 + 0.2*0.5 + 0.1*0.5 = 0.5
        scores = {"happy": 0.5, "sad": 0.5, "aggressive": 0.5, "relaxed": 0.5, "party": 0.5}
        assert clap_mood._derive_valence(scores) == 0.5

    def test_all_positive_caps_at_one(self):
        # happy=party=1, sad=aggressive=0 → 0.4+0.3+0.2+0.1 = 1.0
        scores = {"happy": 1.0, "sad": 0.0, "aggressive": 0.0, "relaxed": 0.5, "party": 1.0}
        assert clap_mood._derive_valence(scores) == 1.0

    def test_all_negative_floors_at_zero(self):
        # happy=party=0, sad=aggressive=1 → 0 + 0 + 0 + 0 = 0
        scores = {"happy": 0.0, "sad": 1.0, "aggressive": 1.0, "relaxed": 0.0, "party": 0.0}
        assert clap_mood._derive_valence(scores) == 0.0

    def test_aggressive_lowers_valence(self):
        calm = {"happy": 0.5, "sad": 0.3, "aggressive": 0.0, "relaxed": 0.7, "party": 0.5}
        rage = {"happy": 0.5, "sad": 0.3, "aggressive": 1.0, "relaxed": 0.7, "party": 0.5}
        assert clap_mood._derive_valence(calm) > clap_mood._derive_valence(rage)

    def test_returns_none_when_required_label_missing(self):
        # Missing aggressive — composition can't be evaluated.
        assert clap_mood._derive_valence({"happy": 0.5, "sad": 0.3, "party": 0.5}) is None

    def test_relaxed_does_not_affect_valence(self):
        """Relaxed is informative as a mood tag but not part of the valence
        composition (high arousal/low arousal axis ≠ Russell-circumplex
        positivity). Same scores with different relaxed values → same valence."""
        a = {"happy": 0.5, "sad": 0.3, "aggressive": 0.2, "relaxed": 0.0, "party": 0.5}
        b = {"happy": 0.5, "sad": 0.3, "aggressive": 0.2, "relaxed": 1.0, "party": 0.5}
        assert clap_mood._derive_valence(a) == clap_mood._derive_valence(b)


# ---------------------------------------------------------------------------
# Payload shape (matches what analysis_worker writes today)
# ---------------------------------------------------------------------------


class TestComputeMoodPayload:
    def test_returns_sorted_list_of_label_confidence_dicts(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # Audio strongly aligned with party → that label tops the sorted list.
        payload = clap_mood.compute_mood_payload_from_clap(labels["party"])
        assert payload is not None
        mood_tags, valence = payload
        assert isinstance(mood_tags, list)
        assert all(set(m.keys()) == {"label", "confidence"} for m in mood_tags)
        # Sorted descending by confidence.
        confs = [m["confidence"] for m in mood_tags]
        assert confs == sorted(confs, reverse=True)
        assert mood_tags[0]["label"] == "party"
        assert mood_tags[0]["confidence"] == 1.0
        assert isinstance(valence, float)

    def test_returns_none_when_scores_unavailable(self, monkeypatch):
        _stub_label_embeddings(monkeypatch, None)
        assert clap_mood.compute_mood_payload_from_clap(np.ones(512) / np.sqrt(512)) is None


# ---------------------------------------------------------------------------
# Decode / apply helpers
# ---------------------------------------------------------------------------


class TestDecodeClapEmbedding:
    def test_roundtrips_base64_float32(self):
        original = np.array([0.1, -0.2, 0.3, 0.4], dtype=np.float32)
        encoded = base64.b64encode(original.tobytes()).decode("ascii")
        decoded = clap_mood.decode_clap_embedding(encoded)
        assert decoded is not None
        np.testing.assert_array_equal(decoded, original)

    def test_returns_none_on_empty(self):
        assert clap_mood.decode_clap_embedding(None) is None
        assert clap_mood.decode_clap_embedding("") is None

    def test_returns_none_on_garbage(self):
        assert clap_mood.decode_clap_embedding("not-base64!!!") is None


class TestApplyClapMoodToResult:
    def test_overrides_mood_tags_and_valence_when_clap_present(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        # Build a result dict that looks like what the worker produces.
        clap_vec = labels["happy"].astype(np.float32)
        result = {
            "file_path": "/x.flac",
            "valence": 0.123,  # EffNet composite value
            "mood_tags": [{"label": "happy", "confidence": 0.05}],  # EffNet output
            "clap_embedding": base64.b64encode(clap_vec.tobytes()).decode("ascii"),
        }

        clap_mood.apply_clap_mood_to_result(result)

        # mood_tags is now CLAP-derived: 5 labels, sorted, "happy" leads at 1.0.
        assert len(result["mood_tags"]) == 5
        assert result["mood_tags"][0] == {"label": "happy", "confidence": 1.0}
        assert result["valence"] != 0.123

    def test_no_op_when_clap_embedding_missing(self, monkeypatch):
        labels = _unit_vectors()
        _stub_label_embeddings(monkeypatch, labels)

        result = {
            "valence": 0.42,
            "mood_tags": [{"label": "happy", "confidence": 0.05}],
        }
        clap_mood.apply_clap_mood_to_result(result)
        # Untouched.
        assert result["valence"] == 0.42
        assert result["mood_tags"] == [{"label": "happy", "confidence": 0.05}]

    def test_no_op_when_label_embeddings_unavailable(self, monkeypatch):
        _stub_label_embeddings(monkeypatch, None)

        clap_vec = np.ones(512, dtype=np.float32) / np.sqrt(512)
        result = {
            "valence": 0.42,
            "mood_tags": [{"label": "happy", "confidence": 0.05}],
            "clap_embedding": base64.b64encode(clap_vec.tobytes()).decode("ascii"),
        }
        clap_mood.apply_clap_mood_to_result(result)
        assert result["valence"] == 0.42
        assert result["mood_tags"] == [{"label": "happy", "confidence": 0.05}]
