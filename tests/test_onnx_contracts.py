"""Layer-2 ONNX model contract tests.

Pins:
  1. The semantic class mapping for every analyser field that reads a
     ``(model_file, col)`` tuple. The class at that column — per the MTG
     metadata snapshot below — must match the field name's semantics.
  2. The input/output shape of every ONNX model the analyser loads.

What this catches that Layer 1 misses:

  - The v2.5 ``mood_sad`` / ``mood_relaxed`` / ``mood_party`` column
    inversion (fixed in c67f235): all three were reading col 0 when the
    named class lived at col 1. Pre-fix this would have failed the
    semantic-class assertion immediately.
  - #99 ``voice_instrumental`` column inversion (fixed in v2.7): code
    used to read col 1 (= ``voice``) and store it under the field named
    ``instrumentalness``. The semantic-class assertion below pins col 0
    so a re-introduction would fail RED.
  - A future model file replacement that silently shifts the class
    ordering (e.g. someone re-exports the ONNX with a different output
    layout). The shape + class-list assertions catch that at CI time
    rather than after the multi-day rescan completes.

The semantic mapping is mirrored from
``app/services/analysis_worker.py:_extract_ml`` — the bottom test pins
the mirror against the actual source so the test goes stale loudly
rather than silently.
"""

from __future__ import annotations

import os

import pytest

# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

# Authoritative class lists from MTG metadata sidecars. Each list is the
# ``classes`` field of
# ``https://essentia.upf.edu/models/classification-heads/<head>/<head>-discogs-effnet-1.json``
# fetched on 2026-05-10. The element at index N is the semantic class that
# column N of the model's softmax output represents.
MTG_CLASSES: dict[str, list[str]] = {
    "danceability-discogs-effnet-1.onnx": ["danceable", "not_danceable"],
    "mood_happy-discogs-effnet-1.onnx": ["happy", "non_happy"],
    "mood_sad-discogs-effnet-1.onnx": ["non_sad", "sad"],
    "mood_aggressive-discogs-effnet-1.onnx": ["aggressive", "not_aggressive"],
    "mood_relaxed-discogs-effnet-1.onnx": ["non_relaxed", "relaxed"],
    "mood_party-discogs-effnet-1.onnx": ["non_party", "party"],
    "voice_instrumental-discogs-effnet-1.onnx": ["instrumental", "voice"],
}

# What field each analyser column SEMANTICALLY reads. The contract:
# ``MTG_CLASSES[model_file][analyser_col] == FIELD_SEMANTIC_CLASS[field]``.
FIELD_SEMANTIC_CLASS: dict[str, str] = {
    "danceability": "danceable",
    "instrumentalness": "instrumental",
    "happy": "happy",
    "sad": "sad",
    "aggressive": "aggressive",
    "relaxed": "relaxed",
    "party": "party",
}

# Mirror of ``analysis_worker._extract_ml``'s ``heads`` and ``mood_models``
# dicts. Updated by the same PR that updates the source. The
# ``test_mirror_matches_analyzer_source`` test below catches drift.
ANALYZER_FIELD_TO_HEAD: dict[str, tuple[str, int]] = {
    "danceability": ("danceability-discogs-effnet-1.onnx", 0),
    "instrumentalness": ("voice_instrumental-discogs-effnet-1.onnx", 0),
    "happy": ("mood_happy-discogs-effnet-1.onnx", 0),
    "sad": ("mood_sad-discogs-effnet-1.onnx", 1),
    "aggressive": ("mood_aggressive-discogs-effnet-1.onnx", 0),
    "relaxed": ("mood_relaxed-discogs-effnet-1.onnx", 1),
    "party": ("mood_party-discogs-effnet-1.onnx", 1),
}

# Pinned I/O signature for every ONNX file the analyser loads. Drift is
# the symptom of a model file replacement that requires re-validating
# every downstream column read. The dimensions use ``None`` for any
# string- or dynamic-batch axis from the ONNX metadata.
EXPECTED_IO: dict[str, dict[str, list[tuple[str, list]]]] = {
    "discogs-effnet-bsdynamic-1.onnx": {
        "in": [("melspectrogram", [None, 128, 96])],
        "out": [("activations", [None, 400]), ("embeddings", [None, 1280])],
    },
    "danceability-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "mood_happy-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "mood_sad-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "mood_aggressive-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "mood_relaxed-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "mood_party-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
    "voice_instrumental-discogs-effnet-1.onnx": {
        "in": [("embeddings", [None, 1280])],
        "out": [("activations", [None, 2])],
    },
}


