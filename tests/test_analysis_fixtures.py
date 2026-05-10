"""Layer-1 end-to-end analysis fixtures.

Generates synthetic audio with known acoustic character, feeds each fixture
through the production ``_analyze_file()`` pipeline, asserts every output
column lands inside the band declared in
``tests/fixtures/audio/manifest.json``.

The bug history that justifies this layer:

  - #42: degenerate zero-norm embedding silently stored as base64-encoded
    zeros, breaking FAISS for ~36 % of the library.
  - #83: wrong ONNX output index (400-dim classifier head used as embedding
    source instead of 1280-dim trunk) → 32k NULL embeddings.
  - #88: ``Loudness()`` Stevens-law power-sum returned 0–5896 instead of
    LUFS; mood column index inversion stored ``sad``/``relaxed``/``party``
    as the negation of the field name; valence pinned at [0, 0.46].
  - #91: Python 3.12 SyntaxError shipped to prod because no test imported
    the affected module on the runtime version.
  - #99 (open): ``voice_instrumental`` column inversion stores voice
    probability under the field named ``instrumentalness``.

None of those were caught by the existing test suite, which mocks the
ONNX session and asserts piece-wise correctness on synthetic inputs.
This module asserts the *output contract* of the real pipeline against
real ONNX models.

Skipped (cleanly) when the Essentia ONNX models are not on disk — the
test runner fingerprints the cache dir, not the venv.
"""

from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path

import numpy as np
import pytest

# The fixture synthesiser does not import any GrooveIQ code, so it's safe
# to load at module top-level even when the analysis pipeline is skipped.
from tests.fixtures.audio import synth

_MANIFEST_PATH = Path(__file__).parent / "fixtures" / "audio" / "manifest.json"
_MANIFEST: dict = json.loads(_MANIFEST_PATH.read_text())

# ---------------------------------------------------------------------------
# Skip gate
# ---------------------------------------------------------------------------
#
# The pipeline needs Essentia + the ONNX model bundle. Both are present in
# the production Docker image but not on a typical developer laptop. Probe
# for the model directory before attempting any imports that would 500.

_ESSENTIA_CACHE = os.environ.get("ESSENTIA_MODELS_DIR", "/cache/essentia/onnx")
_HAS_MODELS = os.path.isdir(_ESSENTIA_CACHE) and any(
    f.endswith(".onnx") for f in os.listdir(_ESSENTIA_CACHE)
) if os.path.isdir(_ESSENTIA_CACHE) else False

pytestmark = pytest.mark.skipif(
    not _HAS_MODELS,
    reason=f"Essentia ONNX models not at {_ESSENTIA_CACHE} — run inside the Docker image",
)


# ---------------------------------------------------------------------------
# Session-scoped pipeline init
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _pipeline_state(tmp_path_factory):
    """Loads ONNX sessions + writes the synthesised fixtures to disk once
    per test session. Reused across every parametrised case."""
    from app.services import analysis_worker as aw

    aw._download_models()
    onnx = aw._init_onnx_sessions()
    clap = aw._init_clap_audio_session()  # may be None if CLAP_ENABLED=false

    rng = np.random.RandomState(seed=20240101)
    proj = (rng.randn(1280, 64) / np.sqrt(64)).astype(np.float32)

    fixture_dir = tmp_path_factory.mktemp("audio_fixtures")
    paths = synth.write_all(str(fixture_dir))
    return {
        "onnx": onnx,
        "clap": clap,
        "proj": proj,
        "paths": paths,
    }


@pytest.fixture(scope="session")
def _fixture_results(_pipeline_state):
    """Runs every fixture through ``_analyze_file()`` exactly once for the
    whole session. Per-fixture tests then assert against the cached result."""
    from app.services import analysis_worker as aw
    import essentia.standard as es

    out = {}
    for name, path in _pipeline_state["paths"].items():
        result = aw._analyze_file(
            path,
            None,
            es,
            _pipeline_state["onnx"],
            _pipeline_state["proj"],
            _pipeline_state["clap"],
        )
        out[name] = result
    return out


# ---------------------------------------------------------------------------
# Assertion helpers
# ---------------------------------------------------------------------------


def _check_range(name: str, value, band, label: str) -> None:
    """Apply a single manifest range to a single analysis output value."""
    if isinstance(band, list) and len(band) == 2:
        lo, hi = band
        assert value is not None, f"[{name}] {label}: expected in [{lo}, {hi}], got None"
        assert isinstance(value, (int, float)), f"[{name}] {label}: not numeric: {value!r}"
        assert not (isinstance(value, float) and (math.isnan(value) or math.isinf(value))), (
            f"[{name}] {label}: NaN/Inf"
        )
        assert lo <= value <= hi, f"[{name}] {label}: {value} not in [{lo}, {hi}]"
    elif band is None:
        assert value is None, f"[{name}] {label}: expected None, got {value!r}"
    elif band == "not_null":
        assert value is not None, f"[{name}] {label}: expected populated, got None"
    elif isinstance(band, dict) and "error" in band:
        # Validated separately (analysis_error is a string match, not a range).
        pass
    else:
        raise AssertionError(f"[{name}] {label}: unknown manifest band shape: {band!r}")


def _decode_emb(b64: str | None) -> np.ndarray | None:
    if not b64:
        return None
    return np.frombuffer(base64.b64decode(b64), dtype=np.float32)


