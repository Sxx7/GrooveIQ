"""
GrooveIQ — Tests for the CLAP text encoder.

Mocks the ONNX session + tokenizer so the test stays fast and doesn't require
the ~50 MB text model on disk. Crucially, asserts the encoder passes only the
tokenizer's actual content tokens to the model — earlier code padded to
length 77 with id 0 (which is BOS for RoBERTa-BPE, not <pad>=1), collapsing
every prompt's vector to >0.99 cosine similarity with every other.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from app.services import clap_text


@pytest.fixture(autouse=True)
def _reset_state():
    """Each test starts with cleared encoder state and cache."""
    clap_text._session = None
    clap_text._tokenizer = None
    clap_text._input_name = None
    clap_text._encode_cached.cache_clear()
    yield
    clap_text._session = None
    clap_text._tokenizer = None
    clap_text._input_name = None
    clap_text._encode_cached.cache_clear()


def _install_fake_session(monkeypatch, *, encode_map: dict[str, list[int]], output_vec: np.ndarray):
    """Wire fake ONNX session + tokenizer into the module so encode_text() runs."""
    captured: dict[str, np.ndarray] = {}

    fake_input = MagicMock()
    fake_input.name = "input_ids"
    fake_input.shape = ["batch_size", "sequence_length"]

    fake_session = MagicMock()
    fake_session.get_inputs.return_value = [fake_input]

    def _run(_outputs, feeds):
        captured["input_ids"] = feeds["input_ids"]
        return [output_vec.reshape(1, -1).astype(np.float32)]

    fake_session.run.side_effect = _run

    fake_tokenizer = MagicMock()

    def _encode(prompt: str):
        ids = encode_map[prompt]
        enc = MagicMock()
        enc.ids = ids
        return enc

    fake_tokenizer.encode.side_effect = _encode

    clap_text._session = fake_session
    clap_text._tokenizer = fake_tokenizer
    clap_text._input_name = "input_ids"
    monkeypatch.setattr(clap_text, "_load", lambda: None)
    return captured


# ---------------------------------------------------------------------------
# The padding-bug regression
# ---------------------------------------------------------------------------


class TestNoPadding:
    """Regression tests for the bug where pad_id=0 (BOS, not <pad>) collapsed
    every prompt's text vector. The fix: don't pad — the ONNX model has
    dynamic sequence_length, so the natural BOS+content+EOS sequence works."""

    def test_passes_actual_token_length_no_padding(self, monkeypatch):
        captured = _install_fake_session(
            monkeypatch,
            encode_map={"hello world": [0, 31373, 995, 2]},  # <s> hello world </s>
            output_vec=np.array([1.0] + [0.0] * 511, dtype=np.float32),
        )

        clap_text.encode_text("hello world")

        # The model received exactly the 4 content tokens — NOT 77 with 73
        # zeros tacked on the end (the bug we just fixed).
        sent = captured["input_ids"]
        assert sent.shape == (1, 4)
        assert sent.dtype == np.int64
        assert sent[0].tolist() == [0, 31373, 995, 2]

    def test_truncates_to_77_for_very_long_prompts(self, monkeypatch):
        long_ids = list(range(500))  # 500 tokens — way past the 77 cap
        captured = _install_fake_session(
            monkeypatch,
            encode_map={"a very long prompt": long_ids},
            output_vec=np.ones(512, dtype=np.float32),
        )

        clap_text.encode_text("a very long prompt")

        # Truncated to the 77-token CLIP/CLAP training budget.
        sent = captured["input_ids"]
        assert sent.shape == (1, 77)
        assert sent[0].tolist() == long_ids[:77]

    def test_different_prompts_produce_different_input_ids(self, monkeypatch):
        """Distinct prompts must yield distinct input sequences. The pre-fix
        code padded both to length 77 with id 0; the prefixes were the only
        thing that differed, but the 70+ trailing BOS tokens dominated the
        model's representation. This test would still have passed under the
        bug (different prefixes are different sequences) — but combined with
        the no-padding assertion above, it pins the contract."""
        captured: dict[str, np.ndarray] = {}

        def _install():
            return _install_fake_session(
                monkeypatch,
                encode_map={
                    "happy upbeat": [0, 100, 101, 2],
                    "sad slow": [0, 200, 201, 2],
                },
                output_vec=np.array([1.0] + [0.0] * 511, dtype=np.float32),
            )

        captured = _install()
        clap_text.encode_text("happy upbeat")
        first = captured["input_ids"][0].tolist()

        clap_text.encode_text("sad slow")
        second = captured["input_ids"][0].tolist()

        assert first != second
        # Neither sequence has trailing zeros / pad tokens past content.
        assert all(t != 0 for t in first[1:])
        assert all(t != 0 for t in second[1:])

    def test_empty_prompt_rejected_by_public_api(self, monkeypatch):
        """encode_text() validates input before reaching _encode_cached, so
        the empty case never hits the tokenizer."""
        _install_fake_session(
            monkeypatch,
            encode_map={},
            output_vec=np.ones(512, dtype=np.float32),
        )
        with pytest.raises(ValueError, match="non-empty"):
            clap_text.encode_text("")
        with pytest.raises(ValueError, match="non-empty"):
            clap_text.encode_text("   ")

    def test_output_l2_normalised(self, monkeypatch):
        """Cosine similarity downstream relies on the audio + text vectors
        sharing unit norm. The encoder must always normalise."""
        _install_fake_session(
            monkeypatch,
            encode_map={"x": [0, 99, 2]},
            # Deliberately non-unit-norm output from the "ONNX session".
            output_vec=np.array([3.0, 4.0] + [0.0] * 510, dtype=np.float32),
        )

        v = clap_text.encode_text("x")
        assert v.shape == (512,)
        assert np.linalg.norm(v) == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# Integration: real ONNX model, asserts the contract that broke in prod
# ---------------------------------------------------------------------------
#
# These tests load the actual CLAP text model (~50 MB) and verify the
# *output* contract — that diverse prompts produce distinguishable vectors.
# The mock unit tests above pin the input shape; these pin the output
# semantics, which is the dimension along which the BOS-padding bug
# manifested. They run on any environment where CLAP is enabled and the
# model files are on disk (dev container, dsvr-prod-03), and skip cleanly
# in CI / on developer laptops without the model.

# Free-standing skip — read raw env so we don't need to import settings
# (which the tests above also avoid).
import os  # noqa: E402

_CLAP_MODEL_DIR = os.environ.get("CLAP_MODEL_DIR", "/data/models/clap")
_clap_text_path = os.path.join(_CLAP_MODEL_DIR, os.environ.get("CLAP_TEXT_MODEL_FILE", "clap_text.onnx"))
_clap_tok_path = os.path.join(_CLAP_MODEL_DIR, os.environ.get("CLAP_TOKENIZER_FILE", "clap_tokenizer.json"))
_HAS_REAL_CLAP = os.path.exists(_clap_text_path) and os.path.exists(_clap_tok_path)


@pytest.mark.skipif(not _HAS_REAL_CLAP, reason="CLAP text model + tokenizer not on disk")
class TestRealEncoderSemantics:
    """Integration-level checks against the actual ONNX text model.

    Catches the *shape* of the BOS-padding bug (and any future regression
    that flattens the text-embedding space) by asserting that genuinely
    different prompts produce genuinely different vectors. The mock tests
    above prove the encoder passes the right tokens; these prove the model
    actually responds to those tokens."""

    @pytest.fixture(autouse=True)
    def _real_clap(self, monkeypatch):
        # Force CLAP_ENABLED=true on the settings singleton for the duration
        # of these tests, even if the surrounding env (conftest) clears it.
        from app.core.config import settings

        monkeypatch.setattr(settings, "CLAP_ENABLED", True)
        monkeypatch.setattr(settings, "CLAP_MODEL_DIR", _CLAP_MODEL_DIR)
        # Drop module-level state so _load() actually runs against the real files.
        clap_text._session = None
        clap_text._tokenizer = None
        clap_text._input_name = None
        clap_text._encode_cached.cache_clear()
        yield

    def test_diverse_prompts_are_distinguishable(self):
        """Six semantically unrelated prompts should produce text vectors
        with max pairwise cosine well below 1.0. Pre-fix, this number was
        consistently > 0.95 across the same prompts on prod (mean 0.98)."""
        prompts = [
            "high-energy gym rap",
            "melancholic rainy-night jazz",
            "aggressive thrash metal",
            "chill lofi study beats",
            "calm acoustic guitar",
            "children playing in a sunny park",
        ]
        vecs = np.stack([clap_text.encode_text(p) for p in prompts])
        cos = vecs @ vecs.T
        off_diag = cos[np.triu_indices_from(cos, k=1)]
        # Loose ceiling — far above the post-fix observed max (~0.63) so
        # benign model swaps don't trip it, but well below the pre-fix
        # observed min (0.96) so the BOS-padding bug would fail it.
        assert off_diag.max() < 0.90, f"text vectors too similar: pairwise cosines {off_diag}"

    def test_opposite_prompts_have_low_similarity(self):
        """Concrete semantic check: 'death metal blast beats' and 'children
        playing in a sunny park' should NOT share most of their representation.
        Pre-fix on prod they had cosine 0.99; post-fix it drops to ~0.19."""
        v_metal = clap_text.encode_text("death metal blast beats")
        v_kids = clap_text.encode_text("children playing in a sunny park")
        cos = float(np.dot(v_metal, v_kids))
        assert cos < 0.7, f"opposite-concept prompts too similar: cos={cos:.4f}"

    def test_synonymous_prompts_are_more_similar_than_opposites(self):
        """Sanity: the embedding space should at least order semantically
        close prompts as more similar than opposites. Pre-fix this didn't
        hold either — every pair was ~0.98 — so this both proves the model
        is responsive to content and pins a useful invariant."""
        v_a = clap_text.encode_text("calm peaceful acoustic music")
        v_b = clap_text.encode_text("relaxing gentle acoustic guitar")
        v_far = clap_text.encode_text("aggressive thrash metal")
        sim_close = float(np.dot(v_a, v_b))
        sim_far = float(np.dot(v_a, v_far))
        assert sim_close > sim_far, f"synonyms ({sim_close:.4f}) should outscore opposites ({sim_far:.4f})"