# ---------------------------------------------------------------------------
# 1. Semantic class assertion (no models on disk needed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,head_col",
    [pytest.param(f, ANALYZER_FIELD_TO_HEAD[f], id=f) for f in ANALYZER_FIELD_TO_HEAD],
)
def test_field_reads_semantically_correct_column(field, head_col):
    """For each analyser field, the column index the code reads must
    correspond to the class the field name implies.

    Catches the v2.5 mood-column inversion shape and #99's
    ``instrumentalness`` (= voice probability) inversion. Re-introducing
    either would fail this assertion.
    """
    model_file, col = head_col
    classes = MTG_CLASSES[model_file]
    actual_class = classes[col]
    expected_class = FIELD_SEMANTIC_CLASS[field]
    assert actual_class == expected_class, (
        f"\n  Field name:  {field!r}\n"
        f"  Model file:  {model_file}\n"
        f"  Col read:    {col}\n"
        f"  Class at col {col}: {actual_class!r}\n"
        f"  Expected (per field name): {expected_class!r}\n"
        f"  Full class list: {classes}\n"
        f"  → Either flip the column index in analysis_worker._extract_ml,\n"
        f"    or rename the field. See #99 for the same shape of bug."
    )


# ---------------------------------------------------------------------------
# 2. Live ONNX I/O shape contract (skipped without models)
# ---------------------------------------------------------------------------

_ESSENTIA_CACHE = os.environ.get("ESSENTIA_MODELS_DIR", "/cache/essentia/onnx")
_HAS_MODELS = os.path.isdir(_ESSENTIA_CACHE) and any(f.endswith(".onnx") for f in os.listdir(_ESSENTIA_CACHE))


def _normalize_shape(shape: list) -> list:
    """Replace string-typed dynamic axes with ``None`` for comparison."""
    return [None if (isinstance(d, str) or d is None) else d for d in shape]


@pytest.mark.skipif(not _HAS_MODELS, reason=f"ONNX bundle not at {_ESSENTIA_CACHE}")
@pytest.mark.parametrize("model_file,spec", list(EXPECTED_IO.items()))
def test_onnx_io_shape(model_file, spec):
    """Every model file on disk must still match the I/O shape the code
    expects to feed and read."""
    import onnxruntime as ort

    path = os.path.join(_ESSENTIA_CACHE, model_file)
    if not os.path.isfile(path):
        pytest.skip(f"{model_file} not present in {_ESSENTIA_CACHE}")

    s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    actual_in = [(i.name, _normalize_shape(i.shape)) for i in s.get_inputs()]
    actual_out = [(o.name, _normalize_shape(o.shape)) for o in s.get_outputs()]
    expected_in = [(n, _normalize_shape(s)) for n, s in spec["in"]]
    expected_out = [(n, _normalize_shape(s)) for n, s in spec["out"]]

    assert actual_in == expected_in, (
        f"\n  {model_file}: input shape drift\n  expected: {expected_in}\n  actual:   {actual_in}"
    )
    assert actual_out == expected_out, (
        f"\n  {model_file}: output shape drift\n  expected: {expected_out}\n  actual:   {actual_out}"
    )


# ---------------------------------------------------------------------------
# 3. Mirror-vs-source drift detector (no models needed)
# ---------------------------------------------------------------------------


def test_mirror_matches_analyzer_source():
    """The local ``ANALYZER_FIELD_TO_HEAD`` mirror must match what
    ``_extract_ml`` actually reads. Ensures this test goes stale loudly
    if someone adds / removes / re-indexes a head in the analyser."""
    import inspect

    from app.services import analysis_worker

    src = inspect.getsource(analysis_worker._extract_ml)
    for field, (model_file, col) in ANALYZER_FIELD_TO_HEAD.items():
        pattern = f'"{field}": ("{model_file}", {col})'
        assert pattern in src, (
            f"\n  Mirror drift detected.\n"
            f"  This file expects analyzer to map {field!r} → "
            f"({model_file!r}, {col}),\n"
            f"  but {pattern!r} is not present in _extract_ml.\n"
            f"  Update ANALYZER_FIELD_TO_HEAD to match the real source, "
            f"or check that the analyzer change was intentional."
        )