# ---------------------------------------------------------------------------
# Per-fixture parametrised test
# ---------------------------------------------------------------------------


_FIXTURE_NAMES = [k for k in _MANIFEST.keys() if not k.startswith("_")]


@pytest.mark.parametrize("name", _FIXTURE_NAMES)
def test_fixture_output_matches_manifest(name, _fixture_results):
    """Every numeric column in the manifest must hold for this fixture."""
    spec = _MANIFEST[name]
    result = _fixture_results[name]

    assert result is not None, f"[{name}] _analyze_file returned None (file unchanged?)"

    # ------------------------------------------------------------------
    # Error-shaped expectations (e.g. "too short")
    # ------------------------------------------------------------------
    err_spec = spec.get("analysis_error")
    if isinstance(err_spec, dict) and "error" in err_spec:
        assert err_spec["error"] in (result.get("analysis_error") or ""), (
            f"[{name}] expected error containing {err_spec['error']!r}, "
            f"got {result.get('analysis_error')!r}"
        )
        # Subset of fields must still be honoured (e.g. duration, embedding null)
        for field, band in spec.items():
            if field.startswith("_") or field == "analysis_error":
                continue
            value = _value_for(result, field)
            _check_range(name, value, band, field)
        return

    # ------------------------------------------------------------------
    # Normal path: every manifest field is range-checked
    # ------------------------------------------------------------------
    for field, band in spec.items():
        if field.startswith("_"):
            continue
        value = _value_for(result, field)
        _check_range(name, value, band, field)


def _value_for(result: dict, field: str):
    """Pull the manifest-named field from the analyser output dict.

    Handles a few cases that aren't direct dict lookups:
      - ``embedding_norm`` / ``embedding_dim`` — derived from the b64 vector.
      - ``embedding`` / ``clap_embedding`` — coerced to None when empty so
        ``null`` / ``not_null`` checks work.
    """
    if field in ("embedding", "clap_embedding"):
        return result.get(field) or None
    if field == "embedding_norm":
        v = _decode_emb(result.get("embedding"))
        return None if v is None else float(np.linalg.norm(v))
    if field == "clap_norm":
        v = _decode_emb(result.get("clap_embedding"))
        return None if v is None else float(np.linalg.norm(v))
    if field == "embedding_dim":
        v = _decode_emb(result.get("embedding"))
        return None if v is None else len(v)
    if field == "clap_dim":
        v = _decode_emb(result.get("clap_embedding"))
        return None if v is None else len(v)
    return result.get(field)


# ---------------------------------------------------------------------------
# Universal invariants — applied to every successful fixture
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [n for n in _FIXTURE_NAMES if not isinstance(_MANIFEST[n].get("analysis_error"), dict)],
)
def test_universal_invariants(name, _fixture_results):
    """No NaN/Inf, mood labels complete, embedding norms unit-length,
    speechiness == 1 - instrumentalness."""
    inv = _MANIFEST["_universal_invariants"]
    result = _fixture_results[name]
    assert result is not None
    assert result.get("analysis_error") is None, f"[{name}] unexpected error: {result.get('analysis_error')}"

    # No NaN/Inf in any numeric field
    for k, v in result.items():
        if isinstance(v, float):
            assert not (math.isnan(v) or math.isinf(v)), f"[{name}] field {k} is NaN/Inf"

    # 5 mood labels present
    mood_tags = result.get("mood_tags") or []
    labels = {m["label"] for m in mood_tags}
    expected = set(inv["all_mood_labels_present"])
    assert labels == expected, f"[{name}] mood labels mismatch: got {labels}, expected {expected}"
    # No NaN/Inf in mood confidences
    for m in mood_tags:
        c = m["confidence"]
        assert isinstance(c, (int, float)) and not math.isnan(c) and not math.isinf(c), (
            f"[{name}] mood {m['label']!r} confidence is non-finite: {c!r}"
        )
        assert 0.0 <= c <= 1.0, f"[{name}] mood {m['label']!r} confidence out of [0,1]: {c}"

    # embedding norms (when populated)
    emb = _decode_emb(result.get("embedding"))
    if emb is not None:
        norm = float(np.linalg.norm(emb))
        lo, hi = inv["embedding_norm"]
        assert lo <= norm <= hi, f"[{name}] embedding norm {norm} not in [{lo}, {hi}]"
        assert len(emb) == inv["embedding_dim"][0], f"[{name}] embedding dim {len(emb)} != {inv['embedding_dim'][0]}"

    clap = _decode_emb(result.get("clap_embedding"))
    if clap is not None:
        norm = float(np.linalg.norm(clap))
        lo, hi = inv["clap_norm"]
        assert lo <= norm <= hi, f"[{name}] clap norm {norm} not in [{lo}, {hi}]"
        assert len(clap) == inv["clap_dim"][0], f"[{name}] clap dim {len(clap)} != {inv['clap_dim'][0]}"

    # speechiness = 1 - instrumentalness invariant
    inst = result.get("instrumentalness")
    speech = result.get("speechiness")
    if inst is not None and speech is not None:
        assert abs(speech - (1.0 - inst)) < 0.01, (
            f"[{name}] speechiness {speech} != 1 - instrumentalness {1.0 - inst}"
        )
