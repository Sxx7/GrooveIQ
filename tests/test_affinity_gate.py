"""Unit tests for the Affinity Radio genre/mood gate (pure logic, no DB/FAISS)."""

from app.models.algorithm_config_schema import AffinityConfig
from app.services.affinity_radio import (
    AffinitySession,
    _gate_and_rank,
    _genre_family,
    _mood_cos,
    _mood_vec,
    gate_active,
)


def test_genre_family_mapping():
    assert _genre_family("Électronique") == "electronic"
    assert _genre_family("Dance") == "electronic"
    assert _genre_family("Melodic Dubstep") == "electronic"
    assert _genre_family("Électronique ou concrète") == "electronic"
    assert _genre_family("Rap") == "hiphop"
    assert _genre_family("Conscious Hip Hop") == "hiphop"
    assert _genre_family("Hip-Hop") == "hiphop"
    assert _genre_family("Contemporary R&B") == "rnb"
    assert _genre_family("Alternatif et Indé") == "rock"
    assert _genre_family("Metal") == "rock"
    assert _genre_family("Classique") == "classical"
    assert _genre_family("Pop") == "pop"
    # unknown / empty -> None
    assert _genre_family("") is None
    assert _genre_family(None) is None
    assert _genre_family("Zydeco Polka") is None


def test_mood_vec_and_cos():
    party = [{"label": "party", "confidence": 0.9}, {"label": "relaxed", "confidence": 0.5}]
    party2 = [{"label": "party", "confidence": 0.85}, {"label": "relaxed", "confidence": 0.55}]
    sad = [{"label": "sad", "confidence": 0.9}, {"label": "aggressive", "confidence": 0.1}]
    v_party, v_party2, v_sad = _mood_vec(party), _mood_vec(party2), _mood_vec(sad)
    assert v_party is not None and v_party.shape == (5,)
    # similar moods score higher than dissimilar ones
    assert _mood_cos(v_party, v_party2) > _mood_cos(v_party, v_sad)
    # missing mood -> neutral 0.5
    assert _mood_cos(v_party, None) == 0.5
    assert _mood_vec(None) is None
    assert _mood_vec([]) is None


def test_soft_gate_reprioritises_same_family():
    cfg = AffinityConfig()  # defaults: soft gate, w_emb 0.55 / w_genre 0.30 / w_mood 0.15
    # A is the single closest by embedding but off-family; B/C are same-family.
    pool = [
        ("A", 0.90, "hiphop", None),
        ("B", 0.85, "electronic", None),
        ("C", 0.80, "electronic", None),
    ]
    out = _gate_and_rank(pool, {"electronic"}, None, cfg, count=3)
    ids = [tid for tid, _ in out]
    assert ids[:2] == ["B", "C"]  # same-family surfaces above the off-family top neighbour
    assert ids[-1] == "A"
    # emb_sim is preserved (not the blended score) so callers report true cosine
    assert dict(out)["B"] == 0.85


def test_hard_gate_drops_off_family_but_falls_back_when_thin():
    pool = [
        ("A", 0.95, "hiphop", None),
        ("B", 0.70, "electronic", None),
        ("C", 0.65, "rock", None),
    ]
    hard = AffinityConfig(hard_gate=True, hard_min_results=1)
    out = _gate_and_rank(pool, {"electronic"}, None, hard, count=3)
    assert [tid for tid, _ in out] == ["B"]  # only the same-family track survives

    # Not enough same-family survivors -> fall back to soft (never runs dry).
    hard_strict = AffinityConfig(hard_gate=True, hard_min_results=3)
    out2 = _gate_and_rank(pool, {"electronic"}, None, hard_strict, count=3)
    assert len(out2) == 3
    assert out2[0][0] == "B"  # same-family still wins on the blended score


def test_gate_active_respects_override_and_anchor():
    base = dict(session_id="s", user_id="u", seed_type="track", seed_value="t")
    with_anchor = AffinitySession(**base, seed_genre_families={"electronic"})
    # config default is gate_enabled=True
    assert gate_active(with_anchor) is True
    # explicit per-session override wins
    with_anchor.gate_override = False
    assert gate_active(with_anchor) is False
    # no genre/mood anchor -> gate can't act, so inactive even when enabled
    no_anchor = AffinitySession(**base)
    assert gate_active(no_anchor) is False
