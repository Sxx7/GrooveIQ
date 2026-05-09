"""
GrooveIQ — Tests for the CLAP audio embedding pipeline.

Covers preprocessing (resample → ClapFeatureExtractor → 4-D mel input)
and the ONNX inference call. We mock the ONNX session so the test stays
fast and doesn't require the 269 MB Xenova audio model to be on disk.

The transformers ``import torch`` workaround is exercised here too: if
the test passes without ``torch`` installed in the venv, the stub is
working correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

# transformers is a hard dep of CLAP audio embeddings. Skip the whole
# module if it's missing — clap_setup.py / requirements.txt make it a
# soft dep at the operator level.
pytest.importorskip("transformers")

from app.services import analysis_worker


@pytest.fixture(autouse=True)
def _reset_extractor_cache():
    """Each test starts with a fresh extractor so caching state doesn't
    leak between tests."""
    analysis_worker._clap_feature_extractor = None
    yield
    analysis_worker._clap_feature_extractor = None


class TestGetClapFeatureExtractor:
    def test_produces_expected_4d_shape_for_10s_audio(self):
        """The Xenova larger_clap_music_and_speech ONNX expects (B, 1, 1001, 64).
        rand_trunc + repeat-pad on a 10-second 48 kHz clip should yield exactly
        that shape with batch=1."""
        fe = analysis_worker._get_clap_feature_extractor()
        audio = np.zeros(480_000, dtype=np.float32)
        out = fe(audio, sampling_rate=48_000, return_tensors="np")
        assert out["input_features"].shape == (1, 1, 1001, 64)
        assert out["input_features"].dtype == np.float32

    def test_handles_short_audio_via_repeat_pad(self):
        """Audio shorter than max_length_s is repeat-padded; the output shape
        must still be the model's required (1, 1, 1001, 64)."""
        fe = analysis_worker._get_clap_feature_extractor()
        audio = np.random.uniform(-0.1, 0.1, 48_000).astype(np.float32)  # 1s
        out = fe(audio, sampling_rate=48_000, return_tensors="np")
        assert out["input_features"].shape == (1, 1, 1001, 64)

    def test_caches_extractor_instance(self):
        a = analysis_worker._get_clap_feature_extractor()
        b = analysis_worker._get_clap_feature_extractor()
        assert a is b


class TestComputeClapEmbedding:
    @staticmethod
    def _fake_session(output_vec=None):
        """Mock ONNX session that returns a fixed 512-dim vector."""
        if output_vec is None:
            output_vec = np.arange(512, dtype=np.float32)
        sess = MagicMock()
        # Match the real model's input metadata so the code can read the name.
        input_meta = MagicMock()
        input_meta.name = "input_features"
        input_meta.shape = ["audio_batch_size", "num_channels", "height", "width"]
        sess.get_inputs.return_value = [input_meta]
        sess.run.return_value = [output_vec.reshape(1, -1)]
        return sess

    def test_feeds_4d_mel_features_to_session(self):
        """The session's ``run`` should be called with a 4-D float32 array
        named ``input_features`` matching the model's expected shape."""
        sess = self._fake_session()
        audio = np.zeros(48_000 * 10, dtype=np.float32)  # 10s @ 48kHz

        result = analysis_worker._compute_clap_embedding(audio, 48_000, sess, es=None)

        assert result is not None
        assert sess.run.called
        feed = sess.run.call_args[0][1]
        assert "input_features" in feed
        feats = feed["input_features"]
        assert feats.dtype == np.float32
        assert feats.shape == (1, 1, 1001, 64)

    def test_returns_l2_normalised_512_dim_vector(self):
        sess = self._fake_session(output_vec=np.array([3.0, 4.0] + [0.0] * 510, dtype=np.float32))
        audio = np.zeros(48_000 * 10, dtype=np.float32)

        result = analysis_worker._compute_clap_embedding(audio, 48_000, sess, es=None)

        assert result is not None
        assert result.shape == (512,)
        assert result.dtype == np.float32
        assert abs(float(np.linalg.norm(result)) - 1.0) < 1e-5
        # Direction preserved (3,4,0,...) → (0.6, 0.8, 0, ...)
        assert abs(result[0] - 0.6) < 1e-5
        assert abs(result[1] - 0.8) < 1e-5

    def test_returns_none_when_model_emits_zero_vector(self):
        """A degenerate zero embedding must surface as None, matching the
        EffNet path behaviour (see #42 — storing zeros breaks FAISS)."""
        sess = self._fake_session(output_vec=np.zeros(512, dtype=np.float32))
        audio = np.zeros(48_000 * 10, dtype=np.float32)

        result = analysis_worker._compute_clap_embedding(audio, 48_000, sess, es=None)

        assert result is None

    def test_returns_none_on_inference_failure(self):
        sess = self._fake_session()
        sess.run.side_effect = RuntimeError("simulated ORT crash")
        audio = np.zeros(48_000 * 10, dtype=np.float32)

        result = analysis_worker._compute_clap_embedding(audio, 48_000, sess, es=None)

        assert result is None

    def test_truncates_overlength_clip_to_target_len(self):
        """If resample rounding produces > target_len samples, the function
        truncates so the extractor stays on the deterministic pad path."""
        sess = self._fake_session()
        # Source rate = target rate, but audio length > 10s → pre-clip kicks in.
        audio = np.random.uniform(-0.1, 0.1, 48_000 * 30).astype(np.float32)

        result = analysis_worker._compute_clap_embedding(audio, 48_000, sess, es=None)

        assert result is not None
        feats = sess.run.call_args[0][1]["input_features"]
        assert feats.shape == (1, 1, 1001, 64)


class TestBuildOnnxProviders:
    def test_cpu_only_when_no_gpu_available(self, monkeypatch):
        """With no OpenVINO/CUDA EP available, only CPU should appear."""
        monkeypatch.setattr(analysis_worker, "_detect_onnx_backend", lambda: "cpu")
        providers = analysis_worker._build_onnx_providers("/tmp/cache")
        assert providers == ["CPUExecutionProvider"]

    def test_openvino_chain_when_intel_igpu_detected(self, monkeypatch):
        monkeypatch.setattr(analysis_worker, "_detect_onnx_backend", lambda: "openvino")
        providers = analysis_worker._build_onnx_providers("/tmp/cache")
        assert len(providers) == 2
        assert providers[0][0] == "OpenVINOExecutionProvider"
        assert providers[0][1]["cache_dir"] == "/tmp/cache"
        assert providers[1] == "CPUExecutionProvider"

    def test_cuda_chain_when_cuda_available(self, monkeypatch):
        monkeypatch.setattr(analysis_worker, "_detect_onnx_backend", lambda: "cuda")
        providers = analysis_worker._build_onnx_providers("/tmp/cache")
        assert len(providers) == 2
        assert providers[0][0] == "CUDAExecutionProvider"
        assert providers[1] == "CPUExecutionProvider"
